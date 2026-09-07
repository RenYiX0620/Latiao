"""v1/v2 循环已由薄循环（agent/loop.py）取代（Stage 4）。

本文件保留：_StubEngine（测试通用引擎桩，test_thin_loop 复用）与
流注册平衡测试（transport 层机制，不随循环消亡）。
"""
import time

import httpx
import pytest

from tests.fake_engine import FakeEngine


class _StubEngine:
    """引擎桩：测试绝对不允许触碰真实 local_llm._engine。所有状态为
    "已停/禁用"，恢复路径永远不启动。"""

    current_model_id = ""
    server_status = "stopped"
    _auto_reloading = True
    _explicit_stop = True

    def mark_engine_busy(self, *a, **k):
        pass

    def mark_stream_enter(self):
        pass

    def mark_stream_exit(self):
        pass

    def mark_engine_idle(self):
        pass

    def _kill_port(self, *a):
        pass

    def _request_reload(self, *a):
        return False


@pytest.mark.asyncio
async def test_stream_registration_balance_with_suspect():
    """09-05 23:31 引擎被误杀回归：suspect 验证路径 enter/exit 只配对一次——
    请求进行中引擎保持"忙"注册，退出后计数归零。"""
    import agent_loop
    import agent.transport as transport
    import local_llm
    counters = {"busy": 0, "idle": 0, "streams": 0}

    class _CountingEngine(_StubEngine):
        current_model_id = "fake-model"

        def mark_engine_busy(self, *a, **k):
            counters["busy"] += 1

        def mark_engine_idle(self):
            counters["idle"] += 1

        def mark_stream_enter(self):
            counters["streams"] += 1

        def mark_stream_exit(self):
            counters["streams"] -= 1

    old_engine = local_llm._engine
    local_llm._engine = _CountingEngine()
    transport._llm_suspect_since = time.monotonic()
    try:
        with FakeEngine() as engine:
            engine.push(engine.text_response("ok"))
            engine.push(engine.text_response("hello"))
            body = {"model": "fake-model", "stream": True,
                    "messages": [{"role": "user", "content": "hi"}]}
            got_chunk = False
            async with httpx.AsyncClient(timeout=httpx.Timeout(60)) as client:
                async with transport._local_llm_stream(
                        client, engine.url, body, {"Authorization": "Bearer fake"}) as r:
                    assert counters["streams"] == 1, (
                        f"流进行中 _active_local_streams 应为 1：{counters}")
                    aiter = r.aiter_lines()
                    while True:
                        line = await aiter.__anext__()
                        if line.startswith("data: "):
                            got_chunk = True
                            if line[6:] == "[DONE]":
                                break
            assert got_chunk, "真实流未收到任何 chunk"
            assert counters["streams"] == 0, f"退出后计数必须归零：{counters}"
            assert counters["busy"] == 1, f"busy 只能标记一次：{counters}"
    finally:
        transport._llm_suspect_since = None
        local_llm._engine = old_engine
