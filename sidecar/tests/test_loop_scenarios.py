"""阶段 2b 机检基线：真实循环 × 假引擎（OpenAI 兼容 SSE 桩）端到端场景。

场景矩阵（当前 v1 基线，后续 2b 合并后必须同等通过）：
- 纯文本单轮（云/本地）
- 工具轮次：模型发 run_cmd ls -la → 工具真实执行 → 第二轮请求携带工具结果
  （桩校验请求体）→ 最终回复
- 流中取消：循环停止且无异常泄漏
跑通此矩阵 = 合并 AgentLoop 时的机检闸门（无需真实模型，20 场景可扩）。
"""
import asyncio
import os
import time

import httpx
import pytest

from tests.fake_engine import FakeEngine

from agent_loop import (
    _agent_loop_stream,
    _clear_session_cancel,
    _local_agent_loop_stream,
    _request_session_cancel,
)

NEUTRAL_TEXT = ("根据刚才的目录输出，这个目录里包含 agents、skills、tests 等目录以及若干 python 文件，"
                "整体结构清晰，具体文件清单见上方工具结果。目录层级与文件组织方式符合常见工程惯例，"
                "对后续分析没有障碍。") * 3

MESSAGES = [{"role": "user", "content": "列出当前目录并告诉我结果"}]
HEADERS = {"Authorization": "Bearer fake"}


class _StubEngine:
    """引擎桩：测试绝对不允许触碰真实 local_llm._engine（此前 v1 本地循环经
    _local_llm_stream 的恢复路径触发过一次真实模型重载——"自动重载已启动"日志
    即证据）。所有状态为"已停/禁用"，恢复路径永远不启动。"""

    current_model_id = ""
    server_status = "stopped"
    _auto_reloading = True
    _explicit_stop = True

    def mark_engine_busy(self, *a, **k):
        pass

    def mark_stream_enter(self):
        pass

    def mark_stream_exit(self):
        pass

    def mark_engine_idle(self):
        pass

    def _kill_port(self, *a):
        pass

    def _request_reload(self, *a):
        return False


@pytest.fixture(autouse=True)
def _stub_engine(monkeypatch):
    import local_llm
    monkeypatch.setattr(local_llm, "_engine", _StubEngine())


async def _collect(agen, timeout=90):
    out = []
    async for chunk in agen:
        out.append(chunk)
    return out


@pytest.mark.asyncio
async def test_cloud_text_only():
    with FakeEngine() as engine:
        engine.push(engine.text_response("你好，我来帮你。"))
        events = await _collect(_agent_loop_stream(
            MESSAGES, "fake-model", engine.url, HEADERS,
            session_id=f"t-cloud-text-{time.time()}", access_mode="full",
        ))
        texts = [e.get("content", "") for e in events if "content" in e]
        assert any("你好" in t for t in texts), events


@pytest.mark.asyncio
async def test_cloud_tool_round_trip():
    with FakeEngine() as engine:
        engine.push(engine.tool_response("run_cmd", {"cmd": "ls -la"}))
        engine.push(engine.asserts_tool_result_present("total"))
        events = await _collect(_agent_loop_stream(
            MESSAGES, "fake-model", engine.url, HEADERS,
            session_id=f"t-cloud-tool-{time.time()}", access_mode="full",
        ))
        assert len(engine.requests) >= 2, "模型应进行第二轮请求"
        # 工具执行（ls -la 输出）出现在第二轮请求体 —— 由桩回调断言；这里补事件断言
        texts = [e.get("content", "") for e in events if "content" in e]
        assert any("完成" in t for t in texts), events


@pytest.mark.asyncio
async def test_cloud_cancel_mid_stream():
    with FakeEngine() as engine:
        def slow_then_cancel_check(body):
            return [
                "data: " + '{"choices":[{"delta":{"content":"开始"}, "index":0}]}' + "\n\n",
                "data: [DONE]\n\n",
            ]
        engine.push(slow_then_cancel_check)

        sid = f"t-cloud-cancel-{time.time()}"
        _request_session_cancel(sid)
        _clear_session_cancel(sid)
        _request_session_cancel(sid)  # 停止已置位 → 循环应在检查点收尾

        events = await _collect(_agent_loop_stream(
            MESSAGES, "fake-model", engine.url, HEADERS,
            session_id=sid, access_mode="full",
        ))
        # 循环不得抛异常；停止后事件流结束
        assert isinstance(events, list)


@pytest.mark.asyncio
async def test_local_text_only():
    with FakeEngine() as engine:
        engine.push(engine.text_response("本地模型回复。"))
        events = await _collect(_local_agent_loop_stream(
            MESSAGES, "fake-model", engine.url, HEADERS,
            session_id=f"t-local-text-{time.time()}", access_mode="full",
        ))
        texts = [e.get("content", "") for e in events if "content" in e]
        assert any("本地模型回复" in t for t in texts), events


@pytest.mark.asyncio
async def test_local_tool_round_trip():
    final_text = NEUTRAL_TEXT  # <80 会被短回答闸门拦下
    with FakeEngine() as engine:
        # 注意：测试环境工具注册表是种子表（插件工具未加载），选 list_dir
        engine.push(engine.local_tool_response("list_dir", {"path": "."}))
        engine.push(engine.asserts_tool_result_present("agent_loop.py"))
        engine.push(engine.text_response(final_text))
        events = await _collect(_local_agent_loop_stream(
            MESSAGES, "fake-model", engine.url, HEADERS,
            session_id=f"t-local-tool-{time.time()}", access_mode="full",
        ))
        assert len(engine.requests) >= 2, "本地循环应进行第二轮请求"
        # 工具轮次证明：local_fence 工具被真实执行（tool_start 事件）且结果回传
        assert any(e.get("event") == "tool_start" for e in events), events
        # 第三轮（工具结果后的续写）交付 ≥80 字符正文
        texts = [e.get("content", "") for e in events if "content" in e]
        assert any("根据刚才的目录输出" in t for t in texts), events


# ═══════════════════════════════════════════════════════════════════════
# 2b 统一驱动（AgentLoop v2）——同一场景矩阵必须等价通过
# ═══════════════════════════════════════════════════════════════════════

def _collect_v2(engine_kind, *, script, session_id):
    from agent_loop_v2 import AgentLoop
    return _collect(AgentLoop(
        engine_kind, MESSAGES, "fake-model", None, HEADERS,  # api_url 由脚本补
        session_id=session_id, access_mode="full",
    ).run())


@pytest.mark.asyncio
async def test_v2_cloud_text_only():
    from agent_loop_v2 import AgentLoop
    with FakeEngine() as engine:
        engine.push(engine.text_response("你好，我来帮你。"))
        events = await _collect(AgentLoop(
            "cloud", MESSAGES, "fake-model", engine.url, HEADERS,
            session_id=f"v2-cloud-text-{time.time()}", access_mode="full",
        ).run())
        texts = [e.get("content", "") for e in events if "content" in e]
        assert any("你好" in t for t in texts), events


@pytest.mark.asyncio
async def test_v2_cloud_tool_round_trip():
    from agent_loop_v2 import AgentLoop
    with FakeEngine() as engine:
        engine.push(engine.tool_response("run_cmd", {"cmd": "ls -la"}))
        engine.push(engine.asserts_tool_result_present("total"))
        events = await _collect(AgentLoop(
            "cloud", MESSAGES, "fake-model", engine.url, HEADERS,
            session_id=f"v2-cloud-tool-{time.time()}", access_mode="full",
        ).run())
        assert len(engine.requests) >= 2, "v2 云端应进行第二轮请求"
        assert any(e.get("event") == "tool_start" for e in events), events
        texts = [e.get("content", "") for e in events if "content" in e]
        assert any("完成" in t for t in texts), events


@pytest.mark.asyncio
async def test_v2_cloud_cancel_mid_stream():
    from agent_loop_v2 import AgentLoop
    from agent_loop import _request_session_cancel, _clear_session_cancel
    with FakeEngine() as engine:
        engine.push(engine.text_response("开始"))
        sid = f"v2-cloud-cancel-{time.time()}"
        _clear_session_cancel(sid)
        _request_session_cancel(sid)
        events = await _collect(AgentLoop(
            "cloud", MESSAGES, "fake-model", engine.url, HEADERS,
            session_id=sid, access_mode="full",
        ).run())
        assert isinstance(events, list)  # 停止后不得抛异常


@pytest.mark.asyncio
async def test_v2_local_text_only():
    from agent_loop_v2 import AgentLoop
    with FakeEngine() as engine:
        engine.push(engine.text_response("本地模型回复。"))
        events = await _collect(AgentLoop(
            "local", MESSAGES, "fake-model", engine.url, HEADERS,
            session_id=f"v2-local-text-{time.time()}", access_mode="full",
        ).run())
        texts = [e.get("content", "") for e in events if "content" in e]
        assert any("本地模型回复" in t for t in texts), events


@pytest.mark.asyncio
async def test_v2_local_tool_round_trip():
    from agent_loop_v2 import AgentLoop
    final_text = NEUTRAL_TEXT
    with FakeEngine() as engine:
        engine.push(engine.local_tool_response("list_dir", {"path": "."}))
        engine.push(engine.asserts_tool_result_present("agent_loop.py"))
        engine.push(engine.text_response(final_text))
        events = await _collect(AgentLoop(
            "local", MESSAGES, "fake-model", engine.url, HEADERS,
            session_id=f"v2-local-tool-{time.time()}", access_mode="full",
        ).run())
        assert len(engine.requests) >= 2, "v2 本地应进行第二轮请求"
        assert any(e.get("event") == "tool_start" for e in events), events
        texts = [e.get("content", "") for e in events if "content" in e]
        assert any("根据刚才的目录输出" in t for t in texts), events


# ═══════════════════════════════════════════════════════════════════════
# 非任务消息零追问（17:11 事故回归）：闲聊 + 模型乱调工具 → 不得 nudge 循环
# ═══════════════════════════════════════════════════════════════════════
CHAT_MSGS = [{"role": "user", "content": "你能做什么"}]
NO_NUDGE_TEXT = "我能做很多事情，有任务随时吩咐。"


@pytest.mark.asyncio
async def test_chat_no_nudge_v2_local():
    from agent_loop_v2 import AgentLoop
    with FakeEngine() as engine:
        engine.push(engine.local_tool_response("list_dir", {"path": "."}))
        engine.push(engine.text_response(NO_NUDGE_TEXT))
        events = await _collect(AgentLoop(
            "local", CHAT_MSGS, "fake-model", engine.url, HEADERS,
            session_id=f"v2-chat-local-{time.time()}", access_mode="full",
        ).run())
        assert len(engine.requests) == 2, "闲聊+乱调工具必须 2 请求内结束（零 nudge）"
        assert not any(e.get("event") == "heartbeat" for e in events), events
        texts = [e.get("content", "") for e in events if "content" in e]
        assert any(NO_NUDGE_TEXT in t for t in texts), events


@pytest.mark.asyncio
async def test_chat_no_nudge_v2_cloud():
    from agent_loop_v2 import AgentLoop
    with FakeEngine() as engine:
        engine.push(engine.tool_response("list_dir", {"path": "."}))
        engine.push(engine.text_response(NO_NUDGE_TEXT))
        events = await _collect(AgentLoop(
            "cloud", CHAT_MSGS, "fake-model", engine.url, HEADERS,
            session_id=f"v2-chat-cloud-{time.time()}", access_mode="full",
        ).run())
        assert len(engine.requests) == 2, "云端闲聊+乱调工具同样零 nudge"
        texts = [e.get("content", "") for e in events if "content" in e]
        assert any(NO_NUDGE_TEXT in t for t in texts), events


@pytest.mark.asyncio
async def test_chat_no_nudge_v1_local():
    with FakeEngine() as engine:
        engine.push(engine.local_tool_response("list_dir", {"path": "."}))
        engine.push(engine.text_response(NO_NUDGE_TEXT))
        events = await _collect(_local_agent_loop_stream(
            CHAT_MSGS, "fake-model", engine.url, HEADERS,
            session_id=f"v1-chat-local-{time.time()}", access_mode="full",
        ))
        assert len(engine.requests) == 2, "v1 本地闲聊同样零 nudge"
        texts = [e.get("content", "") for e in events if "content" in e]
        assert any(NO_NUDGE_TEXT in t for t in texts), events


@pytest.mark.asyncio
async def test_chat_no_nudge_v1_cloud():
    with FakeEngine() as engine:
        engine.push(engine.tool_response("list_dir", {"path": "."}))
        engine.push(engine.text_response(NO_NUDGE_TEXT))
        events = await _collect(_agent_loop_stream(
            CHAT_MSGS, "fake-model", engine.url, HEADERS,
            session_id=f"v1-chat-cloud-{time.time()}", access_mode="full",
        ))
        assert len(engine.requests) == 2, "v1 云端闲聊同样零 nudge"
        texts = [e.get("content", "") for e in events if "content" in e]
        assert any(NO_NUDGE_TEXT in t for t in texts), events


@pytest.mark.asyncio
async def test_v2_cloud_empty_tool_name_guarded():
    """17:23 事故云端回归：delta 工具名空串 → 守卫拦截 + 模型拿到可操作提示。"""
    from agent_loop_v2 import AgentLoop
    with FakeEngine() as engine:
        engine.push(engine.tool_response("", {"path": "."}))
        engine.push(engine.asserts_tool_result_present("工具名为空"))
        events = await _collect(AgentLoop(
            "cloud", [{"role": "user", "content": "测试"}], "fake-model", engine.url, HEADERS,
            session_id=f"v2-empty-name-{time.time()}", access_mode="full",
        ).run())
        assert len(engine.requests) == 2, "守卫后的引导轮应结束"
        texts = [e.get("content", "") for e in events if "content" in e]
        assert any("完成" in t for t in texts), events


# ═══════════════════════════════════════════════════════════════════════
# 09-05 23:52 事故回归：8 分钟纯思考不写正文 → nudge 角色 + 思考预算 + 轮次事件
# ═══════════════════════════════════════════════════════════════════════

ANALYSIS_TEXT = ("根据表格数据，通信板块主力资金净流入 129 亿，居全市场首位；"
                 "CPO 概念涨幅 3.59% 领先，主力净流入 124 亿紧随其后；板块资金整体呈现"
                 "「通信领涨、CPO 跟涨、算力回调」的轮动格局。从成交额看，通信板块成交 1806 亿"
                 "放大明显，说明资金参与度在提升；散户资金净流出 45 亿，筹码向机构集中，"
                 "属于典型的「主力吸筹、散户出局」结构。结论：通信与 CPO 短线资金强度居前，"
                 "轮动大概率延续，可重点关注分歧后的低吸机会，同时防范高位板块的获利回吐。")

THINK_NUDGE_MARK = "只输出了思考过程"


@pytest.mark.asyncio
async def test_local_thinking_only_nudge_is_user_role():
    """23:52 事故回归：思考-only 轮 → nudge 必须是末尾 user 角色（不得追加
    末尾 system）、system 保持 1 条；每轮发 round_start（[1,2]）。"""
    with FakeEngine() as engine:
        # 思考轮 → 终答提取会额外消费一次请求（filler）→ nudge 轮 → 正文
        engine.push(engine.thinking_only_response("让我先梳理一下板块轮动的逻辑。"))
        engine.push(engine.text_response("x"))  # 终答提取吞掉的占位
        engine.push(engine.text_response(ANALYSIS_TEXT))
        events = await _collect(_local_agent_loop_stream(
            MESSAGES, "fake-model", engine.url, HEADERS,
            session_id=f"t-local-thinkonly-{time.time()}", access_mode="full",
        ))
        reqs = [q for q in engine.requests if q.get("stream")]
        assert len(reqs) >= 2, f"应有 nudge 后的第二轮流式请求：{len(engine.requests)} 个请求"
        roles = [m["role"] for m in reqs[1]["messages"]]
        assert roles.count("system") == 1, f"system 必须只有 1 条：{roles}"
        assert roles[-1] == "user", f"nudge 必须是末尾 user：{roles}"
        assert THINK_NUDGE_MARK in str(reqs[1]["messages"][-1]["content"]), (
            "nudge 文案缺失: " + str(reqs[1]["messages"][-1]["content"])[:200])
        rounds = [e["iteration"] for e in events if e.get("event") == "round_start"]
        assert rounds == [1, 2], f"round_start 应为 [1,2]：{rounds}"
        texts = [e.get("content", "") for e in events if "content" in e]
        assert any("通信板块" in t for t in texts), events


@pytest.mark.asyncio
async def test_v2_local_thinking_only_nudge_user_role():
    """v2 同款回归：思考-only → nudge 为 user、round_start 事件齐全。"""
    from agent_loop_v2 import AgentLoop
    # 用户消息的数字必须与正文数字同格式（v2 交付前有数字溯源闸门，
    # "129" vs "129亿" 会被当成编造数字触发 nudge）
    table_msgs = [{"role": "user", "content": (
        "这是板块资金分析表：通信板块 主力净流入129亿、成交额1806亿、散户-45亿；"
        "CPO概念 涨幅3.59%、主力净流入124亿。请分析轮动。")}]
    with FakeEngine() as engine:
        engine.push(engine.thinking_only_response("让我先梳理一下板块轮动的逻辑。"))
        engine.push(engine.text_response("x"))  # 终答提取占位
        engine.push(engine.text_response(ANALYSIS_TEXT))
        events = await _collect(AgentLoop(
            "local", table_msgs, "fake-model", engine.url, HEADERS,
            session_id=f"v2-thinkonly-{time.time()}", access_mode="full",
        ).run())
        reqs = [q for q in engine.requests if q.get("stream")]
        assert len(reqs) >= 2, f"应有 nudge 后的第二轮流式请求：{len(engine.requests)} 个请求"
        roles = [m["role"] for m in reqs[1]["messages"]]
        assert roles[-1] == "user", f"v2 nudge 必须是末尾 user：{roles}"
        rounds = [e["iteration"] for e in events if e.get("event") == "round_start"]
        assert rounds == [1, 2], f"v2 round_start 应为 [1,2]：{rounds}"
        texts = [e.get("content", "") for e in events if "content" in e]
        assert any("主力资金净流入" in t for t in texts), events


@pytest.mark.asyncio
async def test_local_long_input_think_budget():
    """23:52 事故回归：>8K 字符输入首轮 must 注入思考预算（防 27B 纯思考 8 分钟）。"""
    long_user = "板块资金分析表如下：\n" + ("通信 +2.46% 1806 129 -45\n" * 700)
    with FakeEngine() as engine:
        engine.push(engine.text_response(ANALYSIS_TEXT))
        events = await _collect(_local_agent_loop_stream(
            [{"role": "user", "content": long_user}], "fake-model", engine.url, HEADERS,
            session_id=f"t-local-budget-{time.time()}", access_mode="full",
        ))
        assert engine.requests, "至少一次请求"
        sys_text = engine.requests[0]["messages"][0]["content"]
        assert "思考预算" in sys_text, f"首轮 system 缺少思考预算指令：{sys_text[:300]}"
        rounds = [e["iteration"] for e in events if e.get("event") == "round_start"]
        assert rounds == [1], events


@pytest.mark.asyncio
async def test_stream_registration_balance_with_suspect():
    """09-05 23:31 引擎被误杀回归：suspect 验证路径 enter/exit 只配对一次——
    请求进行中引擎保持"忙"注册，退出后计数归零（此前双重 exit 打成 -1，
    busy_until 清零让健康检查对正忙的引擎双连败误杀重载）。"""
    import agent_loop
    import local_llm
    counters = {"busy": 0, "idle": 0, "streams": 0}

    class _CountingEngine(_StubEngine):
        current_model_id = "fake-model"

        def mark_engine_busy(self, *a, **k):
            counters["busy"] += 1

        def mark_engine_idle(self):
            counters["idle"] += 1

        def mark_stream_enter(self):
            counters["streams"] += 1

        def mark_stream_exit(self):
            counters["streams"] -= 1

    old_engine = local_llm._engine
    local_llm._engine = _CountingEngine()
    agent_loop._llm_suspect_since = time.monotonic()
    try:
        with FakeEngine() as engine:
            # 第一次请求是 suspect 健康验证（非流式，吞占位响应）
            engine.push(engine.text_response("ok"))
            engine.push(engine.text_response("hello"))  # 真实流
            body = {"model": "fake-model", "stream": True,
                    "messages": [{"role": "user", "content": "hi"}]}
            got_chunk = False
            async with httpx.AsyncClient(timeout=httpx.Timeout(60)) as client:
                async with agent_loop._local_llm_stream(
                        client, engine.url, body, HEADERS) as r:
                    # 请求进行中：忙注册必须可见（健康检查据此跳过探测）
                    assert counters["streams"] == 1, (
                        f"流进行中 _active_local_streams 应为 1：{counters}")
                    aiter = r.aiter_lines()
                    while True:
                        line = await aiter.__anext__()
                        if line.startswith("data: "):
                            got_chunk = True
                            if line[6:] == "[DONE]":
                                break
            assert got_chunk, "真实流未收到任何 chunk"
            assert counters["streams"] == 0, f"退出后计数必须归零：{counters}"
            assert counters["busy"] == 1, f"busy 只能标记一次：{counters}"
    finally:
        agent_loop._llm_suspect_since = None
        local_llm._engine = old_engine


# ═══════════════════════════════════════════════════════════════════════
# 09-06 原生 function calling 三档模式：native tools / 闲聊快车道 / 400 回退
# ═══════════════════════════════════════════════════════════════════════

import json as _json  # noqa: E402


@pytest.mark.asyncio
async def test_local_native_tools_round_trip():
    """原生 tools 参数下发 → delta.tool_calls 执行 → assistant 携 tool_calls 回传。"""
    import agent_loop
    with FakeEngine() as engine:
        def _round2(body):
            assert isinstance(body.get("tools"), list) and body["tools"], \
                "第二轮仍应携带原生 tools 参数"
            asst = [m for m in body["messages"]
                    if m.get("role") == "assistant" and m.get("tool_calls")]
            assert asst, "原生模式 assistant 消息必须携带 tool_calls"
            return FakeEngine.text_response(NEUTRAL_TEXT)
        engine.push(engine.native_tool_response("list_dir", {"path": "."}))
        engine.push(_round2)
        agent_loop._LOCAL_NATIVE_TOOLS_OVERRIDE = True
        try:
            events = await _collect(_local_agent_loop_stream(
                MESSAGES, "fake-model", engine.url, HEADERS,
                session_id=f"t-native-{time.time()}", access_mode="full",
            ))
        finally:
            agent_loop._LOCAL_NATIVE_TOOLS_OVERRIDE = None
        req1 = engine.requests[0]
        assert isinstance(req1.get("tools"), list) and req1["tools"], "首轮请求必须携带 tools"
        assert "```tool" not in _json.dumps(req1, ensure_ascii=False), \
            "原生模式不得注入围栏提示词"
        assert any(e.get("event") == "tool_start" for e in events), "原生工具应被执行"
        texts = [e.get("content", "") for e in events if "content" in e]
        assert any("根据刚才的目录输出" in t for t in texts), events


@pytest.mark.asyncio
async def test_v2_local_native_tools_round_trip():
    """v2 同款：原生 tools 下发 + delta.tool_calls 摄取 + 工具轮次。"""
    import agent_loop
    from agent_loop_v2 import AgentLoop
    with FakeEngine() as engine:
        engine.push(engine.native_tool_response("list_dir", {"path": "."}))
        engine.push(engine.asserts_tool_result_present("agent_loop.py"))
        engine.push(engine.text_response(NEUTRAL_TEXT))
        agent_loop._LOCAL_NATIVE_TOOLS_OVERRIDE = True
        try:
            events = await _collect(AgentLoop(
                "local", MESSAGES, "fake-model", engine.url, HEADERS,
                session_id=f"v2-native-{time.time()}", access_mode="full",
            ).run())
        finally:
            agent_loop._LOCAL_NATIVE_TOOLS_OVERRIDE = None
        assert isinstance(engine.requests[0].get("tools"), list), "v2 首轮应携带 tools"
        assert any(e.get("event") == "tool_start" for e in events), events
        texts = [e.get("content", "") for e in events if "content" in e]
        assert any("根据刚才的目录输出" in t for t in texts), events


@pytest.mark.asyncio
async def test_local_light_query_fast_path():
    """闲聊快车道：不传 tools、enable_thinking=false、零工具提示注入。"""
    with FakeEngine() as engine:
        engine.push(engine.text_response("收到，一切正常！有什么需要帮忙的吗？"))
        events = await _collect(_local_agent_loop_stream(
            [{"role": "user", "content": "测试"}], "fake-model", engine.url, HEADERS,
            session_id=f"t-light-{time.time()}", access_mode="full",
        ))
        body = engine.requests[0]
        assert "tools" not in body, f"快车道不得携带 tools：{list(body.keys())}"
        assert (body.get("chat_template_kwargs") or {}).get("enable_thinking") is False, \
            "快车道必须关闭思考"
        sys_text = _json.dumps(body["messages"], ensure_ascii=False)
        assert "```tool" not in sys_text and "可用工具" not in sys_text, "快车道不得注入工具提示"
        texts = [e.get("content", "") for e in events if "content" in e]
        assert any("收到" in t for t in texts), events


@pytest.mark.asyncio
async def test_v2_local_light_query_fast_path():
    """v2 闲聊快车道同款。"""
    from agent_loop_v2 import AgentLoop
    with FakeEngine() as engine:
        engine.push(engine.text_response("收到，一切正常！有什么需要帮忙的吗？"))
        events = await _collect(AgentLoop(
            "local", [{"role": "user", "content": "测试"}], "fake-model", engine.url, HEADERS,
            session_id=f"v2-light-{time.time()}", access_mode="full",
        ).run())
        body = engine.requests[0]
        assert "tools" not in body, "v2 快车道不得携带 tools"
        assert (body.get("chat_template_kwargs") or {}).get("enable_thinking") is False
        texts = [e.get("content", "") for e in events if "content" in e]
        assert any("收到" in t for t in texts), events


@pytest.mark.asyncio
async def test_local_native_400_fallback():
    """引擎拒绝 tools（400，模板不支持工具）→ 自动回退围栏提示词，任务完成。"""
    import agent_loop
    with FakeEngine() as engine:
        engine.push(engine.http_error(400, "model does not support tool calling"))
        engine.push(engine.local_tool_response("list_dir", {"path": "."}))
        engine.push(engine.text_response(NEUTRAL_TEXT))
        agent_loop._LOCAL_NATIVE_TOOLS_OVERRIDE = True
        try:
            events = await _collect(_local_agent_loop_stream(
                MESSAGES, "fake-model", engine.url, HEADERS,
                session_id=f"t-400fb-{time.time()}", access_mode="full",
            ))
        finally:
            agent_loop._LOCAL_NATIVE_TOOLS_OVERRIDE = None
        req1, req2 = engine.requests[0], engine.requests[1]
        assert "tools" in req1, "首次请求应携带原生 tools"
        assert "tools" not in req2, "回退后不得再携带 tools"
        assert "```tool" in _json.dumps(req2, ensure_ascii=False), "回退后应注入围栏提示词"
        assert any(e.get("event") == "tool_start" for e in events), events
        texts = [e.get("content", "") for e in events if "content" in e]
        assert any("根据刚才的目录输出" in t for t in texts), events


@pytest.mark.asyncio
async def test_v2_local_native_400_fallback():
    """v2 同款 400 回退。"""
    import agent_loop
    from agent_loop_v2 import AgentLoop
    with FakeEngine() as engine:
        engine.push(engine.http_error(400, "model does not support tool calling"))
        engine.push(engine.local_tool_response("list_dir", {"path": "."}))
        engine.push(engine.text_response(NEUTRAL_TEXT))
        agent_loop._LOCAL_NATIVE_TOOLS_OVERRIDE = True
        try:
            events = await _collect(AgentLoop(
                "local", MESSAGES, "fake-model", engine.url, HEADERS,
                session_id=f"v2-400fb-{time.time()}", access_mode="full",
            ).run())
        finally:
            agent_loop._LOCAL_NATIVE_TOOLS_OVERRIDE = None
        assert "tools" in engine.requests[0]
        assert "tools" not in engine.requests[1], "v2 回退后不得再携带 tools"
        texts = [e.get("content", "") for e in events if "content" in e]
        assert any("根据刚才的目录输出" in t for t in texts), events
