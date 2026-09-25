"""
Latiao Local LLM Engine — 门面（2026-09-24 组合拆分）

- local_llm_probe：GGUF/MLX 探测纯函数
- local_llm_download.ModelDownloader：HF 下载
- local_llm_engine.EngineProcess：引擎进程生命周期

对外 API 不变。`local_llm._engine` 是 LocalLLMEngine 组合门面；
`__getattr__` 把未定义属性代理到 engine/downloader，兼容
`getattr(local_llm._engine, "current_model_id")` 等旧访问。
"""
from __future__ import annotations

import json
import logging
import os
import platform
import re as _re
import subprocess
import sys
from pathlib import Path

from cmd_safety import child_env
from local_llm_engine import EngineProcess
from local_llm_download import ModelDownloader
from local_llm_probe import (  # noqa: F401 — 旧导入路径 re-export
    IS_APPLE_SILICON,
    IS_MAC,
    IS_WINDOWS,
    MODELS_DIR,
    _auto_cache_type,
    _config_file,
    _custom_engine_spec,
    _custom_engine_target,
    _gguf_architecture,
    _read_config,
    _resolve_mlx_path,
    _ggml_type_label,
    _gguf_precheck,
    _gguf_scan,
    _mlx_arch_supported,
    _mlx_reject_reason,
    _detect_model_bits,
    _fix_tokenizer_alias,
    _ssl_ctx,
)

logger = logging.getLogger("latiao-sidecar")

_ENGINE_ENV_PREFIXES = ("DYLD_", "LD_", "MTL_", "GGML_", "OMP_", "CUDA_", "VK_", "HF_", "MLX_")


class LocalLLMEngine:
    """组合门面：ModelDownloader + EngineProcess。"""

    def __init__(self):
        self.downloader = ModelDownloader()
        self.engine = EngineProcess()
        self.engine.downloader = self.downloader  # delete_model_file 要清下载记录

    def __getattr__(self, name):
        d = self.__dict__
        for key in ("engine", "downloader"):
            obj = d.get(key)
            if obj is not None and hasattr(obj, name):
                return getattr(obj, name)
        raise AttributeError(f"LocalLLMEngine has no attribute {name!r}")

    def __setattr__(self, name, value):
        # 写遮蔽（2026-09-24 审计 P0-2）：只定义 __getattr__ 时，
        # `_engine.model_token_limit = x` 会落在门面实例字典上，真正的
        # EngineProcess 仍读旧值（改上下文/装完 mlx-lm 仍报不可用）。
        # 属性存在时转发给真实对象；否则才落在门面上。
        if name in ("engine", "downloader"):
            object.__setattr__(self, name, value)
            return
        d = object.__getattribute__(self, "__dict__")
        for key in ("engine", "downloader"):
            obj = d.get(key)
            if obj is not None and (name in getattr(obj, "__dict__", {}) or hasattr(type(obj), name)):
                setattr(obj, name, value)
                return
        object.__setattr__(self, name, value)

    # ── 下载 ──
    def download_model(self, model_id: str) -> dict:
        return self.downloader.download_model(model_id)

    def pause_download(self, model_id: str) -> dict:
        return self.downloader.pause_download(model_id)

    def resume_download(self, model_id: str) -> dict:
        return self.downloader.resume_download(model_id)

    def cancel_download(self, model_id: str) -> dict:
        return self.downloader.cancel_download(model_id)

    def get_all_downloads(self) -> dict:
        return self.downloader.get_all_downloads()

    def clear_downloads(self, status_filter: str = "") -> dict:
        return self.downloader.clear_downloads(status_filter)

    def get_download_progress(self, model_id: str) -> dict:
        return self.downloader.get_download_progress(model_id)

    # ── 引擎 ──
    def get_status(self) -> dict:
        return self.engine.get_status()

    def is_running(self) -> bool:
        return self.engine.is_running()

    def get_api_url(self) -> str:
        return self.engine.get_api_url()

    def start_model(self, model_id: str, port: int = 1235) -> dict:
        return self.engine.start_model(model_id, port)

    def stop_model(self) -> dict:
        return self.engine.stop_model()

    def shutdown_engine(self) -> None:
        self.engine.shutdown_engine()

    def detach_engine(self) -> None:
        self.engine.detach_engine()

    def delete_model_file(self, model_id: str) -> dict:
        return self.engine.delete_model_file(model_id)

    def get_backend(self) -> str:
        return self.engine.get_backend()

    def get_available_backends(self) -> list[str]:
        return self.engine.get_available_backends()

    def mark_engine_busy(self, grace_sec: float = 45.0) -> None:
        self.engine.mark_engine_busy(grace_sec)

    def mark_engine_idle(self) -> None:
        self.engine.mark_engine_idle()

    def mark_engine_suspect(self) -> None:
        self.engine.mark_engine_suspect()

    def verify_engine_health(self, timeout: float = 20) -> bool:
        return self.engine.verify_engine_health(timeout)

    def ensure_engine_healthy(self, force: bool = False) -> bool:
        return self.engine.ensure_engine_healthy(force)


_engine = LocalLLMEngine()

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
        base = _engine._get_hf_endpoint().rstrip("/")
        url = base + "/api/models?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "Latiao/1.0"})
        with urllib.request.urlopen(req, timeout=15, context=_ssl_ctx) as resp:
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
    # 拼 URL 前校验 model_id，拒绝畸形/带路径穿越的 ID
    if not _re.fullmatch(r'[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+', model_id):
        return {"status": "error", "message": f"模型 ID 格式不合法: {model_id}"}
    try:
        # Fetch model info (respects HF_ENDPOINT mirror)
        # ?blobs=true resolves LFS pointers to get real file sizes
        base = _engine._get_hf_endpoint().rstrip("/")
        url = f"{base}/api/models/{model_id}?blobs=true"
        req = urllib.request.Request(url, headers={"User-Agent": "Latiao/1.0"})
        with urllib.request.urlopen(req, timeout=15, context=_ssl_ctx) as resp:
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
                        if q in fname:
                            quant = q
                            break
                    siblings.append({"filename": fname, "size": size_str, "size_bytes": size_bytes, "quant": quant})

        # Readme excerpt
        readme = ""
        try:
            readme_url = f"{base}/{model_id}/raw/main/README.md"
            readme_req = urllib.request.Request(readme_url, headers={"User-Agent": "Latiao/1.0"})
            with urllib.request.urlopen(readme_req, timeout=10, context=_ssl_ctx) as readme_resp:
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

# run_fix 允许安装的 pip 包白名单（取包名部分比较，不含 extras/版本号）
_PIP_INSTALL_WHITELIST = {
    "mlx", "mlx-lm", "mlx-metal", "llama-cpp-python", "huggingface_hub",
    "hf-xet", "numpy", "tokenizers", "transformers", "sentencepiece",
    "protobuf", "safetensors", "accelerate",
}

def run_fix(fix_type: str, fix_pkg: str = "") -> dict:
    if getattr(sys, "frozen", False):
        return {"status": "error", "message": "依赖已打包在安装包中，请联系开发者更新"}
    """Execute a fix for an environment issue."""
    if fix_type == "pip" and fix_pkg:
        # 包名白名单 + 格式校验：防止任意 pip 安装导致 RCE
        if not _re.fullmatch(r'[A-Za-z0-9_.\-]+(\[[A-Za-z0-9_,\-]+\])?(==[A-Za-z0-9.*]+)?', fix_pkg):
            return {"status": "error", "message": f"包名不合法，已拒绝安装: {fix_pkg}"}
        pkg_name = _re.split(r'[\[=]', fix_pkg, maxsplit=1)[0]
        if pkg_name not in _PIP_INSTALL_WHITELIST:
            return {"status": "error", "message": f"不在允许安装的包白名单内，已拒绝: {pkg_name}"}
        try:
            proc_result = subprocess.run(
                [sys.executable, "-m", "pip", "install", fix_pkg],
                capture_output=True, text=True, timeout=120,
                # ④ pip 同样不该拿到 sidecar token / 云模型密钥；PIP_/UV_ 前缀
                # 保留镜像与索引配置（用户可能靠 PIP_INDEX_URL 走内网源）
                env=child_env(allow_prefixes=("PIP_", "UV_")),
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
    """Scan for GGUF and MLX model files locally.

    Searches the built-in ~/Models/ dir plus third-party model managers
    (LM Studio, Ollama) so models downloaded elsewhere are discoverable.
    """
    models: list[dict] = []
    seen_paths: set[str] = set()

    def _scan_dir(root: Path):
        if not root or not root.exists():
            return
        for f in sorted(root.rglob("*")):
            if not f.is_file() or f.suffix not in (".gguf", ".mlx"):
                continue
            if str(f) in seen_paths:
                continue
            seen_paths.add(str(f))
            size_gb = f.stat().st_size / (1024**3)
            models.append({
                "id": f.stem, "name": f.stem, "path": str(f),
                "size": f"{size_gb:.1f}GB", "format": f.suffix[1:],
            })

    def _dir_size_gb(d: Path) -> float:
        total = 0
        for f in d.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
        return total / (1024**3)

    def _scan_mlx_dirs(root: Path):
        """MLX 模型以目录形式存在（config.json + 权重），扫描首层目录。"""
        if not root or not root.exists():
            return
        for d in sorted(root.iterdir()):
            if not d.is_dir() or str(d) in seen_paths:
                continue
            if not (d / "config.json").exists():
                continue
            has_w = (d / "model.safetensors").exists() \
                or (d / "model.safetensors.index.json").exists() \
                or (d / "weights.npz").exists() \
                or any(d.glob("weights*.npz"))
            if not has_w:
                continue
            seen_paths.add(str(d))
            models.append({
                "id": d.name, "name": d.name, "path": str(d),
                "size": f"{_dir_size_gb(d):.1f}GB", "format": "mlx",
            })

    # MLX 目录扫描：~/Models 首层 + LM Studio/Ollama 管理器目录 + HF hub 快照
    # （repo id 为模型 id）。管理器目录是 <org>/<name> 两层，需逐 org 扫。
    _scan_mlx_dirs(MODELS_DIR)
    for _mgr_root in (Path.home() / ".lmstudio" / "models", Path.home() / ".ollama" / "models"):
        try:
            if _mgr_root.is_dir():
                for _org in _mgr_root.iterdir():
                    if _org.is_dir():
                        _scan_mlx_dirs(_org)
        except OSError:
            continue
    hf_scan = Path.home() / ".cache" / "huggingface" / "hub"
    if hf_scan.exists():
        for dl_info in hf_scan.glob("models--*"):
            snaps = dl_info / "snapshots"
            if snaps.exists():
                mid = dl_info.name.replace("models--", "").replace("--", "/")
                if not any(m["id"] == mid for m in models):
                    models.append({
                        "id": mid, "name": mid.split("/")[-1],
                        "path": str(dl_info), "size": "cached", "format": "mlx",
                    })

    # 1. Built-in model dir (~/Models/)
    _scan_dir(MODELS_DIR)
    # 2. Third-party model managers (reuse models downloaded via LM Studio / Ollama)
    _scan_dir(Path.home() / ".lmstudio" / "models")
    _scan_dir(Path.home() / ".ollama" / "models")
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


def shutdown_engine() -> None:
    return _engine.shutdown_engine()

def delete_model_file(model_id: str) -> dict:
    return _engine.delete_model_file(model_id)


def is_running() -> bool:
    return _engine.is_running()

def get_api_url() -> str:
    return _engine.get_api_url()
