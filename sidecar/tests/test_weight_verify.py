"""权重完整性校验的回归测试（2026-10-11 审计修复）。

审计原文：GGUF 权重下载无 sha256/checksum —— 坏文件直接喂 C++ 解析器。
修法：按 HF 的 LFS etag（=文件 sha256）核对 + GGUF 魔数检查 + 已核验结果缓存。
**能力边界（如实钉住）**：etag 与文件可能同源于镜像，恶意镜像把两者一起换掉
抓不到；能抓住的是下载损坏/截断/中途篡改与"同一文件两次拉取不一致"。
"""
from __future__ import annotations

import hashlib
import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LATIAO_AUTH_TOKEN", "wverify-token")


def _downloader(tmp_path, monkeypatch):
    monkeypatch.setenv("LATIAO_TEST_PROGRESS_DIR", str(tmp_path))
    import importlib

    import config as cfg_mod
    importlib.reload(cfg_mod)
    import local_llm_download as m
    importlib.reload(m)
    d = m.ModelDownloader.__new__(m.ModelDownloader)   # 不跑 __init__（免建目录）
    d._downloads = {}
    d._download_lock = threading.Lock()
    d._dl_lock = threading.Lock()
    d._save_download_state = lambda: None
    return d


def test_magic_only_when_no_etag(tmp_path, monkeypatch):
    d = _downloader(tmp_path, monkeypatch)
    f = tmp_path / "m.gguf"
    f.write_bytes(b"GGUF" + b"\x00" * 64)
    monkeypatch.setattr(d, "_hf_sha256", lambda repo, name: "")
    r = d._verify_weight_file("r/m", f)
    assert r["ok"] and r["mode"] == "magic-only", r


def test_bad_gguf_magic_rejected(tmp_path, monkeypatch):
    d = _downloader(tmp_path, monkeypatch)
    f = tmp_path / "bad.gguf"
    f.write_bytes(b"NOTGGUF" + b"\x00" * 32)
    monkeypatch.setattr(d, "_hf_sha256", lambda repo, name: "")
    r = d._verify_weight_file("r/m", f)
    assert not r["ok"] and "魔数" in r["reason"]


def test_etag_match_and_mismatch(tmp_path, monkeypatch):
    d = _downloader(tmp_path, monkeypatch)
    f = tmp_path / "m.gguf"
    body = b"GGUF" + b"\x01" * 100
    f.write_bytes(body)
    good = hashlib.sha256(body).hexdigest()
    monkeypatch.setattr(d, "_hf_sha256", lambda repo, name: good)
    assert d._verify_weight_file("r/m", f)["mode"] == "verified"
    monkeypatch.setattr(d, "_hf_sha256", lambda repo, name: "0" * 64)
    r = d._verify_weight_file("r/m", f)
    assert not r["ok"] and "不匹配" in r["reason"]


def test_tamper_detected_against_pinned_etag(tmp_path, monkeypatch):
    """损坏/篡改检测：先按权威 etag 核验通过，文件随后被改 → 复验必须失败。"""
    d = _downloader(tmp_path, monkeypatch)
    f = tmp_path / "m.gguf"
    body = b"GGUF" + b"\x03" * 100
    f.write_bytes(body)
    pinned = hashlib.sha256(body).hexdigest()
    monkeypatch.setattr(d, "_hf_sha256", lambda repo, name: pinned)
    assert d.verify_cached_model("r/m", str(tmp_path))["ok"]
    # 文件被替换（内容变、长度也变）→ 与固化 etag 不再一致
    f.write_bytes(b"GGUF" + b"\x09" * 40)
    r = d.verify_cached_model("r/m", str(tmp_path))
    assert not r["ok"], "内容被改必须被抓出"


def test_verified_once_skips_rehash(tmp_path, monkeypatch):
    """已核验的期望值未变时跳过本地哈希重算（20GB 的 sha256 不能每次启动都算）。"""
    d = _downloader(tmp_path, monkeypatch)
    f = tmp_path / "m.gguf"
    body = b"GGUF" + b"\x05" * 64
    f.write_bytes(body)
    pinned = hashlib.sha256(body).hexdigest()
    calls = {"n": 0}

    def stub(repo, name):
        calls["n"] += 1
        return pinned

    monkeypatch.setattr(d, "_hf_sha256", stub)
    d.verify_cached_model("r/m", str(tmp_path))
    n1 = calls["n"]
    d.verify_cached_model("r/m", str(tmp_path))
    assert calls["n"] == n1 + 1, "第二次只取一次 etag（用于比对缓存），不再算本地哈希"
