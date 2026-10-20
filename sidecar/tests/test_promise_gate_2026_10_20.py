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
import json
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


# ── 第三种方言：```json {"tool": ..., "params": {...}} ```（2026-10-20 16:25 现场）
_REAL_FENCE_BODY = """（凑近屏幕，指尖轻点着下巴）主人想看今天的大盘呀……人家这就帮主人查一下，**保证把数据看得明明白白的～** ✨

（打开工具面板，眼神专注）让我先看看三大指数今天的表现……

调用 `mx_query` 查询 A 股大盘行情。

```json
{
  "tool": "mx_query",
  "params": {
    "query": "A股大盘 今日 上证指数 深证成指 创业板指 涨跌幅 成交额",
    "date": "2026-10-20"
  }
}
```
"""


def test_fenced_tool_params_dialect_is_parsed():
    """真机原文：{"tool","params"} 方言必须解析成一次调用，且围栏从正文剥掉。"""
    from agent.parsing import _parse_prompt_tool_calls
    clean, calls = _parse_prompt_tool_calls(_REAL_FENCE_BODY)
    assert len(calls) == 1
    fn = calls[0]["function"]
    assert fn["name"] == "mx_query"
    assert "上证指数" in json.loads(fn["arguments"])["query"]
    assert "```json" not in clean, "围栏必须剥掉（别把 JSON 当回答交付）"


def test_fenced_dialect_requires_known_tool():
    """非标准键名必须点在册工具：正文里的任意 JSON 不能变成一次调用。"""
    from agent.parsing import _parse_prompt_tool_calls
    text = '示例：```json\n{"tool": "not_a_real_tool", "params": {"x": 1}}\n```'
    _clean, calls = _parse_prompt_tool_calls(text)
    assert calls == []


def test_thinking_round_gate_catches_fenced_call():
    """思考轮兜底：正文带日期数字（会被当成"数据"）时，栅栏点名在册工具仍算计划信号。"""
    from tests.test_thinking_first import _loop
    loop = _loop("max", "分析今天大盘")
    assert loop._thinking_round_plan_leak(_REAL_FENCE_BODY) is True


@pytest.mark.asyncio
async def test_fenced_dialect_runs_the_tool_end_to_end():
    """端到端：思考轮用 {"tool","params"} 方言下调用 → 真执行 → 终答交付。"""
    body = ('（打开工具面板，眼神专注）让我先看看目录里有什么……\n\n'
            '```json\n{"tool": "list_dir", "params": {"path": "."}}\n```\n')
    answer = "目录里有 agents、skills、tests 等。" * 2
    from tests.fake_engine import FakeEngine
    events, delivered, _requests = await _run_loop(
        "看看当前目录里有什么",
        [FakeEngine.text_response(body),
         FakeEngine.text_response(answer)],
        "fence-e2e")
    assert any(e.get("event") == "tool_start" and e.get("tool") == "list_dir"
               for e in events), "方言必须被解析并执行（不能只当文本交付）"
    assert "目录里有" in delivered
