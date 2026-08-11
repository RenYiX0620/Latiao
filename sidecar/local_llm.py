"""
Latiao Local LLM Engine — Cross-Platform (Mac / Windows / Linux)

Auto-detects best backend:
  - Apple Silicon + mlx-lm → MLX (fastest)
  - Fallback → llama-cpp-python (cross-platform, GPU accel via Metal/CUDA/Vulkan)
"""

from __future__ import annotations

import collections
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

# Module-level TLS context for HuggingFace API + model downloads.
# Verified via certifi's bundled CAs — sidesteps the broken system cert chain
# on some macOS/Python combos that prompted the old unverified context. Model
# files are still hash-verified by huggingface_hub as a second layer.
import ssl
import certifi

try:
    _ssl_ctx = ssl.create_default_context(cafile=certifi.where())
except Exception:
    # Never fall back to an unverified context — use the system store instead.
    _ssl_ctx = ssl.create_default_context()

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
        # start/stop 主流程串行化锁（RLock：启动失败的错误路径会再调 stop_model）
        self._proc_lock = threading.RLock()
        self._active_backend = ""  # The backend actually used to start the current model
        self.current_model_id = ""
        self.current_model_name = ""
        self.server_port = 1235
        self.server_status = "stopped"  # stopped | starting | running | error
        self.status_message = ""
        self.has_image_support = False
        # _find_gguf 结果缓存（30s TTL），避免重复全盘 rglob 扫描
        self._gguf_find_cache: dict[str, tuple[float, str | None]] = {}

        # Register exit handler as belt-and-suspenders (once per process)
        if not LocalLLMEngine._atexit_registered:
            import atexit
            atexit.register(self._cleanup_child)
            LocalLLMEngine._atexit_registered = True
        self.model_token_limit = int(os.environ.get("LATIAO_CTX_LEN", "8192"))
        self.n_gpu_layers = int(os.environ.get("LATIAO_GPU_LAYERS", "-1"))

        # Download state
        self._download_lock = threading.Lock()
        # 保护 _downloads dict 结构变更（插入/重建/序列化快照）
        self._dl_lock = threading.Lock()
        self._download_state_file = MODELS_DIR / ".downloads.json"
        self._downloads: dict[str, dict] = {}
        self._download_procs: dict[str, subprocess.Popen] = {}
        self._download_threads: dict[str, threading.Thread] = {}
        # Per-model locks serializing download workers across pause/resume.
        self._download_locks: dict[str, threading.Lock] = {}
        self._cache_dir = Path.home() / ".cache" / "huggingface" / "hub"
        self._load_download_state()

        # HF mirror 改为惰性探测：首次需要时才测速，避免 import/实例化时访问网络
        self._hf_endpoint = os.environ.get("HF_ENDPOINT", "")
        self._mirror_detected = bool(self._hf_endpoint)

    def _get_hf_endpoint(self) -> str:
        """Return HF endpoint, auto-detecting the fastest mirror on first use."""
        if not self._mirror_detected:
            self._mirror_detected = True
            try:
                self._hf_endpoint = self._detect_fastest_mirror()
            except Exception:
                self._hf_endpoint = "https://huggingface.co"
        return self._hf_endpoint or "https://huggingface.co"

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
                with urllib.request.urlopen(req, timeout=5, context=_ssl_ctx) as resp:
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
            import copy
            # 锁内做快照，避免序列化期间 dict 被其他线程改到一半
            with self._dl_lock:
                snapshot = copy.deepcopy(self._downloads)
            MODELS_DIR.mkdir(parents=True, exist_ok=True)
            # 先写临时文件再原子替换，避免崩溃时留下写了一半的状态文件
            tmp_file = self._download_state_file.with_suffix(".tmp")
            tmp_file.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False))
            os.replace(tmp_file, self._download_state_file)
        except (OSError, json.JSONDecodeError, RuntimeError):
            logger.warning("Failed to save download state", exc_info=True)

    # ── Download worker ──

    def _download_worker(self, model_id: str):
        # Serialize workers per model: a quick pause→resume must wait for the
        # previous worker (and its chunk threads, drained by the
        # ThreadPoolExecutor `with`-exit) to fully exit before a new worker
        # appends to the same .part files. Runs in the daemon thread so
        # resume_download returns immediately; the epoch field lets stale chunk
        # threads notice they've been superseded and stop without corrupting.
        lock = self._download_locks.setdefault(model_id, threading.Lock())
        with lock:
            self._download_worker_inner(model_id)

    def _download_worker_inner(self, model_id: str):
        dl_info = self._downloads.get(model_id, {})
        my_epoch = dl_info.get("epoch", 0)
        # 排队期间被取消：直接退出，不要覆盖 cancelled 状态重新下载
        if dl_info.get("status") == "cancelled":
            return
        with self._dl_lock:
            dl_info["status"] = "downloading"
            dl_info["started_at"] = time.time()
            dl_info["downloaded_bytes"] = 0
        try:
            import urllib.request
            from concurrent.futures import ThreadPoolExecutor, as_completed

            cache_root = str(self._cache_dir.parent)

            if model_id.endswith(".gguf") or model_id.endswith(".safetensors"):
                # Single file download via HF raw URL with multi-threaded chunked download
                parts = model_id.rsplit("/", 1)
                repo_id = parts[0] if len(parts) == 2 else model_id
                filename = parts[1] if len(parts) == 2 else model_id
                # 拼 URL 前校验 repo_id，拒绝畸形/带路径穿越的模型 ID
                if not _re.fullmatch(r'[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+', repo_id):
                    raise Exception(f"模型 ID 格式不合法: {repo_id}")
                url = f"{self._get_hf_endpoint()}/{repo_id}/resolve/main/{filename}"
                dest_dir = MODELS_DIR / repo_id.replace("/", "--")
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest_path = dest_dir / filename

                # Get file size and check if server supports Range requests
                req = urllib.request.Request(url, method="HEAD",
                    headers={"User-Agent": "Latiao/1.0"})
                with urllib.request.urlopen(req, timeout=15, context=_ssl_ctx) as resp:
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
                    # Multi-threaded chunked download with byte-granular resume.
                    # Each chunk is appended to a stable .part file next to dest; a
                    # paused/failed partial chunk is continued from the bytes already
                    # on disk instead of being re-fetched from scratch. The model lock
                    # + epoch field keep a stale worker (superseded by resume) from
                    # racing the new worker's appends to the same .part files.
                    chunk_plan = dl_info.get("chunk_ranges")
                    if chunk_plan and chunk_plan[-1][1] != total_size - 1:
                        # Remote file changed size since last attempt — discard stale parts.
                        for p in (dl_info.get("chunk_paths") or []):
                            try: os.unlink(p)
                            except OSError: pass
                        chunk_plan = None
                    if not chunk_plan:
                        num_threads = min(6, max(2, total_size // (300 * 1024 * 1024)))  # 1 thread per 300MB, max 6
                        chunk_size = total_size // num_threads
                        chunk_plan = []
                        for i in range(num_threads):
                            start = i * chunk_size
                            end = start + chunk_size - 1 if i < num_threads - 1 else total_size - 1
                            chunk_plan.append([start, end])
                    dl_info["chunk_ranges"] = chunk_plan
                    num_threads = len(chunk_plan)
                    chunk_part_paths = [str(dest_path.parent / f".{dest_path.name}.chunk{i}.part") for i in range(num_threads)]
                    dl_info["chunk_paths"] = chunk_part_paths
                    dl_info["message"] = f"多线程下载 {filename} ({total_size/(1024**3):.1f}GB, {num_threads}线程)..."
                    self._save_download_state()

                    # Seed progress from partial .part files (byte-granular resume).
                    progress_bytes = [0] * num_threads
                    for _i, _sp in enumerate(chunk_part_paths):
                        if os.path.exists(_sp):
                            progress_bytes[_i] = os.path.getsize(_sp)
                    progress_event = threading.Event()
                    last_update = time.time()
                    last_total = sum(progress_bytes)
                    download_error = [None]

                    def download_chunk(idx: int) -> None:
                        start, end = chunk_plan[idx]
                        full_size = end - start + 1
                        stable = chunk_part_paths[idx]
                        # Already fully fetched in a previous run — reuse it.
                        if os.path.exists(stable) and os.path.getsize(stable) >= full_size:
                            progress_bytes[idx] = full_size
                            progress_event.set()
                            return
                        # Append-resume: each attempt continues from the bytes already
                        # on disk, so a paused/failed partial chunk isn't re-fetched
                        # from scratch. The epoch+status checks let a stale worker
                        # (superseded by resume) stop without corrupting the file.
                        for attempt in range(3):
                            try:
                                offset = os.path.getsize(stable) if os.path.exists(stable) else 0
                                if offset >= full_size:
                                    break
                                headers = {"User-Agent": "Latiao/1.0",
                                           "Range": f"bytes={start + offset}-{end}"}
                                req2 = urllib.request.Request(url, headers=headers)
                                with urllib.request.urlopen(req2, timeout=120, context=_ssl_ctx) as resp2:
                                    # If we asked for a partial range but the server
                                    # returned the full body (HTTP 200, ignoring Range),
                                    # appending would duplicate bytes — restart the chunk.
                                    # 非 0 起始的 chunk 必须收到 206；收到 200 说明服务器
                                    # 忽略了 Range、返回全量 body —— 跳过不属于自己的前导
                                    # 字节，且只写本 chunk 的字节数。
                                    range_ignored = resp2.getcode() != 206
                                    if range_ignored:
                                        offset = 0
                                    skip = start if range_ignored else 0
                                    with open(stable, "wb" if range_ignored else "ab") as f:
                                        downloaded = 0
                                        while True:
                                            if dl_info.get("epoch", 0) != my_epoch or \
                                               dl_info.get("status") in ("paused", "cancelled"):
                                                return
                                            data = resp2.read(512 * 1024)  # 512KB chunks
                                            if not data:
                                                break
                                            if skip:
                                                if skip >= len(data):
                                                    skip -= len(data)
                                                    continue
                                                data = data[skip:]
                                                skip = 0
                                            # 只读取该 chunk 的字节数
                                            remaining = full_size - offset - downloaded
                                            if remaining <= 0:
                                                break
                                            if len(data) > remaining:
                                                data = data[:remaining]
                                            f.write(data)
                                            downloaded += len(data)
                                            progress_bytes[idx] = offset + downloaded
                                            progress_event.set()
                                if os.path.getsize(stable) >= full_size:
                                    progress_bytes[idx] = full_size
                                    progress_event.set()
                                    return
                                # Server sent a truncated range — retry to continue.
                            except Exception as e:
                                if attempt == 2:
                                    download_error[0] = str(e)
                                    return
                                time.sleep(1)
                        if os.path.exists(stable) and os.path.getsize(stable) >= full_size:
                            progress_bytes[idx] = full_size
                            progress_event.set()
                        else:
                            download_error[0] = f"分块 {idx} 下载不完整"

                    with ThreadPoolExecutor(max_workers=num_threads) as executor:
                        futures = [executor.submit(download_chunk, i) for i in range(num_threads)]

                        # Real-time progress loop: poll every 0.8s
                        while any(not f.done() for f in futures):
                            if dl_info.get("status") in ("paused", "cancelled"):
                                # Chunk threads check status in their read loop and stop;
                                # the `with`-exit drains them. Stable .part files keep
                                # partial progress for resume — nothing to discard.
                                self._save_download_state()  # 暂停时把进度落盘
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

                        # A newer resume superseded this worker — leave the .part
                        # files (with their partial progress) for the new worker.
                        if dl_info.get("epoch", 0) != my_epoch:
                            return

                        # Cancelled mid-flight: clean up partial .part files too.
                        # 先查 cancelled，避免取消状态被下面的 error 覆盖
                        if dl_info.get("status") == "cancelled":
                            for sp in chunk_part_paths:
                                try: os.unlink(sp)
                                except OSError: pass
                            dl_info.pop("chunk_ranges", None)
                            dl_info.pop("chunk_paths", None)
                            return

                        if download_error[0]:
                            raise Exception(download_error[0])

                        # Sanity-check chunk sizes before merging (guards against a
                        # silent Range mismatch corrupting the output).
                        total_chunk_bytes = sum(
                            os.path.getsize(sp) for sp in chunk_part_paths if os.path.exists(sp))
                        if total_chunk_bytes != total_size:
                            raise Exception(f"分块大小不匹配: {total_chunk_bytes} != {total_size}")

                        # Merge completed chunks in order
                    valid_chunks = [(i, sp) for i, sp in enumerate(chunk_part_paths) if os.path.exists(sp)]
                    valid_chunks.sort(key=lambda x: x[0])
                    dl_info["message"] = f"合并分块 {filename}..."
                    self._save_download_state()
                    with open(dest_path, "wb") as out:
                        for _, sp in valid_chunks:
                            with open(sp, "rb") as inp:
                                while True:
                                    data = inp.read(8 * 1024 * 1024)
                                    if not data: break
                                    out.write(data)
                            os.unlink(sp)
                    dl_info.pop("chunk_ranges", None)
                    dl_info.pop("chunk_paths", None)
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
                                    self._save_download_state()  # 暂停时把进度落盘
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

            with self._dl_lock:
                dl_info["progress"] = 100
                dl_info.update({"status": "done", "path": path, "message": "下载完成"})
        except Exception as e:
            with self._dl_lock:
                # 已被取消的任务保持 cancelled，不被 error 覆盖
                if dl_info.get("status") != "cancelled":
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
                    with self._dl_lock:
                        self._downloads[model_id] = {"status": "done", "progress": 100, "path": path, "message": "已缓存", "model_id": model_id}
                    self._save_download_state()
                    return {"status": "ok", "model_id": model_id, "path": path, "message": "模型已缓存"}

        with self._dl_lock:
            # paused 重下：保留原有分块记录（merge 而非整体覆盖），避免丢 chunk、
            # 遗留孤儿 .part 文件
            prev = self._downloads.get(model_id) or {}
            new_info = {"status": "downloading", "progress": 0, "path": "", "message": "准备下载...",
                        "model_id": model_id, "speed_bps": 0, "eta_seconds": 0, "downloaded_bytes": 0}
            if prev.get("status") == "paused":
                for k in ("chunk_paths", "chunk_ranges", "total_bytes", "epoch"):
                    if k in prev:
                        new_info[k] = prev[k]
            self._downloads[model_id] = new_info
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
        # Bump the epoch so any stale chunk threads still draining from the
        # previous worker notice they've been superseded and stop (rather than
        # appending to .part files the new worker now owns).
        dl_info["epoch"] = dl_info.get("epoch", 0) + 1
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
        # Bump the epoch（同 resume）：排队中的 worker 拿到锁后会发现自己已被
        # superseded，不会无视取消重新下载
        dl_info["epoch"] = dl_info.get("epoch", 0) + 1
        dl_info["status"] = "cancelled"
        # Discard any preserved chunk parts so cancel truly frees the space.
        for p in (dl_info.get("chunk_paths") or []):
            try: os.unlink(p)
            except OSError: pass
        dl_info.pop("chunk_ranges", None)
        dl_info.pop("chunk_paths", None)
        dl_info["message"] = "已取消"
        self._save_download_state()
        return {"status": "ok", "download": dl_info}

    def get_all_downloads(self) -> dict:
        return {"status": "ok", "downloads": list(self._downloads.values())}

    def clear_downloads(self, status_filter: str = "") -> dict:
        with self._dl_lock:
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
        """Kill any process listening on the given port."""
        if platform.system() == "Windows":
            try:
                result = subprocess.run(
                    ["netstat", "-ano"], capture_output=True, text=True, timeout=10
                )
                for line in result.stdout.splitlines():
                    if f":{port}" in line and "LISTENING" in line:
                        pid = line.split()[-1]
                        if pid.isdigit():
                            subprocess.run(["taskkill", "/F", "/PID", pid],
                                           capture_output=True, timeout=10)
            except Exception:
                pass
            return
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
        # 30 秒 TTL 缓存：start/delete 等路径会重复触发全盘 rglob 扫描
        cached = self._gguf_find_cache.get(model_id)
        if cached and time.time() - cached[0] < 30:
            return cached[1]
        result = self._find_gguf_uncached(model_id)
        self._gguf_find_cache[model_id] = (time.time(), result)
        return result

    def _find_gguf_uncached(self, model_id: str) -> str | None:
        if model_id.endswith(".gguf") and Path(model_id).exists():
            return model_id
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
        cache_models = self._cache_dir.parent / "models"
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
        if model_id.endswith(".gguf") and Path(model_id).exists():
            # 直接路径：必须位于 ~/Models 内
            try:
                p = Path(model_id).resolve()
                p.relative_to(models_root)
            except (OSError, ValueError):
                return None
            return str(p)
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

        if platform.system() == "Windows":
            self.server_status = "starting"
            self.status_message = f"正在加载 {self.current_model_name}..."
            return self._start_llama_native(model_path, port)

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
            # stdout 无人读取，必须 DEVNULL，否则管道缓冲满后子进程死锁
            proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, env=env
            )
            self._process = proc

            # Drain stderr in background thread so pipe buffer never blocks the child
            # 用局部 proc 而不是 self._process，避免快速 stop→start 时读到新进程的 stderr
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
            if not self._wait_for_http(port, timeout_sec=300, process=proc):
                t.join(timeout=1)
                err_lines = list(stderr_lines)[-50:] if stderr_lines else []
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
                    "exited early" if proc.poll() is not None else "HTTP timeout",
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

    def _find_llama_server(self) -> Path | None:
        """Find native llama-server.exe (bundled with Windows package)."""
        exe = Path(__file__).parent / "llama-server.exe"
        return exe if exe.exists() else None

    def _start_llama_native(self, model_path: str, port: int) -> dict:
        """Start llama-server.exe directly (Windows only)."""
        exe = self._find_llama_server()
        if not exe:
            self.server_status = "error"
            self.status_message = "找不到 llama-server.exe，请重装 Latiao"
            return self.get_status()

        self.server_status = "starting"
        self.status_message = f"正在加载 {self.current_model_name}..."

        cmd = [
            str(exe),
            "-m", model_path,
            "--port", str(port),
            "--host", "127.0.0.1",
            "-c", str(self.model_token_limit),
            "-ngl", str(self.n_gpu_layers),
        ]
        kv_k, kv_v = _auto_cache_type(model_path)
        cmd += ["--cache-type-k", str(kv_k), "--cache-type-v", str(kv_v)]
        cmd += ["-fa"]
        chat_fmt = self._guess_chat_format(model_path)
        if chat_fmt:
            cmd += ["--chat-template", chat_fmt]

        env = os.environ.copy()
        env.pop("HF_ENDPOINT", None)
        # stdout 无人读取，必须 DEVNULL，否则管道缓冲满后子进程死锁
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, env=env
        )
        self._process = proc

        # 用局部 proc 而不是 self._process，避免快速 stop→start 时读到新进程的 stderr
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

        if not self._wait_for_http(port, timeout_sec=300, process=proc):
            t.join(timeout=1)
            err_lines = list(stderr_lines)[-50:] if stderr_lines else []
            err_summary = ""
            for line in reversed(err_lines):
                stripped = line.strip()
                if stripped and ("Error" in stripped or "error" in stripped.lower()):
                    err_summary = stripped[-150:]
                    break
            if not err_summary and err_lines:
                err_summary = err_lines[-1].strip()[-150:]
            logger.error("llama-server native: %s",
                "exited early" if proc.poll() is not None else "HTTP timeout")
            self.stop_model()
            self.server_status = "error"
            self.status_message = f"启动失败: {err_summary}" if err_summary else "模型加载超时"
            self.current_model_id = ""
            self.current_model_name = ""
            return self.get_status()

        self.server_status = "running"
        self.status_message = f"{self.current_model_name} 运行中"
        self._active_backend = "llama-cpp-native"
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
            # stdout 无人读取，必须 DEVNULL，否则管道缓冲满后子进程死锁
            proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, env=env
            )
            self._process = proc

            # Drain stderr in background thread so pipe buffer never blocks the child
            # 用局部 proc 而不是 self._process，避免快速 stop→start 时读到新进程的 stderr
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
            if not self._wait_for_http(port, timeout_sec=300, process=proc):
                t.join(timeout=1)
                err_lines = list(stderr_lines)[-50:] if stderr_lines else []
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
                    "exited early" if proc.poll() is not None else "HTTP timeout",
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
        # start/stop 主流程持锁，避免并发 start/stop 竞态
        # （RLock：内部错误路径会再调 stop_model）
        with self._proc_lock:
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
            if platform.system() == "Windows":
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(self._process.pid)],
                        capture_output=True, timeout=10
                    )
                except Exception:
                    pass

    def stop_model(self) -> dict:
        with self._proc_lock:
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
            self.server_status = "stopped"
            self.status_message = "已停止"
            self.current_model_id = ""
            self.current_model_name = ""
            self._active_backend = ""
            self.has_image_support = False
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
                        if q in fname: quant = q; break
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

    # 1. Built-in model dir (~/Models/)
    _scan_dir(MODELS_DIR)
    # 2. Third-party model managers (reuse models downloaded via LM Studio / Ollama)
    _scan_dir(Path.home() / ".lmstudio" / "models")
    _scan_dir(Path.home() / ".ollama" / "models")
    # 3. HuggingFace cache
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
