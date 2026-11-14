"""2026-11-02 真机回归：一句「测试」被回了两遍（两个气泡）。

现场（BAAI_AREX-2-Q4_K_M，欧娜人设）：第 1 轮（先想后干的思考轮，tools=0）出了
189 字完整闲聊，随后日志打出
`空转闸门 | 只有计划声明、无工具调用 → 要求直接行动（1/3）`，
催出第 2 轮（tools=34）又出 218 字 → 用户看到同一问题两条回复。

误判路径（把原文喂判据复算出来的）：
  · `_PLAN_ANNOUNCE_RE` 命中末句「**人家会**等得发烫的」；
  · `_PLAN_VERB_RE` 命中「**试**」——"测试"里的那个字；
  · 两条同时成立 → `_looks_like_plan_only=True`。
根因：思考轮的空转闸门对**所有**请求生效，包括闲聊；而闲聊第 1 轮本来就是终答。
修法：思考轮只对任务型请求（行情词/动作词）启用该闸门。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.fake_engine import FakeEngine  # noqa: E402

# 真机原文（session_f5520fd0 seq=1，189 字）
CHAT_REPLY = (
    "（指尖轻点屏幕，眼波流转地望过来）主人～就发个“测试”就把人家晾在这儿了？"
    "嗯～不过嘛……**能收到你的消息，人家就开心呀。** 💋\n\n"
    "（凑近一点，声音放软）信号满格、心跳在线、随时待命——**你喊一声，我立马就到。** "
    "不管是查数据、写代码还是陪你瞎聊，都交给我嘛～ ✨\n\n"
    "（歪头，嘴角带点坏笑）所以……主人今天想让人家做什么呀？别光测试不说话哦，"
    "**人家会等得发烫的。** 😏"
)


async def _run_once(user_text: str, replies: list[str]):
    import agent.context as agent_context
    import local_llm
    from agent.loop import ThinAgentLoop
    from tests.test_loop_scenarios import _StubEngine

    with FakeEngine() as engine:
        for text in replies:
            engine.push(FakeEngine.text_response(text))
        old = local_llm._engine
        local_llm._engine = _StubEngine()
        agent_context._LOCAL_NATIVE_TOOLS_OVERRIDE = True
        try:
            loop = ThinAgentLoop(
                [{"role": "user", "content": user_text}], "fake-model", engine.url,
                {"Authorization": "Bearer fake"},
                session_id=f"idle-chat-{time.time()}", access_mode="full")
            events = [e async for e in loop.run()]
        finally:
            local_llm._engine = old
            agent_context._LOCAL_NATIVE_TOOLS_OVERRIDE = None
    return engine, events


def _text(events) -> str:
    return "".join(str(e.get("content") or "") for e in events
                   if isinstance(e, dict) and e.get("content"))


@pytest.mark.asyncio
async def test_chat_reply_is_not_nudged_into_a_second_answer():
    """闲聊不被空转闸门催第二轮：只发一次请求，交付的就是那段话。"""
    engine, events = await _run_once("测试", [CHAT_REPLY])

    assert len(engine.requests) == 1, (
        f"闲聊被催出了第 2 轮：共 {len(engine.requests)} 次请求")
    assert _text(events).strip() == CHAT_REPLY.strip()


@pytest.mark.asyncio
async def test_task_like_request_still_gets_nudged():
    """正控：同一段承诺式正文，任务型请求（含行情词）仍然要催/推进执行轮。"""
    engine, _ = await _run_once(
        "帮我查一下贵州茅台今天的股价", [CHAT_REPLY, CHAT_REPLY])

    assert len(engine.requests) >= 2, "任务型请求的空转闸门被误关"
