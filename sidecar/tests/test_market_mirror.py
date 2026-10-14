"""市场源可达性回退 + 失败回退旧缓存（2026-10-13）。

背景（用户实测）：扩展市场搜索搜不到官方条目。根因是官方源
`cdn.jsdelivr.net/gh/RenYiX0620/latiao-marketplace@main/marketplace.json`
在本机**直接连不上**（curl 000），而原实现唯一的兜底是 `raw.githubusercontent`
（本机实测 502）——两条都死，官方整块消失。实测只有 gh-proxy 镜像可达（200），
且它**只代理 GitHub 系域名**（给 jsDelivr 加前缀会 404）→ 顺序必须是
原址 → raw 等价物 → gh-proxy 前缀。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LATIAO_AUTH_TOKEN", "market-mirror-token")

from extension_manager import (  # noqa: E402
    _raw_equivalent,
    _url_candidates,
    fetch_marketplace,
)


def test_raw_equivalent_converts_ref_and_path():
    """jsDelivr 的 @ref 必须变成 /ref/（字符串替换会留下 @main → 404）。"""
    assert _raw_equivalent(
        "https://cdn.jsdelivr.net/gh/O/R@main/a/b.json"
    ) == "https://raw.githubusercontent.com/O/R/main/a/b.json"


def test_candidate_order_is_reachability_ordered():
    """原址 → raw 等价物 → gh-proxy 前缀；jsDelivr 不加前缀（实测 404）。"""
    cands = _url_candidates(
        "https://cdn.jsdelivr.net/gh/O/R@main/m.json")
    assert cands == [
        "https://cdn.jsdelivr.net/gh/O/R@main/m.json",
        "https://raw.githubusercontent.com/O/R/main/m.json",
        "https://gh-proxy.com/https://raw.githubusercontent.com/O/R/main/m.json",
    ]
    # raw / codeload 各自也有镜像变体
    assert "https://gh-proxy.com/https://raw.githubusercontent.com/O/R/main/x.zip" in _url_candidates(
        "https://raw.githubusercontent.com/O/R/main/x.zip")
    assert "https://gh-proxy.com/https://codeload.github.com/O/R/zip/refs/heads/main" in _url_candidates(
        "https://codeload.github.com/O/R/zip/refs/heads/main")


def test_fetch_marketplace_falls_back_to_the_mirror(monkeypatch):
    """清单抓取：前两个候选失败 → 第三个（gh-proxy）成功。"""
    import httpx

    calls: list[str] = []

    class _Resp:
        def __init__(self, ok: bool):
            self._ok = ok

        def raise_for_status(self):
            if not self._ok:
                raise httpx.ConnectError("blocked")

        def json(self):
            return {"name": "m", "plugins": [{"name": "finance-pack", "version": "1.0.0"}]}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            calls.append(url)
            return _Resp(ok=url.startswith("https://gh-proxy.com/"))

    monkeypatch.setattr(httpx, "Client", _Client)
    monkeypatch.setattr("extension_manager._load_installed", lambda: {"extensions": []})
    out = fetch_marketplace("https://cdn.jsdelivr.net/gh/O/R@main/m.json")
    assert out["status"] == "ok"
    assert [p["name"] for p in out["plugins"]] == ["finance-pack"]
    assert len(calls) == 3 and calls[-1].startswith("https://gh-proxy.com/"), calls


def test_fetch_marketplace_reports_error_only_when_all_candidates_fail(monkeypatch):
    import httpx

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            raise httpx.ConnectError("blocked")

    monkeypatch.setattr(httpx, "Client", _Client)
    out = fetch_marketplace("https://cdn.jsdelivr.net/gh/O/R@main/m.json")
    assert out["status"] == "error" and "拉取市场失败" in out["message"]


def test_source_cache_stale_if_error():
    """抓取失败时回退最近一次**成功**结果并标 stale；成功后缓存被刷新。"""
    from extension_manager import _SOURCE_CACHE, _cached_source_fetch

    key = "https://example.invalid/m.json"
    _SOURCE_CACHE.pop(key, None)
    good = {"status": "ok", "plugins": [{"name": "a"}]}
    assert _cached_source_fetch(key, lambda: good)["plugins"][0]["name"] == "a"
    # TTL 内直接命中缓存，不调 fetcher
    hit = _cached_source_fetch(key, lambda: (_ for _ in ()).throw(AssertionError("不该调用")))
    assert hit["status"] == "ok"
    # 让缓存过期 → fetcher 失败 → 回退旧的成功结果
    _SOURCE_CACHE[key] = (_SOURCE_CACHE[key][0] - 10**6, good)
    stale = _cached_source_fetch(key, lambda: {"status": "error", "message": "boom"})
    assert stale["status"] == "ok" and stale.get("stale") is True
    # 且旧的失败**不会**覆盖缓存：再来一次仍能回退
    _SOURCE_CACHE[key] = (_SOURCE_CACHE[key][0] - 10**6, good)
    assert _cached_source_fetch(key, lambda: {"status": "error", "message": "boom"}).get("stale") is True
    _SOURCE_CACHE.pop(key, None)


def test_source_cache_no_stale_returns_the_error():
    from extension_manager import _SOURCE_CACHE, _cached_source_fetch

    key = "https://example.invalid/none.json"
    _SOURCE_CACHE.pop(key, None)
    out = _cached_source_fetch(key, lambda: {"status": "error", "message": "boom"})
    assert out["status"] == "error" and out["message"] == "boom"
    assert key not in _SOURCE_CACHE, "失败结果不该进缓存（会顶掉旧的成功结果）"
