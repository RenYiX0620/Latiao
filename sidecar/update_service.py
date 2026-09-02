"""update_service — 应用自更新预下载服务（根治大文件断流）。

背景：Tauri updater 插件下载 211MB 安装包不支持断点续传，网络断流即全废。
本模块用 sidecar 做"预下载代理"：
  1. prepare_update(): 后台线程从 GitHub 拉最新清单、比较版本、断点续传下载
     安装包到 ~/.local-ai-os/update/（Range 续传 + 重试 + 状态落盘，跨重启存活）
  2. updater 插件的 endpoint 切到 sidecar 本地端点——检查与下载都走
     127.0.0.1（本地文件流式返回，无断流可能），验签/安装仍由官方插件完成

平台映射：darwin-aarch64 / windows-x86_64（mac arm/x86、win x64）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import ssl
import threading
import time
import urllib.request
from pathlib import Path

logger = logging.getLogger("latiao-sidecar")

UPDATE_DIR = Path.home() / ".local-ai-os" / "update"
STATE_FILE = UPDATE_DIR / "state.json"
GITHUB_LATEST = "https://github.com/RenYiX0620/Latiao/releases/latest/download/latest.json"


def current_app_version() -> str:
    """当前 App 版本（Rust spawn sidecar 时经 LATIAO_APP_VERSION 注入）。"""
    return os.environ.get("LATIAO_APP_VERSION", "")

_state_lock = threading.RLock()  # 可重入：worker 持锁时会嵌套调 _save_state，
                                 # 普通 Lock 会自死锁（真实事故：事件循环整体卡死）
_state: dict = {}


def _current_platform() -> str:
    """tauri latest.json 的平台键。"""
    import sys
    if sys.platform == "darwin":
        return "darwin-aarch64" if os.uname().machine == "arm64" else "darwin-x86_64"
    if sys.platform.startswith("win"):
        return "windows-x86_64"
    return "linux-x86_64"


def _version_tuple(v: str) -> tuple:
    parts = re.findall(r"\d+", v or "")
    return tuple(int(p) for p in parts[:4]) or (0,)


def _is_newer(remote: str, current: str) -> bool:
    return _version_tuple(remote) > _version_tuple(current)


def _load_state() -> dict:
    global _state
    with _state_lock:
        if _state:
            return _state
    # 文件 IO 在锁外（防止写盘慢时阻塞其他线程的锁获取）
    try:
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            with _state_lock:
                if not _state:
                    _state = data
    except Exception:
        logger.warning("update state load failed", exc_info=True)
    with _state_lock:
        return _state or {}


def _save_state() -> None:
    """状态落盘。锁内只取快照（仅基本类型字段，防 mock/对象泄漏进序列化），
    文件 IO 在锁外——写盘卡顿绝不阻塞其他线程（此前 worker 持锁写盘挂死）。"""
    with _state_lock:
        snapshot = {k: v for k, v in _state.items()
                    if isinstance(v, (str, int, float, bool, list))}
    try:
        UPDATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except Exception:
        logger.warning("update state save failed", exc_info=True)


def get_progress() -> dict:
    """当前预下载状态（前端轮询）。"""
    st = _load_state()
    out = {
        "status": st.get("status", "idle"),
        "version": st.get("version", ""),
        "downloaded": st.get("downloaded", 0),
        "total": st.get("total", 0),
        "error": st.get("error", ""),
    }
    if out["status"] == "downloading":
        # 实时字节数从 .part 文件读（下载线程每秒刷状态，这里读文件更准）
        part = st.get("part_path", "")
        if part and os.path.exists(part):
            out["downloaded"] = os.path.getsize(part)
    return out


def _ssl_context() -> "ssl.SSLContext":
    """HTTPS 上下文：certifi + 系统 keychain CA（Watt Toolkit MITM 证书在
    keychain 而非 certifi——discovery.py 同款修复，urllib 默认不认会
    CERTIFICATE_VERIFY_FAILED，预下载因此失败）。"""
    import ssl
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(certifi.where())
    except Exception:
        pass
    try:
        import subprocess
        out = subprocess.run(
            ["security", "find-certificate", "-a", "-p", "/Library/Keychains/System.keychain"],
            capture_output=True, text=True, timeout=20,
        )
        if out.stdout:
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".pem", delete=False, mode="w") as f:
                f.write(out.stdout)
                f.flush()
            ctx.load_verify_locations(f.name)
    except Exception:
        logger.debug("keychain CA 加载失败", exc_info=True)
    return ctx


def fetch_remote_manifest() -> dict | None:
    """拉 GitHub 最新清单（原样）。失败返回 None。"""
    try:
        req = urllib.request.Request(GITHUB_LATEST, headers={"User-Agent": "Latiao/1.0"})
        with urllib.request.urlopen(req, timeout=20, context=_ssl_context()) as resp:
            return json.loads(resp.read().decode("utf-8", "ignore"))
    except Exception:
        logger.warning("update manifest fetch failed", exc_info=True)
        return None


def _download_worker(url: str, dest: Path, version: str) -> None:
    """断点续传下载（单线程 Range 续传 + 重试 8 次 + 状态落盘）。"""
    part = dest.with_suffix(dest.suffix + ".part")
    max_retries = 8
    for attempt in range(max_retries):
        try:
            offset = part.stat().st_size if part.exists() else 0
            req = urllib.request.Request(url, headers={"User-Agent": "Latiao/1.0"})
            if offset > 0:
                req.add_header("Range", f"bytes={offset}-")
            with _state_lock:
                _state.update({"status": "downloading", "part_path": str(part)})
                _save_state()
            with urllib.request.urlopen(req, timeout=60, context=_ssl_context()) as resp:
                total = int(resp.getheader("Content-Length", 0)) + offset
                with _state_lock:
                    _state["total"] = total
                mode = "ab" if offset > 0 else "wb"
                with open(part, mode) as f, _state_lock:
                    while True:
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        _state["downloaded"] = f.tell()
                        # 状态每 2 秒落一次盘（防频繁 IO）
                        if time.time() - _state.get("_last_flush", 0) > 2:
                            _state["_last_flush"] = time.time()
                            _save_state()
            if part.stat().st_size >= total > 0:
                # 下载完成：重命名为正式文件
                os.replace(part, dest)
                with _state_lock:
                    _state.update({"status": "done", "downloaded": total, "part_path": ""})
                    _save_state()
                logger.info("更新包预下载完成: %s (%d 字节)", dest.name, total)
                return
            raise RuntimeError(f"下载数据不足: {part.stat().st_size}/{total}")
        except Exception as e:
            logger.warning("更新包下载中断(第 %d 次): %s", attempt + 1, e)
            with _state_lock:
                _state["error"] = str(e)[:200]
                _save_state()
            time.sleep(min(30, 3 * (attempt + 1)))  # 退避重试
    with _state_lock:
        _state["status"] = "failed"
        _save_state()


def start_prepare(current_version: str) -> dict:
    """启动预下载（后台线程，幂等）。返回当前状态。"""
    platform = _current_platform()
    st = _load_state()
    if st.get("status") in ("downloading", "done"):
        # downloading: 已在跑；done: 且版本未变 → 幂等返回
        if st.get("status") == "done" and _is_newer(st.get("version", ""), current_version or "0.0.0"):
            pass  # done 但 state 版本仍比当前新 → 复用（文件在磁盘）
        return get_progress()

    def worker():
        try:
            with _state_lock:
                _state.update({"status": "checking", "error": ""})
                _save_state()
            manifest = fetch_remote_manifest()
            if not manifest:
                with _state_lock:
                    _state.update({"status": "failed", "error": "无法获取更新清单"})
                    _save_state()
                return
            remote_ver = str(manifest.get("version", ""))
            if not _is_newer(remote_ver, current_version or "0.0.0"):
                with _state_lock:
                    _state.update({"status": "up_to_date", "version": remote_ver})
                    _save_state()
                return
            entry = (manifest.get("platforms") or {}).get(platform)
            if not entry or not entry.get("url"):
                with _state_lock:
                    _state.update({"status": "failed", "error": f"清单无 {platform} 条目"})
                    _save_state()
                return
            url = entry["url"]
            dest = UPDATE_DIR / url.split("/")[-1]
            with _state_lock:
                _state.update({"status": "downloading", "version": remote_ver,
                               "url": url, "signature": entry.get("signature", ""),
                               "downloaded": 0, "total": 0})
                _save_state()
            _download_worker(url, dest, remote_ver)
        except Exception as e:
            logger.warning("update prepare failed", exc_info=True)
            with _state_lock:
                _state.update({"status": "failed", "error": str(e)[:200]})
                _save_state()

    threading.Thread(target=worker, daemon=True).start()
    return get_progress()


def get_tauri_manifest(current_version: str) -> dict:
    """生成 tauri updater 格式清单（供 /v1/update-latest.json 端点）。

    - 预下载 done 且版本比当前新 → 返回清单（url 指向本地端点）
    - 否则返回 current_version（updater 判无更新）
    """
    st = _load_state()
    remote_ver = str(st.get("version", ""))
    done = st.get("status") == "done"
    if not done or not _is_newer(remote_ver, current_version or "0.0.0"):
        return {"version": current_version or "0.0.0",
                "notes": "", "pub_date": st.get("pub_date", ""),
                "platforms": {}}
    dest = UPDATE_DIR / f"Latiao_{remote_ver}_{_current_platform().replace('-','_')}.pkg"
    # 实际文件名来自 url（跨平台名不同）
    url = st.get("url", "")
    if url:
        dest = UPDATE_DIR / url.split("/")[-1]
    if not dest.exists():
        return {"version": current_version or "0.0.0", "notes": "", "platforms": {}}
    return {
        "version": remote_ver,
        "notes": "Latiao v" + remote_ver,
        "pub_date": st.get("pub_date", ""),
        "platforms": {
            _current_platform(): {
                "signature": st.get("signature", ""),
                "url": "http://127.0.0.1:8765/v1/update-file",
            }
        },
    }


def get_update_file_path() -> Path | None:
    """本地安装包路径（流式返回给 updater）。"""
    st = _load_state()
    url = st.get("url", "")
    if not url:
        return None
    dest = UPDATE_DIR / url.split("/")[-1]
    return dest if dest.exists() else None
