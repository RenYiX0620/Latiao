"""模型下载（HF 镜像 / 断点续传 / 进度状态）— ModelDownloader。

从 LocalLLMEngine 拆出（2026-09-24）。
"""
from __future__ import annotations

import json
import logging
import os
import re as _re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from local_llm_probe import MODELS_DIR, _ssl_ctx

logger = logging.getLogger("latiao-sidecar")


class ModelDownloader:
    def drop_download_record(self, model_id: str) -> None:
        """删除模型文件时同步清下载记录（门面层组合调用，不摸私有字段）。"""
        with self._download_lock:
            self._downloads.pop(model_id, None)
            self._save_download_state()


    """HF 模型下载：启停/暂停/恢复/取消 + 本地状态落盘。"""

    def __init__(self):
        self._download_lock = threading.Lock()
        self._dl_lock = threading.Lock()
        self._download_state_file = MODELS_DIR / ".downloads.json"
        self._downloads: dict[str, dict] = {}
        self._download_procs: dict[str, subprocess.Popen] = {}
        self._download_threads: dict[str, threading.Thread] = {}
        self._download_locks: dict[str, threading.Lock] = {}
        self._cache_dir = Path.home() / ".cache" / "huggingface" / "hub"
        self._hf_endpoint = os.environ.get("HF_ENDPOINT", "")
        self._mirror_detected = bool(self._hf_endpoint)
        self._load_download_state()

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

    # ── Download state persistence ──

    def _load_download_state(self):
        try:
            if self._download_state_file.exists():
                saved = json.loads(self._download_state_file.read_text(encoding="utf-8"))
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
            from concurrent.futures import ThreadPoolExecutor

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
                            try:
                                os.unlink(p)
                            except OSError:
                                pass
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
                            try:
                                f.result()
                            except Exception:
                                pass

                        # A newer resume superseded this worker — leave the .part
                        # files (with their partial progress) for the new worker.
                        if dl_info.get("epoch", 0) != my_epoch:
                            return

                        # Cancelled mid-flight: clean up partial .part files too.
                        # 先查 cancelled，避免取消状态被下面的 error 覆盖
                        if dl_info.get("status") == "cancelled":
                            for sp in chunk_part_paths:
                                try:
                                    os.unlink(sp)
                                except OSError:
                                    pass
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
                                    if not data:
                                        break
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
                                if not chunk:
                                    break
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

                os.environ.setdefault("HF_ENDPOINT", self._get_hf_endpoint())
                # XetHub CAS 存储需鉴权（镜像/匿名会 401），禁用后走传统 LFS 流
                os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
                dl_info["message"] = "正在准备下载模型仓库..."
                self._save_download_state()
                local_path = snapshot_download(
                    repo_id=model_id,
                    cache_dir=cache_root,
                    resume_download=True,
                    allow_patterns=["*.gguf", "*.safetensors", "*.npz", "*.json", "*.md", "*.txt"],
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
            try:
                os.unlink(p)
            except OSError:
                pass
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

