"""本地引擎进程（llama.cpp / native / MLX 启动 + 健康守护）— EngineProcess。

从 EngineProcess 拆出（2026-09-24）。
"""
from __future__ import annotations

import json
import logging
import os
import platform
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from local_llm_probe import (
    IS_APPLE_SILICON,
    IS_MAC,
    IS_WINDOWS,
    MODELS_DIR,
    _custom_engine_spec,
    _custom_engine_target,
    _read_config,
)

logger = logging.getLogger("latiao-sidecar")

_ENGINE_ENV_PREFIXES = ("DYLD_", "LD_", "MTL_", "GGML_", "OMP_", "CUDA_", "VK_", "HF_", "MLX_")


class EngineProcess:
    # 启动器已拆到 local_llm_launch_*.py（三后端各一文件）；下面保留薄委托，
    # 旧调用点（self._start_llama_cpp 等）不变。

    """本地模型引擎进程：启动/停止/健康探测/多后端启动器。"""

    _atexit_registered = False

    _atexit_registered = False

    def __init__(self):
        self.backend = "llama-cpp"
        self.mlx_available = False
        self.llama_cpp_available = False

        # Detect backends
        try:
            import mlx_lm  # noqa: F401
            self.mlx_available = True
            if IS_APPLE_SILICON:
                self.backend = "mlx"
        except (ImportError, RuntimeError):
            pass

        try:
            import llama_cpp  # noqa: F401
            self.llama_cpp_available = True
            if not self.mlx_available:
                self.backend = "llama-cpp"
        except (ImportError, RuntimeError):
            pass

        if not self.mlx_available and not self.llama_cpp_available:
            self.backend = "none"

        # Runtime server state
        self._process: subprocess.Popen | None = None
        # start/stop 主流程串行化锁（RLock：启动失败的错误路径会再调 stop_model）
        self._proc_lock = threading.RLock()
        self._active_backend = ""  # The backend actually used to start the current model
        self.current_model_id = ""
        self.current_model_name = ""
        self.server_port = 1235
        self.server_status = "stopped"  # stopped | starting | running | error
        self.status_message = ""
        # 引擎健康状态：agent 层发现异常（空响应/中断残留）后 mark_engine_suspect，
        # 下一次请求前强制发 mini 请求验证，失败则杀掉引擎进程避免继续带病运行。
        self._health_ok = True
        self._health_verified_at = 0.0
        self._health_fail_count = 0
        self._auto_reloading = False
        # User explicitly requested a stop — get_status() must NOT flip back to
        # "running" via the reconnect probe while the port is still draining.
        self._explicit_stop = False
        # 加载取消事件：stop_model 置位后，正在轮询等待的 _wait_for_http
        # 立即中止（否则加载期 start_model 持锁，Stop 会被阻塞最长 300s）
        self._cancel_load = threading.Event()
        # External engine mode (LM Studio / Ollama): when an external
        # OpenAI-compatible server is running, Latiao forwards local-model
        # requests to it instead of launching its own llama.cpp process.
        # Needed for models whose GGUF architecture (e.g. muse-glimmer) the
        # bundled llama-cpp-python does not support yet, but LM Studio does.
        self._external_engine = ""   # "" | "lmstudio" | "ollama"
        self._external_url = ""      # e.g. "http://127.0.0.1:1234/v1"
        self.has_image_support = False
        # _find_gguf 结果缓存（30s TTL），避免重复全盘 rglob 扫描
        self._gguf_find_cache: dict[str, tuple[float, str | None]] = {}
        self._restore_engine_state()

        # Register exit handler as belt-and-suspenders (once per process)
        if not EngineProcess._atexit_registered:
            import atexit
            atexit.register(self._cleanup_child)
            EngineProcess._atexit_registered = True
        self.model_token_limit = int(os.environ.get("LATIAO_CTX_LEN", "8192"))
        self.n_gpu_layers = int(os.environ.get("LATIAO_GPU_LAYERS", "-1"))
        # 并发槽位（09-23）：model_token_limit 是**每个会话**的窗口，引擎总量 = 槽位 × 窗口。
        # 不显式声明 --parallel 时该构建按 auto 处理（n_parallel=4 且 kv_unified=true）：
        # 4 个槽**共用一个** -c 池 —— 多会话同跑时每路只分到 1/4 窗口。显式声明后
        # llama-server 按 n_ctx/n_parallel 均分，每槽拿到完整窗口。
        # 来源：环境变量 LATIAO_LLM_SLOTS > config.json 的 local_llm.slots > 2；上限 4。
        self.parallel_slots = self._resolve_parallel_slots()

        # 引擎**实际**按几个槽启动（未启动/取不到时由 transport 退化为串行）；切模型后
        # 才生效，transport 用这个而不是配置值，避免"配置已改、引擎还是旧参数"的不一致。
        self._launched_slots = 1



    def get_backend(self) -> str:
        return self.backend

    def get_available_backends(self) -> list[str]:
        backends = []
        if self.mlx_available:
            backends.append("mlx")
        if self.llama_cpp_available:
            backends.append("llama-cpp")
        return backends or ["none"]

    def open_path(self, path: str) -> dict:
        try:
            if IS_MAC:
                p = subprocess.Popen(["open", path])
                # 后台回收子进程，避免僵尸进程堆积
                threading.Thread(target=p.wait, daemon=True).start()
            elif IS_WINDOWS:
                os.startfile(path)
            else:
                p = subprocess.Popen(["xdg-open", path])
                threading.Thread(target=p.wait, daemon=True).start()
            return {"status": "ok", "message": f"已打开: {path}"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # ── Server status ──

    def get_status(self) -> dict:
        # External engine mode: probe the external server each call. If LM
        # Studio / Ollama was closed or its model unloaded, drop our state so
        # the UI reflects reality instead of showing "running" on a dead target.
        if self._external_engine and self._external_url:
            if not self._probe_external_engine():
                self._external_engine = ""
                self._external_url = ""
                self.server_status = "stopped"
                self.status_message = "外部引擎已断开"
                self.current_model_id = ""
                self.current_model_name = ""
                self._active_backend = ""
        if self._process and self._process.poll() is not None:
            self._handle_dead_process()
        # If engine thinks we're stopped but a model server is still on our port
        # (e.g. sidecar restarted, model process outlived it), reconnect so the UI
        # shows accurate status without waiting for a chat request.
        # BUT: never do this right after the user explicitly pressed stop — the
        # model process may still be draining (killing a multi-GB model takes a
        # moment) and the port probe would flip the UI back to "running", making
        # it look like the stop did nothing.
        if (self.server_status == "stopped" and not self._explicit_stop
                and not self._engine_busy()
                and self._probe_port(self.server_port)):
            self.server_status = "running"
            self.status_message = "(reconnected after sidecar restart)"
            if not self._active_backend:
                self._active_backend = self.backend
        return {
            "backend": self._active_backend or self.backend,
            "available_backends": self.get_available_backends(),
            "status": self.server_status,
            "model_id": self.current_model_id,
            "model_name": self.current_model_name,
            "port": self.server_port,
            "message": self.status_message,
            "has_image_support": self.has_image_support,
            "token_limit": self.model_token_limit,
            "platform": platform.system(),
            "gpu_layers": self.n_gpu_layers,
        }

    def is_running(self) -> bool:
        # External engine mode: rely on the external server (no own process).
        if self._external_engine and self._external_url:
            try:
                import urllib.request
                urllib.request.urlopen(f"{self._external_url}/models", timeout=3)
                return True
            except Exception:
                return False
        if not self._process or self._process.poll() is not None:
            return False
        try:
            import urllib.request
            urllib.request.urlopen(f"http://127.0.0.1:{self.server_port}/v1/models", timeout=3)
            return True
        except Exception:
            return False

    def get_api_url(self) -> str:
        # External engine mode: requests go to LM Studio / Ollama, not our own
        # llama.cpp process (which may not support the model's architecture).
        if self._external_engine and self._external_url:
            return self._external_url.rstrip("/")
        if self.is_running():
            return f"http://127.0.0.1:{self.server_port}/v1"
        # Engine was restarted — check if a model server is still running on our port
        # (e.g. sidecar was killed and restarted, but model process outlived it).
        # 复用的可能是旧会话遗留的引擎（参数/状态未知，甚至已损坏）——
        # 必须实测健康后才复用，否则空响应/挂起会全部传导给用户。
        if self._probe_port(self.server_port):
            if not self.ensure_engine_healthy():
                # 不健康返回 False 时 ensure_engine_healthy 内部已完成处置
                # （杀端口 + 后台重载）。此前这里返回空串，导致请求 URL 缺协议
                # （UnsupportedProtocol）秒死。返回标准 URL 让 agent 层进入
                # 等待-重试循环，重载完成后自然恢复。
                return f"http://127.0.0.1:{self.server_port}/v1"
            self.server_status = "running"
            self.status_message = "(reconnected after sidecar restart)"
            if not self._active_backend:
                self._active_backend = self.backend  # best guess: platform default
            return f"http://127.0.0.1:{self.server_port}/v1"
        # 端口也没了（引擎进程彻底退出）：有当前模型则后台自动重载，
        # 返回 URL 让 agent 层在重载窗口内排队等待（6 分钟重试窗口覆盖加载）
        model_id = self.current_model_id
        process_dead = self._process is None or self._process.poll() is not None
        if model_id and process_dead:
            self._process = None  # 丢弃僵尸句柄，让 start_model 能重新 Popen
            logger.info("引擎进程已退出，请求自动重载模型: %s", model_id)
            self._request_reload(model_id)
        return f"http://127.0.0.1:{self.server_port}/v1"

    # ── 引擎状态持久化（B1）──
    # sidecar 重启会丢失内存里的 current_model_id → get_api_url 的自动重载
    # 分支条件（model_id 非空）不成立 → 引擎死了不恢复，请求干等 6 分钟报错。
    _engine_state_file = Path.home() / ".local-ai-os" / ".engine_state.json"

    def _save_engine_state(self):
        try:
            self._engine_state_file.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "model_id": self.current_model_id,
                "model_name": self.current_model_name,
                "backend": self._active_backend,
                "port": self.server_port,
                "saved_at": time.time(),
            }
            tmp = self._engine_state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
            os.replace(tmp, self._engine_state_file)
        except Exception:
            logger.warning("Failed to save engine state", exc_info=True)

    def _clear_engine_state(self):
        try:
            self._engine_state_file.unlink(missing_ok=True)
        except Exception:
            pass

    def _restore_engine_state(self):
        """sidecar 启动时读回上次加载的模型（仅恢复元数据，不自动拉起进程）。"""
        try:
            if self._engine_state_file.exists():
                data = json.loads(self._engine_state_file.read_text(encoding="utf-8"))
                mid = data.get("model_id", "")
                if mid:
                    # 本地路径型 model_id 必须真实存在。状态文件可能被测试
                    # 写入的假 id 污染（如 /models/test-35b），恢复后 sidecar
                    # 反复对着不存在的模型自动重载 -> 启动日志刷屏且引擎永不就绪。
                    if mid.startswith(("/", "~", "./")) or ".gguf" in mid or mid.startswith("\\"):
                        from pathlib import Path as _P
                        expanded = _P(os.path.expanduser(mid)) if mid.startswith("~") else _P(mid)
                        # exists() 对目录也成立：LM Studio 的 .gguf 目录合法存在，
                        # 直接放行（start_model 会解析到内部真实文件）。
                        if not expanded.exists():
                            logger.warning(
                                "引擎状态文件中的模型路径不存在，丢弃: %s", mid)
                            self._clear_engine_state()
                            return
                    self.current_model_id = mid
                    self.current_model_name = data.get("model_name", "")
                    self._active_backend = data.get("backend", "")
                    logger.info("已恢复引擎状态: %s (backend=%s)", mid, data.get("backend"))
        except Exception:
            logger.warning("Failed to restore engine state", exc_info=True)

    def _resolve_parallel_slots(self) -> int:
        """并发槽位数：环境变量 LATIAO_LLM_SLOTS > config.json local_llm.slots > 2，钳到 1..4。

        1 = 老行为（transport 串行、-c 就是窗口总量）；>1 = 每槽独占 model_token_limit，
        引擎总量按槽位线性放大（q4_0 KV 实测每 64k 约 0.4GB）。
        """
        _raw = os.environ.get("LATIAO_LLM_SLOTS", "")
        if not _raw:
            try:
                _ll = _read_config().get("local_llm") or {}
                _raw = str(_ll.get("slots", "") or "")
            except Exception:
                _raw = ""
        try:
            _n = int(_raw)
        except (TypeError, ValueError):
            _n = 2
        return max(1, min(4, _n))

    def _auto_reload(self, model_id: str):
        """后台自动重载：加载完成后复位 _auto_reloading 标记。"""
        try:
            self.start_model(model_id)
        finally:
            self._auto_reloading = False
            # start_model 失败路径可能经 stop_model 置位 _explicit_stop——但这是
            # 自动重载失败，不是用户手动停止；复位让排队中的请求收到准确的
            # "自动重载失败"错误，而不是误导性的"已被手动停止"（14:41 事故）。
            if self.server_status == "error":
                self._explicit_stop = False

    def _request_reload(self, model_id: str) -> bool:
        """统一的重载请求入口（带防重入守卫）。

        所有自动重载路径（get_api_url / ensure_engine_healthy）必须走这里：
        已有重载进行中时直接跳过并返回 False。此前 ensure_engine_healthy
        直接裸起 start_model 线程、不设 _auto_reloading，与 get_api_url 的
        重载并发时两个 start_model 串行执行——第二个先杀掉刚加载完的引擎
        再加载一遍，旧新权重同时驻留 = 内存 95% 尖峰。"""
        if getattr(self, "_auto_reloading", False):
            logger.info("重载已在进行中，跳过重复重载请求: %s", model_id)
            return False
        self._auto_reloading = True
        logger.info("自动重载已启动: %s (后台加载中，请求将排队等待)", model_id)
        threading.Thread(
            target=lambda mid=model_id: self._auto_reload(mid),
            daemon=True,
        ).start()
        return True

    # ── 忙引擎保护（A3）──
    # 本地流式进行中时，引擎对健康探测的响应必然超时（串行处理），
    # 不能据此判定引擎死亡。agent_loop 在流开始/结束时调用这两个方法，
    # 健康检查看到宽限窗口内的忙标志直接视为存活。
    _engine_busy_until = 0.0
    _active_local_streams = 0  # 活跃本地流计数（agent_loop 进入/退出时增减）

    def mark_engine_busy(self, grace_sec: float = 45.0):
        """标记引擎正在服务流式请求（时间戳兜底，主判定看 _active_local_streams）。"""
        import time as _t
        type(self)._engine_busy_until = _t.monotonic() + max(grace_sec, 180.0)

    def mark_engine_idle(self):
        type(self)._engine_busy_until = 0.0

    @classmethod
    def mark_stream_enter(cls):
        cls._active_local_streams += 1
        import time as _t
        cls._engine_busy_until = _t.monotonic() + 300.0

    @classmethod
    def mark_stream_exit(cls):
        cls._active_local_streams = max(0, cls._active_local_streams - 1)

    def _engine_busy(self) -> bool:
        import time as _t
        # 活跃流计数是主判定：只要还有本地流在读，引擎就是在正常干活
        if type(self)._active_local_streams > 0:
            return True
        return _t.monotonic() < type(self)._engine_busy_until

    def _probe_external_engine(self) -> tuple[str, str, str] | None:
        """Detect an external OpenAI-compatible local server (LM Studio 1234,
        Ollama 11434). Returns (engine_name, base_url, loaded_model_id) or None.
        Requires the server to have a model loaded (LM Studio must have a model
        running in its UI; Ollama requires one pulled)."""
        candidates = [
            ("lmstudio", "http://127.0.0.1:1234/v1"),
            ("ollama", "http://127.0.0.1:11434/v1"),
        ]
        for name, base in candidates:
            try:
                with urllib.request.urlopen(f"{base}/models", timeout=2) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="replace"))
                models = data.get("data") or []
                if models:
                    mid = models[0].get("id", "")
                    return (name, base, mid)
            except Exception:
                continue
        return None

    @staticmethod
    def _probe_port(port: int, timeout: float = 5) -> bool:
        """Check if a model server is listening on this port.
        Uses TCP connect first (fast), then HTTP GET /v1/models as confirmation."""
        import socket
        # Fast check: is anything listening?
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=2)
            sock.close()
        except Exception:
            return False
        # Confirm it's an OpenAI-compatible server
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/v1/models", timeout=timeout,
            )
            return True
        except Exception:
            # /v1/models may return empty body on some servers (MLX) —
            # the TCP connect above already confirmed the port is alive
            return True

    def mark_engine_suspect(self):
        """Agent 层发现引擎行为异常（空响应/流被中断后残留线程）时调用，
        下一次请求前强制做一次健康验证，避免继续向损坏的引擎发请求。"""
        self._health_verified_at = 0.0

    def verify_engine_health(self, timeout: float = 20) -> bool:
        """向引擎发一个最小 chat 请求，确认它能真正产出文本。

        被中断/长时间运行的 llama.cpp 引擎可能处于"端口活着但生成异常"的
        状态（空响应/挂起），只探测端口发现不了，必须实测生成。

        注意：model 必须用真实加载的模型 id。此前用假名 "health-check"，
        mlx_lm.server 会按名字去 HuggingFace Hub 解析 → 镜像证书不被内置
        Python 信任 → SSL 校验失败 → 健康检查对健康引擎也永远报死，
        把"引擎挂起检测"整条链弄瞎（误杀健康引擎 + 真挂起时探测不到）。
        """
        if not self._probe_port(self.server_port, timeout=1):
            return False
        try:
            _model_ref = self.current_model_id or self.current_model_name or ""
            body = json.dumps({
                "model": _model_ref, "stream": False, "max_tokens": 4,
                "messages": [{"role": "user", "content": "hi"}],
            }).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.server_port}/v1/chat/completions",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
            # 只看响应结构：推理模型（Ornith 等）max_tokens=4 时 token 全进
            # <think>，content 恒为空——按 content 判会永远认为引擎不健康
            # （20:25 事故：35B 运行中被"健康检查连续失败"杀重载循环）
            msg = (data.get("choices") or [{}])[0].get("message")
            return bool(msg)
        except Exception:
            return False

    def ensure_engine_healthy(self, force: bool = False) -> bool:
        """TTL 缓存的健康检查。

        重要：mlx_lm/llama server 串行处理请求——长生成（35B 可达数分钟）期间
        健康探测会排队超时，单次失败不能判定引擎死亡，否则会误杀运行中的模型
        并触发 26GB 重载（旧新两份权重同时驻留 → 内存 98%）。
        连续两次失败才判死并处置。"""
        now = time.time()
        if not force and self._health_ok and now - self._health_verified_at < 30:
            return True
        # 忙引擎保护：流式进行中（或刚结束的宽限期内）引擎对探测必然响应慢，
        # 不能据此判死——直接视为存活，避免误杀正在干活的模型触发重载
        if self._engine_busy():
            self._health_ok = True
            self._health_verified_at = now
            logger.info("引擎忙于流式请求，健康检查跳过（视为存活）")
            return True
        ok = self.verify_engine_health()
        self._health_ok = ok
        self._health_verified_at = now
        if ok:
            self._health_fail_count = 0
            return True
        # 两连败之间要求最小时间窗 60s：连续探测间隔过近（cron+用户消息并发），
        # 引擎忙于长生成时两次都会超时 -> 判死。间隔不足视为同一次事件。
        prev_fail_at = getattr(self, "_health_first_fail_at", 0.0)
        if prev_fail_at == 0.0 or now - prev_fail_at < 60:
            self._health_first_fail_at = now
            # 空闲引擎（无活跃流 + 不在宽限期）：挂起状态不会自愈，
            # 3s 后立即复验一次，仍失败直接处置--不再让用户面对 60s 黑盒。
            # 忙引擎维持 60s 窗口（复验会排队超时，快速判死有误杀长生成风险）。
            if not self._engine_busy():
                if self.server_status == "starting":
                    # 加载中的引擎探活失败是常态（模型未就绪时 chat 404/无响应），
                    # 快速处置只会杀掉正在加载的引擎、再拉一轮重载（14:38 事故）
                    logger.info("引擎健康检查首次失败（模型加载中），暂不处置")
                    return True
                EngineProcess._wait_before_recheck(3)
                # 复验期间流可能进入（用户发了新消息）-> 退回慢路径
                if not self._engine_busy() and not self.verify_engine_health():
                    self._health_first_fail_at = 0.0
                    self._dispose_dead_engine()
                    return False
                self._health_first_fail_at = 0.0
            logger.info("引擎健康检查首次失败（可能正忙于长生成），暂不处置")
            return True
        # 第二次失败且间隔 ≥60s -> 进入处置
        self._dispose_dead_engine()
        return False

    # 空闲复验前的等待（默认 3s）；类属性便于测试替换成零等待
    _wait_before_recheck = staticmethod(time.sleep)

    def _handle_dead_process(self):
        """引擎子进程意外退出（崩溃/OOM/被杀）的统一处置。

        保留模型记录并自动重载（与 _dispose_dead_engine 同口径）——此前
        get_status 轮询在这里直接清空 current_model_id，正在排队等待恢复的
        请求会因"无模型记录"被秒死，前端也显示已卸载。用户主动停止由
        _explicit_stop 区分（stop_model 已自行清空状态并置位）。"""
        if self.server_status != "error":
            self.server_status = "stopped"
            self.status_message = (
                f"进程已退出 (code: {getattr(self._process, 'returncode', '?')})")
        if (self.current_model_id and not self._explicit_stop
                and not self._auto_reloading and self.server_status != "error"):
            self.status_message = "引擎异常，正在自动重新加载模型..."
            self._request_reload(self.current_model_id)

    def _dispose_dead_engine(self):
        """杀掉判定已死的引擎进程并触发后台自动重载（两处判死路径共用）。"""
        self._health_fail_count = 0
        self._health_first_fail_at = 0.0
        logger.warning("本地模型引擎健康检查连续失败，停止引擎进程")
        self._kill_port(self.server_port)
        self.server_status = "stopped"
        self.status_message = ""
        # 引擎崩溃/被杀（如系统内存压力）：有当前模型则后台自动重载，
        # 下一条消息直接可用（重载需要时间，等待期间显示提示）
        model_id = self.current_model_id
        if model_id:
            self.status_message = "引擎异常，正在自动重新加载模型..."
            self._request_reload(model_id)

    # ── Start / Stop ──

    def _wait_for_http(self, port: int, timeout_sec: float = 120, process: subprocess.Popen | None = None,
                      cancel: "threading.Event | None" = None) -> bool:
        """Poll the model server until a REAL chat completion succeeds, times out, or dies.

        ⚠️ 不能只探 GET /v1/models：mlx_lm.server 在模型加载完成前就无条件
        200（/health、/v1/models 都是裸 200）——端口通了 ≠ 模型就绪。
        20:05-20:07 事故：35B 权重还在加载即被标记 running，用户消息 chat
        404 → agent_loop 判"引擎损坏"杀进程 → 假完成 → 再 404 → 每 5s 死循环，
        内存永远驻留不下来。这里 POST 最小 chat，返回 200 且有内容才是真就绪；
        加载期间 mlx server 对 chat 回 404，继续等。

        cancel 置位时立即返回 False（用户点停止 / 要取消加载）。"""
        # ⚠️ model 字段必须用真实加载的模型 id（或省略）：mlx_lm.server 会把
        # 未知模型名当 HuggingFace repo 解析 → SSL 失败 → 404（假名 health-check
        # 是 verify_engine_health 已踩过的同款坑，20:17 事故重演）。
        _model_ref = self.current_model_id or self.current_model_name or ""
        body = json.dumps({
            "model": _model_ref, "stream": False, "max_tokens": 16,
            "messages": [{"role": "user", "content": "hi"}],
            # 探测必须关思考：推理模型（Ornith 等）16 个 token 全进 <think>
            # → 单次探测 ~9s，超过探测超时 10s → 被放弃的请求在引擎串行队列
            # 里积压 → 后续探测永远超时 → "启动中"卡死（09-07 20:06 事故）。
            # 关思考后探测秒回，且 content 非空可直接判就绪。
            "chat_template_kwargs": {"enable_thinking": False},
        }).encode()
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            if cancel is not None and cancel.is_set():
                return False
            if process is not None and process.poll() is not None:
                return False  # Process died — caller will read stderr
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    data=body, headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read())
                # 生成成功 = 就绪。判定标准：content 或 reasoning 任一非空。
                # 推理模型（Ornith/GLM）max_tokens 小时 token 全进 <think>，
                # content 空但 reasoning 有字（20:33 事故）；反之坏的量化
                # 模型（如 LM Studio 下载损坏的 4bit）16 个 token 全空、
                # content 和 reasoning 都是 None——此时不能判就绪，应报错
                # 而不是静默空响应（09-01 事故）。
                _msg = (data.get("choices") or [{}])[0].get("message") or {}
                _has_output = bool(
                    (_msg.get("content") or "").strip()
                    or (_msg.get("reasoning") or "").strip())
                if resp.status == 200 and _has_output:
                    return True
            except urllib.error.HTTPError as e:
                if e.code in (404, 503):
                    time.sleep(1)  # 模型仍在加载（mlx 加载期 chat=404）
                    continue
            except Exception:
                pass
            time.sleep(0.5)
        return False

    @staticmethod
    def _kill_port(port: int):
        """Kill any process listening on the given port."""
        if platform.system() == "Windows":
            try:
                result = subprocess.run(
                    ["netstat", "-ano"], capture_output=True, text=True, timeout=10
                )
                for line in result.stdout.splitlines():
                    # 只认 LISTENING 且本地地址列精确匹配 :port —— 否则会命中
                    # 远端地址列（作为客户端连出去的连接，包括 sidecar 自己）
                    parts = line.split()
                    if len(parts) >= 4 and parts[3] == "LISTENING" \
                            and parts[1].rstrip(":").endswith(f":{port}"):
                        pid = parts[-1]
                        if pid.isdigit():
                            subprocess.run(["taskkill", "/F", "/PID", pid],
                                           capture_output=True, timeout=10)
            except Exception:
                pass
            return
        try:
            # -sTCP:LISTEN 只匹配监听 socket。裸 `lsof -ti :PORT` 会把 sidecar
            # 自己（作为客户端探测该端口后的 TIME_WAIT 连接）也列出来并 SIGKILL
            # —— 等于停止/健康检查按钮随机自杀整个 sidecar。
            result = subprocess.run(
                ["lsof", "-ti", f":{port}", "-sTCP:LISTEN"],
                capture_output=True, text=True, timeout=5,
            )
            my_pid = os.getpid()
            for pid_str in result.stdout.strip().split("\n"):
                pid = pid_str.strip()
                if pid and pid.isdigit():
                    if int(pid) == my_pid:
                        continue  # 保险带：绝不能杀自己
                    try:
                        logger.warning(" killing pid=%s on port %s", pid, port)
                        os.kill(int(pid), 9)
                    except OSError:
                        pass
        except Exception:
            pass

    def _find_gguf(self, model_id: str) -> str | None:
        # 30 秒 TTL 缓存：start/delete 等路径会重复触发全盘 rglob 扫描
        cached = self._gguf_find_cache.get(model_id)
        if cached and time.time() - cached[0] < 30:
            return cached[1]
        result = self._find_gguf_uncached(model_id)
        self._gguf_find_cache[model_id] = (time.time(), result)
        return result

    def _find_gguf_uncached(self, model_id: str) -> str | None:
        if model_id.endswith(".gguf"):
            p = Path(model_id)
            if p.is_file():
                return model_id
            # LM Studio 布局：名字以 .gguf 结尾的其实是目录，真正的模型
            # 文件同名地放在里面（~/.lmstudio/models/<org>/<name>.gguf/<name>.gguf）。
            # 此前用 exists() 判断--目录也算存在，整个目录被当成模型文件
            # 传给 llama.cpp -> "Failed to load model from file"（目录无法 mmap）。
            if p.is_dir():
                inner = p / p.name
                if inner.is_file():
                    return str(inner)
                # 目录里唯一的 .gguf 也接受（兼容部分变体布局）
                candidates = sorted(p.glob("*.gguf"))
                if len(candidates) == 1:
                    return str(candidates[0])
        # Strip .gguf suffix and repo prefix for fuzzy matching
        key = model_id.replace(".gguf", "").lower()
        # Also try just the filename part
        key_short = model_id.rsplit("/", 1)[-1].replace(".gguf", "").lower()

        def _search_dir(root: Path):
            """rglob a directory for a gguf file fuzzy-matching the key."""
            if not root or not root.exists():
                return None
            for f in root.rglob("*.gguf"):
                if not f.is_file():
                    continue
                stem = f.stem.lower()
                if key in stem or stem in key or key_short in stem:
                    return str(f)
            return None

        # Search MODELS_DIR first (user-placed models, ~/Models/)
        hit = _search_dir(MODELS_DIR)
        if hit:
            return hit
        # Also search third-party model managers so files downloaded elsewhere
        # (LM Studio, Ollama, etc.) are reusable without manual copying.
        for extra in (
            Path.home() / ".lmstudio" / "models",
            Path.home() / ".ollama" / "models",
        ):
            hit = _search_dir(extra)
            if hit:
                return hit
        # Also search download cache (~/.cache/huggingface/models/)
        # _cache_dir 定义在 ModelDownloader；EngineProcess 没有它（组合体的
        # __getattr__ 管不到类内 self）。直接按标准 HF 缓存布局定位。
        cache_models = Path.home() / ".cache" / "huggingface" / "models"
        return _search_dir(cache_models)

    def _find_gguf_for_delete(self, model_id: str) -> str | None:
        """删除专用的精确查找：只在 ~/Models 内、stem 或文件名完全相等。

        不做双向子串模糊匹配，也不搜 LM Studio/Ollama 等第三方目录，
        避免误删不属于自己的模型文件。
        """
        try:
            models_root = MODELS_DIR.resolve()
        except OSError:
            return None
        resolved: Path | None = None
        if model_id.endswith(".gguf"):
            p = Path(model_id)
            if p.is_file():
                resolved = p
            elif p.is_dir():
                # LM Studio 式目录：指向内部同名 .gguf，删除同样只认 ~/Models 内的
                inner = p / p.name
                resolved = inner if inner.is_file() else (p.glob("*.gguf") and next(iter(sorted(p.glob("*.gguf"))), None))
        if resolved is not None:
            # 直接路径：必须位于 ~/Models 内
            try:
                rp = resolved.resolve()
                rp.relative_to(models_root)
            except (OSError, ValueError):
                return None
            return str(rp)
        key = model_id.replace(".gguf", "").lower()
        key_short = model_id.rsplit("/", 1)[-1].replace(".gguf", "").lower()
        filename = model_id.rsplit("/", 1)[-1].lower()
        if not MODELS_DIR.exists():
            return None
        for f in MODELS_DIR.rglob("*.gguf"):
            if not f.is_file():
                continue
            stem = f.stem.lower()
            if stem == key or stem == key_short or f.name.lower() == filename:
                return str(f)
        return None

    @staticmethod
    def _guess_chat_format(model_path: str):
        from local_llm_launch_common import guess_chat_format
        return guess_chat_format(model_path)

    def _find_llama_server(self, model_path: str = ""):
        from local_llm_launch_common import find_llama_server
        return find_llama_server(model_path)

    def _start_llama_cpp(self, model_id: str, port: int) -> dict:
        from local_llm_launch_llamacpp import start_llama_cpp
        return start_llama_cpp(self, model_id, port)

    def _start_llama_native(self, model_path: str, port: int, **kw) -> dict:
        from local_llm_launch_native import start_llama_native
        return start_llama_native(self, model_path, port, **kw)

    def _start_mlx(self, model_id: str, port: int) -> dict:
        from local_llm_launch_mlx import start_mlx
        return start_mlx(self, model_id, port)

    def start_model(self, model_id: str, port: int = 1235) -> dict:
        # start/stop 主流程持锁，避免并发 start/stop 竞态
        # （RLock：内部错误路径会再调 stop_model）
        with self._proc_lock:
            self._explicit_stop = False
            self._cancel_load.clear()
            # 引擎启动时刻：agent_loop 的 404 判死用它做 120s 宽限，
            # 防止"假完成"（端口通但权重仍在加载）时误杀刚启动的引擎
            self._engine_started_at = time.monotonic()

            # ── External engine mode (LM Studio / Ollama) ──
            # 默认关闭：辣条自启引擎加载模型（冷加载 MoE 大模型需 5-8 分钟）。
            # 开启方式：环境变量 LATIAO_EXTERNAL_ENGINE=1 或 config.json
            # "external_engine": true —— 后者用于"模型已在 LM Studio 内存中、
            # 希望秒级就绪"的场景（8 月底此前用户即体验此模式）。
            _ext_cfg = ""
            try:
                _ext_cfg = _read_config().get("external_engine", "")
            except Exception:
                pass
            if os.environ.get("LATIAO_EXTERNAL_ENGINE", "") or _ext_cfg:
                external = self._probe_external_engine()
                if external:
                    eng_name, eng_url, eng_model = external
                    self._external_engine = eng_name
                    self._external_url = eng_url
                    self.current_model_id = model_id
                    self.current_model_name = Path(model_id).stem
                    self.server_status = "running"
                    self.status_message = f"通过 {eng_name} 加载 ({eng_model})"
                    self._active_backend = eng_name
                    self.server_port = port  # keep for status display
                    self.has_image_support = False
                    return self.get_status()

            # No external engine - clear stale external mode before self-start
            self._external_engine = ""
            self._external_url = ""

            # Kill any stale process on the target port before starting
            self._kill_port(port)
            if self._process and self._process.poll() is None:
                # 旧进程还在跑（常见于刚被 SIGKILL 但内核尚未回收的僵尸窗口）。
                # 绝不能调 stop_model——它会置 _cancel_load 取消事件，让本次
                # 加载的 _wait_for_http 立即失败并杀掉新引擎（15:04 事故根因：
                # 处置杀引擎后 1ms 重载线程进来，poll() 误判旧进程存活）。
                # 这里直接 terminate 旧进程即可。
                try:
                    self._process.terminate()
                    self._process.wait(timeout=5)
                except Exception:
                    try:
                        self._process.kill()
                        self._process.wait(timeout=5)
                    except Exception:
                        pass
                self._process = None
            self.server_port = port

            if self.backend == "none":
                return {"status": "error", "message": "无可用引擎。安装: pip install llama-cpp-python"}

            # ── 自定义引擎优先（09-20）──────────────────────────────
            # 匹配 custom_engine 的模型**直接交给它**，不走下面按格式判定的分支：
            # MLX/custom-runtime 包会因为"平台默认 mlx"被 mlx-lm 预检拦掉，
            # 而那种包的正确出路就是它自带的运行时。同时跳过 GGUF 预检
            # （量化类型/完整性闸门的理由正是"我们的引擎读不了这种文件"）。
            _custom = _custom_engine_spec(model_id)
            if _custom:
                _ok, _target = _custom_engine_target(model_id)
                if _ok != "ok":
                    self.server_status = "error"
                    self.status_message = f"自定义引擎（{_custom['name']}）无法加载：{_target}"
                    self.current_model_id = ""
                    self.current_model_name = ""
                    return self.get_status()
                logger.info("使用自定义引擎 '%s': %s ← %s", _custom["name"], _custom["binary"], _target)
                self.current_model_id = model_id
                self.current_model_name = Path(model_id).stem
                self.server_status = "starting"
                self.status_message = (f"正在用自定义引擎 {_custom['name']} 加载 "
                                       f"{self.current_model_name}...")
                return self._start_llama_native(_target, port, exe=_custom["binary"],
                                                extra_args=_custom["args"],
                                                backend="llama-cpp-custom",
                                                backend_name=_custom["name"])

            # ── Auto-detect model format → choose best backend ──
            use_llama = False
            use_mlx = False

            # Layer 1: file extension
            model_lower = model_id.lower()
            if model_lower.endswith(".gguf"):
                use_llama = True
            elif model_lower.endswith(".mlx"):
                use_mlx = True
            # Layer 2: HuggingFace model ID heuristics
            elif model_id.startswith("mlx-community/") or "/mlx-" in model_id:
                use_mlx = True
            elif any(kw in model_id.lower() for kw in ["gguf", "llama-cpp", "bartowski/"]):
                use_llama = True
            # Layer 3: search MODEL_DIR for matching file
            elif self._find_gguf(model_id):
                use_llama = True

            if use_llama:
                if not self.llama_cpp_available:
                    return {"status": "error", "message": "GGUF 模型需要 llama-cpp-python。安装: pip install llama-cpp-python"}
                return self._start_llama_cpp(model_id, port)
            if use_mlx:
                if not self.mlx_available:
                    return {"status": "error", "message": "MLX 模型需要 mlx-lm。安装: pip install mlx-lm"}
                return self._start_mlx(model_id, port)

            # Layer 4: fall back to platform default
            if self.backend == "mlx" and self.mlx_available:
                return self._start_mlx(model_id, port)
            else:
                return self._start_llama_cpp(model_id, port)

    def _cleanup_child(self):
        """atexit handler — kill child model server so it doesn't become orphaned."""
        if self._process and self._process.poll() is None:
            try:
                self._process.kill()
                self._process.wait(timeout=5)
            except Exception:
                pass
            if platform.system() == "Windows":
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(self._process.pid)],
                        capture_output=True, timeout=10
                    )
                except Exception:
                    pass

    def detach_engine(self):
        """sidecar 重启/部署前调用：放弃对模型子进程的所有权，使其独立存活。

        模型加载耗时巨大（35B MLX 冷启动可达数十分钟），sidecar 重启后
        get_status 的 reconnect 探测会重新接管端口上幸存的模型服务，
        避免"部署一次模型就没了"。"""
        with self._proc_lock:
            if self._process and self._process.poll() is None:
                logger.info("Detaching engine pid=%s (model=%s)", self._process.pid, self.current_model_id)
            self._process = None
            self._active_backend = ""

    def shutdown_engine(self) -> None:
        """应用退出时调用：杀掉本地模型进程并释放端口（释放显存/内存）。

        与 stop_model 的区别：不动引擎状态文件（.engine_state.json），
        下次启动仍可按需自动重新加载上次的模型（B1 恢复路径）。
        外部引擎模式（LM Studio/Ollama）不碰任何外部进程。
        """
        if self._external_engine:
            return
        with self._proc_lock:
            if self._process:
                try:
                    self._process.terminate()
                    self._process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait()
                except Exception:
                    try:
                        self._process.kill()
                    except Exception:
                        pass
                self._process = None
        # detach 过的引擎（sidecar 重启后接管端口）没有 _process 句柄，
        # 端口兜底清理同样要杀——应用都退出了，模型不该继续占内存
        self._kill_port(self.server_port)
        logger.info("Sidecar 关闭 — 本地模型引擎已停止（状态保留，下次自动重载）")

    def stop_model(self) -> dict:
        # 先置取消事件：让加载期持锁的 start_model 尽快释放锁，
        # 否则 Stop 会被阻塞到加载完成（最长 300s）
        self._cancel_load.set()
        with self._proc_lock:
            # External engine mode: the model runs inside LM Studio/Ollama —
            # never kill their process or their port, just drop our state.
            if self._external_engine:
                self._external_engine = ""
                self._external_url = ""
                self.server_status = "stopped"
                self.status_message = "已停止（外部引擎未受影响）"
                self.current_model_id = ""
                self.current_model_name = ""
                self._active_backend = ""
                self.has_image_support = False
                self._clear_engine_state()
                return self.get_status()
            if self._process:
                try:
                    self._process.terminate()
                    self._process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait()
                except Exception:
                    # 兜底：异常时也要 kill + wait，避免留下僵尸进程
                    try:
                        self._process.kill()
                        self._process.wait(timeout=5)
                    except Exception:
                        pass
                self._process = None
            # Belt-and-suspenders: ensure port is actually free
            self._kill_port(self.server_port)
            self._explicit_stop = True
            # Wait for the port to truly drain before reporting status. Without
            # this, get_status() runs while the dying process still holds the
            # socket and the reconnect probe flips status back to "running" —
            # the UI then shows the model as still loaded after pressing stop.
            for _ in range(10):
                if not self._probe_port(self.server_port, timeout=1):
                    break
                time.sleep(0.5)
            self.server_status = "stopped"
            self.status_message = "已停止"
            self.current_model_id = ""
            self.current_model_name = ""
            self._active_backend = ""
            self.has_image_support = False
            self._clear_engine_state()
            logger.info("模型已停止并卸载 (port=%s)", self.server_port)
            return self.get_status()

    def delete_model_file(self, model_id: str) -> dict:
        """Delete a local model GGUF file by model_id and clear download record."""
        # 只允许删除 ~/Models 内的文件，且精确匹配，避免误删 LM Studio/Ollama 模型
        path = self._find_gguf_for_delete(model_id)
        if not path:
            return {"status": "error", "message": f"找不到模型文件: {model_id}（仅可删除 ~/Models 内的模型）"}
        # 模型正在运行时拒绝删除
        if self._process and self._process.poll() is None:
            cur_stem = Path(self.current_model_name).stem.lower() if self.current_model_name else ""
            if self.current_model_id == model_id or (cur_stem and Path(path).stem.lower() == cur_stem):
                return {"status": "error", "message": "模型正在运行中，请先停止再删除"}
        try:
            os.unlink(path)
            logger.info(f"Deleted model file: {path}")
            # 清掉 _find_gguf 缓存，避免 30s TTL 内还命中已删除的路径
            self._gguf_find_cache.clear()
            # Also remove download record
            self.downloader.drop_download_record(model_id)
            return {"status": "ok", "message": f"已删除: {Path(path).name}"}
        except Exception as e:
            return {"status": "error", "message": f"删除失败: {str(e)}"}
