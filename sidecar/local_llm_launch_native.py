"""原生 llama-server 启动（含 custom_engine 第三方引擎）。

从 local_llm_engine.EngineProcess 拆出（2026-09-24）：纯函数，engine 作首参。"""

from __future__ import annotations

import collections
import os
import subprocess
import threading
import time
from pathlib import Path
from cmd_safety import child_env
from local_llm_probe import (
    _GGML_TYPE_SUSPECT,
    _auto_cache_type,
    _ggml_type_label,
    _gguf_architecture,
    _gguf_scan,
)
from local_llm_launch_common import _ENGINE_ENV_PREFIXES, find_mmproj_for

import logging
logger = logging.getLogger("latiao-sidecar")

# 引擎 stderr 落盘上限（超过就从头截断重来，不做多份轮转——排障只看近期）
_ENGINE_LOG_MAX_BYTES = 5 * 1024 * 1024


def engine_log_path() -> Path:
    """引擎日志路径（transport 在流中断时会指路到这里）。"""
    return Path.home() / ".local-ai-os" / "engine.log"


def _append_engine_log(line: str) -> None:
    """把引擎 stderr 追加到 engine.log。失败静默（排障能力不能反过来搞挂启动）。"""
    try:
        p = engine_log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists() and p.stat().st_size > _ENGINE_LOG_MAX_BYTES:
            p.write_text("", encoding="utf-8")
        with open(p, "a", encoding="utf-8") as f:
            f.write(line if line.endswith("\n") else line + "\n")
    except Exception:
        pass



def start_llama_native(engine, model_path: str, port: int,
                        exe=None, extra_args: list | None = None,
                        backend: str = "llama-cpp-native",
                        backend_name: str = "") -> dict:
    """启动原生 llama-server（自带上游引擎，或 custom_engine 指定的第三方引擎）。

    exe/extra_args 由"自定义引擎"路径传入（第三方 fork 的二进制 + 其私有参数）；
    不传则用辣条自带的上游 llama-server。两种情况的进程管理/健康检查完全一致。
    """
    _custom = backend != "llama-cpp-native"
    # 先按串行保守复位：只有下面真正拼出带 --parallel 的命令行才抬高（启动失败/非原生
    # 引擎时留 1，transport 不会对单线程的 python 引擎并发下发）。
    engine._launched_slots = 1
    exe = exe or engine._find_llama_server(model_path)
    if not exe:
        engine.server_status = "error"
        engine.status_message = ("自定义引擎路径无效（检查 config.json 的 custom_engine.binary）"
                               if _custom else "找不到 llama-server.exe，请重装 Latiao")
        return engine.get_status()

    engine.server_status = "starting"
    engine.status_message = f"正在加载 {engine.current_model_name}..."

    # -ngl -1（“未设置”默认）在旧版 llama-server 被接受，新版（XHToken fork）
    # 要求非负（0-999）——启动即被拒 → “exited early/HTTP timeout”
    # （09-08 Spark 回退失败根因）。映射为 999（全部 layer），两版兼容。
    _ngl = engine.n_gpu_layers if engine.n_gpu_layers >= 0 else 999
    # 并发槽位：只对**自带引擎**声明 --parallel。自定义引擎（fork）不代加——fork 未必
    # 认这个开关，启动即被拒就是 09-08 那类"exited early"事故；容量按用户自己 args 里
    # 有没有 --parallel 算，没写就是 1（等于今天的串行行为）。python 引擎（llama_cpp
    # .server）单实例单线程，容量恒为 1，走下面另一个分支。
    if _custom:
        _slots = 1
        try:
            _ai = extra_args.index("--parallel")
            _slots = max(1, min(4, int(extra_args[_ai + 1])))
        except (ValueError, IndexError, TypeError):
            pass
    else:
        _slots = max(1, int(engine.parallel_slots))
    engine._launched_slots = _slots
    cmd = [
        str(exe),
        "-m", model_path,
        "--port", str(port),
        "--host", "127.0.0.1",
        # 总量 = 槽位 × 每会话窗口（llama-server 按 n_ctx/n_parallel 均分给每个槽）
        "-c", str(engine.model_token_limit * _slots),
        "-ngl", str(_ngl),
    ]
    if _slots > 1:
        cmd += ["--parallel", str(_slots)]
        engine.status_message = (f"正在加载 {engine.current_model_name}"
                               f"（{_slots} 并发 × {engine.model_token_limit} 上下文）...")
    # 识图：模型旁有 mmproj*.gguf 就挂上（Hermes/Qwen-VL 等多模态 GGUF）
    _mmproj = find_mmproj_for(model_path)
    if _mmproj:
        cmd += ["--mmproj", str(_mmproj)]
        engine.has_image_support = True
        logger.info("识图已启用（mmproj=%s）", _mmproj)
    else:
        engine.has_image_support = False
    kv_k, kv_v = _auto_cache_type(model_path)
    # cache-type 值兼容：旧版 CLI 接受数字（8），新版（XHToken fork）要求
    # 字符串（q8_0）——统一用字符串（llama.cpp 各版本 CLI 均接受）
    _CACHE_TYPE_STR = {1: "f16", 2: "q4_0", 8: "q8_0"}
    cmd += ["--cache-type-k", _CACHE_TYPE_STR.get(kv_k, str(kv_k)),
            "--cache-type-v", _CACHE_TYPE_STR.get(kv_v, str(kv_v))]
    # -fa 兼容性：旧版 llama-server 为无值开关，新版（XHToken fork）要求
    # 带值 [on|off|auto]——省略本参数（新版默认 auto / 旧版默认 off，均可运行）
    # 09-08 Spark 回退引擎“HTTP timeout”根因即 -fa 参数不兼容
    # 09-20：不再用 --chat-template 覆盖模型自带模板。_guess_chat_format 返回的是
    # **格式名**（如 "qwen"）而非 jinja 模板串，覆盖会把模型的工具区/思考区模板
    # 一起替换掉——而原生工具调用正是靠模型模板解析出来的（本版 --jinja 默认开）。
    # 需要旧行为时设 LATIAO_NATIVE_CHAT_TEMPLATE_OVERRIDE=1。
    if os.environ.get("LATIAO_NATIVE_CHAT_TEMPLATE_OVERRIDE", ""):
        chat_fmt = engine._guess_chat_format(model_path)
        if chat_fmt:
            cmd += ["--chat-template", chat_fmt]
    # 自定义引擎：追加用户配置的私有参数（如 Prism fork 需要的开关）
    if extra_args:
        cmd += [str(a) for a in extra_args]
        logger.info("自定义引擎额外参数: %s", " ".join(str(a) for a in extra_args))
    # 注意：原生 llama-server 的 interrupt-requests 默认即关闭（新请求排队
    # 而非掐断当前生成），与 macOS 路径显式 --interrupt_requests False
    # 行为一致，无需额外参数。

    env = child_env(allow_prefixes=_ENGINE_ENV_PREFIXES)
    env.pop("HF_ENDPOINT", None)
    # stdout 无人读取，必须 DEVNULL，否则管道缓冲满后子进程死锁
    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, env=env
    )
    engine._process = proc

    # 用局部 proc 而不是 engine._process，避免快速 stop→start 时读到新进程的 stderr
    stderr_lines: collections.deque[str] = collections.deque(maxlen=300)
    # 暴露给 transport：流中断时它要记下"引擎为什么死"（退出码 + stderr 尾部）。
    # 此前这段 stderr 只留在内存、成功加载后没人读、也不落盘 —— 引擎中途死亡
    # 在日志里查不到原因（2026-09-24 实测）。
    engine._stderr_tail = stderr_lines
    def _drain():
        if proc.stderr:
            try:
                for line in proc.stderr:
                    stderr_lines.append(line)
                    _append_engine_log(line)
            except Exception:
                pass
    t = threading.Thread(target=_drain, daemon=True)
    t.start()

    _load_t0 = time.monotonic()
    _ready = engine._wait_for_http(port, timeout_sec=900, process=proc, cancel=engine._cancel_load)
    if _ready:
        # 成功也留痕：此前加载成功一行都不写，日志读起来像卡死（2026-09-24 实测
        # 用户看到"自动重载已启动"后就再无下文，无法判断到底加载完没有）
        logger.info("引擎已就绪: %s（加载耗时 %.1fs，端口 %s，日志 %s）",
                    os.path.basename(str(model_path)), time.monotonic() - _load_t0,
                    port, engine_log_path())
    if not _ready:
        t.join(timeout=1)
        err_lines = list(stderr_lines)[-50:] if stderr_lines else []
        err_summary = ""
        for line in reversed(err_lines):
            stripped = line.strip()
            if stripped and ("Error" in stripped or "error" in stripped.lower()):
                err_summary = stripped[-300:]
                break
        if not err_summary and err_lines:
            err_summary = err_lines[-1].strip()[-300:]
        logger.error("llama-server native: %s",
            "exited early" if proc.poll() is not None else "HTTP timeout")
        engine.stop_model()
        # 启动失败 → 回到串行容量：引擎没起来，别让 transport 以为有几个槽可并发
        engine._launched_slots = 1
        engine.server_status = "error"
        # 引擎原始报错对用户没意义（尤其"failed to read magic"这种误导文案，
        # 实际是"架构不被支持"）。读 GGUF 架构名换成可执行的提示。
        _low = ("".join(err_lines) or "").lower()
        # 09-20 重写分类：把三种成因分开——量化类型不支持 / 文件不完整 /
        # 架构真的不支持。旧实现把前两种（尤其 "model loading error" 这条
        # 兜底串）都归到第三种，提示"换 MLX / 等上游支持架构"，全是误导。
        import re as _re
        _m_type = _re.search(r"invalid ggml type (\d+)", _low)
        _m_bounds = ("not within the file bounds" in _low
                     or "corrupted or incomplete" in _low)
        _m_arch = ("unknown model architecture" in _low
                   or "unsupported model architecture" in _low
                   or "architecture is not supported" in _low)
        _m_magic = "failed to read magic" in _low
        _scan = _gguf_scan(model_path) if (_m_type or _m_bounds or _m_magic) else None
        if _custom:
            engine.status_message = (
                f"自定义引擎（{backend_name or 'custom'}）启动失败: "
                f"{err_summary or '无输出'}。请检查该引擎的二进制与参数"
                "（config.json 的 custom_engine）。")
        elif _m_type or _m_bounds or _m_magic:
            _bad = {t: c for t, c in (_scan or {}).get("types", {}).items()
                    if t > _GGML_TYPE_SUSPECT}
            _need = int((_scan or {}).get("need_bytes") or 0)
            _size = int((_scan or {}).get("size") or 0)
            if _bad:
                _d = "、".join(f"{_ggml_type_label(t)}（{c} 个张量）"
                               for t, c in sorted(_bad.items()))
                engine.status_message = (
                    f"该模型的量化格式不被当前引擎支持：{_d}。"
                    "这通常不是架构问题，而是某个 fork 自造的私有量化（上游 llama.cpp "
                    "未收录该类型号）。可选：① 换用产出该量化的那个 fork 的引擎；"
                    "② 换用同一模型的常规量化版本（Q4_K_M / Q8_0 等）；"
                    f"③ 若确实要低位量化，请选上游标准写法（如 TQ2_0）。"
                    f"（引擎原始报错：{_m_type.group(0)}）" if _m_type else
                    ("该模型的量化格式不被当前引擎支持"
                     "（引擎在读取张量表时失败）。可选：① 换用产出该量化的 fork 引擎；"
                     "② 换用同一模型的常规量化版本（Q4_K_M / Q8_0 等）。"))
            elif _need and _size and _size < _need * 0.995:
                engine.status_message = (
                    f"模型文件不完整（下载未完成或文件损坏）：按张量表至少需要 "
                    f"{_need/1e9:.2f} GB，当前 {_size/1e9:.2f} GB，"
                    f"还缺 {(_need-_size)/1e9:.2f} GB。请等下载完成（或重新下载）后再加载。")
            else:
                engine.status_message = (
                    "模型文件读取失败：不是有效的 GGUF，或文件不完整（下载未完成/被截断）。"
                    "请确认下载已完成后重试。")
        elif _m_arch:
            _arch = _gguf_architecture(model_path) or "未知"
            engine.status_message = (
                f"该模型架构（{_arch}）暂不被 llama.cpp 支持（上游尚未实现该架构）。"
                "可选：① 换用该模型的 MLX 转换（社区常以 -MLX-4bit/8bit 命名）——"
                "能否运行取决于架构是否被 Latiao 的 MLX 引擎（mlx-lm）收录："
                "config.json 的 model_type 需在 mlx-lm 内置列表内，或仓库用 model_file "
                "自带 MLX 实现（只被 mlx-vlm 收录的架构不在此列）；"
                "② 换用其他模型；③ 等上游支持后更新引擎（每次发版会自动抓最新的上游引擎）。")
        else:
            engine.status_message = f"启动失败: {err_summary}" if err_summary else "模型加载超时"
        engine.current_model_id = ""
        engine.current_model_name = ""
        return engine.get_status()

    engine.server_status = "running"
    engine.status_message = (f"{engine.current_model_name} 运行中"
                           + (f"（自定义引擎 {backend_name}）" if _custom else ""))
    engine._active_backend = backend
    return engine.get_status()
