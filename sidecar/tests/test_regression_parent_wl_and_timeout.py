"""parent_whitelist 接线 / 工具超时入史 / 空白名单语义（2026-10-11 深审 Medium）。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LATIAO_AUTH_TOKEN", "med-wl-token")


def test_parent_whitelist_wired_via_contextvar():
    """delegate_task 必须从执行上下文取 parent_whitelist 并传给 run_sub_agent。"""
    src = Path(__file__).resolve().parents[1].joinpath("agent", "subagent.py").read_text("utf-8")
    assert "CURRENT_TOOL_WHITELIST" in src
    assert "parent_whitelist=_parent_wl" in src
    # tool_exec 派发前 set
    src2 = Path(__file__).resolve().parents[1].joinpath("agent", "tool_exec.py").read_text("utf-8")
    assert "CURRENT_TOOL_WHITELIST.set" in src2
    assert "CURRENT_TOOL_WHITELIST.reset" in src2


def test_tool_timeout_writes_tool_message():
    """超时路径必须写 role:tool 消息（模型才知道工具超时了）。"""
    src = Path(__file__).resolve().parents[1].joinpath("agent", "tool_exec.py").read_text("utf-8")
    idx = src.find("asyncio.TimeoutError")
    seg = src[idx:idx + 800]
    assert '"role": "tool"' in seg or "'role': 'tool'" in seg


def test_empty_whitelist_message_is_clear():
    """空白名单（非 None）拒绝时给出清晰文案，不是空的"可用工具："。"""
    import asyncio

    from agent import tool_exec as TE

    async def run():
        msgs = []
        call = {"id": "c1", "type": "function",
                "function": {"name": "read_file", "arguments": '{"path": "/tmp/x"}'}}
        ok, ev = await TE._handle_tool_execution(
            call, msgs, "s", "latiao", "full", tool_whitelist=set())
        return ok, ev

    ok, ev = asyncio.run(run())
    res = next((e.get("result", "") for e in ev if isinstance(e, dict)), "")
    assert "白名单为空" in res or "不允许任何工具" in res, res
    assert ok is False
