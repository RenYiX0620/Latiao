"""停止按钮必须同步收口子智能体（2026-09-24 用户报：点了结束任务详情仍「执行中」）。"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LATIAO_AUTH_TOKEN", "sub-cancel-token")


def test_cancel_marks_running_subtasks_and_cancels_task():
    import agent.subagent as sa

    sa._SUBTASKS.clear()
    sa._SUBTASK_CANCEL_HANDLES.clear()
    sa._SUBTASK_EVENTS.clear()

    cancelled = {"hit": False}

    class _T:
        def done(self):
            return False
        def cancel(self):
            cancelled["hit"] = True

    sa._SUBTASKS["sub_x"] = {
        "agent": "explore", "task": "analyze", "status": "running",
        "steps": 1, "result": "", "started_at": 1.0, "updated_at": 1.0,
        "session": "sess_a",
    }
    sa._SUBTASKS["sub_y"] = {
        "agent": "explore", "task": "other", "status": "running",
        "steps": 1, "result": "", "started_at": 1.0, "updated_at": 1.0,
        "session": "sess_b",
    }
    sa._SUBTASK_CANCEL_HANDLES["sub_x"] = _T()

    sa.cancel_subtasks_for_session("sess_a")

    assert sa._SUBTASKS["sub_x"]["status"] == "error"
    assert "中断" in sa._SUBTASKS["sub_x"]["result"]
    assert cancelled["hit"] is True, "必须 cancel 后台协程"
    assert sa._SUBTASKS["sub_y"]["status"] == "running", "别的会话的子任务不该被误杀"


def test_request_session_cancel_also_cancels_subtasks():
    from agent.session_events import _clear_session_cancel, _request_session_cancel
    import agent.subagent as sa

    sa._SUBTASKS.clear()
    sa._SUBTASK_CANCEL_HANDLES.clear()
    _clear_session_cancel("sess_c")
    sa._SUBTASKS["sub_z"] = {
        "agent": "explore", "task": "t", "status": "running",
        "steps": 0, "result": "", "started_at": 1.0, "updated_at": 1.0,
        "session": "sess_c",
    }
    _request_session_cancel("sess_c")
    assert sa._SUBTASKS["sub_z"]["status"] == "error"
    _clear_session_cancel("sess_c")


def test_child_session_cancel_follows_parent():
    from agent.session_events import (
        _clear_session_cancel, _request_session_cancel, _session_cancel_requested,
    )
    _clear_session_cancel("sess_d")
    _request_session_cancel("sess_d")
    assert _session_cancel_requested("sess_d")
    assert _session_cancel_requested("sess_d:sub_1")
    _clear_session_cancel("sess_d")


async def _swallow_cancel():
    import agent.subagent as sa
    sa._SUBTASKS.clear()
    sa._SUBTASK_CANCEL_HANDLES.clear()
    sa._SUBTASK_EVENTS.clear()
    sa._SUBTASKS["sub_c"] = {
        "agent": "explore", "task": "t", "status": "running",
        "steps": 0, "result": "", "started_at": 1.0, "updated_at": 1.0,
        "session": "sess_e",
    }

    async def _hang():
        await asyncio.sleep(60)

    t = asyncio.create_task(_hang())
    sa._SUBTASK_CANCEL_HANDLES["sub_c"] = t
    sa.cancel_subtasks_for_session("sess_e")
    try:
        await t
    except asyncio.CancelledError:
        pass
    assert sa._SUBTASKS["sub_c"]["status"] == "error"


def test_cancelled_bg_task_gets_error_status():
    asyncio.run(_swallow_cancel())
