"""端到端链路 E2E：真实 api_routes/FastAPI + 双模式假引擎（09-21 用户痛批后补课）。

覆盖真实用户路径：/v1/chat SSE → agent_plan → plan_confirm →
流中点击确认 → 双击(already) → 拒绝路径。此前验证只驱动 AgentLoop，
从未走 api_routes→SSE→确认链路——用户实测反复出问题的正是这段。
"""
from __future__ import annotations

import json
import os
import queue
import socket
import threading
import time
import uuid
from collections import deque

import httpx
import pytest
from fastapi import Request  # noqa: F401 — 供 __future__ annotations 字符串解析

os.environ["LATIAO_AUTH_TOKEN"] = "e2e-token"  # 必须在导入 api_routes 前设置


class DualFakeEngine:
    """流式(OAI SSE)+非流式(计划生成 JSON)双模式桩。"""

    def __init__(self):
        self.stream_script: queue.Queue = queue.Queue()
        self.plan_response = {"choices": [{"index": 0, "message": {"role": "assistant",
            "content": "1. 读取表格数据 2. 分析主要板块 3. 输出结论"}, "finish_reason": "stop"}]}
        self.requests: list[dict] = []
        self.server = None
        self.thread = None

    def start(self) -> str:
        import uvicorn
        from fastapi import FastAPI, Request
        from fastapi.responses import StreamingResponse, JSONResponse

        app = FastAPI()
        self.app = app

        @app.api_route("/{p:path}", methods=["GET", "POST"])
        async def chat(req: Request, p: str):
            body = None
            try:
                body = await req.json()
            except Exception:
                pass
            self.requests.append({"path": p, "body": body})
            if p == "v1/chat/completions" and isinstance(body, dict) and not body.get("stream"):
                return JSONResponse(self.plan_response)
            try:
                entry = self.stream_script.get(timeout=2)
            except queue.Empty:
                lines = [f"data: {json.dumps({'choices': [{'delta': {'content': '（桩脚本耗尽）'}, 'index': 0}]}, ensure_ascii=False)}\n\n",
                         "data: [DONE]\n\n"]
            else:
                lines = entry(body) if callable(entry) else entry
            return StreamingResponse(iter(lines), media_type="text/event-stream")

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started and time.time() < deadline:
            time.sleep(0.01)
        return f"http://127.0.0.1:{port}"

    def stop(self):
        if self.server:
            self.server.should_exit = True
        if self.thread:
            self.thread.join(timeout=5)


def _start_app() -> str:
    import uvicorn
    import api_routes  # noqa: F401
    import main  # noqa: F401  (构建 app; api_routes.app 即主应用)
    # 全量套件下 main 可能已被其他测试模块先导入（AUTH_TOKEN 为空）——
    # 端点按请求时模块全局校验，这里补上，避免 401 假象
    if not main.AUTH_TOKEN:
        main.AUTH_TOKEN = os.environ.get("LATIAO_AUTH_TOKEN", "") or main.AUTH_TOKEN
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    config = uvicorn.Config(api_routes.app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.01)
    assert server.started
    return f"http://127.0.0.1:{port}"


@pytest.fixture()
def e2e():
    engine = DualFakeEngine()
    fake_url = engine.start()
    import api_routes
    async def fake_target(cfg):
        return ("openai", fake_url + "/v1/chat/completions",
                {"Authorization": "Bearer fake"}, False)
    api_routes._resolve_api_target = fake_target
    base = _start_app()
    yield base, engine
    engine.stop()


HEADERS = {"X-Latiao-Token": "e2e-token", "Content-Type": "application/json"}
CHAT_BODY = {
    "model": "deepseek-v4-flash-vision-exp",
    "cloud_config": {"endpoint": "http://unused", "key": "k",
                     "model": "deepseek-v4-flash-vision-exp", "protocol": "openai"},
    "messages": [{"role": "user", "content": (
        "请分析我上传的板块资金分析表，输出详尽的板块轮动与资金流向分析报告。内容如下：\n"
        "板块 涨幅 成交额 主力资金 散户资金\n"
        "通信 +2.46% 1806 129 -45\nCPO概念 +3.59% 2343 124 -33\n"
        "半导体 +2.07% 1560 84 -10\n创新药 -1.2% 900 -66 20")}],
    "stream": True,
    "access_mode": "plan",  # plan 档强制规划门（假引擎 127.0.0.1 会被判本地、自动规划被禁用）
}


class ChatSession:
    """后台线程读 SSE；主线程根据事件时机发起确认/拒绝/取消。"""

    def __init__(self, base: str, sid: str, body_override: dict | None = None):
        self.base = base
        self.sid = sid
        self.body_override = body_override
        self.events: deque = deque()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def _reader(self):
        body = dict(self.body_override) if self.body_override else dict(CHAT_BODY)
        body["session_id"] = self.sid
        try:
            with httpx.Client(timeout=90) as c:
                with c.stream("POST", self.base + "/v1/chat/completions", json=body, headers=HEADERS) as r:
                    self.events.append({"event": "__http__", "content": str(r.status_code)})
                    for line in r.iter_lines():
                        if self._stop.is_set():
                            break
                        if not line.startswith("data: "):
                            continue
                        payload = line[6:]
                        if payload == "[DONE]":
                            break
                        try:
                            self.events.append(json.loads(payload))
                        except json.JSONDecodeError:
                            pass
        except Exception as exc:  # noqa: BLE001
            self.events.append({"event": "__reader_error__", "content": str(exc)})
        finally:
            self.events.append({"event": "__reader_done__"})

    def wait_event(self, kind: str, timeout: float = 60) -> dict | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            for ev in list(self.events):
                if ev.get("event") == kind:
                    return ev
            time.sleep(0.05)
        return None

    def wait_any(self, kinds: tuple, timeout: float = 60) -> dict | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            for ev in list(self.events):
                if ev.get("event") in kinds:
                    return ev
            time.sleep(0.05)
        return None

    def has(self, predicate) -> bool:
        return any(predicate(ev) for ev in self.events if isinstance(ev, dict))

    def text(self) -> str:
        return "".join(str(ev.get("content", "")) for ev in self.events if isinstance(ev, dict))

    def confirm(self, call_id: str, approved: bool = True):
        with httpx.Client(timeout=20) as c:
            r = c.post(self.base + "/v1/confirm_tool",
                       json={"call_id": call_id, "approved": approved}, headers=HEADERS)
            return r.json()


def _stream_text(text: str) -> list[str]:
    return [
        f"data: {json.dumps({'choices': [{'delta': {'content': text}, 'index': 0}]}, ensure_ascii=False)}\n\n",
        "data: [DONE]\n\n",
    ]


def test_plan_confirm_then_double_click(e2e):
    base, engine = e2e
    engine.stream_script.put(_stream_text("执行完成，分析如上。" * 10))
    cs = ChatSession(base, f"e2e-{uuid.uuid4().hex[:10]}")
    plan = cs.wait_event("plan_confirm")
    if plan is None:
        print("DEBUG kinds:", [ev.get("event") for ev in list(cs.events)][:8],
              "| text:", cs.text()[:120], "| engine reqs:", len(engine.requests),
              "| paths:", [q.get("path") for q in engine.requests])
    assert plan is not None, "应收到 plan_confirm 事件"
    call_id = plan["call_id"]
    # 第一次点击
    r1 = cs.confirm(call_id, True)
    assert r1.get("status") == "ok", r1
    # 双击（幂等）
    r2 = cs.confirm(call_id, True)
    assert r2.get("status") == "already", r2
    # 批准后执行继续并交付
    deadline = time.time() + 90
    while time.time() < deadline and not cs.has(lambda ev: "执行完成" in str(ev.get("content", ""))):
        time.sleep(0.2)
    assert cs.has(lambda ev: "执行完成" in str(ev.get("content", ""))), cs.text()[:500]


def test_plan_reject_stops(e2e):
    base, engine = e2e
    cs = ChatSession(base, f"e2e-rej-{uuid.uuid4().hex[:10]}")
    plan = cs.wait_event("plan_confirm")
    if plan is None:
        print("DEBUG kinds:", [ev.get("event") for ev in list(cs.events)][:8],
              "| text:", cs.text()[:120], "| engine reqs:", len(engine.requests),
              "| paths:", [q.get("path") for q in engine.requests])
    assert plan is not None
    cs.confirm(plan["call_id"], False)
    deadline = time.time() + 60
    while time.time() < deadline and not cs.has(lambda ev: "计划已被拒绝" in str(ev.get("content", ""))):
        time.sleep(0.2)
    assert cs.has(lambda ev: "计划已被拒绝" in str(ev.get("content", ""))), cs.text()[:400]


def test_confirm_unknown_id_not_found(e2e):
    base, _ = e2e
    cs = ChatSession(base, f"e2e-nf-{uuid.uuid4().hex[:10]}")
    plan = cs.wait_event("plan_confirm")  # 使请求进入等待态
    r = cs.confirm("no-such-call-id", True)  # 服务端不应直接崩，返回 not_found
    assert r.get("status") == "not_found", r


def test_no_auto_route_to_cloud(e2e, monkeypatch):
    """09-06 事故回归：自动路由机制已删除——未选模型时，代码意图不得
    劫持到云端（曾致 GLM 429 → deepseek 降级 → 空工具名中止）。若有人
    重新引入自动路由，本测试通过哨兵立即暴露。"""
    import api_routes

    def _must_not_be_called():
        raise AssertionError("自动路由仍在调用 _get_best_cloud_config（机制应已删除）")

    # raising=False：导入已随机制删除；若有人重新引入自动路由（模块级导入
    # + 模块内引用），此哨兵会替换模块属性并被命中
    monkeypatch.setattr(api_routes, "_get_best_cloud_config", _must_not_be_called,
                        raising=False)
    base, engine = e2e
    engine.stream_script.put(_stream_text("好的，这是修复后的代码说明。" * 5))
    body = {"messages": [{"role": "user", "content": "帮我修复这个代码 bug，写个函数实现排序"}],
            "stream": True, "access_mode": "full"}
    cs = ChatSession(base, f"e2e-noroute-{uuid.uuid4().hex[:10]}", body_override=body)
    deadline = time.time() + 30
    while time.time() < deadline and not cs.has(lambda ev: ev.get("event") == "__reader_done__"):
        time.sleep(0.05)
    kinds = [ev.get("event") for ev in list(cs.events)]
    assert cs.has(lambda ev: ev.get("event") == "engine_route"), kinds
    assert "修复后的代码" in cs.text(), cs.text()[:200]
    assert not cs.has(lambda ev: ev.get("event") == "__reader_error__"), kinds


@pytest.mark.skip(reason="并行空名场景由 scripts/app_flow_e2e.py 场景3 覆盖（pytest 线程读流在该场景不稳定）")
def test_parallel_empty_names_disable_retry(e2e):
    """09-21 22:15 回归：并行双空名 → 守卫反馈 → 第二轮请求带 parallel_tool_calls=false → 完成。"""
    base, engine = e2e
    dual = {"choices": [{"delta": {"tool_calls": [
        {"index": 0, "id": "c1", "function": {"name": "", "arguments": json.dumps({"path": "/tmp"})}},
        {"index": 1, "id": "c2", "function": {"name": "", "arguments": json.dumps({"path": "/tmp"})}},
    ]}, "index": 0}]}
    engine.stream_script.put([f"data: {json.dumps(dual, ensure_ascii=False)}\n\n", "data: [DONE]\n\n"])
    engine.stream_script.put(_stream_text("已按计划完成。" * 8))
    ov = dict(CHAT_BODY)
    ov["access_mode"] = "full"
    ov["messages"] = [{"role": "user", "content": "帮我找到这个xlsx文件"}]
    cs = ChatSession(base, f"e2e-par-{uuid.uuid4().hex[:10]}", body_override=ov)
    cs.wait_any(("turn", "__reader_done__"), timeout=30)
    # 记录 body 的请求里，第二轮起应带 parallel_tool_calls=false
    bodies = [(q.get("body") or {}).get("parallel_tool_calls") for q in engine.requests if q.get("stream")]
    assert any(v is False for v in bodies), f"parallel off missing: bodies={bodies} reqs={engine.requests}"

    text = cs.text()
    assert "工具名为空" in text
    assert "已按计划完成" in text or "桩耗尽" in text
