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


# ── 坏引号的围栏调用（2026-10-20 17:06 现场："又停了"的真凶）
_BROKEN_QUOTE_BODY = """（轻咬下唇，眼波流转）主人贪心呢～光看大盘怎么够？**人家这就帮主人把那些乱糟糟的板块概念都揪出来，看看今天谁在争宠～** ✨

（手指在键盘上轻快地敲击，眼神专注）先搜一下今天的热榜……

调用 `tavily_search` 搜索今日A股热门板块。

```json
{
  "tool": "tavily_search,
  "params": {
    "query": "2026年10月20日 A股热门板块 领涨概念 今日行情",
    "max_results": 5
  }
}
```
"""


def test_broken_quote_fence_call_is_parsed():
    """缺闭合引号的围栏调用也要救回：容错取名字 + 内层 params 作为参数。"""
    from agent.parsing import _parse_prompt_tool_calls
    clean, calls = _parse_prompt_tool_calls(_BROKEN_QUOTE_BODY)
    assert len(calls) == 1
    fn = calls[0]["function"]
    assert fn["name"] == "tavily_search"
    assert "A股热门板块" in json.loads(fn["arguments"])["query"]
    assert "```json" not in clean


def test_broken_quote_fence_is_a_plan_signal():
    from tests.test_thinking_first import _loop
    loop = _loop("max", "板块和概念呢")
    assert loop._thinking_round_plan_leak(_BROKEN_QUOTE_BODY) is True


def test_numbers_inside_fence_do_not_count_as_data():
    """围栏里的日期/参数不是"数据事实"：散文只有承诺时，行情请求仍要进执行轮。"""
    from tests.test_thinking_first import _loop
    body = ("（凑近屏幕）主人想看今天的大盘呀……人家这就帮主人查一下～ ✨\n\n"
            "```json\n{\"date\": \"2026-10-20\", \"max_results\": 5}\n```\n")
    loop = _loop("max", "分析今天大盘")
    assert loop._thinking_round_plan_leak(body) is True


# ── 散文里的日期骗过两道闸门（2026-10-20 19:30 现场："还是一样啊"的真凶）
# 真机原文（326 字）：一个日期"2026-10-20"被数成 3 个"数字事实"，计划漏网闸
# 按"多个数字事实＝真汇报"整条放行（mx_query 点名判据被挡在后面），"思考轮
# 转执行"兜底闸的 _DATA_SIGNAL_RE 也命中"2026" → 纯承诺被当终答交付。
_DATE_PROMISE_BODY = (
    "（凑近屏幕，指尖轻轻点着你的脸颊）主人今天想听人家把盘面掰开揉碎讲给你听呀？"
    "（眼波流转）我已经准备好啦，**马上调取今日A股大盘与热门板块的实时数据**，"
    "保证让你一眼看穿主力动向～（轻咬下唇）需要调用 `mx_query` 获取2026-10-20的"
    "三大指数行情、概念/行业板块涨幅榜及资金流向。拿到数据后，我会先帮你锁定领涨"
    "主线，再结合量能变化拆解板块轮动逻辑，最后挑出资金持续流入的潜力概念给你参考"
    "哦～（微微歪头，眼神里满是期待）主人要是看到喜欢的标的，随时喊人家帮你深挖"
    "基本面嘛。💋 现在就去调数据啦，主人乖乖等我的报告哦。")


def test_date_in_prose_is_not_data_facts():
    """"2026-10-20"是一个日期，不是三个数字事实。"""
    from agent.loop import _prose_data_fact_count
    assert _prose_data_fact_count(_DATE_PROMISE_BODY) == 0


def test_date_promise_is_plan_signal():
    """真机原文：带日期 + mx_query 点名的纯承诺必须判计划（不当终答交付）。"""
    from tests.test_thinking_first import _loop
    loop = _loop("max", "分析今天大盘板块和概念")
    assert loop._thinking_round_plan_leak(_DATE_PROMISE_BODY) is True
    assert _looks_like_plan_only(_DATE_PROMISE_BODY, True) is True


def test_real_report_with_dates_still_has_substance():
    """真汇报不受影响：剥掉日期后仍有价格/百分比等 ≥2 个事实。"""
    from agent.loop import _prose_data_fact_count
    report = ("今天（2026年10月20日）上证收于3250.5点，跌0.89%，主力净流出214亿，"
              "成交额1.2万亿。")
    assert _prose_data_fact_count(report) >= 2


@pytest.mark.asyncio
async def test_date_promise_reaches_execution_round():
    """端到端：19:30 那种带日期的纯承诺 → 必须进执行轮真调工具，不得当终答交付。"""
    answer = "今天领涨的是半导体与AI算力板块，主力净流入居前，成交额明显放大。" * 2
    from tests.fake_engine import FakeEngine
    events, delivered, requests = await _run_loop(
        "分析今天大盘板块和概念",
        [FakeEngine.text_response(_DATE_PROMISE_BODY),
         FakeEngine.native_tool_response("list_dir", {"path": "."}),
         FakeEngine.text_response(answer)],
        "date-promise-e2e")
    assert requests and not (requests[0].get("tools") or []), "思考轮不下发工具表"
    assert requests[1].get("tools"), "带日期的纯承诺必须推进执行轮（恢复工具表）"
    assert any(e.get("event") == "tool_start" for e in events), "必须真调工具"
    assert "主人乖乖等我的报告" not in delivered, "纯承诺不得进交付文本"
    assert "领涨" in delivered, "终答必须交付"


# ── 零闪现（2026-10-20 19:49 真机："出一下消息然后闪现没了"）
# 思考轮 437 字计划边生成边流进气泡、轮末才被撤回 → 用户看着一段字打出来又
# 消失。修法：思考轮正文不实时下发（缓冲），判成终答才一次性放出。
@pytest.mark.asyncio
async def test_thinking_round_plan_never_streams():
    """思考轮计划句不得出现在任何 content 事件里（撤回前的闪现整条消失）。"""
    promise = ("（凑近屏幕）主人想看今天的大盘呀……人家这就帮主人查一下，"
               "需要调用 `mx_query` 获取2026-10-20的三大指数行情哦。")
    answer = "今天上证收于3735点，涨0.12%，领涨的是半导体与AI算力板块。" * 2
    from tests.fake_engine import FakeEngine
    events, delivered, _requests = await _run_loop(
        "分析今天大盘板块和概念",
        [FakeEngine.text_response(promise),
         FakeEngine.native_tool_response("list_dir", {"path": "."}),
         FakeEngine.text_response(answer)],
        "noflash-e2e")
    streamed = [e.get("content") or "" for e in events if not e.get("event")]
    assert all(promise[:12] not in c for c in streamed), \
        "思考轮计划句不得流进气泡（零闪现）"
    assert "领涨" in delivered, "终答必须正常流式交付"


@pytest.mark.asyncio
async def test_thinking_round_direct_answer_streams_live():
    """非任务型直答（闲聊/知识问答）：第 1 轮正文仍实时流出（09-06 契约不回退）。"""
    answer = "明天是周末，休市，不用等开盘；周一再帮主人看盘哦。" * 3
    from tests.fake_engine import FakeEngine
    events, delivered, requests = await _run_loop(
        "明天开市吗",
        [FakeEngine.text_response(answer)],
        "chat-e2e")
    assert len(requests) == 1, "直答不该被推进执行轮"
    assert any(not e.get("event") and "休市" in (e.get("content") or "")
               for e in events), "闲聊直答必须实时流出（不被缓冲）"
    assert "休市" in delivered


@pytest.mark.asyncio
async def test_tasklike_round1_data_answer_released():
    """任务型请求的第 1 轮带数据直答（记忆型回答）：缓冲的正文必须经释放点交付。

    这是 _release_think_hold 唯一的"放出"场景（其余场景缓冲直接丢弃）——
    缓冲后忘了释放 = 任务型直答整条消失，用例锁死这条链路。
    """
    answer = ("今天上证收于3735.37点，涨0.12%；深证成指12001.55点，跌0.34%；"
              "创业板指2560.31点，跌0.89%，主力净流出214亿。整体呈震荡格局，"
              "主力风格偏防御，没有持续领涨的主线。")
    from tests.fake_engine import FakeEngine
    events, delivered, requests = await _run_loop(
        "分析今天大盘板块和概念",
        [FakeEngine.text_response(answer)],
        "tasklike-release-e2e")
    assert len(requests) == 1, "带数据的记忆型直答不该被推进执行轮"
    assert "3735" in delivered, "缓冲的正文必须经释放点一次性交付"


@pytest.mark.asyncio
async def test_broken_quote_fence_runs_end_to_end():
    """坏引号围栏在思考轮被解析后要真执行（用本地 list_dir，不联网）。"""
    body = ('（打开工具面板）让我看看目录里有什么……\n\n调用 `list_dir` 看看当前目录。\n\n'
            '```json\n{\n  "tool": "list_dir,\n  "params": {"path": "."}\n}\n```\n')
    answer = "目录里有 agents、skills、tests 等。" * 2
    from tests.fake_engine import FakeEngine
    events, delivered, _requests = await _run_loop(
        "看看当前目录里有什么",
        [FakeEngine.text_response(body),
         FakeEngine.text_response(answer)],
        "broken-fence-e2e")
    assert any(e.get("event") == "tool_start" and e.get("tool") == "list_dir"
               for e in events), "坏引号方言必须被救回并执行"
    assert "目录里有" in delivered


# ── 收口轮工具形态锁定 → 摘要视图兜底（2026-10-20 17:17 现场）
def test_finalize_digest_request_has_no_tools_or_tool_history():
    """摘要视图：无 tools 键、无 tool 角色/tool_calls 历史，只留【数据N】+ 提问。"""
    from agent.loop import ThinAgentLoop
    loop = ThinAgentLoop([{"role": "user", "content": "板块和概念呢"}], "m",
                         "http://127.0.0.1:1/v1", {}, "t-digest", thinking_level="max")
    loop.steps = 6
    loop._finalize_round = True
    loop._finalize_digest = True
    loop.current_msgs.append({
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "tavily_search", "arguments": "{}"}}]})
    loop.current_msgs.append({"role": "tool", "tool_call_id": "c1",
                              "content": "搜索结果：机器人、低空经济领涨。"})
    body = loop._build_request("m")
    assert "tools" not in body, "摘要视图不能下发工具表"
    assert all((m.get("role") or "") != "tool" for m in body["messages"])
    assert all(not m.get("tool_calls") for m in body["messages"])
    assert "不要输出任何工具调用格式" in str(body["messages"][0].get("content"))
    assert any("【数据" in str(m.get("content")) for m in body["messages"]), "数据摘要缺失"


@pytest.mark.asyncio
async def test_tool_locked_model_retries_with_digest(monkeypatch):
    """真机复刻：反复发同一个搜索 → 重复闸/饱和收口 → 收口轮仍发调用
    → 摘要视图重采 → 产出分析（而不是"⚠️ 未能生成分析 + 数据堆"）。"""
    import agent.context as agent_context
    import agent_loop
    import local_llm
    import time
    from agent.loop import RoundTextAccumulator, ThinAgentLoop
    from tests.fake_engine import FakeEngine
    from tests.test_loop_scenarios import _StubEngine

    # 不打网络：把 tavily_search 换成固定结果
    monkeypatch.setitem(agent_loop.TOOL_DISPATCH, "tavily_search",
                        lambda args: "【搜索结果】机器人、低空经济、短剧传媒领涨。")
    call = {"query": "2026年10月20日 A股热门板块", "max_results": 5}
    answer = "今天板块方面：机器人概念受多重催化领涨，低空经济延续，短剧传媒分化。" * 2
    with FakeEngine() as engine:
        for _ in range(4):                     # 同一调用 ×4（第 3、4 次会被重复闸拒绝）
            engine.push(engine.native_tool_response("tavily_search", call))
        # 收口轮：一句承诺 + 又一个同样的调用（工具形态锁定）
        engine.push(engine.native_tool_response(
            "tavily_search", call, content="（指尖轻点）让人家整理一下～"))
        engine.push(engine.text_response(answer))   # 摘要视图重采 → 终答
        old = local_llm._engine
        _old = agent_context._LOCAL_NATIVE_TOOLS_OVERRIDE
        local_llm._engine = _StubEngine()
        agent_context._LOCAL_NATIVE_TOOLS_OVERRIDE = True
        try:
            loop = ThinAgentLoop([{"role": "user", "content": "板块和概念呢"}],
                                 "fake-model", engine.url, {},
                                 session_id=f"locked-e2e-{time.time()}",
                                 access_mode="full", thinking_level="max")
            acc = RoundTextAccumulator()
            events = []
            async for ev in loop.run():
                acc.feed(ev)
                events.append(ev)
        finally:
            local_llm._engine = old
            agent_context._LOCAL_NATIVE_TOOLS_OVERRIDE = _old
    delivered = acc.join()
    assert "未能生成分析" not in delivered, "不该落到数据兜底"
    assert "机器人概念" in delivered, "摘要视图重采必须产出分析"
    last = engine.requests[-1]
    assert "tools" not in last, "摘要视图请求不能带工具表"
    assert all((m.get("role") or "") != "tool" for m in last.get("messages") or [])


# ── 数据型分析不该因末句客套被判计划（2026-10-20 17:25 现场：一问回三条）
_DATA_ANALYSIS = ("（轻咬下唇，眼波流转）主人想看的板块数据来啦～人家早就帮你看好了呢✨\n\n"
                  "**今日板块行情速览**：\n"
                  "- 人工智能板块 +2.35%，主力净流入 20.37亿元\n"
                  "- 半导体 +2.93%，主力净流出 19.29亿元\n"
                  "- 白酒Ⅱ -1.11%，主力净流出 1.6亿元\n"
                  "- 新能源 +0.25%，主力净流出 31.68亿元\n\n"
                  "还想看哪些板块呀？人家随时帮你查嘛～💋")


def test_data_rich_analysis_with_persona_tail_is_not_a_plan():
    """真机误伤的复刻：完整分析 + "人家随时帮你查嘛" 不得判成计划。"""
    assert _looks_like_plan_only(_DATA_ANALYSIS, True) is False


def test_finalize_junk_does_not_flag_data_analysis():
    from agent.loop import _finalize_body_is_junk
    assert _finalize_body_is_junk(_DATA_ANALYSIS, False) is False


@pytest.mark.asyncio
async def test_one_question_yields_one_bubble():
    """真机复刻（17:25）：计划（撤回）→ 工具 → 数据型分析（末句客套）→ 直接交付；
    不再催促重写，整轮只应有一次撤回（思考轮那条计划句）。"""
    plan = "（凑近屏幕）主人想看板块呀～让人家先查一下最近的热门板块～（轻咬下唇）"
    from tests.fake_engine import FakeEngine
    events, delivered, _requests = await _run_loop(
        "板块和概念呢",
        [FakeEngine.text_response(plan),
         FakeEngine.native_tool_response("list_dir", {"path": "."}),
         FakeEngine.text_response(_DATA_ANALYSIS)],
        "one-bubble-e2e")
    retractions = [e for e in events
                   if e.get("event") == "content_revised" and not e.get("content")]
    assert len(retractions) == 1, f"只应撤回思考轮的计划句（实际 {len(retractions)} 次）"
    assert "人工智能" in delivered and "人家随时帮你查" in delivered, "分析必须直接交付"
