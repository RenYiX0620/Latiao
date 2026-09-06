"""Stage 2 薄循环测试矩阵：模型驱动终止、错误即结果、原生协议、快车道、steer。

对标 Codex/dph 场景矩阵：错误回填→模型自纠（替代 v1 nudge 用例）。
"""
import json
import time

import pytest

from tests.fake_engine import FakeEngine

HEADERS = {"Authorization": "Bearer fake"}
NEUTRAL_TEXT = ("根据刚才的目录输出，这个目录里包含 agents、skills、tests 等目录以及若干 python 文件，"
                "整体结构清晰，具体文件清单见上方工具结果。") * 2

MESSAGES = [{"role": "user", "content": "列出当前目录并告诉我结果"}]


async def _collect(agen):
    out = []
    async for chunk in agen:
        out.append(chunk)
    return out


def _run_loop(engine, messages, *, access_mode="full", session_suffix=""):
    """构造 ThinAgentLoop（本地引擎模式）。"""
    import agent_loop
    import agent.transport as transport
    from agent.loop import ThinAgentLoop
    from tests.test_loop_scenarios import _StubEngine
    import local_llm

    old = local_llm._engine
    local_llm._engine = _StubEngine()
    old_override = getattr(transport, "_LOCAL_NATIVE_TOOLS_OVERRIDE", None)
    agent_loop_stub = agent_loop
    agent_loop_stub._LOCAL_NATIVE_TOOLS_OVERRIDE = None
    agent_context._LOCAL_NATIVE_TOOLS_OVERRIDE = True  # 测试强制原生（假引擎当 mlx）
    sid = f"thin-{time.time()}-{session_suffix}"
    return sid, ThinAgentLoop(messages, "fake-model", engine.url, HEADERS,
                              session_id=sid, access_mode=access_mode)


def _finally_restore():
    import agent_loop
    import agent.context as agent_context
    import local_llm
    # 占位：真正的恢复在测试体内 try/finally 完成


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
    import agent_loop
    import agent.context as agent_context
    import local_llm
    from tests.test_loop_scenarios import _StubEngine
    from agent.loop import ThinAgentLoop

    def _round2(body):
        assert isinstance(body.get("tools"), list) and body["tools"], "第二轮仍应携带 tools"
        asst = [m for m in body["messages"]
                if m.get("role") == "assistant" and m.get("tool_calls")]
        assert asst, "原生模式 assistant 消息必须携带 tool_calls"
        assert (body.get("chat_template_kwargs") or {}).get("enable_thinking") is False, \
            "工具后续轮必须关闭思考"
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
    import agent_loop
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
    import agent.context as agent_context
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
    import agent.context as agent_context
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
    import agent_loop
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
