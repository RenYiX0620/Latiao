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
