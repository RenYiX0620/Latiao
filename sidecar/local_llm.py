"""
Latiao Local LLM Engine — Cross-Platform (Mac / Windows / Linux)

Auto-detects best backend:
  - Apple Silicon + mlx-lm → MLX (fastest)
  - Fallback → llama-cpp-python (cross-platform, GPU accel via Metal/CUDA/Vulkan)
"""

from __future__ import annotations

import json
import os
import platform
import re as _re
import subprocess
import sys
import threading
import time
from pathlib import Path

MODELS_DIR = Path.home() / "Models"
MODELS_DIR.mkdir(parents=True, exist_ok=True)

IS_MAC = platform.system() == "Darwin"
IS_WINDOWS = platform.system() == "Windows"
IS_APPLE_SILICON = IS_MAC and (platform.processor() == "arm" or "Apple" in platform.processor())


class LocalLLMEngine:
    """Singleton engine managing all local LLM state: backend, downloads, server process."""

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
        except ImportError:
            pass

        try:
            import llama_cpp  # noqa: F401
            self.llama_cpp_available = True
            if not self.mlx_available:
                self.backend = "llama-cpp"
        except ImportError:
            pass

        if not self.mlx_available and not self.llama_cpp_available:
            self.backend = "none"

        # Runtime server state
        self._process: subprocess.Popen | None = None
        self.current_model_id = ""
        self.current_model_name = ""
        self.server_port = 1235
        self.server_status = "stopped"  # stopped | starting | running | error
        self.status_message = ""
        self.has_image_support = False
        self.model_token_limit = 32768
        self.n_gpu_layers = -1  # -1 = auto

        # Download state
        self._download_state_file = MODELS_DIR / ".downloads.json"
        self._downloads: dict[str, dict] = {}
        self._download_procs: dict[str, subprocess.Popen] = {}
        self._download_threads: dict[str, threading.Thread] = {}
        self._cache_dir = Path.home() / ".cache" / "huggingface" / "hub"
        self._load_download_state()

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
        except Exception:
            pass

    def _save_download_state(self):
        try:
            MODELS_DIR.mkdir(parents=True, exist_ok=True)
            self._download_state_file.write_text(json.dumps(self._downloads, indent=2, ensure_ascii=False))
        except Exception:
            pass

    # ── Download worker ──

    def _download_worker(self, model_id: str):
        dl_info = self._downloads.get(model_id, {})
        dl_info["status"] = "downloading"
        dl_info["started_at"] = time.time()
        dl_info["downloaded_bytes"] = 0
        try:
            import shutil as _shutil  # noqa: F401
            cache_root = str(self._cache_dir.parent)
            cmd = [
                sys.executable, "-m", "huggingface_hub.commands.download", model_id,
                "--cache-dir", cache_root, "--resume-download",
            ]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self._download_procs[model_id] = proc

            for line in proc.stderr:
                if dl_info.get("status") == "paused":
                    proc.terminate()
                    proc.wait()
                    return
                line_s = line.strip()
                dl_info["message"] = line_s[:120]
                m = _re.search(r'(\d+)%', line_s)
                if m:
                    pct = int(m.group(1))
                    now = time.time()
                    elapsed = now - dl_info.get("started_at", now)
                    dl_info["progress"] = pct
                    if elapsed > 0 and pct > 0:
                        estimated_total = dl_info.get("total_bytes", 5 * 1024**3)
                        current_bytes = int(estimated_total * pct / 100)
                        dl_info["downloaded_bytes"] = current_bytes
                        delta_t = now - dl_info.get("_last_ts", now)
                        if delta_t > 0:
                            dl_info["speed_bps"] = int((current_bytes - dl_info.get("_prev_bytes", 0)) / delta_t)
                        dl_info["_prev_bytes"] = current_bytes
                        dl_info["_last_ts"] = now
                        if dl_info.get("speed_bps", 0) > 0:
                            remaining = estimated_total - current_bytes
                            dl_info["eta_seconds"] = int(remaining / dl_info["speed_bps"])
                    self._save_download_state()
                m2 = _re.search(r'(\d+\.?\d*)\s*(GB|MB|KB|bytes)', line_s, _re.IGNORECASE)
                if m2:
                    size_val = float(m2.group(1))
                    unit = m2.group(2).upper()
                    multipliers = {"GB": 1024**3, "MB": 1024**2, "KB": 1024, "BYTES": 1}
                    dl_info["total_bytes"] = int(size_val * multipliers.get(unit, 1))

            proc.wait()
            self._download_procs.pop(model_id, None)
            if dl_info.get("status") == "paused":
                return
            if proc.returncode == 0:
                model_dir = self._cache_dir / f"models--{model_id.replace('/', '--')}"
                snaps = model_dir / "snapshots"
                path = str(snaps) if snaps.exists() else str(model_dir)
                dl_info.update({"status": "done", "progress": 100, "path": path, "message": "下载完成"})
            else:
                dl_info.update({"status": "error", "progress": dl_info.get("progress", 0), "message": f"退出码: {proc.returncode}"})
        except Exception as e:
            dl_info.update({"status": "error", "message": str(e)[:300]})
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
        dl_info["message"] = "已暂停"
        proc = self._download_procs.get(model_id)
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        self._download_procs.pop(model_id, None)
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
        proc = self._download_procs.get(model_id)
        if proc:
            try:
                proc.kill()
                proc.wait()
            except Exception:
                pass
        self._download_procs.pop(model_id, None)
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
        if model_id in self._downloads:
            return self._downloads[model_id]
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
            self.server_status = "stopped"
            self.status_message = f"进程已退出 (code: {self._process.returncode})"
            self.current_model_id = ""
            self.current_model_name = ""
        return {
            "backend": self.backend,
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
        return ""

    # ── Start / Stop ──

    def _find_gguf(self, model_id: str) -> str | None:
        if model_id.endswith(".gguf") and Path(model_id).exists():
            return model_id
        for f in MODELS_DIR.rglob("*.gguf"):
            if model_id.lower() in f.stem.lower():
                return str(f)
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
            env = os.environ.copy()
            env.pop("HF_ENDPOINT", None)
            self._process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env
            )
            # Poll for startup — large models can take 30s+ to load on first run
            for _ in range(300):
                time.sleep(0.2)
                if self._process.poll() is not None:
                    break  # Process exited early — check for errors below
            if self._process.poll() is not None:
                err = self._process.stderr.read() if self._process.stderr else ""
                self.server_status = "error"
                self.status_message = f"启动失败: {err[:200]}"
                self.current_model_id = ""
                self.current_model_name = ""
                return self.get_status()

            self.server_status = "running"
            self.status_message = f"{self.current_model_name} 运行中"
            return self.get_status()
        except Exception as e:
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
            # Poll for startup — large models can take 30s+ to load on first run
            for _ in range(300):
                time.sleep(0.2)
                if self._process.poll() is not None:
                    break  # Process exited early — check for errors below
            if self._process.poll() is not None:
                err = self._process.stderr.read() if self._process.stderr else ""
                out = self._process.stdout.read() if self._process.stdout else ""
                self.server_status = "error"
                self.status_message = f"启动失败: {err[:300] or out[:300]}"
                self.current_model_id = ""
                self.current_model_name = ""
                return self.get_status()

            self.server_status = "running"
            self.status_message = f"{self.current_model_name} 运行中 (MLX)"
            self.has_image_support = "vision" in model_id.lower() or "llama-4" in model_id.lower()
            return self.get_status()
        except Exception as e:
            self.server_status = "error"
            self.status_message = str(e)[:200]
            return self.get_status()

    def start_model(self, model_id: str, port: int = 1235) -> dict:
        if self._process and self._process.poll() is None:
            self.stop_model()
        self.server_port = port

        if self.backend == "none":
            return {"status": "error", "message": "无可用引擎。安装: pip install llama-cpp-python"}

        if self.backend == "mlx" and self.mlx_available:
            return self._start_mlx(model_id, port)
        else:
            return self._start_llama_cpp(model_id, port)

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
        self.server_status = "stopped"
        self.status_message = "已停止"
        self.current_model_id = ""
        self.current_model_name = ""
        self.has_image_support = False
        return self.get_status()


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
    except ImportError:
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
        except ImportError:
            pass
    else:
        try:
            import torch
            if torch.cuda.is_available():
                gpu_info["type"] = "cuda"
                gpu_info["name"] = torch.cuda.get_device_name(0)
                gpu_info["vram_gb"] = round(torch.cuda.get_device_properties(0).total_memory / (1024**3), 1)
        except ImportError:
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
    except ImportError:
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
    """Search huggingface for models."""
    try:
        import urllib.parse
        import urllib.request
        params = {"search": query, "limit": limit, "sort": "downloads", "direction": "-1", "full": "true"}
        if library:
            params["library"] = library
        url = "https://huggingface.co/api/models?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "Latiao/1.0"})
        resp = urllib.request.urlopen(req, timeout=15)
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
                    except ImportError:
                        pass
                elif fix_pkg == "llama-cpp-python":
                    try:
                        import llama_cpp  # noqa: F401
                        _engine.llama_cpp_available = True
                    except ImportError:
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

def start_model(model_id: str, port: int = 1235) -> dict:
    return _engine.start_model(model_id, port)

def stop_model() -> dict:
    return _engine.stop_model()

def is_running() -> bool:
    return _engine.is_running()

def get_api_url() -> str:
    return _engine.get_api_url()
