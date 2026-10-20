"""承诺闸门漏网与催促轮复读（2026-10-20 真机，occamy）。

现场（session_8ef3b444-9247-4e46-9a31-2eba46388d3f）：
① 用户"找找看" → 思考轮只回一句
   "好嘞～主人说找，那我就帮主人把官方安装指引找出来嘛。（动作）"
   → 无工具调用、无数据 → **直接交付**（用户："执行一半停的"）。
   漏网原因：这一句没有任何 我来/让我/这就/人家 意图词，只有"我…帮主人把…"；
   空转闸门的意图词表整条失明。
② 用户"mac上部署有案例吗？" → 同一句"……让我帮你查一下有没有相关案例嘛"
   在 3 次催促里原样重复，每轮气泡都留在前端 → 四个一模一样的"马上帮你查"。
   原因：催促轮只有**思考轮**会撤回气泡，执行段的催促轮不撤、也不做近重复抑制。
"""
import time

import pytest

from agent.loop import _looks_like_plan_only


# ── 判据层 ──────────────────────────────────────────────────────
def test_bang_zhuren_ba_promise_is_a_plan():
    body = ("（轻咬下唇，眼波流转）好嘞～主人说找，那我就帮主人把官方安装指引找出来嘛。"
            "（凑近屏幕，指尖在键盘上轻快地敲击）")
    assert _looks_like_plan_only(body, True) is True


def test_bang_ni_ba_promise_is_a_plan():
    assert _looks_like_plan_only("主人别急，人家这就帮您把数据整理出来。", True) is True


def test_completed_report_with_data_is_not_a_plan():
    """带数据的完成汇报不拦（末句是数据，不是"帮…把…"）。"""
    assert _looks_like_plan_only("我帮你查好了。上证收于 3707.48 点。", True) is False


def test_past_tense_help_without_ba_is_not_a_plan():
    """裸"帮"不算（"我帮你改好了"是完成时）；只认"帮X…把…"结构。"""
    assert _looks_like_plan_only("我帮你改好了", True) is False


# ── 端到端（假引擎台架）────────────────────────────────────────
async def _run_loop(user_text: str, responses: list, tag: str):
    """驱动真循环：返回 (事件列表, 拼接后的交付文本, 请求列表)。"""
    import agent.context as agent_context
    import local_llm
    from agent.loop import RoundTextAccumulator, ThinAgentLoop
    from tests.fake_engine import FakeEngine
    from tests.test_loop_scenarios import _StubEngine

    with FakeEngine() as engine:
        for r in responses:
            engine.push(r)
        old = local_llm._engine
        _old = agent_context._LOCAL_NATIVE_TOOLS_OVERRIDE
        local_llm._engine = _StubEngine()
        agent_context._LOCAL_NATIVE_TOOLS_OVERRIDE = True
        try:
            loop = ThinAgentLoop([{"role": "user", "content": user_text}],
                                 "fake-model", engine.url, {},
                                 session_id=f"{tag}-{time.time()}",
                                 access_mode="full", thinking_level="max")
            acc = RoundTextAccumulator()
            events = []
            async for ev in loop.run():
                acc.feed(ev)
                events.append(ev)
            return events, acc.join(), list(engine.requests)
        finally:
            local_llm._engine = old
            agent_context._LOCAL_NATIVE_TOOLS_OVERRIDE = _old


@pytest.mark.asyncio
async def test_promise_reaches_execution_round_2026_10_20():
    """真机复刻：思考轮一句"我帮主人把…找出来" → 必须进执行轮真调工具再作答。"""
    promise = ("（轻咬下唇，眼波流转）好嘞～主人说找，那我就帮主人把官方安装指引找出来嘛。"
               "（凑近屏幕，指尖在键盘上轻快地敲击）")
    answer = "官方安装指引在仓库 README 里：先装依赖，再按 OS 选择对应脚本。" * 2

    from tests.fake_engine import FakeEngine
    events, delivered, requests = await _run_loop(
        "找找看",
        [FakeEngine.text_response(promise),
         FakeEngine.native_tool_response("list_dir", {"path": "."}),
         FakeEngine.text_response(answer)],
        "promise-e2e")

    assert requests and not (requests[0].get("tools") or []), "思考轮不下发工具表"
    assert requests[1].get("tools"), "承诺句必须推进执行轮（恢复工具表）"
    assert any(e.get("event") == "tool_start" and e.get("tool") == "list_dir"
               for e in events), "执行轮必须真调工具"
    assert promise not in delivered, "承诺句不得进交付文本"
    assert "官方安装指引在仓库" in delivered, "终答必须交付"


@pytest.mark.asyncio
async def test_repeated_promise_bubbles_retracted_2026_10_20():
    """真机复刻：催促轮里同一句承诺重复 → 气泡撤回，交付里只剩终答。"""
    promise = "（轻咬下唇，眼波流转）主人想在 Mac 上部署呀～让我帮你查一下有没有相关案例嘛。"
    answer = "没有找到 Mac 部署的公开实测；建议先看官方仓库文档，再按内存情况判断。" * 2

    from tests.fake_engine import FakeEngine
    events, delivered, _requests = await _run_loop(
        "mac上部署有案例吗？",
        [FakeEngine.text_response(promise),
         FakeEngine.text_response(promise),
         FakeEngine.text_response(promise),
         FakeEngine.text_response(answer)],
        "dup-e2e")

    retractions = [e for e in events
                   if e.get("event") == "content_revised" and not e.get("content")]
    assert len(retractions) >= 3, f"重复的承诺气泡必须撤回（实际 {len(retractions)} 次）"
    assert promise not in delivered, "承诺句不得进交付文本"
    assert "没有找到 Mac 部署" in delivered, "终答必须交付"
