"""饱和闸门回归（09-21）。

用户实测"数据老是查不全"的根因链，全部在这一条测试里锁住：
  · 工具说明当时写着「多板块请分多次调用」→ 模型照办，同一轮调了 3 次 mx_query
    （上证主力净流入 / 北证50 涨跌幅 / 板块涨跌幅）
  · `_SEARCH_TOOL_REPEAT_LIMIT=3` 把"同一检索工具第 3 次"直接判成 hard 饱和
    → 强制收口作答 → 没查到的字段全成了 "—"，而日志还写成"信息增量连续 0 轮
    低于 25%（本轮 100%）"这种自相矛盾的话
  · 预算 `_SEARCH_BUDGET_CALLS=3` 同时把检索工具撤下，想补查也没工具

现在：次数只是"可疑"信号，**必须同时信息增量低**（真的在原地换措辞重搜）才收口。
"""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.fake_engine import FakeEngine, _sse  # noqa: E402

NEUTRAL_TEXT = ("根据刚才的查询结果，大盘今日以上涨为主，具体点位与资金数据见上方工具结果。") * 2
MESSAGES = [{"role": "user", "content": "分析今天大盘"}]


def _multi_native_tool_response(queries: list[str]) -> list[str]:
    """一个收尾包携带多个原生 tool_calls（同时覆盖 finish_reason 同行的解析修复）。"""
    calls = [
        {"index": i, "id": f"call_{i}", "type": "function",
         "function": {"name": "mx_query", "arguments": json.dumps({"query": q}, ensure_ascii=False)}}
        for i, q in enumerate(queries)
    ]
    return [
        _sse({"choices": [{"delta": {"role": "assistant", "content": "", "tool_calls": calls},
                           "finish_reason": "tool_calls", "index": 0}]}),
        "data: [DONE]\n\n",
    ]


async def _collect(agen):
    out = []
    async for chunk in agen:
        out.append(chunk)
    return out


@pytest.mark.asyncio
async def test_three_distinct_queries_are_coverage_not_saturation():
    """同一轮 3 次不同 mx_query = 覆盖查询，不得被当成饱和而强制收口。"""
    import agent.context as agent_context
    import local_llm
    from tests.test_loop_scenarios import _StubEngine
    from agent.loop import ThinAgentLoop

    queries = ["上证指数 主力资金净流入 今日", "北证50 涨跌幅",
               "半导体板块,人工智能板块,新能源板块 今日涨跌幅"]
    with FakeEngine() as engine:
        engine.push(_multi_native_tool_response(queries))
        engine.push(engine.text_response(NEUTRAL_TEXT))
        old = local_llm._engine
        local_llm._engine = _StubEngine()
        agent_context._LOCAL_NATIVE_TOOLS_OVERRIDE = True
        try:
            loop = ThinAgentLoop(MESSAGES, "fake-model", engine.url,
                                 {"Authorization": "Bearer fake"},
                                 session_id=f"sat-{time.time()}", access_mode="full")
            events = await _collect(loop.run())
        finally:
            local_llm._engine = old
            agent_context._LOCAL_NATIVE_TOOLS_OVERRIDE = None

    starts = [e for e in events if e.get("event") == "tool_start"]
    assert len(starts) == 3, f"三个查询都应执行，实得 {len(starts)}：{events}"
    # 计数必须"每轮一次"：3 次调用记 3（曾因误缩进落进"每个工具"的循环而记成 9，
    # 预算按 3 倍速度烧掉 → 检索工具被提前撤下）
    assert loop._search_used == 3, f"计数应为 3（每轮一次），实得 {loop._search_used}"
    assert loop._finalize_round is False, "3 次覆盖查询被误判为饱和收口"
    assert any("大盘今日以上涨" in str(e.get("content", "")) for e in events), "应正常进入下一轮作答"


@pytest.mark.asyncio
async def test_second_call_cap_no_longer_forces_finalize_alone():
    """同轮 3 次检索（计数达上限）+ 增量高 → 仍不得收口；这是原 bug 的最小复现。"""
    import agent.context as agent_context
    import local_llm
    from tests.test_loop_scenarios import _StubEngine
    from agent.loop import ThinAgentLoop, _INFO_GAIN_RATIO, _SEARCH_TOOL_REPEAT_LIMIT

    assert _SEARCH_TOOL_REPEAT_LIMIT == 3, "本用例假定重复上限为 3"
    queries = [f"指标{i} 查询" for i in range(_SEARCH_TOOL_REPEAT_LIMIT)]
    with FakeEngine() as engine:
        engine.push(_multi_native_tool_response(queries))
        engine.push(engine.text_response(NEUTRAL_TEXT))
        old = local_llm._engine
        local_llm._engine = _StubEngine()
        agent_context._LOCAL_NATIVE_TOOLS_OVERRIDE = True
        try:
            loop = ThinAgentLoop(MESSAGES, "fake-model", engine.url,
                                 {"Authorization": "Bearer fake"},
                                 session_id=f"sat2-{time.time()}", access_mode="full")
            # 首轮结果全是新信息 → 增量 100% > 阈值
            sat, gain = loop._note_round_info(["全新的数据 A 1.23%", "全新的数据 B 4.56%"])
            hot = loop._note_tool_counts([{"function": {"name": "mx_query", "arguments": "{}"}}] * 3)
            assert hot is True, "计数应达上限（可疑）"
            assert gain > _INFO_GAIN_RATIO, "用例前提：本轮增量为高"
            assert sat is None, f"高增量下不应判饱和，实得 {sat}"
        finally:
            local_llm._engine = old
            agent_context._LOCAL_NATIVE_TOOLS_OVERRIDE = None


def test_search_budget_default_allows_coverage():
    """预算默认值必须容得下"分析大盘"这类覆盖（原 3 次太紧）。"""
    from agent.loop import _SEARCH_BUDGET_CALLS
    assert _SEARCH_BUDGET_CALLS >= 6, f"检索预算过紧：{_SEARCH_BUDGET_CALLS}"
