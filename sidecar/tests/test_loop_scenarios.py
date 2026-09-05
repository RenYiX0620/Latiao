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

MESSAGES = [{"role": "user", "content": "列出当前目录并告诉我结果"}]
HEADERS = {"Authorization": "Bearer fake"}


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
    final_text = "已完成目录查看，这是目录里的主要文件清单与结构说明。" * 6  # <80 会被短回答闸门拦下
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
        assert any("已完成目录查看" in t for t in texts), events
