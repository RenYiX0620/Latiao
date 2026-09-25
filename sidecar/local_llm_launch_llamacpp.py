"""llama.cpp（llama-cpp-python / llama-server）后端启动。

从 local_llm_engine.EngineProcess 拆出（2026-09-24）：纯函数，engine 作首参。"""

from __future__ import annotations

from pathlib import Path
import collections
import os
import platform
import subprocess
import sys
import threading
from cmd_safety import child_env
from local_llm_probe import (
    _auto_cache_type,
    _custom_engine_spec,
    _gguf_precheck,
    _read_config,
)
from local_llm_launch_common import _ENGINE_ENV_PREFIXES, find_mmproj_for

import logging
logger = logging.getLogger("latiao-sidecar")



def start_llama_cpp(engine, model_id: str, port: int) -> dict:
    model_path = engine._find_gguf(model_id)
    if not model_path:
        engine.server_status = "error"
        engine.status_message = f"找不到 GGUF 模型: {model_id}。请先下载 .gguf 文件到 ~/Models/"
        engine.current_model_id = ""
        engine.current_model_name = ""
        return engine.get_status()

    # ── 自定义引擎（路线 A，09-20）────────────────────────────────
    # 配了 custom_engine 就用它加载这个 GGUF：**跳过**量化类型/完整性预检
    # （那两道闸门的存在理由正是"上游读不了这种文件"，而它就是要交给第三方引擎
    # 去读的），只做最基本的"是不是 GGUF"检查，避免把目录喂给进程。
    _custom = _custom_engine_spec(model_path)
    if _custom:
        try:
            with open(model_path, "rb") as _cf:
                _magic_ok = _cf.read(4) == b"GGUF"
        except Exception:
            _magic_ok = False
        if not _magic_ok:
            engine.server_status = "error"
            engine.status_message = f"不是有效的 GGUF 文件: {model_path}"
            engine.current_model_id = ""
            engine.current_model_name = ""
            return engine.get_status()
        logger.info("使用自定义引擎 '%s': %s", _custom["name"], _custom["binary"])
        engine.current_model_id = model_id
        engine.current_model_name = Path(model_path).stem
        engine.server_status = "starting"
        engine.status_message = f"正在用自定义引擎 {_custom['name']} 加载 {engine.current_model_name}..."
        return engine._start_llama_native(
            model_path, port, exe=_custom["binary"], extra_args=_custom["args"],
            backend="llama-cpp-custom", backend_name=_custom["name"])

    # 加载前预检（09-20）：量化类型不受支持 / 文件不完整 → 直接给出可执行结论，
    # 不必等引擎失败后再由错误分类猜原因
    _pre_err = _gguf_precheck(model_path)
    if _pre_err:
        logger.warning("GGUF 预检拦截: %s", _pre_err)
        engine.server_status = "error"
        engine.status_message = _pre_err
        engine.current_model_id = ""
        engine.current_model_name = ""
        return engine.get_status()

    engine.current_model_id = model_id
    engine.current_model_name = Path(model_path).stem
    engine.server_status = "starting"
    engine.status_message = f"正在加载 {engine.current_model_name}..."

    # ── 引擎顺序（09-20 改：原生 llama-server 优先）────────────────
    # 原生引擎：支持模板级原生工具调用（tools 字段真的被读、按模型模板解析
    # tool_calls）、提供 timings（缓存命中率等指标的唯一来源）、加载快
    # （本机 18.5GB GGUF 实测 3.4s）。
    # python 引擎（llama-cpp-python）：**完全忽略 tools 字段**（实测带/不带
    # tools，prompt_tokens 都是 36）→ 模型看不到工具清单，只能靠文字提示猜
    # 工具名（实测它编出 web_search / get_historical_data），也没有 usage。
    # 故默认先原生，失败再回退 python。回旧顺序：LATIAO_GGUF_ENGINE=python
    # 或 config.json {"gguf_engine": "python"}。
    if platform.system() != "Windows":
        _pref = ""
        try:
            _pref = str(_read_config().get("gguf_engine", "") or "")
        except Exception:
            pass
        _pref = (os.environ.get("LATIAO_GGUF_ENGINE", "") or _pref).strip().lower()
        if _pref != "python" and engine._find_llama_server(model_path):
            logger.info("GGUF 引擎顺序：原生 llama-server 优先（LATIAO_GGUF_ENGINE=python 可回退）")
            _native_res = engine._start_llama_native(model_path, port)
            if _native_res.get("status") == "running":
                return _native_res
            logger.warning("原生引擎启动失败，回退 python 引擎: %s",
                           _native_res.get("message"))
            # _start_llama_native 失败路径调过 stop_model()，会置取消事件；
            # 不清掉的话下面的 _wait_for_http(cancel=...) 会立刻失败
            engine._cancel_load.clear()
            engine.server_status = "starting"
            engine.current_model_id = model_id
            engine.current_model_name = Path(model_path).stem
            engine.status_message = f"正在加载 {engine.current_model_name}..."
    elif platform.system() == "Windows":
        engine.server_status = "starting"
        engine.status_message = f"正在加载 {engine.current_model_name}..."
        return engine._start_llama_native(model_path, port)

    try:
        # python 引擎（llama_cpp.server）单实例单线程，永远只有 1 个槽
        engine._launched_slots = 1
        cmd = [
            sys.executable, "-m", "llama_cpp.server",
            "--model", model_path,
            "--port", str(port),
            "--host", "127.0.0.1",
            "--n_ctx", str(engine.model_token_limit),
            "--n_gpu_layers", str(engine.n_gpu_layers),
            # sidecar 已用 asyncio 锁串行化所有请求；引擎自带的
            # interrupt_requests 会在新请求到达时强掐正在生成的流，
            # 而生成线程无法被取消，导致残留线程与新请求并发访问模型
            # 实例 → 空响应/挂起/崩溃。必须关闭。
            "--interrupt_requests", "False",
        ]
        # ── Auto-select KV cache quant based on model quant ──
        # Q4 model → Q4_0 KV; Q5+ model → Q8_0 KV
        kv_k, kv_v = _auto_cache_type(model_path)
        cmd += ["--type_k", str(kv_k), "--type_v", str(kv_v)]
        cmd += ["--flash_attn", "1"]
        # Enable function calling via chat format for models that support it
        chat_fmt = engine._guess_chat_format(model_path)
        if chat_fmt:
            cmd += ["--chat_format", chat_fmt]
        # 识图：llama_cpp.server 用 --clip_model_path 挂 mmproj/CLIP
        _mmproj = find_mmproj_for(model_path)
        if _mmproj:
            cmd += ["--clip_model_path", str(_mmproj)]
            engine.has_image_support = True
            logger.info("识图已启用（clip/mmproj=%s）", _mmproj)
        else:
            engine.has_image_support = False
        env = child_env(allow_prefixes=_ENGINE_ENV_PREFIXES)
        env["HF_ENDPOINT"] = engine._get_hf_endpoint()  # 镜像优先（国内 huggingface.co 常不可达）
        env["HF_HUB_DISABLE_XET"] = "1"
        # stdout 无人读取，必须 DEVNULL，否则管道缓冲满后子进程死锁
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, env=env
        )
        engine._process = proc

        # Drain stderr in background thread so pipe buffer never blocks the child
        # 用局部 proc 而不是 engine._process，避免快速 stop→start 时读到新进程的 stderr
        stderr_lines: collections.deque[str] = collections.deque(maxlen=300)
        def _drain():
            if proc.stderr:
                try:
                    for line in proc.stderr:
                        stderr_lines.append(line)
                except Exception:
                    pass
        t = threading.Thread(target=_drain, daemon=True)
        t.start()

        # Poll HTTP immediately — returns as soon as model is ready (no dead-wait)
        # 就绪超时 15 分钟：MoE 大模型冷加载（Ornith-35B-A3B-MLX 18GB）需 5-8 分钟，
        # 300s 会在临近就绪时误杀引擎（09-04 事故）→ UI 卡死无反应。
        # _wait_for_http 是轮询，就绪即返回，不会真的等满 15 分钟。
        if not engine._wait_for_http(port, timeout_sec=900, process=proc, cancel=engine._cancel_load):
            t.join(timeout=1)
            err_lines = list(stderr_lines)[-50:] if stderr_lines else []
            err_text = "".join(err_lines)
            # Extract only the last meaningful error line (skip traceback clutter)
            err_summary = ""
            for line in reversed(err_lines):
                stripped = line.strip()
                if stripped and ("Error" in stripped or "error" in stripped.lower() or "ValueError" in stripped or "does not exist" in stripped.lower() or "No such file" in stripped):
                    err_summary = stripped[-300:]
                    break
            if not err_summary and err_lines:
                err_summary = err_lines[-1].strip()[-300:]
            logger.error(
                "llama-cpp server %s: %s",
                "exited early" if proc.poll() is not None else "HTTP timeout",
                err_text[:500] if err_text else "no stderr",
            )
            # Spark 类新架构（python 引擎不支持）：自动回退原生 llama-server
            # （XHToken fork）重试——旧模型仍走 python 路径，只有 python 加载
            # 失败的模型才触发回退（09-08 Spark-X2.5 架构支持方案）
            _native = engine._find_llama_server(model_path)
            if not _native:
                _hint = ("该模型需要原生 llama.cpp 引擎（Spark 类新架构），"
                         "但本安装包未包含它：请更新到 v0.3.25+ 或重新安装")
                logger.error("python 引擎加载失败且无原生引擎可用: %s | %s", model_path, _hint)
                engine.server_status = "error"
                engine.status_message = _hint
                return engine.get_status()
            if _native:
                logger.warning("llama-cpp python 引擎加载失败，回退原生 llama-server 重试: %s", model_path)
                # ⚠️ 不能在此调 stop_model()：其首行 engine._cancel_load.set() 会置位
                # 加载取消事件，导致 native 的 _wait_for_http 一进来即命中
                # cancel.is_set() → 立即返回 False（1 秒假超时）→ 失败处理
                # 再 stop_model 杀掉刚启动成功的 fork（09-08 UI 反复
                # "启动失败 listening" 的最终根因）。native 启动自带
                # _kill_port 清场，无需先停 python 引擎；仅清取消事件。
                engine._cancel_load.clear()
                return engine._start_llama_native(model_path, port)
            engine.stop_model()
            engine.server_status = "error"
            engine.status_message = f"启动失败: {err_summary}" if err_summary else "模型加载超时或进程已退出"
            engine.current_model_id = ""
            engine.current_model_name = ""
            return engine.get_status()

        engine.server_status = "running"
        engine.status_message = f"{engine.current_model_name} 运行中"
        engine._active_backend = "llama-cpp"
        engine._save_engine_state()
        logger.info("模型加载完成 (llama-cpp) port=%s model=%s", port, engine.current_model_name)
        return engine.get_status()
    except Exception as e:
        logger.error("Failed to start llama-cpp server: %s", e)
        engine.server_status = "error"
        engine.status_message = str(e)[:200]
        return engine.get_status()
