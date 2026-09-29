"""通道（2026-09-29）：飞书消息解析、桥接会话 id、入向端点校验。

不联网、不装 lark-oapi 也能跑（适配器显式降级）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LATIAO_AUTH_TOKEN", "channels-token")


# ── 会话 id（稳定、可读、按通道隔离）──
def test_session_id_stable_and_sanitized():
    from channels_bridge import channel_session_id
    a = channel_session_id("feishu", "oc_abc123")
    b = channel_session_id("feishu", "oc_abc123")
    assert a == b, "同一通道同一会话必须得到同一 id（多轮上下文靠它）"
    assert a.startswith("channel:feishu:")
    # 非法字符被清掉，不会污染会话 id
    dirty = channel_session_id("we chat", "id/with:slash")
    assert " " not in dirty and "/" not in dirty
    assert channel_session_id("feishu", "x") != channel_session_id("wechat", "x"), "通道间隔离"


# ── 飞书消息解析 ──
def test_extract_text_from_feishu_message():
    from channels.feishu import _extract_text
    import json
    msg = {"message_type": "text",
           "content": json.dumps({"text": "帮我记一笔：9月29日 打车 38 元"})}
    assert _extract_text(msg) == "帮我记一笔：9月29日 打车 38 元"


def test_extract_text_ignores_non_text():
    from channels.feishu import _extract_text
    assert _extract_text({"message_type": "image", "content": "{}"}) == ""
    assert _extract_text({"message_type": "text", "content": "not-json"}) == ""
    assert _extract_text({}) == ""


# ── 配置读取（未启用 → 不启动，且不报错）──
def test_feishu_not_started_without_config(monkeypatch):
    import channels.feishu as f
    monkeypatch.setattr(f, "_started", False)
    monkeypatch.setattr(f, "_read_cfg", lambda: {})
    assert f.start() is False, "未配置时 start() 必须安静返回 False"


def test_feishu_degrades_without_sdk(monkeypatch):
    import channels.feishu as f
    monkeypatch.setattr(f, "_started", False)
    monkeypatch.setattr(f, "_read_cfg", lambda: {"enabled": True, "app_id": "cli_x", "app_secret": "s"})
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def _fake_import(name, *a, **k):
        if name == "lark_oapi":
            raise ImportError("no lark")
        return real_import(name, *a, **k)

    monkeypatch.setattr("builtins.__import__", _fake_import)
    assert f.start() is False, "缺 lark-oapi 时必须降级返回 False（不抛异常）"


# ── 入向端点校验 ──
@pytest.mark.asyncio
async def test_inbound_validates_required_fields():
    from fastapi import FastAPI
    from starlette.testclient import TestClient
    import api_routes_channels as m
    app = FastAPI()
    app.include_router(m.router)
    client = TestClient(app)
    r = client.post("/v1/channels/inbound", json={"channel": "wechat"})
    assert r.status_code == 200 and r.json()["status"] == "error"
    r2 = client.post("/v1/channels/inbound", json={"channel": "wechat", "chat_id": "c1", "text": "  "})
    assert r2.json()["status"] == "error" and "text" in r2.json()["message"]


@pytest.mark.asyncio
async def test_inbound_calls_bridge(monkeypatch):
    import channels_bridge as cb
    from fastapi import FastAPI
    from starlette.testclient import TestClient
    import api_routes_channels as m

    async def _fake(channel, chat_id, text, agent="latiao"):
        return f"[{channel}/{chat_id}] 收到：{text}"

    monkeypatch.setattr(cb, "run_channel_turn", _fake)
    app = FastAPI()
    app.include_router(m.router)
    client = TestClient(app)
    r = client.post("/v1/channels/inbound",
                    json={"channel": "wechat", "chat_id": "wx1", "text": "记一笔"})
    body = r.json()
    assert body["status"] == "ok"
    assert body["reply"] == "[wechat/wx1] 收到：记一笔"
    assert body["session_id"] == "channel:wechat:wx1"
