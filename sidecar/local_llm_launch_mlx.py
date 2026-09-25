"""MLX（Apple Silicon）后端启动。

从 local_llm_engine.EngineProcess 拿出（2026-09-24）：纯函数，engine 作首参。"""

from __future__ import annotations

from pathlib import Path
import collections
import subprocess
import sys
import threading
import time
from cmd_safety import child_env
from local_llm_probe import (
    _mlx_arch_supported,
    _mlx_reject_reason,
    _resolve_mlx_path,
)
from local_llm_launch_common import _ENGINE_ENV_PREFIXES

import logging
logger = logging.getLogger("latiao-sidecar")



def start_mlx(engine, model_id: str, port: int) -> dict:
    _ok, _mt = _mlx_arch_supported(model_id)
    if not _ok:
        engine.server_status = "error"
        # 09-20：判别原因的判据重写（见 _mlx_reject_reason）。旧实现只看
        # preprocessor_config.json → 把"需要自带运行时"的包说成"多模态 MLX-VLM"。
        _kind, engine.status_message = _mlx_reject_reason(model_id, _mt)
        logger.warning("MLX 架构预检拦截: %s (model_type=%s, 原因=%s)",
                       model_id, _mt, _kind)
        return engine.get_status()
    engine.current_model_id = model_id
    engine.current_model_name = model_id.split("/")[-1] if "/" in model_id else model_id
    engine.server_status = "starting"
    engine.status_message = f"正在加载 {engine.current_model_name}..."
    # ⚠️ 不要在此加"首次需下载"：本地文件存在时走的是本地加载（不联网），
    # 该文案会让用户误以为在消费流量（09-04 事故）。只有未解析到本地路径
    # 且后续真正走 HuggingFace 拉取时才提示下载。

    try:
        model_path = _resolve_mlx_path(model_id)
        # 引擎真实 id = 解析后的完整路径（/v1/models 返回它）。就绪探测和
        # 后续请求必须用它——短名会被 mlx 当 HF repo 解析 → 404 → 就绪
        # 探测死循环（09-07 20:17/20:24 事故：模型页点"启动"卡 15 分钟）。
        if model_path:
            engine.current_model_id = model_path
        if model_path != model_id and Path(model_path).is_dir():
            # 本地 MLX 目录：权重缺失时提前给出明确错误
            has_w = (Path(model_path) / "model.safetensors").exists() \
                or (Path(model_path) / "model.safetensors.index.json").exists() \
                or (Path(model_path) / "weights.npz").exists() \
                or any(Path(model_path).glob("weights*.npz"))
            if not has_w:
                engine.stop_model()
                engine.server_status = "error"
                engine.status_message = f"MLX 模型目录缺少权重文件（model.safetensors / weights.npz）: {model_path}"
                engine.current_model_id = ""
                engine.current_model_name = ""
                return engine.get_status()
        cmd = [
            sys.executable, "-m", "mlx_lm.server",
            "--model", model_path,
            "--port", str(port),
            "--host", "127.0.0.1",
        ]
        env = child_env(allow_prefixes=_ENGINE_ENV_PREFIXES)
        env.pop("HF_ENDPOINT", None)
        # mlx_lm.server 的 GET /v1/models 会调 scan_cache_dir()：HF hub
        # 缓存目录缺失时抛 CacheNotFound → 该请求必崩 → 就绪/健康探测
        # 永远失败 → sidecar 误判"正在加载"直到超时杀引擎（09-04 事故：
        # 15 分钟"假加载"的根因——模型实际 15 秒即可就绪）。确保目录存在。
        _hf_cache = Path.home() / ".cache" / "huggingface" / "hub"
        try:
            _hf_cache.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        if not env.get("HF_HUB_CACHE"):
            env["HF_HUB_CACHE"] = str(_hf_cache.parent)
        # 端口残留兜底：前一个引擎刚被杀（冻结/崩溃/残留），socket 可能短暂
        # 滞留，直接 bind 会 "Address already in use" 秒崩（14:41 事故）。
        # 先清场并等端口真正释放（probe 从"能连"变为"拒绝"即已释放）。
        engine._kill_port(port)
        for _ in range(20):
            if not engine._probe_port(port, timeout=0.5):
                break
            time.sleep(0.5)
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
        if not engine._wait_for_http(port, timeout_sec=900, process=proc, cancel=engine._cancel_load):
            t.join(timeout=1)
            err_lines = list(stderr_lines)[-50:] if stderr_lines else []
            err_text = "".join(err_lines)
            err_summary = ""
            for line in reversed(err_lines):
                stripped = line.strip()
                if stripped and ("Error" in stripped or "error" in stripped.lower() or "ValueError" in stripped or "does not exist" in stripped.lower()):
                    err_summary = stripped[-300:]
                    break
            if not err_summary and err_lines:
                err_summary = err_lines[-1].strip()[-300:]
            logger.error(
                "MLX server %s: %s",
                "exited early" if proc.poll() is not None else "HTTP timeout",
                err_text[:500] if err_text else "no stderr",
            )
            # 失败清理：杀残留进程即可。绝不能调 stop_model——它会连
            # current_model_id 和状态文件一起清空，自动重载场景下
            # "请求自动重载"分支从此失明（14:41 事故），后续请求只能
            # 报"请到模型页加载"而无法自愈。保留模型记录 + 标记 error，
            # 排队请求会收到明确的"自动重载失败"错误。
            try:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=5)
            except Exception:
                pass
            engine._kill_port(port)
            engine._process = None
            engine.server_status = "error"
            engine.status_message = f"启动失败: {err_summary}" if err_summary else "模型加载超时或进程已退出"
            return engine.get_status()

        engine.server_status = "running"
        engine.status_message = f"{engine.current_model_name} 运行中 (MLX)"
        engine.has_image_support = "vision" in model_id.lower() or "llama-4" in model_id.lower()
        engine._active_backend = "mlx"
        engine._save_engine_state()
        logger.info("模型加载完成 (mlx) port=%s model=%s", port, engine.current_model_name)
        return engine.get_status()
    except Exception as e:
        logger.error("Failed to start MLX server: %s", e)
        engine.server_status = "error"
        engine.status_message = str(e)[:200]
        return engine.get_status()
