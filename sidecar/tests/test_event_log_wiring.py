"""阶段 1 事件日志接线测试：SSE 消费层 turn 边界（api_routes._logged_agent_turn）。

覆盖：正常完成 / 取消（aborted）/ 异常（error + 异常透传）。
日志实例经 session_log._default_conn 注入内存 SQLite，避免写用户真实 memory.db。
"""
import sqlite3

import pytest

import session_log
from session_log import SessionLog


@pytest.fixture()
def conn(monkeypatch):
    conn = sqlite3.connect(":memory:")
    monkeypatch.setenv("LATIAO_EVENT_LOG", "1")
    monkeypatch.setattr(session_log, "_default_conn", lambda: conn)
    yield conn
    conn.close()


@pytest.fixture()
def logged_turn(monkeypatch):
    # 延迟导入：api_routes 依赖较重（main/fastapi），仅在测试环境需要
    from api_routes import _logged_agent_turn
    return _logged_agent_turn


async def _collect(agen):
    return [item async for item in agen]


@pytest.mark.asyncio
async def test_completed_turn(logged_turn, conn):
    async def inner():
        yield {"event": "content", "content": "hi"}
        yield {"event": "content", "content": " done"}

    events = await _collect(logged_turn("sess-1", [{"role": "user", "content": "hello"}], inner()))
    assert [e["event"] for e in events] == ["content", "content"]
    log = SessionLog.load("sess-1", conn=conn)
    assert [e.type for e in log] == ["turn/start", "user/message", "turn/end"]
    assert log[-1].data == {"reason": "completed"}
    assert log[1].data == {"text": "hello"}


@pytest.mark.asyncio
async def test_aborted_turn(logged_turn, conn):
    from agent_loop import _request_session_cancel, _clear_session_cancel

    async def inner():
        yield {"event": "content", "content": "partial"}
        _request_session_cancel("sess-2")

    await _collect(logged_turn("sess-2", [{"role": "user", "content": "stop me"}], inner()))
    log = SessionLog.load("sess-2", conn=conn)
    assert log[-1].type == "turn/end"
    assert log[-1].data == {"reason": "aborted"}
    _clear_session_cancel("sess-2")


@pytest.mark.asyncio
async def test_error_turn_propagates(logged_turn, conn):
    class Boom(Exception):
        pass

    async def inner():
        yield {"event": "content", "content": "partial"}
        raise Boom("engine died")

    with pytest.raises(Boom):
        await _collect(logged_turn("sess-3", [], inner()))
    log = SessionLog.load("sess-3", conn=conn)
    assert log[-1].data == {"reason": "error"}


def test_event_log_for_cached_keeps_seq_contiguous(conn):
    from agent_loop import _event_log_for
    log_a = _event_log_for("sess-cache")
    log_b = _event_log_for("sess-cache")
    assert log_a is log_b
    log_a.append("turn/start", {"turn": 1})
    log_b.append("step/start", {"turn": 1, "step": 1})
    assert log_a._events[1].seq == 1  # 连续序号来自同一实例，而非各实例从 0 起步
    assert [e.type for e in log_a._events] == ["turn/start", "step/start"]


@pytest.mark.asyncio
async def test_tool_call_and_result_paired(monkeypatch, conn):
    import agent_loop

    async def _dummy_tool(args):
        return "tool output"

    monkeypatch.setitem(agent_loop.TOOL_DISPATCH, "dummy-tool", _dummy_tool)
    tc = {"id": "call-1", "function": {"name": "dummy-tool", "arguments": "{}"}}
    verify_failed, events = await agent_loop._handle_tool_execution(
        tc, [], "sess-tool", "latiao", access_mode="full",  # full 档免确认，验证执行配对
    )
    assert verify_failed is False
    assert any(isinstance(ev, dict) and ev.get("event") == "tool_end" for ev in events)
    log = SessionLog.load("sess-tool", conn=conn)
    types = [e.type for e in log]
    assert types == ["tool/call", "tool/result"]
    call_ev, result_ev = log
    assert call_ev.data["name"] == "dummy-tool"
    assert call_ev.data["call_id"] == "call-1"
    assert result_ev.surface_op == "append"
    assert result_ev.source_seqs == (call_ev.seq,)  # 结果引用其调用事件
    assert "tool output" in result_ev.data["result"]

