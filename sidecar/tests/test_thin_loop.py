"""Stage 2 薄循环测试矩阵：模型驱动终止、错误即结果、原生协议、快车道、steer。

对标 Codex/dph 场景矩阵：错误回填→模型自纠（替代 v1 nudge 用例）。
"""
import json
import time

import pytest

from tests.fake_engine import FakeEngine, _sse

HEADERS = {"Authorization": "Bearer fake"}
NEUTRAL_TEXT = ("根据刚才的目录输出，这个目录里包含 agents、skills、tests 等目录以及若干 python 文件，"
                "整体结构清晰，具体文件清单见上方工具结果。") * 2

MESSAGES = [{"role": "user", "content": "列出当前目录并告诉我结果"}]


async def _collect(agen):
    out = []
    async for chunk in agen:
        out.append(chunk)
    return out


@pytest.mark.asyncio
async def test_thin_local_text_round_trip():
    import local_llm
    from tests.test_loop_scenarios import _StubEngine
    from agent.loop import ThinAgentLoop
    with FakeEngine() as engine:
        engine.push(engine.text_response(NEUTRAL_TEXT))
        old = local_llm._engine
        local_llm._engine = _StubEngine()
        try:
            events = await _collect(ThinAgentLoop(
                MESSAGES, "fake-model", engine.url, HEADERS,
                session_id=f"thin-t1-{time.time()}", access_mode="full").run())
        finally:
            local_llm._engine = old
        rounds = [e["iteration"] for e in events if e.get("event") == "round_start"]
        assert rounds == [1], "纯文本一轮即终止（模型驱动）"
        texts = [e.get("content", "") for e in events if "content" in e]
        assert any("根据刚才的目录输出" in t for t in texts), events


@pytest.mark.asyncio
async def test_thin_cloud_text_round_trip():
    from agent.loop import ThinAgentLoop
    with FakeEngine() as engine:
        engine.push(engine.text_response("你好，我来帮你。"))
        events = await _collect(ThinAgentLoop(
            MESSAGES, "fake-model", engine.url, HEADERS,
            session_id=f"thin-c1-{time.time()}", access_mode="full").run())
        texts = [e.get("content", "") for e in events if "content" in e]
        assert any("你好" in t for t in texts), events


@pytest.mark.asyncio
async def test_thin_native_tool_round_trip():
    """原生 tools 下发 → delta.tool_calls 执行 → assistant 携 tool_calls → 完成。"""
    import agent.context as agent_context
    import local_llm
    from tests.test_loop_scenarios import _StubEngine
    from agent.loop import ThinAgentLoop

    def _round2(body):
        assert isinstance(body.get("tools"), list) and body["tools"], "第二轮仍应携带 tools"
        asst = [m for m in body["messages"]
                if m.get("role") == "assistant" and m.get("tool_calls")]
        assert asst, "原生模式 assistant 消息必须携带 tool_calls"
        # 09-19 起不再"工具后续轮一律关思考"——那等于界面上的档位只在第一回合生效。
        # 现在所有轮次都沿用用户档位；本用例未指定档位 → 默认（高）→ 保持开启。
        assert (body.get("chat_template_kwargs") or {}).get("enable_thinking") is True, \
            "未指定档位时沿用默认（高）档：工具后续轮不再强制关思考"
        return engine.text_response(NEUTRAL_TEXT)

    with FakeEngine() as engine:
        engine.push(engine.native_tool_response("list_dir", {"path": "."}))
        engine.push(_round2)
        old = local_llm._engine
        local_llm._engine = _StubEngine()
        agent_context._LOCAL_NATIVE_TOOLS_OVERRIDE = True
        try:
            events = await _collect(ThinAgentLoop(
                MESSAGES, "fake-model", engine.url, HEADERS,
                session_id=f"thin-t2-{time.time()}", access_mode="full").run())
        finally:
            local_llm._engine = old
            agent_context._LOCAL_NATIVE_TOOLS_OVERRIDE = None
        assert isinstance(engine.requests[0].get("tools"), list), "首轮应携带原生 tools"
        assert any(e.get("event") == "tool_start" for e in events), events
        assert any("根据刚才的目录输出" in str(e.get("content", "")) for e in events), events


@pytest.mark.asyncio
async def test_thin_error_as_result_model_self_corrects():
    """错误即结果：工具失败的结构化错误回填后模型自纠，无 nudge 介入。"""
    import agent.context as agent_context
    import local_llm
    from tests.test_loop_scenarios import _StubEngine
    from agent.loop import ThinAgentLoop
    with FakeEngine() as engine:
        engine.push(engine.native_tool_response("list_dir", {"path": "/nonexistent-dir-xyz"}))
        # 第二轮：请求体应包含失败原文（模型自己看到错误），随后给出纠正
        def _round2(body):
            assert "No such file" in json.dumps(body, ensure_ascii=False) or \
                   "不存在" in json.dumps(body, ensure_ascii=False) or \
                   "Error" in json.dumps(body, ensure_ascii=False), \
                "错误必须作为结果回填给模型"
            return engine.text_response("该目录不存在。已为你确认：路径无效，请提供有效路径。")
        engine.push(_round2)
        old = local_llm._engine
        local_llm._engine = _StubEngine()
        agent_context._LOCAL_NATIVE_TOOLS_OVERRIDE = True
        try:
            events = await _collect(ThinAgentLoop(
                [{"role": "user", "content": "列出 /nonexistent-dir-xyz 目录"}],
                "fake-model", engine.url, HEADERS,
                session_id=f"thin-t3-{time.time()}", access_mode="full").run())
        finally:
            local_llm._engine = old
            agent_context._LOCAL_NATIVE_TOOLS_OVERRIDE = None
        assert len(engine.requests) == 2, "错误回填后模型一轮自纠，无 nudge 循环"
        texts = [str(e.get("content", "")) for e in events if "content" in e]
        assert any("无效" in t or "不存在" in t for t in texts), events


@pytest.mark.asyncio
async def test_thin_light_query_fast_path():
    from agent.loop import ThinAgentLoop
    with FakeEngine() as engine:
        engine.push(engine.text_response("收到，一切正常！有什么需要帮忙的吗？"))
        events = await _collect(ThinAgentLoop(
            [{"role": "user", "content": "测试"}], "fake-model", engine.url, HEADERS,
            session_id=f"thin-t4-{time.time()}", access_mode="full").run())
        body = engine.requests[0]
        assert "tools" not in body or not body.get("tools"), "快车道不携带 tools"
        assert (body.get("chat_template_kwargs") or {}).get("enable_thinking") is False
        texts = [e.get("content", "") for e in events if "content" in e]
        assert any("收到" in t for t in texts), events


@pytest.mark.asyncio
async def test_thin_native_400_fallback():
    from tests.test_loop_scenarios import _StubEngine
    from agent.loop import ThinAgentLoop
    import agent.context as agent_context
    import local_llm
    with FakeEngine() as engine:
        engine.push(engine.http_error(400, "model does not support tool calling"))
        engine.push(engine.local_tool_response("list_dir", {"path": "."}))
        engine.push(engine.text_response(NEUTRAL_TEXT))
        old = local_llm._engine
        local_llm._engine = _StubEngine()
        agent_context._LOCAL_NATIVE_TOOLS_OVERRIDE = True
        try:
            events = await _collect(ThinAgentLoop(
                MESSAGES, "fake-model", engine.url, HEADERS,
                session_id=f"thin-t5-{time.time()}", access_mode="full").run())
        finally:
            local_llm._engine = old
            agent_context._LOCAL_NATIVE_TOOLS_OVERRIDE = None
        assert "tools" in engine.requests[0], "首次请求带原生 tools"
        assert "tools" not in engine.requests[1], "回退后不再携带 tools"
        assert "```tool" in json.dumps(engine.requests[1], ensure_ascii=False), \
            "回退后应注入围栏提示词"
        assert any(e.get("event") == "tool_start" for e in events), events


@pytest.mark.asyncio
async def test_thin_steer_claimed_at_step_boundary():
    """steer：队列中的新消息在 step 边界并入同轮续跑。"""
    from tests.test_loop_scenarios import _StubEngine
    from agent.loop import ThinAgentLoop, queue_steer
    import local_llm
    sid = f"thin-t6-{time.time()}"
    with FakeEngine() as engine:
        engine.push(engine.text_response("第一轮回复，内容较长。" * 20))
        def _round2(body):
            assert any("补充要求X" in str(m.get("content", "")) for m in body["messages"]), \
                "steer 消息必须并入上下文"
            return engine.text_response("已根据补充要求更新结果。" * 20)
        engine.push(_round2)
        old = local_llm._engine
        local_llm._engine = _StubEngine()
        queue_steer(sid, "补充要求X：请用更正式的语气")
        try:
            events = await _collect(ThinAgentLoop(
                [{"role": "user", "content": "写一段自我介绍"}], "fake-model", engine.url,
                HEADERS, session_id=sid, access_mode="full").run())
        finally:
            local_llm._engine = old
        rounds = [e["iteration"] for e in events if e.get("event") == "round_start"]
        assert rounds == [1, 2], f"steer 应触发第二轮：{rounds}"
        assert any("更新结果" in str(e.get("content", "")) for e in events), events


@pytest.mark.asyncio
async def test_thin_think_only_assist():
    """思考-only → 终答提取辅助（弱模型辅助层，仅本地）。"""
    from tests.test_loop_scenarios import _StubEngine
    from agent.loop import ThinAgentLoop
    import local_llm
    with FakeEngine() as engine:
        engine.push(engine.thinking_only_response("让我思考一下这个问题……"))
        old = local_llm._engine
        local_llm._engine = _StubEngine()
        try:
            events = await _collect(ThinAgentLoop(
                [{"role": "user", "content": "分析一下当前情况"}], "fake-model", engine.url,
                HEADERS, session_id=f"thin-t7-{time.time()}", access_mode="full").run())
        finally:
            local_llm._engine = old
        texts = "".join(str(e.get("content", "")) for e in events if "content" in e)
        assert "思考" in texts, "应触发思考-only 辅助提示而非静默结束"


@pytest.mark.asyncio
async def test_thin_cloud_body_model_is_cloud_name():
    """09-06 19:29 事故回归：本地引擎加载着 Qwen 时，云端请求的 model
    必须仍是云端模型名（此前返回本地路径 → deepseek 400）。"""
    import local_llm
    from agent.loop import ThinAgentLoop
    from tests.test_loop_scenarios import _StubEngine
    with FakeEngine() as engine:
        old = local_llm._engine
        local_llm._engine = _StubEngine()
        local_llm._engine.current_model_id = "/Users/x/Qwen3.8-27B-MLX-4bit"
        try:
            loop = ThinAgentLoop(MESSAGES, "deepseek-v4-flash-vision-exp", engine.url,
                                 HEADERS, session_id=f"thin-t8-{time.time()}",
                                 access_mode="full", is_local=False)
            body = loop._build_request(loop._engine_model())
        finally:
            local_llm._engine = old
        assert body["model"] == "deepseek-v4-flash-vision-exp", \
            f"云端请求 model 必须是云端名：{body['model']}"


@pytest.mark.asyncio
async def test_thin_streams_content_deltas_live():
    """09-06 19:29 反馈回归：正文必须逐段实时流出（不是结束时一次蹦出）。"""
    from tests.test_loop_scenarios import _StubEngine
    from agent.loop import ThinAgentLoop
    import agent.transport as transport
    import local_llm
    with FakeEngine() as engine:
        deltas = ["第一段结论。", "第二段展开说明。", "第三段补充细节。"]
        lines = []
        for d in deltas:
            lines.append(f"data: {json.dumps({'choices': [{'delta': {'content': d}, 'index': 0}]}, ensure_ascii=False)}\n\n")
        lines.append("data: [DONE]\n\n")
        engine.push(lines)
        old = local_llm._engine
        local_llm._engine = _StubEngine()
        transport._LOCAL_NATIVE_TOOLS_OVERRIDE = True
        try:
            events = await _collect(ThinAgentLoop(
                [{"role": "user", "content": "写个分析"}], "fake-model", engine.url,
                HEADERS, session_id=f"thin-t9-{time.time()}", access_mode="full").run())
        finally:
            local_llm._engine = old
            transport._LOCAL_NATIVE_TOOLS_OVERRIDE = None
        contents = [e["content"] for e in events if "content" in e]
        assert contents == deltas, f"content delta 必须逐段原样流出：{contents}"



def _gen_requests(engine) -> list[dict]:
    """只数**生成**请求（stream=True）。交付闸门的翻译走 stream=False 且也会打到同一引擎，
    不能混进来——首版测试因此把"翻译的 2 次"算成了多出来的重试。"""
    return [r for r in engine.requests if r.get("stream")]


# ── 语言漂移早退（2026-09-23 真机：中文提问收到英文，还要等翻译）────────

_DRIFT_EN = ('**"嗯～主人要我看 it? Okay then~"** I crawl back onto all fours, pressing my chest '
             'flat against the bed and lifting my hips high — just like a puppy waiting to be '
             'ridden, and my ass is still young and tight in the way you remember it.')


@pytest.mark.asyncio
async def test_lang_drift_discards_and_retries_local():
    """首段判为漂移 → 丢弃这次生成（不下发）→ 带更强语言要求重试一次。"""
    import local_llm
    from tests.test_loop_scenarios import _StubEngine
    from agent.loop import ThinAgentLoop
    with FakeEngine() as engine:
        engine.push(engine.text_response(_DRIFT_EN))        # 第一次：英文（漂移）
        engine.push(engine.text_response(NEUTRAL_TEXT))      # 第二次：中文（重试成功）
        old = local_llm._engine
        local_llm._engine = _StubEngine()
        try:
            events = await _collect(ThinAgentLoop(
                MESSAGES, "fake-model", engine.url, HEADERS,
                session_id=f"thin-drift-{time.time()}", access_mode="full").run())
        finally:
            local_llm._engine = old
    reqs = _gen_requests(engine)
    assert len(reqs) == 2, f"应重试一次（共 2 次生成），实际 {len(reqs)}"
    last_msg = reqs[1]["messages"][-1]["content"]
    assert "上一个回答因为用了英文已被丢弃" in last_msg, "重试请求必须带更强的语言要求"
    texts = [e.get("content", "") for e in events if "content" in e]
    assert any("根据刚才的目录输出" in t for t in texts), "应交付重试后的中文"
    assert not any("I crawl back onto all fours" in t for t in texts), "被丢弃的那次绝不能下发"


@pytest.mark.asyncio
async def test_lang_drift_retries_only_once_local():
    """重试仍漂移 → 不再重试（只一次），照常交付交给交付闸门翻译。"""
    import local_llm
    from tests.test_loop_scenarios import _StubEngine
    from agent.loop import ThinAgentLoop
    with FakeEngine() as engine:
        engine.push(engine.text_response(_DRIFT_EN))
        engine.push(engine.text_response(_DRIFT_EN))
        old = local_llm._engine
        local_llm._engine = _StubEngine()
        try:
            await _collect(ThinAgentLoop(
                MESSAGES, "fake-model", engine.url, HEADERS,
                session_id=f"thin-drift2-{time.time()}", access_mode="full").run())
        finally:
            local_llm._engine = old
    assert len(_gen_requests(engine)) == 2, \
        f"只允许重试一次，实际生成 {len(_gen_requests(engine))} 次"


@pytest.mark.asyncio
async def test_no_retry_when_language_matches():
    """正常中文回复：一次请求，且实时下发（不因早退机制多花一次生成）。"""
    import local_llm
    from tests.test_loop_scenarios import _StubEngine
    from agent.loop import ThinAgentLoop
    with FakeEngine() as engine:
        engine.push(engine.text_response(NEUTRAL_TEXT))
        old = local_llm._engine
        local_llm._engine = _StubEngine()
        try:
            events = await _collect(ThinAgentLoop(
                MESSAGES, "fake-model", engine.url, HEADERS,
                session_id=f"thin-nodrift-{time.time()}", access_mode="full").run())
        finally:
            local_llm._engine = old
    assert len(_gen_requests(engine)) == 1, "语言正常不该重试"
    texts = [e.get("content", "") for e in events if "content" in e]
    assert any("根据刚才的目录输出" in t for t in texts)


@pytest.mark.asyncio
async def test_no_lang_guard_when_language_not_confident():
    """语言判定不确定时不掐生成（detect_language_decision 文档：confident=False
    不得据此判断回答是否符合用户语言）——否则纯符号/短消息会被误掐。"""
    import local_llm
    from tests.test_loop_scenarios import _StubEngine
    from agent.loop import ThinAgentLoop
    with FakeEngine() as engine:
        engine.push(engine.text_response(_DRIFT_EN))      # 只推一次：若误掐就会请求第二次
        old = local_llm._engine
        local_llm._engine = _StubEngine()
        try:
            loop = ThinAgentLoop(
                [{"role": "user", "content": "???"}],        # 纯符号 → 语言不确定
                "fake-model", engine.url, HEADERS,
                session_id=f"thin-noconf-{time.time()}", access_mode="full")
            await _collect(loop.run())
        finally:
            local_llm._engine = old
    assert loop.user_lang_confident is False, "前置条件：这条消息的语言判定应为不确定"
    assert len(_gen_requests(engine)) == 1, "语言不确定时不该掐掉重试"


class TestFinalizeBodyIsJunk:
    """终答轮交付闸（2026-09-28）：25 字计划碎片被当答案交付（mimo 实况）。

    「最后 1 次检索机会，用来补盘面下跌的新闻面解释。」——模型仍发工具调用
    被剥离后，正文只剩这句自语；旧的 <10 字空判定拦不住。
    """

    def test_short_fragment_with_stripped_tools_is_junk(self):
        from agent.loop import _finalize_body_is_junk
        assert _finalize_body_is_junk(
            "最后 1 次检索机会，用来补盘面下跌的新闻面解释。", True) is True

    def test_tiny_body_always_junk(self):
        from agent.loop import _finalize_body_is_junk
        assert _finalize_body_is_junk("好的。", False) is True
        assert _finalize_body_is_junk("", False) is True

    def test_legit_short_answer_without_tool_attempt_delivers(self):
        from agent.loop import _finalize_body_is_junk
        # 没发工具尝试的短正文不是碎片（合法短答）
        assert _finalize_body_is_junk("今日大盘收跌，跌幅有限。", False) is False

    def test_legit_short_numeric_answer_with_stripped_tools_delivers(self):
        from agent.loop import _finalize_body_is_junk
        # 剥离过工具调用，但正文是含数字的短答、无工具动词 → 不拦
        assert _finalize_body_is_junk("上证 -0.99%，收 3877 点。", True) is False

    def test_long_body_never_junk(self):
        from agent.loop import _finalize_body_is_junk
        long_text = "今天大盘全面收跌。" + "主力资金净流出明显。" * 20
        assert _finalize_body_is_junk(long_text, True) is False



# 按真实事故的量级构造（思考 340 字 > 80 字碎片闸上限）：若 body_text 被# 思考污染，碎片闸（<80 字）与 <10 字空判定都拦不住，测试必失败。
THINK_BODY = ("用户要分析今天大盘。按规则先查行情数据，用 mx_query 查指数、板块、资金流向。"
              "有指数数据、资金流、涨跌家数、支撑压力、布林线。还差板块涨跌（热点板块）。"
              "我可以并行调用 mx_query 查三个热门板块的涨跌幅与资金流向，再用 tavily_search 补"
              "今天盘面下跌的新闻面解释。时间：2026-09-28 星期一。注意 identical call 已经被拒绝过，"
              "需要换参数。预算：还剩 3 次检索机会，最后一次留给新闻面。")
assert len(THINK_BODY) > 100
RECOVERED_ANSWER = "今日大盘收跌：上证指数 -0.99%，主力资金净流出 417 亿元。"
@pytest.mark.asyncio
async def test_finalize_round_think_only_tool_call_recovers():
    """终答轮"思考+工具调用、正文 0 字"必须重采出真答案（2026-09-28 20:11 实况）。

    mimo 链路：同参空转触发收口 → 终答轮模型仍发 1 个工具调用 + 思考若干字 +
    0 字正文 → 旧实现把含思考的 streamed 赋回 body_text，被判"已交付 340 字"
    直接收尾，用户只看到思考块。修复后：body_text 保持仅正文口径 → 空判定
    命中 → 温度抖动重采一次 → 真答案交付。

    脚本按请求内容有状态分派：请求带收口指令（"📣 收尾"）= 终答轮（第一次）
    返回思考+工具调用、正文 0；重采轮（第二次带指令）返回真答案；其余轮返回
    同参工具调用以触发收口。对收口触发时机不敏感。
    """
    import agent.context as agent_context
    import local_llm
    from tests.test_loop_scenarios import _StubEngine
    from agent.loop import ThinAgentLoop

    DIRECTIVE = "📣 收尾"
    state = {"final_seen": 0}

    # FakeEngine 的脚本一次一弹（每个请求消费一条），所以每轮推一个 callable，
    # 由共享状态按"请求里是否带收口指令"分派。
    def _make_round():
        def _round(body):
            has_directive = DIRECTIVE in json.dumps(body["messages"], ensure_ascii=False)
            if has_directive:
                state["final_seen"] += 1
                if state["final_seen"] == 1:
                    # 终答轮：仿 mimo——思考 + 1 个工具调用（finish 同包）、正文 0 字
                    return [
                        _sse({"choices": [{"delta": {"reasoning_content": THINK_BODY}, "index": 0}]}),
                        _sse({"choices": [{"delta": {"tool_calls": [
                            {"index": 0, "id": "call_final_1", "type": "function",
                             "function": {"name": "list_dir",
                                          "arguments": json.dumps({"path": "."})}},
                        ]}, "finish_reason": "tool_calls", "index": 0}]}),
                        "data: [DONE]\n\n",
                    ]
                # 温度重采轮：给真答案
                return engine.text_response(RECOVERED_ANSWER)
            # 常规轮：同参调用（重复会被拒绝器挡回），把循环推向收口
            return engine.native_tool_response("list_dir", {"path": "."})
        return _round

    with FakeEngine() as engine:
        for _ in range(15):
            engine.push(_make_round())
        old = local_llm._engine
        local_llm._engine = _StubEngine()
        agent_context._LOCAL_NATIVE_TOOLS_OVERRIDE = True
        try:
            events = await _collect(ThinAgentLoop(
                MESSAGES, "fake-model", engine.url, HEADERS,
                session_id=f"thin-fin-{time.time()}", access_mode="full").run())
        finally:
            local_llm._engine = old
            agent_context._LOCAL_NATIVE_TOOLS_OVERRIDE = None
        contents = " ".join(str(e.get("content", "")) for e in events if "content" in e)
        assert state["final_seen"] >= 1, "脚本应观察到终答轮请求"
        assert RECOVERED_ANSWER in contents, \
            f"重采出的真答案必须交付；实际 events 末尾：{events[-6:]}"


class TestLoopGaps:
    """Loop 三缺口（2026-09-29）：工具输出回收 / 预算守卫 / 同错硬升级。"""

    # ── 工具输出回收 ──
    def test_fold_old_large_tool_results(self):
        from agent.loop import _fold_old_tool_results, _FOLD_MARK
        msgs = [{"role": "user", "content": "q"}]
        for i in range(9):
            msgs.append({"role": "tool", "content": f"结果{i}：" + "x" * 2000})
        n = _fold_old_tool_results(msgs, keep_recent=6)
        assert n == 3, f"应折 9-6=3 条，实际 {n}"
        folded = [m for m in msgs if str(m.get("content", "")).startswith(_FOLD_MARK)]
        assert len(folded) == 3
        # 最近 6 条保持原样
        tail_tools = [m for m in msgs if m.get("role") == "tool"][-6:]
        assert all(not str(m["content"]).startswith(_FOLD_MARK) for m in tail_tools)

    def test_fold_is_idempotent_and_skips_small(self):
        from agent.loop import _fold_old_tool_results, _FOLD_MARK
        msgs = [{"role": "tool", "content": "小结果"} for _ in range(8)]
        assert _fold_old_tool_results(msgs, keep_recent=6) == 0, "小结果不值得折"
        big = [{"role": "tool", "content": "y" * 3000} for _ in range(8)]
        assert _fold_old_tool_results(big, keep_recent=6) == 2
        assert _fold_old_tool_results(big, keep_recent=6) == 0, "二次调用必须幂等"
        assert sum(1 for m in big if str(m["content"]).startswith(_FOLD_MARK)) == 2

    # ── 同错硬升级 ──
    def test_escalates_after_three_same_errors(self):
        from agent.loop import ThinAgentLoop
        loop = ThinAgentLoop.__new__(ThinAgentLoop)
        loop._fail_sigs = {}
        loop._escalated = False
        err = "Error: 金融数据查询失败: ConnectionError: RemoteDisconnected"
        assert loop._note_tool_failure("ak_finance", err) is None
        assert loop._note_tool_failure("ak_finance", err) is None
        out = loop._note_tool_failure("ak_finance", err)
        assert out and out["n"] == 3 and "ak_finance" not in out["err"]
        # 只触发一次
        assert loop._note_tool_failure("ak_finance", err) is None

    def test_success_and_different_tools_not_counted(self):
        from agent.loop import ThinAgentLoop
        loop = ThinAgentLoop.__new__(ThinAgentLoop)
        loop._fail_sigs = {}
        loop._escalated = False
        assert loop._note_tool_failure("mx_query", "查询结果：| date | 上证 |") is None
        loop._note_tool_failure("ak_finance", "Error: A")
        loop._note_tool_failure("mx_query", "Error: B")
        assert not loop._escalated, "不同工具/不同错误不该触发升级"

    def test_varying_numbers_same_error_still_counts(self):
        from agent.loop import ThinAgentLoop
        loop = ThinAgentLoop.__new__(ThinAgentLoop)
        loop._fail_sigs = {}
        loop._escalated = False
        for n in (1, 2, 3):
            out = loop._note_tool_failure("ak_finance", f"Error: HTTP 500 after {n * 3} tries")
        assert out is not None, "错误里的数字不应破坏签名（时间戳/计数每次都变）"
