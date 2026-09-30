"""轮次生命周期守卫（2026-09-30）——修 1：思考立即下发。

模型长时间只思考不写正文时，思考 delta 曾被"语言漂移闸"攒着（判据本来只看正文）
→ 后端零字节 → 前端 180s 看门狗误杀整轮（13:33 / 21:29 两次实测）。
本用例是**计时判据**：思考必须在正文出现之前就下发（旧实现会等满正文延迟）。

2a/2b（静默心跳 + 静默超限退出）第一版用"独立 task 包装行流"实现，把基础流式路径
打断了（test_thin_* 全红）→ 已回退，改为在 transport 的泵/队列内做，方案见
docs/zombie-turn-fix-plan.md。
"""
import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.loop import ThinAgentLoop  # noqa: E402


def _sse(payload: dict) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False)


def _reasoning_line(text: str) -> str:
    return _sse({"choices": [{"delta": {"reasoning": text}, "index": 0}]})


def _content_line(text: str) -> str:
    return _sse({"choices": [{"delta": {"content": text}, "index": 0}]})


def _loop_for_sample():
    loop = ThinAgentLoop.__new__(ThinAgentLoop)
    loop.session_id = "t-watchdog"
    loop.current_msgs = []
    loop.user_lang = "zh"
    loop.user_lang_confident = True
    loop._usage_source, loop._usage_parent = "main_turn", ""
    loop._lang_retried = False
    loop.steps = 1
    loop._step_log = lambda *a, **k: None
    loop._note_retry = lambda *a, **k: None
    return loop


@pytest.mark.asyncio
async def test_thinking_is_streamed_before_content(monkeypatch):
    """思考必须在**正文出现之前**就下发（旧实现把它攒到流末 → 前端 180s 误杀）。"""
    loop = _loop_for_sample()

    async def _stream(*_a, **_k):
        yield _reasoning_line("先想一想")
        await asyncio.sleep(0.35)
        yield _content_line("正文来了，足够长足够长足够长足够长")   # >30 字才会开闸

    monkeypatch.setattr(loop, "_stream", _stream)
    t0 = time.monotonic()
    seen_reasoning_at = None
    async for ev in loop._sample(None, {}):
        if isinstance(ev, dict) and ev.get("reasoning") and seen_reasoning_at is None:
            seen_reasoning_at = time.monotonic() - t0
    assert seen_reasoning_at is not None, "思考事件必须下发"
    assert seen_reasoning_at < 0.3, (
        f"思考必须立刻下发（实测等了 {seen_reasoning_at:.2f}s，旧实现会等满 0.35s 的正文延迟）")
