"""2026-11-01 真机回归：长计划被当终答交付，执行轮从未发生。

现场（Nanbeige4.3-33B + 欧娜人设，行情/持仓问答）：每轮日志都是
`[THIN][step 1] 请求构建 … tools=0` / `采样完成 … 工具调用=0`，正文里却写着
"我马上用 market_insight 拉支撑压力位""按下回车"——思考轮没有工具表，模型把
"我要去查"写成散文；而正文引用了上一轮的涨跌百分比（-5.48%），旧口径先命中
"%" 数据判据，整条被判成"真汇报"交付，第 2 轮执行阶段从未发生。

本用例用桩引擎驱动**真实循环**：第 1 轮回一段 1000+ 字的承诺式计划，断言循环
必须进第 2 轮（带工具表）并真的执行一次工具，最后交付的是答案而不是那段计划。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.fake_engine import FakeEngine  # noqa: E402

REQUEST = "我手上是中天科技、紫光股份、诺德股份、莲花控股"
ANSWER = "【验收标记】以上是结合数据的完整回答。"

# 1000+ 字的"承诺式计划"：人设铺垫 + 引用上一轮的涨跌百分比 + 工具名 + 行动承诺。
PLAN = (
    "（凑近屏幕，把查询命令调出来，手指悬在回车键上）主人你看，我手里有现成的"
    "工具——market_insight，能查大盘支撑压力位，还有涨跌、成交、资金流向这些"
    "详细指标。但关键数字必须现在去查，不能凭记忆说。\n\n"
    "（指尖在屏幕上轻轻点了两下，眉头微皱）你看数据：科创50 连跌两天到 -5.48%"
    "（东方财富指数日线，10/30 收盘），芯片暗盘却在集体拉升，说明基本面没崩。\n\n"
    "（坐直身子，正经地说）所以我的意思是：反弹到什么程度抛掉筹码，取决于压力位"
    "和资金流向。如果反弹后触及压力位、主力资金转为净流出、成交量萎缩——那就是"
    "该减的信号；放量突破压力位就不抛，继续持有。\n\n"
    "（凑近屏幕，认真地说）第一档：反弹到前低附近，比如科创50 守住 1160 点一线，"
    "先减仓一半。这是修复性反弹，别指望它一口气涨回去，更别在恐慌最深处割肉。\n\n"
    "（指尖在暗盘拉升那行停住，轻轻点了两下）第二档：如果反弹后放量突破前高，"
    "或者资金持续流入，再考虑减仓。但这得看硬科技板块能不能借暗盘拉升止跌企稳，"
    "企稳信号出现，才是减仓的好时机。\n\n"
    "（歪头看你，语气有点得意）主人……你这么问，人家心里还挺开心的。原来你不是"
    "只想听个结论，是想听人家怎么想的呀？这可是正经的持仓判断呢。\n\n"
    "（指尖在屏幕上画了个圈，又收回）但主人，这些标准得用实时数据来套。你现在要"
    "查哪个标的？报个代码或者板块给我——我马上用 market_insight 拉支撑压力位，"
    "然后告诉你反弹到什么价位该减。😏"
)


async def _run():
    import agent.context as agent_context
    import local_llm
    from agent.loop import ThinAgentLoop
    from tests.test_loop_scenarios import _StubEngine

    with FakeEngine() as engine:
        engine.push(FakeEngine.text_response(PLAN))
        engine.push(FakeEngine.tool_response("list_dir", {"path": "."}))
        engine.push(FakeEngine.text_response(ANSWER))
        old = local_llm._engine
        local_llm._engine = _StubEngine()
        agent_context._LOCAL_NATIVE_TOOLS_OVERRIDE = True
        try:
            loop = ThinAgentLoop(
                [{"role": "user", "content": REQUEST}], "fake-model", engine.url,
                {"Authorization": "Bearer fake"}, session_id=f"plan-long-{time.time()}",
                access_mode="full")
            events = [e async for e in loop.run()]
        finally:
            local_llm._engine = old
            agent_context._LOCAL_NATIVE_TOOLS_OVERRIDE = None
    return engine, events


def _text(events) -> str:
    return "".join(str(e.get("content") or "") for e in events
                   if isinstance(e, dict) and e.get("content"))


@pytest.mark.asyncio
async def test_long_promise_plan_advances_to_execution_round():
    assert len(PLAN) > 600, "复现现场：计划必须长过旧护栏"
    engine, events = await _run()

    # 1) 思考轮之后必须还有请求：旧代码在这里就交付了（只会有 1 条请求）
    assert len(engine.requests) >= 3, (
        f"执行轮从未发生：只打了 {len(engine.requests)} 次请求")
    # 2) 第 2 轮必须带工具表（思考轮 tools=0 是有意为之）
    second = engine.requests[1]
    assert second.get("tools"), "第 2 轮没有工具表——计划仍被当成了终答"
    assert any(t.get("function", {}).get("name") == "list_dir"
               for t in second["tools"])
    # 3) 真的执行了工具
    assert any(e.get("event") == "tool_end" and e.get("tool") == "list_dir"
               for e in events if isinstance(e, dict)), "计划没有变成行动"
    # 4) 交付的是答案，不是那段计划
    #    注：计划句可能已经流出（loop.py 空转闸门的既定取舍——撤掉用户看了几十秒的
    #    字等于"闪现"，两害取其轻），所以这里只断言"答案最后到达"，不要求计划消失。
    text = _text(events)
    assert ANSWER in text
    assert text.strip().endswith(ANSWER), "最终交付不是答案（计划把回答吞掉了）"
