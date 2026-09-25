"""路由 smoke 测（2026-09-24，god file 拆分安全网）。

对 app 里**每一条**路由发一次最小请求，断言：
- 不是 500（NameError/ImportError 类拆分事故会直接 500）
- 或 500 但 traceback 里没有 NameError/ImportError（业务错可容忍）

拆 api_routes / local_llm 前后都跑它——拆坏 import 会立刻红。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LATIAO_AUTH_TOKEN", "smoke-token")

from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="module")
def client():
    import main as m
    m.AUTH_TOKEN = "smoke-token"  # 钉死，防被其它测试模块的 token 覆盖
    app = m.app
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(scope="module")
def headers():
    return {"X-Latiao-Token": "smoke-token", "Content-Type": "application/json"}


_PATH_PARAM_VALUES = {
    "job_id": "smoke-job",
    "agent_id": "latiao",
    "session_id": "smoke-session",
    "task_id": "smoke-task",
    "name": "smoke-tool",
}


def _fill_path(path: str) -> str:
    out = path
    for k, v in _PATH_PARAM_VALUES.items():
        out = out.replace("{" + k + "}", v)
    return out


def _collect_routes(app):
    from starlette.routing import Route
    out = []
    def walk(routes, prefix=""):
        for r in routes:
            # FastAPI include_router 后可能是 _IncludedRouter/Mount，递归展开
            inner = getattr(r, "routes", None) or getattr(getattr(r, "original_router", None), "routes", None)
            if inner and not isinstance(r, Route):
                walk(inner, prefix)
                continue
            if isinstance(r, Route) and r.methods:
                for method in r.methods:
                    if method in ("GET", "POST", "PUT", "DELETE", "PATCH"):
                        out.append((method, prefix + r.path))
    walk(app.routes)
    return sorted(set(out))


def test_every_route_survives_minimal_request(client, headers):
    import main as m
    routes = _collect_routes(m.app)
    assert len(routes) >= 100, f"路由数量异常：{len(routes)}"

    bad: list[str] = []
    for method, path in routes:
        url = _fill_path(path)
        body = {} if method in ("POST", "PUT", "PATCH") else None
        # 个别端点带 query 参数要求
        params = {}
        if "memory/search" in path:
            params["q"] = "x"
        try:
            if body is not None:
                resp = client.request(method, url, headers=headers, json=body, params=params)
            else:
                resp = client.request(method, url, headers=headers, params=params)
        except Exception as e:  # noqa: BLE001 — 拆分事故也在这里暴露
            bad.append(f"{method} {url} -> EXC {type(e).__name__}: {e}")
            continue
        text = resp.text or ""
        fatal = ("NameError" in text) or ("ImportError" in text) or ("is not defined" in text)
        if resp.status_code >= 500 and fatal:
            bad.append(f"{method} {url} -> {resp.status_code} {text[:120]}")
    assert not bad, "路由拆分/import 事故：\n" + "\n".join(bad[:20])


def test_health_exempt_from_auth(client):
    resp = client.get("/health")
    assert resp.status_code == 200


def test_auth_required_for_v1(client):
    resp = client.get("/v1/tools")
    assert resp.status_code == 401
