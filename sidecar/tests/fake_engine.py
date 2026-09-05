"""假引擎（test-only）：OpenAI 兼容 SSE 桩服务器，驱动真实 agent 循环。

用法（场景脚本化）：
    with FakeEngine() as engine:
        engine.script = [
            engine.text_response("你好"),                       # 第 1 次请求
            engine.tool_response("run_cmd", {"cmd": "ls -la"}), # 第 2 次请求（工具轮）
            engine.text_response("完成"),                        # 第 3 次请求（工具结果后）
        ]
        # 驱动真实循环连 engine.url —— 桩收集所有请求体，测试断言工具轮次。
"""
from __future__ import annotations

import json
import queue
import threading
import time
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


class FakeEngine:
    """一次启动一个桩服务器；script 为请求响应序列（LIFO 消费，生产顺序）。"""

    def __init__(self):
        self._app = FastAPI()
        self._requests: list[dict] = []
        self._script: queue.Queue = queue.Queue()
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self.url = ""

        @self._app.post("/v1/chat/completions")
        async def chat(req: Request):
            body = await req.json()
            self._requests.append(body)
            try:
                entry = self._script.get(timeout=2)
            except queue.Empty:
                # 脚本耗尽：返回占位"已结束"（观测循环多打请求的意图，而非 500 惊吓）
                return StreamingResponse(
                    iter(FakeEngine.text_response("（桩脚本耗尽）")),
                    media_type="text/event-stream",
                )
            if callable(entry):
                lines = entry(body)
            else:
                lines = entry
            return StreamingResponse(
                iter(lines), media_type="text/event-stream",
                headers={"Cache-Control": "no-cache"},
            )

    @property
    def requests(self) -> list[dict]:
        return self._requests

    def __enter__(self) -> "FakeEngine":
        import socket
        # 先占用再释放以探测空闲端口（测试可容忍极小竞态窗口）
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        config = uvicorn.Config(
            self._app, host="127.0.0.1", port=port, log_level="error",
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.time() + 10
        while not self._server.started and time.time() < deadline:
            time.sleep(0.01)
        assert self._server.started, "fake engine failed to start"
        self.url = f"http://127.0.0.1:{port}/v1/chat/completions"
        return self

    def __exit__(self, *exc):
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)

    # ── 场景素材 ────────────────────────────────────────────────────────
    def push(self, entry):
        """追加一个响应（行列表或 callable(body)->行列表）。"""
        self._script.put(entry)

    @staticmethod
    def text_response(text: str) -> list[str]:
        return [
            _sse({"choices": [{"delta": {"content": text}, "index": 0}]}),
            _sse({"choices": [{"delta": {}, "finish_reason": "stop", "index": 0}]}),
            "data: [DONE]\n\n",
        ]

    @staticmethod
    def tool_response(name: str, args: dict) -> list[str]:
        return [
            _sse({"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_fake_1",
                 "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}},
            ]}, "index": 0}]}),
            _sse({"choices": [{"delta": {}, "finish_reason": "stop", "index": 0}]}),
            "data: [DONE]\n\n",
        ]

    @staticmethod
    def local_tool_response(name: str, args: dict) -> list[str]:
        """本地循环的提示词栅栏格式：```tool name\\n{json}\\n```。"""
        args_json = json.dumps(args, ensure_ascii=False)
        text = f"```tool {name}\n{args_json}\n```"
        return [
            _sse({"choices": [{"delta": {"content": text}, "index": 0}]}),
            _sse({"choices": [{"delta": {}, "finish_reason": "stop", "index": 0}]}),
            "data: [DONE]\n\n",
        ]

    @staticmethod
    def asserts_tool_result_present(expected_fragment: str):
        """回调：校验请求体携带工具结果（工具轮次回传的证明）。"""
        def _check(body: dict) -> list[str]:
            texts = json.dumps(body, ensure_ascii=False)
            assert expected_fragment in texts, \
                f"tool result missing in request body: {texts[:500]}"
            return FakeEngine.text_response("完成")
        return _check
