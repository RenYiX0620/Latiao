"""
Latiao Local LLM Engine — Cross-Platform (Mac / Windows / Linux)

Auto-detects best backend:
  - Apple Silicon + mlx-lm → MLX (fastest)
  - Fallback → llama-cpp-python (cross-platform, GPU accel via Metal/CUDA/Vulkan)
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re as _re
import subprocess
import sys
import threading
import time
from pathlib import Path

# Module-level SSL context for Python 3.14 macOS compatibility
# (huggingface.co connections can use unverified context; files are hash-verified)
try:
    _ssl_ctx = __import__('ssl')._create_unverified_context()
except Exception:
    _ssl_ctx = None

logger = logging.getLogger("latiao-sidecar")

MODELS_DIR = Path(os.environ.get("LATIAO_MODELS_DIR", Path.home() / "Models"))
MODELS_DIR.mkdir(parents=True, exist_ok=True)

# ── Model quant → KV cache quant mapping ──
# KV cache precision should never exceed model weight precision.
# Q2/Q3/Q4 model → Q4_0 KV; Q5+ model → Q8_0 KV

def _detect_model_bits(model_path: str) -> int:
    """Detect model quantization bits from filename. Returns 4, 5, 6, 8, or 16."""
    import re
    name = Path(model_path).name.upper().replace(".GGUF", "")
    # Match common quantization markers: Q4_K_M, Q5_0, IQ3_XXS, Q8_0, etc.
    m = re.search(r'(?:^|[._-])(?:Q|IQ)(\d)', name)
    if m:
        return int(m.group(1))
    m = re.search(r'(?:^|[._-])(F16|FP16|F32|FP32)', name)
    if m:
        return 16
    # Default: assume 4-bit (most common download)
    return 4

def _auto_cache_type(model_path: str) -> tuple[int, int]:
    """Return (type_k, type_v) as ggml_type ints based on model quantization level.
    KV cache precision should never exceed model precision.
    ggml_type: F16=1, Q4_0=2, Q8_0=8"""
    bits = _detect_model_bits(model_path)
    if bits <= 4:
        return (2, 2)    # Q4 model → Q4_0 KV (max memory savings)
    elif bits <= 8:
        return (8, 8)    # Q5-Q8 model → Q8_0 KV (balanced)
    else:
        return (8, 8)    # F16+ model → Q8_0 KV

IS_MAC = platform.system() == "Darwin"
IS_WINDOWS = platform.system() == "Windows"
IS_APPLE_SILICON = IS_MAC and (platform.processor() == "arm" or "Apple" in platform.processor())


class LocalLLMEngine:
    """Singleton engine managing all local LLM state: backend, downloads, server process."""

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
        self._active_backend = ""  # The backend actually used to start the current model
        self.current_model_id = ""
        self.current_model_name = ""
        self.server_port = 1235
        self.server_status = "stopped"  # stopped | starting | running | error
        self.status_message = ""
        self.has_image_support = False

        # Clean up any orphan model server from previous session on startup.
        # (atexit can be skipped by SIGKILL, so startup cleanup is the safety net.)
        self._kill_port(self.server_port)

        # Register exit handler as belt-and-suspenders (once per process)
        if not LocalLLMEngine._atexit_registered:
            import atexit
            atexit.register(self._cleanup_child)
            LocalLLMEngine._atexit_registered = True
        self.model_token_limit = int(os.environ.get("LATIAO_CTX_LEN", "8192"))
        self.n_gpu_layers = int(os.environ.get("LATIAO_GPU_LAYERS", "-1"))

        # Download state
        self._download_lock = threading.Lock()
        self._download_state_file = MODELS_DIR / ".downloads.json"
        self._downloads: dict[str, dict] = {}
        self._download_procs: dict[str, subprocess.Popen] = {}
        self._download_threads: dict[str, threading.Thread] = {}
        self._cache_dir = Path.home() / ".cache" / "huggingface" / "hub"
        self._load_download_state()

        # Auto-detect fastest HuggingFace mirror (hf-mirror.com for China)
        self._hf_endpoint = os.environ.get("HF_ENDPOINT", "")
        if not self._hf_endpoint:
            self._hf_endpoint = self._detect_fastest_mirror()

    def _detect_fastest_mirror(self) -> str:
        """Test hf-mirror.com vs huggingface.co, pick the faster one."""
        import urllib.request
        mirrors = {
            "https://hf-mirror.com": 999,
            "https://huggingface.co": 999,
        }
        for url_base, _ in mirrors.items():
            try:
                url = f"{url_base}/api/models?search=gguf&limit=1&full=false"
                req = urllib.request.Request(url, headers={"User-Agent": "Latiao/1.0"})
                start = time.time()
                resp = urllib.request.urlopen(req, timeout=5, context=_ssl_ctx)
                resp.read(1024)
                elapsed = time.time() - start
                mirrors[url_base] = elapsed
            except Exception:
                mirrors[url_base] = 999
        fastest = min(mirrors, key=mirrors.get)
        if mirrors[fastest] < 900:
            logger.info(f"HF mirror: {fastest} ({mirrors[fastest]:.2f}s) vs hf-mirror: {mirrors['https://hf-mirror.com']:.2f}s")
            return fastest
        return "https://huggingface.co"

    # ── Backend info ──

    def get_backend(self) -> str:
        return self.backend

    def get_available_backends(self) -> list[str]:
        backends = []
        if self.mlx_available:
            backends.append("mlx")
        if self.llama_cpp_available:
            backends.append("llama-cpp")
        return backends or ["none"]

    # ── Download state persistence ──

    def _load_download_state(self):
        try:
            if self._download_state_file.exists():
                saved = json.loads(self._download_state_file.read_text())
                for k, v in saved.items():
                    if v.get("status") not in ("downloading", "paused"):
                        self._downloads[k] = v
                    else:
                        self._downloads[k] = {**v, "status": "paused", "message": "上次未完成的下载 (已暂停)"}
        except (OSError, json.JSONDecodeError, ValueError):
            logger.warning("Failed to load download state", exc_info=True)

    def _save_download_state(self):
        try:
            MODELS_DIR.mkdir(parents=True, exist_ok=True)
            self._download_state_file.write_text(json.dumps(self._downloads, indent=2, ensure_ascii=False))
        except (OSError, json.JSONDecodeError):
            logger.warning("Failed to save download state", exc_info=True)

    # ── Download worker ──

    def _download_worker(self, model_id: str):
        dl_info = self._downloads.get(model_id, {})
        dl_info["status"] = "downloading"
        dl_info["started_at"] = time.time()
        dl_info["downloaded_bytes"] = 0
        try:
            import urllib.request
            from concurrent.futures import ThreadPoolExecutor, as_completed
            import tempfile

            cache_root = str(self._cache_dir.parent)

            if model_id.endswith(".gguf") or model_id.endswith(".safetensors"):
                # Single file download via HF raw URL with multi-threaded chunked download
                parts = model_id.rsplit("/", 1)
                repo_id = parts[0] if len(parts) == 2 else model_id
                filename = parts[1] if len(parts) == 2 else model_id
                url = f"{self._hf_endpoint}/{repo_id}/resolve/main/{filename}"
                dest_dir = MODELS_DIR / repo_id.replace("/", "--")
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest_path = dest_dir / filename

                # Get file size and check if server supports Range requests
                req = urllib.request.Request(url, method="HEAD",
                    headers={"User-Agent": "Latiao/1.0"})
                resp = urllib.request.urlopen(req, timeout=15, context=_ssl_ctx)
                total_size = int(resp.getheader("Content-Length", 0))
                accepts_ranges = resp.getheader("Accept-Ranges") == "bytes"
                dl_info["total_bytes"] = total_size

                # Check if already fully downloaded
                if dest_path.exists() and dest_path.stat().st_size == total_size:
                    path = str(dest_dir)
                    dl_info.update({"status": "done", "progress": 100, "path": path, "message": "已下载"})
                    self._save_download_state()
                    return

                if accepts_ranges and total_size > 50 * 1024 * 1024:
                    # Multi-threaded download with real-time progress tracking
                    num_threads = min(6, max(2, total_size // (300 * 1024 * 1024)))  # 1 thread per 300MB, max 6
                    chunk_size = total_size // num_threads
                    dl_info["message"] = f"多线程下载 {filename} ({total_size/(1024**3):.1f}GB, {num_threads}线程)..."
                    self._save_download_state()

                    chunk_tmp_paths: list = [None] * num_threads
                    # Use a shared array for progress tracking (bytes written per thread)
                    progress_bytes = [0] * num_threads
                    progress_lock = threading.Lock()
                    progress_event = threading.Event()
                    last_update = time.time()
                    last_total = 0
                    download_error = [None]

                    def download_chunk(idx: int) -> None:
                        start = idx * chunk_size
                        end = start + chunk_size - 1 if idx < num_threads - 1 else total_size - 1
                        if start >= total_size:
                            progress_bytes[idx] = 0
                            return
                        headers = {"User-Agent": "Latiao/1.0", "Range": f"bytes={start}-{end}"}
                        for attempt in range(3):
                            try:
                                req2 = urllib.request.Request(url, headers=headers)
                                with urllib.request.urlopen(req2, timeout=120, context=_ssl_ctx) as resp2:
                                    tmpf = tempfile.NamedTemporaryFile(delete=False, suffix=".chunk")
                                    chunk_tmp_paths[idx] = tmpf.name
                                    downloaded = 0
                                    while True:
                                        if dl_info.get("status") in ("paused", "cancelled"):
                                            tmpf.close()
                                            os.unlink(tmpf.name)
                                            chunk_tmp_paths[idx] = None
                                            return
                                        data = resp2.read(512 * 1024)  # 512KB chunks
                                        if not data:
                                            break
                                        tmpf.write(data)
                                        downloaded += len(data)
                                        progress_bytes[idx] = downloaded
                                        progress_event.set()
                                    tmpf.close()
                                    return
                            except Exception as e:
                                if attempt == 2:
                                    download_error[0] = str(e)
                                    return
                                progress_bytes[idx] = 0
                                time.sleep(1)

                    with ThreadPoolExecutor(max_workers=num_threads) as executor:
                        futures = [executor.submit(download_chunk, i) for i in range(num_threads)]

                        # Real-time progress loop: poll every 0.8s
                        while any(not f.done() for f in futures):
                            if dl_info.get("status") in ("paused", "cancelled"):
                                executor.shutdown(wait=False, cancel_futures=True)
                                for p in chunk_tmp_paths:
                                    if p:
                                        try: os.unlink(p)
                                        except: pass
                                return
                            # Wait for progress update or timeout
                            progress_event.wait(0.8)
                            progress_event.clear()
                            total_downloaded = sum(progress_bytes)
                            now = time.time()
                            delta = now - last_update
                            if delta >= 0.5 and total_downloaded > 0:
                                dl_info["downloaded_bytes"] = total_downloaded
                                if total_size > 0:
                                    dl_info["progress"] = int(total_downloaded * 100 / total_size)
                                if delta > 0 and total_downloaded > last_total:
                                    dl_info["speed_bps"] = int((total_downloaded - last_total) / delta)
                                    if dl_info["speed_bps"] > 0:
                                        dl_info["eta_seconds"] = int((total_size - total_downloaded) / dl_info["speed_bps"])
                                dl_info["message"] = f"下载中 {filename} ({total_downloaded/(1024**2):.0f}MB / {total_size/(1024**3):.1f}GB) · {(dl_info.get('speed_bps') or 0)/(1024**2):.1f}MB/s"
                                last_update = now
                                last_total = total_downloaded
                                self._save_download_state()

                        # Collect results
                        for f in futures:
                            try: f.result()
                            except: pass

                    if download_error[0]:
                        raise Exception(download_error[0])

                    # Merge chunks in order
                    valid_chunks = [(i, p) for i, p in enumerate(chunk_tmp_paths) if p]
                    valid_chunks.sort(key=lambda x: x[0])
                    dl_info["message"] = f"合并分块 {filename}..."
                    self._save_download_state()
                    with open(dest_path, "wb") as out:
                        for _, cp in valid_chunks:
                            with open(cp, "rb") as inp:
                                while True:
                                    data = inp.read(8 * 1024 * 1024)
                                    if not data: break
                                    out.write(data)
                            os.unlink(cp)
                else:
                    # Single-threaded fallback for small files or servers without Range support
                    dl_info["message"] = f"正在下载 {filename} ({total_size/(1024**3):.1f}GB)..."
                    self._save_download_state()
                    headers = {"User-Agent": "Latiao/1.0"}
                    req2 = urllib.request.Request(url, headers=headers)
                    with urllib.request.urlopen(req2, timeout=60, context=_ssl_ctx) as resp2:
                        with open(dest_path, "wb") as f:
                            downloaded = 0
                            last_update = time.time()
                            last_bytes = 0
                            while True:
                                if dl_info.get("status") in ("paused", "cancelled"):
                                    return
                                chunk = resp2.read(1024 * 1024)
                                if not chunk: break
                                f.write(chunk)
                                downloaded += len(chunk)
                                now = time.time()
                                if now - last_update >= 1:
                                    dl_info["downloaded_bytes"] = downloaded
                                    if total_size > 0:
                                        dl_info["progress"] = int(downloaded * 100 / total_size)
                                    delta_t = now - last_update
                                    if delta_t > 0:
                                        dl_info["speed_bps"] = int((downloaded - last_bytes) / delta_t)
                                        if dl_info["speed_bps"] > 0 and total_size > 0:
                                            dl_info["eta_seconds"] = int((total_size - downloaded) / dl_info["speed_bps"])
                                    dl_info["message"] = f"下载中 {filename} ({downloaded/(1024**2):.0f}MB / {total_size/(1024**3):.1f}GB) · {dl_info['speed_bps']/(1024**2):.1f}MB/s"
                                    last_update = now
                                    last_bytes = downloaded
                                    self._save_download_state()

                path = str(dest_dir)
            else:
                # Full repo: use huggingface_hub for repo-level operations
                from huggingface_hub import snapshot_download

                dl_info["message"] = "正在准备下载模型仓库..."
                self._save_download_state()
                local_path = snapshot_download(
                    repo_id=model_id,
                    cache_dir=cache_root,
                    resume_download=True,
                    allow_patterns=["*.gguf", "*.safetensors", "*.json", "*.md", "*.txt"],
                )
                path = local_path

            dl_info["progress"] = 100
            dl_info.update({"status": "done", "path": path, "message": "下载完成"})
        except Exception as e:
            dl_info.update({"status": "error", "message": f"下载失败: {str(e)[:300]}"})
        self._save_download_state()

    # ── Download API ──

    def download_model(self, model_id: str) -> dict:
        if model_id in self._downloads:
            dl_info = self._downloads[model_id]
            if dl_info["status"] == "downloading":
                return {"status": "ok", "message": "已在下载中", "download": dl_info}
            if dl_info["status"] == "done":
                return {"status": "ok", "message": "已下载完成", "download": dl_info}

        model_dir = self._cache_dir / f"models--{model_id.replace('/', '--')}"
        if (model_dir / "snapshots").exists():
            snaps = list((model_dir / "snapshots").iterdir())
            for snap in snaps:
                files = list(snap.rglob("*"))
                model_files = [f for f in files if f.suffix in (".safetensors", ".gguf", ".bin", ".json")]
                if model_files:
                    path = str(snap)
                    self._downloads[model_id] = {"status": "done", "progress": 100, "path": path, "message": "已缓存", "model_id": model_id}
                    self._save_download_state()
                    return {"status": "ok", "model_id": model_id, "path": path, "message": "模型已缓存"}

        self._downloads[model_id] = {"status": "downloading", "progress": 0, "path": "", "message": "准备下载...",
                                      "model_id": model_id, "speed_bps": 0, "eta_seconds": 0, "downloaded_bytes": 0}
        t = threading.Thread(target=self._download_worker, args=(model_id,), daemon=True)
        self._download_threads[model_id] = t
        t.start()
        return {"status": "ok", "model_id": model_id, "message": "下载已启动", "download": self._downloads[model_id]}

    def pause_download(self, model_id: str) -> dict:
        dl_info = self._downloads.get(model_id)
        if not dl_info or dl_info["status"] != "downloading":
            return {"status": "error", "message": "没有正在下载的任务"}
        dl_info["status"] = "paused"
        dl_info["message"] = "已暂停（当前文件下载完成后生效）"
        self._save_download_state()
        return {"status": "ok", "download": dl_info}

    def resume_download(self, model_id: str) -> dict:
        dl_info = self._downloads.get(model_id)
        if not dl_info or dl_info["status"] != "paused":
            return {"status": "error", "message": "没有暂停的任务"}
        dl_info["status"] = "downloading"
        dl_info["message"] = "恢复下载..."
        t = threading.Thread(target=self._download_worker, args=(model_id,), daemon=True)
        self._download_threads[model_id] = t
        t.start()
        return {"status": "ok", "download": dl_info}

    def cancel_download(self, model_id: str) -> dict:
        dl_info = self._downloads.get(model_id)
        if not dl_info:
            return {"status": "error", "message": "未找到下载任务"}
        dl_info["status"] = "cancelled"
        dl_info["message"] = "已取消"
        self._save_download_state()
        return {"status": "ok", "download": dl_info}

    def get_all_downloads(self) -> dict:
        return {"status": "ok", "downloads": list(self._downloads.values())}

    def clear_downloads(self, status_filter: str = "") -> dict:
        if status_filter:
            self._downloads = {k: v for k, v in self._downloads.items() if v["status"] != status_filter}
        else:
            self._downloads = {k: v for k, v in self._downloads.items() if v["status"] in ("downloading", "paused")}
        self._save_download_state()
        return {"status": "ok", "downloads": list(self._downloads.values())}

    def get_download_progress(self, model_id: str) -> dict:
        with self._download_lock:
            if model_id in self._downloads:
                return dict(self._downloads[model_id])
        return {"status": "unknown", "progress": 0, "path": "", "message": "未找到下载记录"}

    def open_path(self, path: str) -> dict:
        try:
            if IS_MAC:
                subprocess.Popen(["open", path])
            elif IS_WINDOWS:
                os.startfile(path)
            else:
                subprocess.Popen(["xdg-open", path])
            return {"status": "ok", "message": f"已打开: {path}"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # ── Server status ──

    def get_status(self) -> dict:
        if self._process and self._process.poll() is not None:
            # Only overwrite if not already set to a detailed error by startup
            if self.server_status != "error":
                self.server_status = "stopped"
                self.status_message = f"进程已退出 (code: {self._process.returncode})"
            self.current_model_id = ""
            self.current_model_name = ""
        # If engine thinks we're stopped but a model server is still on our port
        # (e.g. sidecar restarted, model process outlived it), reconnect so the UI
        # shows accurate status without waiting for a chat request.
        if self.server_status == "stopped" and self._probe_port(self.server_port):
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
        if not self._process or self._process.poll() is not None:
            return False
        try:
            import urllib.request
            urllib.request.urlopen(f"http://127.0.0.1:{self.server_port}/v1/models", timeout=3)
            return True
        except Exception:
            return False

    def get_api_url(self) -> str:
        if self.is_running():
            return f"http://127.0.0.1:{self.server_port}/v1"
        # Engine was restarted — check if a model server is still running on our port
        # (e.g. sidecar was killed and restarted, but model process outlived it)
        if self._probe_port(self.server_port):
            self.server_status = "running"
            self.status_message = "(reconnected after sidecar restart)"
            if not self._active_backend:
                self._active_backend = self.backend  # best guess: platform default
            return f"http://127.0.0.1:{self.server_port}/v1"
        return ""

    @staticmethod
    def _probe_port(port: int, timeout: float = 5) -> bool:
        """Check if a model server is listening on this port.
        Uses TCP connect first (fast), then HTTP GET /v1/models as confirmation."""
        import socket
        import urllib.request
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

    # ── Start / Stop ──

    @staticmethod
    def _wait_for_http(port: int, timeout_sec: float = 120, process: subprocess.Popen | None = None) -> bool:
        """Poll the model server's /v1/models endpoint until it responds, times out, or the process dies."""
        import urllib.request
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            if process is not None and process.poll() is not None:
                return False  # Process died — caller will read stderr
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=3)
                return True
            except Exception:
                time.sleep(0.5)
        return False

    @staticmethod
    def _kill_port(port: int):
        """Kill any process listening on the given port. Works on macOS and Linux."""
        try:
            result = subprocess.run(
                ["lsof", "-ti", f":{port}"], capture_output=True, text=True, timeout=5,
            )
            for pid_str in result.stdout.strip().split("\n"):
                pid = pid_str.strip()
                if pid and pid.isdigit():
                    try:
                        os.kill(int(pid), 9)
                    except OSError:
                        pass
        except Exception:
            pass

    def _find_gguf(self, model_id: str) -> str | None:
        if model_id.endswith(".gguf") and Path(model_id).exists():
            return model_id
        # Strip .gguf suffix and repo prefix for fuzzy matching
        key = model_id.replace(".gguf", "").lower()
        # Also try just the filename part
        key_short = model_id.rsplit("/", 1)[-1].replace(".gguf", "").lower()
        # Search MODELS_DIR first (user-placed models)
        for f in MODELS_DIR.rglob("*.gguf"):
            stem = f.stem.lower()
            if key in stem or stem in key or key_short in stem:
                return str(f)
        # Also search download cache (~/.cache/huggingface/models/)
        cache_models = self._cache_dir.parent / "models"
        if cache_models.exists():
            for f in cache_models.rglob("*.gguf"):
                stem = f.stem.lower()
                if key in stem or stem in key or key_short in stem:
                    return str(f)
        return None

    @staticmethod
    def _guess_chat_format(model_path: str) -> str | None:
        """Guess llama-cpp chat format from model path for proper function calling.
        Covers tool-calling models: Hermes-2-Pro, Qwen2.5+, Functionary, Gemma, Llama3, Mistral."""
        lower = model_path.lower()
        if "hermes" in lower and ("pro" in lower or "2-pro" in lower):
            return "hermes-2-pro"
        if "functionary" in lower:
            return "functionary"
        if "qwen" in lower:
            if any(v in lower for v in ["2.5", "3.", "qwen3"]):
                return "qwen"  # Qwen 2.5+ has native tool calling
            return "qwen"
        if "gemma" in lower:
            # Gemma models have proper chat templates in GGUF metadata.
            # Don't override with llama.cpp's default Gemma format
            # (which doesn't support system role).
            return None
        if "phi-4" in lower or "phi4" in lower:
            return "phi"
        if "llama-4" in lower or "llama4" in lower:
            return "llama3"  # Llama 4 supports tool calling
        if "llama-3.2" in lower or "llama-3.1" in lower or "llama-3" in lower:
            return "llama3"
        if "llama" in lower:
            return "llama3"  # Default for newer Llamas
        if "mistral" in lower or "mixtral" in lower:
            return "mistral-instruct"
        if "command-r" in lower or "c4ai" in lower:
            return "command-r"
        if "deepseek" in lower and "v3" in lower:
            return "deepseek"
        return None

    def _start_llama_cpp(self, model_id: str, port: int) -> dict:
        model_path = self._find_gguf(model_id)
        if not model_path:
            self.server_status = "error"
            self.status_message = f"找不到 GGUF 模型: {model_id}。请先下载 .gguf 文件到 ~/Models/"
            self.current_model_id = ""
            self.current_model_name = ""
            return self.get_status()

        self.current_model_id = model_id
        self.current_model_name = Path(model_path).stem
        self.server_status = "starting"
        self.status_message = f"正在加载 {self.current_model_name}..."

        try:
            cmd = [
                sys.executable, "-m", "llama_cpp.server",
                "--model", model_path,
                "--port", str(port),
                "--host", "127.0.0.1",
                "--n_ctx", str(self.model_token_limit),
                "--n_gpu_layers", str(self.n_gpu_layers),
            ]
            # ── Auto-select KV cache quant based on model quant ──
            # Q4 model → Q4_0 KV; Q5+ model → Q8_0 KV
            kv_k, kv_v = _auto_cache_type(model_path)
            cmd += ["--type_k", str(kv_k), "--type_v", str(kv_v)]
            cmd += ["--flash_attn", "1"]
            # Enable function calling via chat format for models that support it
            chat_fmt = self._guess_chat_format(model_path)
            if chat_fmt:
                cmd += ["--chat_format", chat_fmt]
            env = os.environ.copy()
            env.pop("HF_ENDPOINT", None)
            self._process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env
            )

            # Drain stderr in background thread so pipe buffer never blocks the child
            stderr_lines: list[str] = []
            def _drain():
                if self._process and self._process.stderr:
                    try:
                        for line in self._process.stderr:
                            stderr_lines.append(line)
                    except Exception:
                        pass
            t = threading.Thread(target=_drain, daemon=True)
            t.start()

            # Poll HTTP immediately — returns as soon as model is ready (no dead-wait)
            if not self._wait_for_http(port, timeout_sec=300, process=self._process):
                t.join(timeout=1)
                err_lines = stderr_lines[-50:] if stderr_lines else []
                err_text = "".join(err_lines)
                # Extract only the last meaningful error line (skip traceback clutter)
                err_summary = ""
                for line in reversed(err_lines):
                    stripped = line.strip()
                    if stripped and ("Error" in stripped or "error" in stripped.lower() or "ValueError" in stripped or "does not exist" in stripped.lower() or "No such file" in stripped):
                        err_summary = stripped[-150:]
                        break
                if not err_summary and err_lines:
                    err_summary = err_lines[-1].strip()[-150:]
                logger.error(
                    "llama-cpp server %s: %s",
                    "exited early" if self._process.poll() is not None else "HTTP timeout",
                    err_text[:500] if err_text else "no stderr",
                )
                self.stop_model()
                self.server_status = "error"
                self.status_message = f"启动失败: {err_summary}" if err_summary else "模型加载超时或进程已退出"
                self.current_model_id = ""
                self.current_model_name = ""
                return self.get_status()

            self.server_status = "running"
            self.status_message = f"{self.current_model_name} 运行中"
            self._active_backend = "llama-cpp"
            return self.get_status()
        except Exception as e:
            logger.error("Failed to start llama-cpp server: %s", e)
            self.server_status = "error"
            self.status_message = str(e)[:200]
            return self.get_status()

    def _start_mlx(self, model_id: str, port: int) -> dict:
        self.current_model_id = model_id
        self.current_model_name = model_id.split("/")[-1] if "/" in model_id else model_id
        self.server_status = "starting"
        self.status_message = f"正在加载 {self.current_model_name} (首次需下载)..."

        try:
            cmd = [
                sys.executable, "-m", "mlx_lm.server",
                "--model", model_id,
                "--port", str(port),
                "--host", "127.0.0.1",
            ]
            env = os.environ.copy()
            env.pop("HF_ENDPOINT", None)
            self._process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env
            )

            # Drain stderr in background thread so pipe buffer never blocks the child
            stderr_lines: list[str] = []
            def _drain():
                if self._process and self._process.stderr:
                    try:
                        for line in self._process.stderr:
                            stderr_lines.append(line)
                    except Exception:
                        pass
            t = threading.Thread(target=_drain, daemon=True)
            t.start()

            # Poll HTTP immediately — returns as soon as model is ready (no dead-wait)
            if not self._wait_for_http(port, timeout_sec=300, process=self._process):
                t.join(timeout=1)
                err_lines = stderr_lines[-50:] if stderr_lines else []
                err_text = "".join(err_lines)
                err_summary = ""
                for line in reversed(err_lines):
                    stripped = line.strip()
                    if stripped and ("Error" in stripped or "error" in stripped.lower() or "ValueError" in stripped or "does not exist" in stripped.lower()):
                        err_summary = stripped[-150:]
                        break
                if not err_summary and err_lines:
                    err_summary = err_lines[-1].strip()[-150:]
                logger.error(
                    "MLX server %s: %s",
                    "exited early" if self._process.poll() is not None else "HTTP timeout",
                    err_text[:500] if err_text else "no stderr",
                )
                self.stop_model()
                self.server_status = "error"
                self.status_message = f"启动失败: {err_summary}" if err_summary else "模型加载超时或进程已退出"
                self.current_model_id = ""
                self.current_model_name = ""
                return self.get_status()

            self.server_status = "running"
            self.status_message = f"{self.current_model_name} 运行中 (MLX)"
            self.has_image_support = "vision" in model_id.lower() or "llama-4" in model_id.lower()
            self._active_backend = "mlx"
            return self.get_status()
        except Exception as e:
            logger.error("Failed to start MLX server: %s", e)
            self.server_status = "error"
            self.status_message = str(e)[:200]
            return self.get_status()

    def start_model(self, model_id: str, port: int = 1235) -> dict:
        # Kill any stale process on the target port before starting
        self._kill_port(port)
        if self._process and self._process.poll() is None:
            self.stop_model()
        self.server_port = port

        if self.backend == "none":
            return {"status": "error", "message": "无可用引擎。安装: pip install llama-cpp-python"}

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

    def stop_model(self) -> dict:
        if self._process:
            try:
                self._process.terminate()
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
            except Exception:
                pass
            self._process = None
        # Belt-and-suspenders: ensure port is actually free
        self._kill_port(self.server_port)
        self.server_status = "stopped"
        self.status_message = "已停止"
        self.current_model_id = ""
        self.current_model_name = ""
        self._active_backend = ""
        self.has_image_support = False
        return self.get_status()

    def delete_model_file(self, model_id: str) -> dict:
        """Delete a local model GGUF file by model_id and clear download record."""
        path = self._find_gguf(model_id)
        if not path:
            return {"status": "error", "message": f"找不到模型文件: {model_id}"}
        try:
            os.unlink(path)
            logger.info(f"Deleted model file: {path}")
            # Also remove download record
            with self._download_lock:
                self._downloads.pop(model_id, None)
                self._save_download_state()
            return {"status": "ok", "message": f"已删除: {Path(path).name}"}
        except Exception as e:
            return {"status": "error", "message": f"删除失败: {str(e)}"}


# ── Singleton instance ──
_engine = LocalLLMEngine()


# ═══════════════════════════════════════════════════════
#  Module-level API (delegates to singleton — backward compatible)
# ═══════════════════════════════════════════════════════

def get_backend() -> str:
    return _engine.get_backend()

def get_available_backends() -> list[str]:
    return _engine.get_available_backends()

def detect_system() -> dict:
    """Auto-detect hardware and recommend optimal config."""
    info = {
        "os": platform.system(),
        "os_version": platform.version(),
        "arch": platform.machine(),
        "cpu": platform.processor() or "Unknown",
        "cpu_cores": os.cpu_count(),
        "python": sys.version.split()[0],
    }

    # RAM
    try:
        import psutil
        mem = psutil.virtual_memory()
        info["ram_total_gb"] = round(mem.total / (1024**3), 1)
        info["ram_available_gb"] = round(mem.available / (1024**3), 1)
    except (ImportError, RuntimeError):
        info["ram_total_gb"] = "unknown (pip install psutil)"

    # GPU detection
    gpu_info = {"type": "none", "name": "未知"}
    if IS_APPLE_SILICON:
        try:
            proc_result = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True)
            chip = proc_result.stdout.strip()
            gpu_info = {"type": "apple_silicon", "name": chip, "metal": True}
            if isinstance(info.get("ram_total_gb"), (int, float)):
                usable = int(info["ram_total_gb"] * 0.6)
                gpu_info["vram_usable_gb"] = usable
        except Exception:
            pass
    elif IS_WINDOWS:
        gpu_info["type"] = "discrete_windows"
        try:
            import torch
            if torch.cuda.is_available():
                gpu_info["type"] = "cuda"
                gpu_info["name"] = torch.cuda.get_device_name(0)
                gpu_info["vram_gb"] = round(torch.cuda.get_device_properties(0).total_memory / (1024**3), 1)
        except (ImportError, RuntimeError):
            pass
    else:
        try:
            import torch
            if torch.cuda.is_available():
                gpu_info["type"] = "cuda"
                gpu_info["name"] = torch.cuda.get_device_name(0)
                gpu_info["vram_gb"] = round(torch.cuda.get_device_properties(0).total_memory / (1024**3), 1)
        except (ImportError, RuntimeError):
            pass

    info["gpu"] = gpu_info

    rec = {}
    if IS_APPLE_SILICON:
        rec["backend"] = "mlx" if _engine.mlx_available else "llama-cpp"
        rec["gpu_layers"] = -1
        if isinstance(gpu_info.get("vram_usable_gb"), (int, float)):
            v = gpu_info["vram_usable_gb"]
            if v >= 32:
                rec["recommended_tier"] = "旗舰"
            elif v >= 16:
                rec["recommended_tier"] = "推荐"
            else:
                rec["recommended_tier"] = "入门"
    else:
        rec["backend"] = "llama-cpp"
        rec["gpu_layers"] = -1 if gpu_info.get("type") in ("cuda",) else 0
        rec["recommended_tier"] = "推荐" if gpu_info.get("type") == "cuda" else "入门"

    rec["available_backends"] = _engine.get_available_backends()
    info["recommendation"] = rec
    return info

def check_setup() -> dict:
    """Check system environment and report what needs to be installed."""
    issues = []
    ok = []

    if _engine.mlx_available:
        ok.append({"item": "MLX 引擎 (Apple Silicon)", "status": "ok"})
    elif IS_APPLE_SILICON:
        issues.append({"item": "MLX 引擎", "status": "missing", "fix": "pip3 install mlx-lm", "fix_type": "pip", "fix_pkg": "mlx-lm"})

    if _engine.llama_cpp_available:
        ok.append({"item": "llama-cpp 引擎 (跨平台)", "status": "ok"})
    else:
        issues.append({"item": "llama-cpp 引擎", "status": "missing", "fix": "pip3 install llama-cpp-python", "fix_type": "pip", "fix_pkg": "llama-cpp-python"})

    py_ver = sys.version_info
    if py_ver >= (3, 10):
        ok.append({"item": f"Python {py_ver.major}.{py_ver.minor}", "status": "ok"})
    else:
        issues.append({"item": f"Python {py_ver.major}.{py_ver.minor} (建议 ≥3.10)", "status": "warning",
                        "fix": "brew install python@3.12", "fix_type": "command"})

    try:
        import psutil
        ram = psutil.virtual_memory().total / (1024**3)
        if ram >= 16:
            ok.append({"item": f"内存 {ram:.0f}GB", "status": "ok"})
        else:
            issues.append({"item": f"内存 {ram:.0f}GB (建议 ≥16GB)", "status": "warning", "fix": "小模型 (≤3B) 仍可运行"})
    except (ImportError, RuntimeError):
        pass

    try:
        import shutil
        free = shutil.disk_usage(MODELS_DIR).free / (1024**3)
        if free >= 20:
            ok.append({"item": f"可用磁盘 {free:.0f}GB", "status": "ok"})
        else:
            issues.append({"item": f"可用磁盘 {free:.0f}GB (建议 ≥20GB)", "status": "warning", "fix": "清理磁盘空间"})
    except Exception:
        pass

    return {
        "ready": len(issues) == 0 or all(i["status"] == "warning" for i in issues),
        "ok": ok,
        "issues": issues,
        "backend": _engine.backend,
        "available": _engine.get_available_backends(),
        "system": detect_system(),
    }

def search_huggingface(query: str, limit: int = 10, library: str = "") -> list[dict]:
    """Search huggingface for models. Uses HF_ENDPOINT mirror if configured."""
    try:
        import urllib.parse
        import urllib.request
        params = {"search": query, "limit": limit, "sort": "downloads", "direction": "-1", "full": "true"}
        if library:
            params["library"] = library
        base = _engine._hf_endpoint.rstrip("/")
        url = base + "/api/models?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "Latiao/1.0"})
        resp = urllib.request.urlopen(req, timeout=15, context=_ssl_ctx)
        data = json.loads(resp.read())
        results = []
        for m in data:
            results.append({
                "id": m.get("id", ""),
                "author": m.get("author", ""),
                "downloads": m.get("downloads", 0),
                "likes": m.get("likes", 0),
                "tags": m.get("tags", []),
                "pipeline_tag": m.get("pipeline_tag", ""),
                "last_modified": m.get("lastModified", ""),
            })
        return results
    except Exception:
        return []

def get_model_detail(model_id: str) -> dict:
    """Fetch model detail from HuggingFace: metadata, file siblings, README."""
    import urllib.request
    try:
        # Fetch model info (respects HF_ENDPOINT mirror)
        # ?blobs=true resolves LFS pointers to get real file sizes
        base = _engine._hf_endpoint.rstrip("/")
        url = f"{base}/api/models/{model_id}?blobs=true"
        req = urllib.request.Request(url, headers={"User-Agent": "Latiao/1.0"})
        resp = urllib.request.urlopen(req, timeout=15, context=_ssl_ctx)
        data = json.loads(resp.read())

        # Get REAL file sizes via huggingface_hub (resolves LFS pointers)
        siblings = []
        try:
            from huggingface_hub import repo_info
            hf_info = repo_info(model_id, files_metadata=True)
            quant_keys = ["Q2_K","Q3_K_S","Q3_K_M","Q3_K_L","Q4_0","Q4_K_S","Q4_K_M",
                          "Q5_0","Q5_K_S","Q5_K_M","Q6_K","Q8_0","F16","IQ","fp16","MXFP4"]
            for sib in hf_info.siblings:
                fname = sib.rfilename or ""
                if fname.endswith(".gguf") or fname.endswith(".safetensors") or fname.endswith(".bin"):
                    size_bytes = sib.size or 0
                    size_str = f"{size_bytes / (1024**3):.1f} GB" if size_bytes > 0 else ""
                    quant = next((q for q in quant_keys if q in fname), "")
                    siblings.append({"filename": fname, "size": size_str, "size_bytes": size_bytes, "quant": quant})
        except Exception as e:
            logger.error("Failed to get file sizes via huggingface_hub for %s: %s", model_id, e)
            # Fallback to REST API siblings (blobs=true already resolved LFS sizes)
            for sib in data.get("siblings", []):
                fname = sib.get("rfilename", "")
                if fname.endswith(".gguf") or fname.endswith(".safetensors") or fname.endswith(".bin"):
                    size_bytes = sib.get("size", 0)
                    size_str = f"{size_bytes / (1024**3):.1f} GB" if size_bytes > 1024**3 else f"{size_bytes / (1024**2):.0f} MB" if size_bytes > 0 else ""
                    quant = ""
                    for q in ["Q2_K","Q3_K_S","Q3_K_M","Q3_K_L","Q4_0","Q4_K_S","Q4_K_M",
                              "Q5_0","Q5_K_S","Q5_K_M","Q6_K","Q8_0","F16","IQ","fp16"]:
                        if q in fname: quant = q; break
                    siblings.append({"filename": fname, "size": size_str, "size_bytes": size_bytes, "quant": quant})

        # Readme excerpt
        readme = ""
        try:
            readme_url = f"{base}/{model_id}/raw/main/README.md"
            readme_req = urllib.request.Request(readme_url, headers={"User-Agent": "Latiao/1.0"})
            readme_resp = urllib.request.urlopen(readme_req, timeout=10)
            readme_raw = readme_resp.read().decode("utf-8", errors="replace")
            readme = readme_raw[:3000]  # First 3000 chars
        except Exception:
            pass

        return {
            "status": "ok",
            "id": data.get("id", model_id),
            "author": data.get("author", ""),
            "downloads": data.get("downloads", 0),
            "likes": data.get("likes", 0),
            "tags": data.get("tags", []),
            "pipeline_tag": data.get("pipeline_tag", ""),
            "last_modified": data.get("lastModified", ""),
            "siblings": siblings,
            "readme": readme,
            "card_data": data.get("cardData", {}),
            "private": data.get("private", False),
        }
    except Exception:
        return {"status": "error", "message": "Failed to fetch model details"}

def run_fix(fix_type: str, fix_pkg: str = "") -> dict:
    """Execute a fix for an environment issue."""
    if fix_type == "pip" and fix_pkg:
        try:
            proc_result = subprocess.run(
                [sys.executable, "-m", "pip", "install", fix_pkg],
                capture_output=True, text=True, timeout=120
            )
            if proc_result.returncode == 0:
                if fix_pkg == "mlx-lm":
                    try:
                        import mlx_lm  # noqa: F401
                        _engine.mlx_available = True
                    except (ImportError, RuntimeError):
                        pass
                elif fix_pkg == "llama-cpp-python":
                    try:
                        import llama_cpp  # noqa: F401
                        _engine.llama_cpp_available = True
                    except (ImportError, RuntimeError):
                        pass
                return {"status": "ok", "output": proc_result.stdout[-500:]}
            return {"status": "error", "output": proc_result.stderr[-500:]}
        except subprocess.TimeoutExpired:
            return {"status": "error", "message": "安装超时"}
        except Exception as e:
            return {"status": "error", "message": str(e)}
    if fix_type == "command":
        return {"status": "info", "message": "请在终端手动执行此命令"}
    return {"status": "error", "message": "未知的修复类型"}

def download_model(model_id: str) -> dict:
    return _engine.download_model(model_id)

def pause_download(model_id: str) -> dict:
    return _engine.pause_download(model_id)

def resume_download(model_id: str) -> dict:
    return _engine.resume_download(model_id)

def cancel_download(model_id: str) -> dict:
    return _engine.cancel_download(model_id)

def get_all_downloads() -> dict:
    return _engine.get_all_downloads()

def clear_downloads(status_filter: str = "") -> dict:
    return _engine.clear_downloads(status_filter)

def get_download_progress(model_id: str) -> dict:
    return _engine.get_download_progress(model_id)

def open_path(path: str) -> dict:
    return _engine.open_path(path)

def estimate_max_context(model_path: str = "") -> dict:
    """Estimate the maximum safe context length based on available memory.
    Returns recommended and max context lengths, plus memory breakdown."""
    # Get available memory
    avail_gb = 8.0  # conservative default
    total_gb = 16.0
    try:
        import psutil
        mem = psutil.virtual_memory()
        avail_gb = mem.available / (1024**3)
        total_gb = mem.total / (1024**3)
    except (ImportError, RuntimeError):
        if IS_MAC:
            try:
                # macOS fallback using sysctl
                result = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True)
                total_gb = int(result.stdout.strip()) / (1024**3)
                # Estimate available from memory pressure
                result2 = subprocess.run(["sysctl", "-n", "vm.page_free_count"], capture_output=True, text=True)
                pages_free = int(result2.stdout.strip()) * 16384  # page size
                avail_gb = min(pages_free / (1024**3), total_gb * 0.7)
            except Exception:
                pass

    # Model weight size (estimate from file or use default)
    model_size_gb = 7.0  # default for ~7B Q4 model
    if model_path and Path(model_path).exists() and model_path != ".":
        p = Path(model_path)
        if p.is_file():
            model_size_gb = p.stat().st_size / (1024**3)
        elif p.is_dir() and (p / "config.json").exists():
            # MLX or HF model directory with config.json
            total = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
            model_size_gb = total / (1024**3)
        elif p.suffix == ".gguf":
            model_size_gb = p.stat().st_size / (1024**3)

    # KV cache estimation: ~0.064 bytes per token per parameter for 12B models
    # Conservative: ~2.3 GB per 8K context for a ~12B model
    kv_cache_per_8k_gb = 2.3
    # Scale by actual model size
    kv_cache_per_8k_gb = kv_cache_per_8k_gb * (model_size_gb / 7.0)

    # Safety margin for system + other processes
    system_overhead_gb = 4.0

    # available memory already excludes model weights (if loaded).
    # Only subtract overhead, not model_size_gb (avoids double-counting).
    memory_for_context = max(0, avail_gb - system_overhead_gb)

    # Max context calculation
    max_ctx = int(memory_for_context / (kv_cache_per_8k_gb / 8192))
    # Snap to practical limits
    max_ctx = min(max_ctx, 131072)  # Most models top out at 128K
    max_ctx = max(max_ctx, 2048)    # Minimum usable context

    # Recommended: 70% of max for safety headroom, snapped to nearest common value
    target = int(max_ctx * 0.7)
    recommended = 2048
    common_values = [2048, 4096, 8192, 16384, 32768, 65536, 98304, 131072]
    for cv in common_values:
        if cv <= target:
            recommended = cv

    return {
        "ram_total_gb": round(total_gb, 1),
        "ram_available_gb": round(avail_gb, 1),
        "model_size_gb": round(model_size_gb, 1),
        "kv_cache_per_8k_gb": round(kv_cache_per_8k_gb, 1),
        "memory_for_context_gb": round(memory_for_context, 1),
        "max_context": max_ctx,
        "recommended_context": recommended,
        "current_context": _engine.model_token_limit,
    }

def set_context_limit(new_limit: int) -> dict:
    """Set the model context limit at runtime (only applies to next model start)."""
    if not isinstance(new_limit, int) or new_limit < 512:
        return {"status": "error", "message": "Context must be at least 512"}
    _engine.model_token_limit = new_limit
    return {"status": "ok", "context_limit": new_limit, "message": f"上下文已设置为 {new_limit}（重启模型后生效）"}

def get_status() -> dict:
    return _engine.get_status()

def list_local_models() -> list[dict]:
    """Scan for GGUF and MLX model files locally."""
    models = []
    if not MODELS_DIR.exists():
        return models
    for f in sorted(MODELS_DIR.rglob("*")):
        if f.is_file() and f.suffix in (".gguf", ".mlx"):
            size_gb = f.stat().st_size / (1024**3)
            models.append({
                "id": f.stem, "name": f.stem, "path": str(f),
                "size": f"{size_gb:.1f}GB", "format": f.suffix[1:],
            })
    hf = Path.home() / ".cache" / "huggingface" / "hub"
    if hf.exists():
        for dl_info in hf.glob("models--*"):
            snaps = dl_info / "snapshots"
            if snaps.exists():
                mid = dl_info.name.replace("models--", "").replace("--", "/")
                if not any(m["id"] == mid for m in models):
                    models.append({
                        "id": mid, "name": mid.split("/")[-1],
                        "path": str(dl_info), "size": "cached", "format": "mlx",
                    })
    return models

def get_recommended_models() -> list[dict]:
    """Return a curated list of recommended models based on available backends."""
    recommended = []
    if IS_APPLE_SILICON and _engine.mlx_available:
        recommended += [
            {"id": "mlx-community/Qwen3-8B-4bit", "name": "Qwen3 8B (MLX)", "size": "~5GB", "tier": "入门", "pipeline": "text-generation"},
            {"id": "mlx-community/Qwen3-14B-4bit", "name": "Qwen3 14B (MLX)", "size": "~8GB", "tier": "推荐", "pipeline": "text-generation"},
            {"id": "mlx-community/Qwen3-32B-4bit", "name": "Qwen3 32B (MLX)", "size": "~18GB", "tier": "旗舰", "pipeline": "text-generation"},
            {"id": "mlx-community/Llama-4-Scout-4bit", "name": "Llama 4 Scout (MLX)", "size": "~10GB", "tier": "推荐", "pipeline": "text-generation"},
            {"id": "mlx-community/DeepSeek-R1-Distill-Qwen-7B-4bit", "name": "DeepSeek R1 7B (MLX)", "size": "~4GB", "tier": "入门", "pipeline": "text-generation"},
        ]
    else:
        recommended += [
            {"id": "Qwen/Qwen3-8B", "name": "Qwen3 8B (GGUF)", "size": "~5GB", "tier": "入门", "pipeline": "text-generation"},
            {"id": "bartowski/Qwen3-14B-GGUF", "name": "Qwen3 14B (GGUF)", "size": "~9GB", "tier": "推荐", "pipeline": "text-generation"},
            {"id": "bartowski/Llama-4-Scout-GGUF", "name": "Llama 4 Scout (GGUF)", "size": "~10GB", "tier": "推荐", "pipeline": "text-generation"},
        ]
    # Mark download status for each model
    downloads = _engine._downloads
    for m in recommended:
        dl = downloads.get(m["id"])
        m["download_status"] = dl["status"] if dl else "none"
    return recommended

def start_model(model_id: str, port: int = 1235) -> dict:
    return _engine.start_model(model_id, port)

def stop_model() -> dict:
    return _engine.stop_model()

def delete_model_file(model_id: str) -> dict:
    return _engine.delete_model_file(model_id)


def is_running() -> bool:
    return _engine.is_running()

def get_api_url() -> str:
    return _engine.get_api_url()
