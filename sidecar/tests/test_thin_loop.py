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

    # ── 可丢弃工具名单（gap P3）：按工具类别分档折叠 ──
    @staticmethod
    def _call(i, name, n_chars, fill="x"):
        """一条 assistant(tool_calls) + tool 结果 的配对（带 id→name 映射）。"""
        cid = f"c{i}"
        return [{"role": "assistant", "content": "", "tool_calls": [
                    {"id": cid, "type": "function",
                     "function": {"name": name, "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": cid, "content": fill * n_chars}]

    def test_fold_policy_by_tool_category(self):
        from agent.loop import _fold_old_tool_results, _FOLD_MARK
        msgs = [{"role": "user", "content": "q"}]
        # ① 读文件类：1000 字符就该折，且只留开头 250（可重拿）
        msgs += self._call(1, "read_file", 1000, "r")
        # ② 行情类 2000 字符：**不折**（数字即结论；阈值 2500）
        msgs += self._call(2, "mx_query", 2000, "m")
        # ③ 行情类超大：折，但多留数据行（900）
        msgs += self._call(3, "mx_query", 4000, "n")
        # ④ 一般工具（run_cmd）：默认档 1500 才折
        msgs += self._call(4, "run_cmd", 1600, "c")
        # ⑤…⑱ 十四个读文件类结果充作"更后面的调用"：
        #      — 一般工具的保护窗是最近 6 条 → ⑤…⑫ 可折（8 条）
        #      — 行情类的保护窗是最近 6+6=12 条 → ③ 要活下来就得更旧
        for i in range(5, 19):
            msgs += self._call(i, "read_file", 900, "z")

        n = _fold_old_tool_results(msgs, keep_recent=6)
        assert n == 11, f"应折 1(read_file)+1(mx_query 大)+1(run_cmd)+8(read_file 填充)，实际 {n}"
        tool_msgs = {m["tool_call_id"]: m["content"] for m in msgs if m.get("role") == "tool"}
        assert str(tool_msgs["c1"]).startswith(_FOLD_MARK), "读文件类结果该被回收"
        assert str(tool_msgs["c1"]).count("r") < 300, "读文件类只留开头（250）"
        assert "read_file" in str(tool_msgs["c1"]), "标记里要写明是哪个工具的结果，便于重拿"
        assert not str(tool_msgs["c2"]).startswith(_FOLD_MARK), "行情类 2000 字符不折"
        assert str(tool_msgs["c3"]).startswith(_FOLD_MARK), "行情类超大才折"
        assert str(tool_msgs["c3"]).count("n") >= 900, "行情类多留数据行（900）"
        assert str(tool_msgs["c4"]).startswith(_FOLD_MARK), "一般工具走默认档（1500）"
        # 保护窗：最近 6 条一律不动
        for i in range(13, 19):
            assert not str(tool_msgs[f"c{i}"]).startswith(_FOLD_MARK), "最近 6 条必须原样"

    def test_fold_keeps_numeric_results_longer_than_reads(self):
        """同为 1200 字符、同样旧：读文件被折、行情类保持原样（数字要"更久"）。"""
        from agent.loop import _fold_old_tool_results, _FOLD_MARK
        msgs = [{"role": "user", "content": "q"}]
        msgs += self._call(1, "read_file", 1200, "r")
        msgs += self._call(2, "mx_query", 1200, "m")
        for i in range(3, 20):
            msgs += self._call(i, "read_file", 900, "z")
        _fold_old_tool_results(msgs, keep_recent=6)
        tool_msgs = {m["tool_call_id"]: m["content"] for m in msgs if m.get("role") == "tool"}
        assert str(tool_msgs["c1"]).startswith(_FOLD_MARK), "读文件类该折"
        assert not str(tool_msgs["c2"]).startswith(_FOLD_MARK), "行情类 1200 字符不折"

    # ── 审计③：恢复的历史里工具结果是 user 形态 `[工具结果] <name> …` ──
    @staticmethod
    def _restored(name, n_chars, fill="r"):
        return {"role": "user",
                "content": f"[工具结果] {name} {{\"p\":\"x\"}}\n" + fill * n_chars}

    def test_restored_history_results_are_attributed_and_folded(self):
        """前端保存/回灌的历史没有 role=tool——此前**一条都不折**、也拿不到分档。"""
        from agent.loop import _fold_old_tool_results, _FOLD_MARK, _restored_tool_name
        assert _restored_tool_name(self._restored("mx_query", 10)) == "mx_query"
        assert _restored_tool_name({"role": "user", "content": "[工具结果] 这不是工具名"}) == ""
        assert _restored_tool_name({"role": "user", "content": "普通消息"}) == ""

        msgs = [{"role": "user", "content": "q"}]
        msgs += [self._restored("read_file", 900, "r")]      # 可丢弃类 ≥700 → 折
        msgs += [self._restored("mx_query", 900, "m")]       # 保久类 <2500 → 不折
        msgs += [self._restored("read_file", 900, "r")]
        for i in range(8):                                   # 补足保护窗
            msgs += [self._restored("read_file", 900, "z"), {"role": "user", "content": "说"}]
        # 候选共 10 条工具结果（前两条 + 8 条填充），保护窗 6 条 → 可折 4 条；
        # 其中 mx_query 因 900 < 2500（保久类门槛）被豁免 → 实际折 3 条 read_file… 加第 2 条
        n = _fold_old_tool_results(msgs, keep_recent=6)
        folded = [str(m["content"]) for m in msgs if str(m.get("content", "")).startswith(_FOLD_MARK)]
        assert n == 4, f"候选 10 条 - 保护 6 条 = 4 条可折（mx_query 被门槛豁免但占位）：{n}"
        assert all("read_file" in f for f in folded), f"折叠标记必须写明工具名：{folded[:1]}"
        assert not any("mx_query" in f for f in folded), "行情类 900 字符不该折（<2500）"
        kept = [str(m["content"]) for m in msgs if "mx_query" in str(m.get("content", ""))[:20]]
        assert kept and kept[0].startswith("[工具结果] mx_query"), "行情结果应原样保留"

    def test_fold_leaves_context_with_a_drop_segment(self):
        """50 轮长对话：回收造成上下文**下降段**（在压缩阈值兜底之前）。"""
        from agent.loop import _fold_old_tool_results
        msgs = [{"role": "user", "content": "q"}]
        for i in range(50):
            # 每轮一次读文件（可丢弃类）+ 一次行情（保久类，但 1200 < 2500 不折）
            msgs += self._call(i * 2, "read_file", 1200)
            msgs += self._call(i * 2 + 1, "mx_query", 1200)
        before = sum(len(str(m.get("content") or "")) for m in msgs)
        assert before > 18000, "先超过压缩阈值，才能说明回收是新的早出口"
        n = _fold_old_tool_results(msgs, keep_recent=6)
        after = sum(len(str(m.get("content") or "")) for m in msgs)
        assert n == 47, f"100 条结果里：行情类 50 条不折、读文件 50 条中 3 条在保护窗 → 应折 47，实际 {n}"
        assert after < before - 30000, f"下降段应显著（{before} → {after}）"

    # ── 同错硬升级（按轮去重）──
    def _loop_with_tool_msgs(self, msgs):
        from agent.loop import ThinAgentLoop
        loop = ThinAgentLoop.__new__(ThinAgentLoop)
        loop.current_msgs = msgs
        loop._fail_sigs = {}
        loop._fail_scan_from = 0
        loop._escalated = False
        return loop

    def _tool_msg(self, content, name="ak_finance", cid="c1"):
        return [
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": cid, "type": "function",
                             "function": {"name": name, "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": cid, "content": content},
        ]

    def test_escalates_after_three_rounds_of_same_error(self):
        err = "Error: 金融数据查询失败: ConnectionError: RemoteDisconnected"
        loop = self._loop_with_tool_msgs([])
        for i in range(2):
            loop.current_msgs.extend(self._tool_msg(err))
            assert loop._scan_tool_failures() is None, f"第 {i+1} 轮不应升级"
        loop.current_msgs.extend(self._tool_msg(err))
        out = loop._scan_tool_failures()
        assert out and out["n"] == 3 and out["tool"] == "ak_finance"
        assert loop._scan_tool_failures() is None, "只升级一次"

    def test_one_round_with_three_distinct_failures_is_not_escalated(self):
        """一轮里 3 个不同查询同时失败 = 合法覆盖查询（test_saturation_gate 的教训）。"""
        loop = self._loop_with_tool_msgs([])
        msgs = []
        for i in range(3):
            msgs.extend(self._tool_msg(
                f"Error: 网络不可用，任务 {i}", name="mx_query", cid=f"c{i}"))
        loop.current_msgs.extend(msgs)
        assert loop._scan_tool_failures() is None, "同一轮内的多次失败只记一次"

    def test_success_and_guard_replies_not_counted(self):
        loop = self._loop_with_tool_msgs([])
        loop.current_msgs.extend(self._tool_msg("查询结果：| date | 上证 |", name="mx_query"))
        loop.current_msgs.extend(self._tool_msg(
            "⛔ 该调用与此前已成功执行的调用完全相同（mx_query），已拒绝重复执行", name="mx_query"))
        assert loop._scan_tool_failures() is None
        assert loop._fail_sigs == {}, "成功与闸门回复都不该进失败计数"

    def test_varying_numbers_same_error_counts_once_per_round(self):
        loop = self._loop_with_tool_msgs([])
        for i in (1, 2, 3):
            loop.current_msgs.extend(self._tool_msg(f"Error: HTTP 500 after {i * 3} tries"))
            out = loop._scan_tool_failures()
        assert out is not None, "错误里的数字变化不应破坏签名"


@pytest.mark.asyncio
async def test_duplicate_narration_withdraws_bubble():
    """重复叙述抑制要下发空 content_revised（撤回本轮气泡）。

    2026-10-05 occamy 三连复述事故：抑制只改了历史、用户仍看到三遍。
    回归钉住：两轮相同开场白 + 工具调用 → 第二轮必须出现 content="" 的
    content_revised 撤回事件；第一轮（首次出现）不撤。
    """
    from agent.loop import ThinAgentLoop
    from tests.test_loop_scenarios import _StubEngine
    import local_llm
    with FakeEngine() as engine:
        NARR = "分析美股走势情况。"
        engine.push(engine.native_tool_response("list_dir", {"path": "."}, content=NARR))
        engine.push(engine.native_tool_response("list_dir", {"path": "sidecar"}, content=NARR))
        engine.push(engine.text_response(NEUTRAL_TEXT))
        old = local_llm._engine
        local_llm._engine = _StubEngine()
        try:
            events = await _collect(ThinAgentLoop(
                MESSAGES, "fake-model", engine.url, HEADERS,
                session_id=f"thin-wd-{time.time()}", access_mode="full").run())
        finally:
            local_llm._engine = old
        withdraws = [e for e in events
                     if e.get("event") == "content_revised" and not str(e.get("content") or "").strip()]
        assert len(withdraws) == 1, [e.get("event") for e in events]
        # 撤回事件之后不应再有该叙述的 content 增量（终答轮不受影响）
        idx = events.index(withdraws[0])
        tail_texts = [str(e.get("content") or "") for e in events[idx:]
                      if e.get("event") != "content_revised" and e.get("content")]
        assert all(NARR not in t for t in tail_texts), tail_texts


def test_round_text_accumulator_withdraw_and_fold():
    """非流式/cron 的按轮缓冲：撤回丢本轮、折叠换本轮、轮间空行。"""
    from agent.loop import RoundTextAccumulator
    acc = RoundTextAccumulator()
    for ev in [
        {"event": "round_start", "iteration": 1},
        {"content": "分析美股走势情况。"},
        {"event": "tool_start", "tool": "list_dir"},
        {"event": "tool_end"},
        {"event": "round_start", "iteration": 2},
        {"content": "分析美股走势情况。"},
        {"event": "content_revised", "content": ""},          # 撤回第二轮
        {"event": "round_start", "iteration": 3},
        {"content": "美股上周收出小阳周K。"},
        {"event": "round_start", "iteration": 4},
        {"content": "超长叙述" * 300},
        {"event": "content_revised", "content": "（折叠）"},   # 折叠第四轮
    ]:
        acc.feed(ev)
    out = acc.join()
    assert out == "分析美股走势情况。\n\n美股上周收出小阳周K。\n\n（折叠）"
    # 空轮（撤光）不产生空段落
    assert "\n\n\n" not in out


@pytest.mark.asyncio
async def test_cancel_mid_stream_stops_promptly():
    """停止键要在**生成中**生效（2026-10-05 事故回归）。

    此前取消只在每步开头检查：用户点停止后生成还跑完 108.9s。现在 _sample
    每 4 行探测一次取消标记，命中即断流（显式 aclose 关引擎连接）+ run 收尾。
    这里用 10 行流触发 %4 探测路径，断言：只跑 1 轮、以"已停止"收尾。
    """
    import time as _t
    import agent_loop as _al
    from agent.loop import ThinAgentLoop
    from tests.test_loop_scenarios import _StubEngine
    import local_llm

    sid = f"thin-cancel-a-{_t.time()}"
    state = {"n": 0}
    def fake_cancel(_sid):
        state["n"] += 1
        return state["n"] > 1          # 步骤开头第 1 次 False，流中探测起 True

    with FakeEngine() as engine:
        engine.push([_sse({"choices": [{"delta": {"content": "句子。"}}]} ) for _ in range(10)]
                    + ["data: [DONE]\n\n"])
        old = local_llm._engine
        local_llm._engine = _StubEngine()
        orig = _al._session_cancel_requested
        _al._session_cancel_requested = fake_cancel
        try:
            t0 = _t.monotonic()
            events = await _collect(ThinAgentLoop(
                MESSAGES, "fake-model", engine.url, HEADERS,
                session_id=sid, access_mode="full").run())
            elapsed = _t.monotonic() - t0
        finally:
            local_llm._engine = old
            _al._session_cancel_requested = orig

    rounds = [e for e in events if e.get("event") == "round_start"]
    assert len(rounds) == 1, "取消后不得进入下一轮"
    texts = "".join(str(e.get("content", "")) for e in events if isinstance(e, dict))
    assert "已停止" in texts, events
    assert elapsed < 10, f"取消收尾太慢：{elapsed:.1f}s"


@pytest.mark.asyncio
async def test_cancel_before_tool_execution_skips_tool():
    """取消到达后模型刚请求的工具**不得执行**（事故里点了停止还写了文件）。

    短响应走"流末兜底探测"路径：__result__ 仍回传，但 run 见 _cancelled
    直接收尾——不执行 write_file。
    """
    import os
    import time as _t
    import agent_loop as _al
    from agent.loop import ThinAgentLoop
    from tests.test_loop_scenarios import _StubEngine
    import local_llm

    victim = f"/tmp/cancel-skip-{os.getpid()}.txt"
    if os.path.exists(victim):
        os.unlink(victim)

    sid = f"thin-cancel-b-{_t.time()}"
    state = {"n": 0}
    def fake_cancel(_sid):
        state["n"] += 1
        return state["n"] > 1

    with FakeEngine() as engine:
        engine.push(engine.native_tool_response(
            "write_file", {"path": victim, "content": "should not exist"}))
        old = local_llm._engine
        local_llm._engine = _StubEngine()
        orig = _al._session_cancel_requested
        _al._session_cancel_requested = fake_cancel
        try:
            events = await _collect(ThinAgentLoop(
                MESSAGES, "fake-model", engine.url, HEADERS,
                session_id=sid, access_mode="full").run())
        finally:
            local_llm._engine = old
            _al._session_cancel_requested = orig

    assert not os.path.exists(victim), "取消后工具仍被执行（写了文件）"
    assert not any(isinstance(e, dict) and e.get("event") == "tool_start" for e in events), events
    texts = "".join(str(e.get("content", "")) for e in events if isinstance(e, dict))
    assert "已停止" in texts, events


@pytest.mark.asyncio
async def test_tool_failure_promise_fragment_gets_nudged():
    """工具失败后的"承诺碎片"不交付（2026-10-07 事故回归）。

    现场：ak_finance 失败（东财拒连）→ 模型连续两轮只说"这就帮你看看"
    "马上用 mx_query 帮你查"就结束生成（22 字/94 字），任务半途而废。
    修复：工具失败 + 短承诺正文（无数字/表格）→ 注入 toolfail_nudge 继续。
    """
    import time as _t
    from agent.loop import ThinAgentLoop
    from tests.test_loop_scenarios import _StubEngine
    import local_llm

    with FakeEngine() as engine:
        # ① 模型调 read_file（不存在 → Error 结果 = 工具失败）
        engine.push(engine.native_tool_response("read_file", {"path": "/nonexistent-xyz"}))
        # ② 模型只说承诺（无工具调用）
        engine.push(engine.text_response("好的主人，欧娜这就帮你看看这三只票的情况～💋"))
        # ③ 被 nudge 后给出完整回答
        engine.push(engine.text_response(NEUTRAL_TEXT))
        old = local_llm._engine
        local_llm._engine = _StubEngine()
        try:
            events = await _collect(ThinAgentLoop(
                MESSAGES, "fake-model", engine.url, HEADERS,
                session_id=f"thin-pf-{_t.time()}", access_mode="full").run())
        finally:
            local_llm._engine = old

    texts = "".join(str(e.get("content", "")) for e in events if isinstance(e, dict))
    # 承诺碎片不能作为终答交付（后续有完整回答 = 循环被续跑）
    assert "根据刚才的目录输出" in texts, "nudge 后应继续循环并交付完整回答"
    # 碎片本身也不该出现（它只应作为历史，不当终答下发）——宽松断言：
    # 交付的最后一条不是碎片
    assert not texts.rstrip().endswith("💋"), texts[-200:]


def test_promise_regex_matches_incident_fragments():
    """事故里的两句承诺语必须命中 _PROMISE_RE（正则即回归）。"""
    from agent.loop import _PROMISE_RE, _DATA_SIGNAL_RE
    a = "好的主人，欧娜这就帮你看看这三只票的情况～💋"
    b = ("哎呀主人别急嘛～刚才 ak_finance 的东财接口抽风了（连接被拒绝），"
         "欧娜没及时切到备用工具，所以卡住了 😘 现在马上用 mx_query 帮你查这三只票的情况！💋")
    assert _PROMISE_RE.search(a)
    assert _PROMISE_RE.search(b)
    assert not _DATA_SIGNAL_RE.search(a)   # 无数字/表格
    assert not _DATA_SIGNAL_RE.search(b)
    # 对照：含数据的真答案不该命中承诺信号
    real = "中天科技 收 12.34 元（+2.5%），领益智造 收 8.90 元\n| a | b |\n|---|---|"
    assert _DATA_SIGNAL_RE.search(real)
