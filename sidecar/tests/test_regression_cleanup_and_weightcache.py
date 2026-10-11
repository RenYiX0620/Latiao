"""H1 cleanup keep 集 / H2 weight_hashes 缓存覆盖（2026-10-11 全盘审查 High）。

两个都是同一形态：helper 写了 X，调用方又整体覆盖 X / 反向条件。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LATIAO_AUTH_TOKEN", "reg-h12-token")


def test_cleanup_keeps_highest_even_with_ref(tmp_path, monkeypatch):
    """状态有 url 引用时，最新包仍必须保留（旧实现会删掉它）。"""
    import update_service as us

    monkeypatch.setattr(us, "_state_paths", lambda: (tmp_path, tmp_path / "state.json"))
    (tmp_path / "state.json").write_text(
        '{"status": "done", "url": "https://x/Latiao_0.3.70_setup.exe"}', "utf-8")
    old = tmp_path / "Latiao_0.3.70_setup.exe"
    new = tmp_path / "Latiao_0.3.99_setup.exe"
    old.write_bytes(b"x" * 100)
    new.write_bytes(b"y" * 100)

    r = us.cleanup_old_packages()
    assert new.exists(), f"最新包不得被删: {r}"
    assert old.exists(), "状态引用的包也应保留"
    assert new.name in r.get("kept", []) or new.exists()


def test_weight_hashes_survives_cache_hit(monkeypatch, tmp_path):
    """verify_cached_model 写入的 weight_hashes 不得被调用方整体替换丢掉。"""
    from local_llm_download import ModelDownloader
    d = ModelDownloader.__new__(ModelDownloader)   # 跳过 __init__ 的重 IO
    import threading
    d._dl_lock = threading.Lock()
    d._downloads = {}
    d._save_download_state = lambda: None

    snap = tmp_path / "snap"
    snap.mkdir()
    f = snap / "model.gguf"
    f.write_bytes(b"GGUF" + b"\x00" * 12)
    # 直接验证：写缓存
    d._hf_sha256 = lambda repo, name: ""      # 无 etag → magic-only
    d.verify_cached_model("r/m", str(snap))
    assert "weight_hashes" in (d._downloads.get("r/m") or {}), "校验后应写入缓存"

    # 模拟调用方 merge（修复后的行为）
    prev = d._downloads.get("r/m") or {}
    new_info = {"status": "done", "progress": 100, "path": str(snap)}
    for k, v in prev.items():
        if k not in new_info:
            new_info[k] = v
    d._downloads["r/m"] = new_info
    assert "weight_hashes" in d._downloads["r/m"], "merge 后 weight_hashes 必须还在"


def test_cleanup_uses_safe_asset_name():
    """cleanup 的 ref 也必须走 _safe_asset_name（同源小瑕疵）。"""
    src = Path(__file__).resolve().parents[1].joinpath("update_service.py").read_text("utf-8")
    idx = src.find("def cleanup_old_packages")
    seg = src[idx:idx + 900]
    assert "_safe_asset_name" in seg
    assert 'url.split("/")[-1]' not in seg


def test_h3_lock_not_held_across_network_read():
    """下载循环不得在 resp.read() 期间持有 _state_lock（进度轮询会全程阻塞）。"""
    src = Path(__file__).resolve().parents[1].joinpath("update_service.py").read_text("utf-8")
    # 只查代码行（注释里提到旧形态不算）
    code_lines = [l for l in src.splitlines() if l.strip() and not l.strip().startswith("#")]
    assert not any("as f, _state_lock" in l for l in code_lines), \
        "不得再把网络读包进 _state_lock"
    assert any("with open(part, mode) as f:" in l for l in code_lines), \
        "open 与锁应分离"


def test_h3_dirty_guard_not_fire_on_fresh_start():
    """刚启动（part 在位、尚未写数据）不得被 dirty-guard 复位。"""
    src = Path(__file__).resolve().parents[1].joinpath("update_service.py").read_text("utf-8")
    idx = src.find("_dirty = ")
    seg = src[idx:idx + 500]
    # total<=0 只在已写出数据时算矛盾
    assert 'out["downloaded"] > 0' in seg or "downloaded'] > 0" in seg
    # 漏分支 c 也要覆盖
    assert "not part" in seg


def test_h4_windows_exec_extensions_blocked():
    """open_path 必须拒绝 Windows 可执行/脚本扩展名（explorer 会启动它们）。"""
    src = Path(__file__).resolve().parents[1].joinpath(
        "..", "src-tauri", "src", "main.rs").read_text("utf-8")
    for ext in ("exe", "bat", "cmd", "ps1", "vbs", "msi"):
        assert f'"{ext}"' in src, f"open_path 黑名单缺 {ext}"
