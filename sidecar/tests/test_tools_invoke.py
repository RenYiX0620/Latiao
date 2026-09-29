"""UI 直调工具 /v1/tools/invoke —— 与 Agent 同源（2026-09-29 Action 同源）。

契约：
- 与 Agent 共用 execute_tool，不是另写一套业务逻辑
- confirm 级工具必须 confirmed:true（UI 点击即用户确认）
- 结果写入 tool_calls 台账（与 Agent 路径同一张表）
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LATIAO_AUTH_TOKEN", "invoke-token")

from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture()
def client():
    import main as mi
    mi.AUTH_TOKEN = "invoke-token"
    return TestClient(mi.app, raise_server_exceptions=False)


def _h():
    return {"X-Latiao-Token": "invoke-token"}


def test_invoke_unknown_tool(client):
    r = client.post("/v1/tools/invoke", json={"name": "no_such_tool", "args": {}}, headers=_h())
    assert r.status_code == 200
    assert r.json()["status"] == "error"


def test_invoke_safe_tool_runs_same_path(client, monkeypatch):
    from agent import tool_exec as te
    calls = []

    async def fake_exec(name, args):
        calls.append((name, args))
        return "ok-from-execute_tool"

    recorded = []
    monkeypatch.setattr(te, "execute_tool", fake_exec)
    monkeypatch.setattr(te, "_record_tool_call_db",
                        lambda sid, n, a, r: recorded.append((sid, n, r)))

    r = client.post("/v1/tools/invoke",
                    json={"name": "list_dir", "args": {"path": "."}},
                    headers=_h())
    data = r.json()
    assert data["status"] == "ok"
    assert data["result"] == "ok-from-execute_tool"
    assert calls == [("list_dir", {"path": "."})]
    assert recorded and recorded[0][1] == "list_dir"


def test_invoke_confirm_tool_requires_flag(client):
    from agent_loop import TOOL_PERMISSIONS
    # 找一个 confirm 级工具（write_file 是）
    assert TOOL_PERMISSIONS.get("write_file") == "confirm"
    r = client.post("/v1/tools/invoke",
                    json={"name": "write_file", "args": {"path": "/tmp/x", "content": "y"}},
                    headers=_h())
    data = r.json()
    assert data["status"] == "need_confirm"
    assert data["permission"] == "confirm"


def test_invoke_confirm_tool_with_confirmed(client, monkeypatch):
    from agent import tool_exec as te

    async def fake_exec(name, args):
        return "written"

    monkeypatch.setattr(te, "execute_tool", fake_exec)
    monkeypatch.setattr(te, "_record_tool_call_db", lambda *a, **k: None)

    r = client.post("/v1/tools/invoke",
                    json={"name": "write_file",
                          "args": {"path": "/tmp/x", "content": "y"},
                          "confirmed": True},
                    headers=_h())
    assert r.json()["status"] == "ok"


def test_recent_ledger_endpoint_exists(client):
    r = client.get("/v1/memory/recent?limit=3", headers=_h())
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert "records" in r.json()
