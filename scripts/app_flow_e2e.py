"""端到端链路验证脚本（代替不可靠的 pytest 线程化 SSE 读法）。

验证真实用户链路：/v1/chat/completions SSE → engine_route → agent_plan →
plan_confirm → 点击确认(双击幂等) → 执行继续；以及拒绝停止、取消。
用法：sidecar python 下直接运行；输出每步断言结果。
"""
from __future__ import annotations

import json
import os
import queue
import socket
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "sidecar"))

os.environ["LATIAO_AUTH_TOKEN"] = "e2e-token"  # 必须先于 api_routes 导入

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse, StreamingResponse  # noqa: E402


class Engine:
    """流式(SSE)+非流式(计划 JSON)双模式桩（请求日志）。"""

    def __init__(self):
        self.stream_script: queue.Queue = queue.Queue()
        self.plan_response = {"choices": [{"index": 0, "message": {"role": "assistant",
            "content": "1. 读取表格数据 2. 分析主要板块 3. 输出结论"}, "finish_reason": "stop"}]}
        self.requests: list = []
        self.app = FastAPI()
        self._register()
        self.server = None
        self.thread = None

    def _register(self):
        @self.app.api_route("/{p:path}", methods=["GET", "POST"])
        async def handle(req: Request, p: str):
            body = None
            try:
                body = await req.json()
            except Exception:
                pass
            self.requests.append({"path": p, "stream": body.get("stream") if isinstance(body, dict) else None, "body": body})
            if p == "v1/chat/completions" and isinstance(body, dict) and not body.get("stream"):
                return JSONResponse(self.plan_response)
            try:
                entry = self.stream_script.get(timeout=2)
            except queue.Empty:
                lines = [f"data: {json.dumps({'choices': [{'delta': {'content': '（桩耗尽）'}, 'index': 0}]}, ensure_ascii=False)}\n\n",
                         "data: [DONE]\n\n"]
            else:
                lines = entry(body) if callable(entry) else entry
            return StreamingResponse(iter(lines), media_type="text/event-stream")

    def start(self) -> str:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=port, log_level="error"))
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


def serve_app() -> str:
    import api_routes  # noqa: F401
    import main  # noqa: F401
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(api_routes.app, host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.01)
    assert server.started
    return f"http://127.0.0.1:{port}"


HEADERS = {"X-Latiao-Token": "e2e-token", "Content-Type": "application/json"}
BODY = {
    "model": "deepseek-v4-flash-vision-exp",
    "cloud_config": {"endpoint": "http://unused", "key": "k",
                     "model": "deepseek-v4-flash-vision-exp", "protocol": "openai"},
    "messages": [{"role": "user", "content": (
        "请分析我上传的板块资金分析表，输出详尽的板块轮动与资金流向分析报告。内容如下：\n"
        "板块 涨幅 成交额 主力资金 散户资金\n通信 +2.46% 1806 129 -45\nCPO概念 +3.59% 2343 124 -33")}],
    "stream": True,
    "access_mode": "plan",
}


def run_scenario(base: str, engine: Engine, action: str, approve: bool = True) -> list:
    """读 SSE 至完成；action: confirm/reject/none；流中点击。"""
    body = dict(BODY)
    body["session_id"] = f"e2e-{uuid.uuid4().hex[:10]}"
    events: list = []
    clicked = {"done": False}

    def click_when_plan():
        deadline = time.time() + 30
        while time.time() < deadline and not clicked["done"]:
            if any(ev.get("event") == "plan_confirm" for ev in events):
                plan_id = next(ev["call_id"] for ev in events if ev.get("event") == "plan_confirm")
                with httpx.Client(timeout=20) as c:
                    r = c.post(base + "/v1/confirm_tool",
                               json={"call_id": plan_id, "approved": approve},
                               headers=HEADERS)
                print(f"   confirm({approve}) -> {r.json()}")
                clicked["done"] = True
                return
            time.sleep(0.1)

    if action in ("confirm", "reject"):
        threading.Thread(target=click_when_plan, daemon=True).start()

    with httpx.Client(timeout=120) as c:
        with c.stream("POST", base + "/v1/chat/completions", json=body, headers=HEADERS) as r:
            print("   HTTP", r.status_code)
            for line in r.iter_lines():
                if line.startswith("data: "):
                    p = line[6:]
                    if p == "[DONE]":
                        break
                    try:
                        events.append(json.loads(p))
                    except json.JSONDecodeError:
                        pass
    clicked["done"] = True
    return events


def main():
    engine = Engine()
    fake_url = engine.start() + "/v1/chat/completions"
    import api_routes
    async def fake_target(cfg):
        return ("openai", fake_url, {"Authorization": "Bearer fake"}, False)
    api_routes._resolve_api_target = fake_target
    base = serve_app()
    print("== 场景1: 计划确认 + 双击幂等 ==")
    engine.stream_script.put([
        f"data: {json.dumps({'choices': [{'delta': {'content': '执行完成，分析如上。' * 6}, 'index': 0}]}, ensure_ascii=False)}\n\n",
        "data: [DONE]\n\n"])
    evs = run_scenario(base, engine, "confirm")
    kinds = [ev.get("event", "?") for ev in evs]
    ok = ("agent_plan" in kinds and "plan_confirm" in kinds
          and any("执行完成" in str(ev.get("content", "")) for ev in evs))
    print(f"   事件: {kinds[:8]} | 结果: {'✅' if ok else '❌'}")
    if not ok:
        print("   events:", json.dumps(evs[:6], ensure_ascii=False)[:600])

    print("== 场景2: 拒绝计划应停止 ==")
    evs2 = run_scenario(base, engine, "reject", approve=False)
    ok2 = any("计划已被拒绝" in str(ev.get("content", "")) for ev in evs2)
    print(f"   结果: {'✅' if ok2 else '❌'}")
    if not ok2:
        print("   events:", json.dumps(evs2[:4], ensure_ascii=False)[:400])

    scenario_parallel_empty_names(base, engine)
    engine.stop()



def scenario_parallel_empty_names(base: str, engine: Engine):
    """09-21 22:15 复现场景：并行两个空名 tool_calls → 守卫反馈 →
    第二轮请求应带 parallel_tool_calls: false → 完成。"""
    body = dict(BODY)
    body["access_mode"] = "full"          # 本场景专测空名重试，绕过计划门
    body["messages"] = [{"role": "user", "content": "帮我找到这个xlsx文件"}]
    body["session_id"] = f"e2e-par-{uuid.uuid4().hex[:10]}"
    req_start = len(engine.requests)
    # 并行双空名（path 多候选不可恢复 → 走守卫反馈）
    dual_payload = {"choices": [{"delta": {"tool_calls": [
        {"index": 0, "id": "c1", "function": {"name": "", "arguments": json.dumps({"path": "/tmp"})}},
        {"index": 1, "id": "c2", "function": {"name": "", "arguments": json.dumps({"path": "/tmp"})}},
    ]}, "index": 0}]}
    dual = [f"data: {json.dumps(dual_payload, ensure_ascii=False)}\n\n",
            "data: [DONE]\n\n"]
    engine.stream_script.put(dual)
    engine.stream_script.put([
        f"data: {json.dumps({'choices': [{'delta': {'content': '好的，已按计划完成。' * 8}, 'index': 0}]}, ensure_ascii=False)}\n\n",
        "data: [DONE]\n\n"])
    events = []
    try:
        with httpx.Client(timeout=45) as c:
            with c.stream("POST", base + "/v1/chat/completions", json=body, headers=HEADERS) as r:
                for line in r.iter_lines():
                    if line.startswith("data: "):
                        p2 = line[6:]
                        if p2 == "[DONE]":
                            break
                        try:
                            events.append(json.loads(p2))
                        except json.JSONDecodeError:
                            pass
    except Exception as exc:
        print("   [场景3读取异常]", type(exc).__name__, str(exc)[:120])
        print("   engine requests:", [(q.get('stream'), (q.get('body') or {}).get('parallel_tool_calls')) for q in engine.requests[req_start:]][:6])
        print("   events so far:", [ev.get("event") for ev in events][:6])
        return False
    kinds = [ev.get("event", "?") for ev in events]
    # 服务端第二轮请求（stream 请求中寻找 parallel_tool_calls 键）
    stream_reqs = [q for q in engine.requests[req_start:] if q.get("stream")]
    second_has_flag = any((q.get("body") or {}).get("parallel_tool_calls") is False for q in stream_reqs)
    ev_text = " ".join(str(ev.get("content", "")) + " " + str(ev.get("result", "")) for ev in events)
    ok = ("工具名为空" in ev_text
          and second_has_flag
          and any("已按计划完成" in str(ev.get("content", "")) or "已按计划完成" in str(ev.get("result", "")) for ev in events))
    print(f"== 场景3: 并行空名→parallel=false ==")
    print(f"   事件: {kinds[:6]} | parallel_flag: {second_has_flag} | 结果: {'✅' if ok else '❌'}")
    if not ok:
        print("   engine bodies:", [(q.get('stream'), (q.get('body') or {}).get('parallel_tool_calls')) for q in engine.requests[req_start:]][:5])
    return ok



if __name__ == "__main__":
    main()
