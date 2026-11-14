"""2026-11-02 真机回归：缺开头 `<tool_call>` 的 function 方言被当正文交付。

现场（BAAI_AREX-2-Q4_K_M，14:25）：模型输出
    <function=market_insight>
    <parameter=focus>
    大盘
    </parameter>
    </function>
    </tool_call>
——**没有开头的 `<tool_call>`**。`_looks_like_tool_markup` 认得出，但
`_parse_prompt_tool_calls` 的 `_TOOLCALL_FN_XML_RE` 要求首尾标签齐全 → 0 个调用，
日志 `工具解析 | 原生=0`、`工具调用=0`，用户看到裸标签（14:29 又用 mx_query 重演一次）。

修法：外层 `<tool_call>` 开/闭都改为可选，孤立收尾标记一并剥掉。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.parsing import _parse_prompt_tool_calls  # noqa: E402

REAL = ("<function=market_insight>\n<parameter=focus>\n大盘\n</parameter>\n"
        "</function>\n</tool_call>")


def _one(text):
    cleaned, calls = _parse_prompt_tool_calls(text)
    assert len(calls) == 1, f"应解析出 1 个调用，实际 {len(calls)}"
    return cleaned, calls[0]


def test_missing_opening_tag_is_parsed():
    """真机原文：缺开头标签也必须识别，参数原样传对。"""
    cleaned, call = _one(REAL)
    assert call["function"]["name"] == "market_insight"
    assert json.loads(call["function"]["arguments"]) == {"focus": "大盘"}
    assert cleaned.strip() == "", "标记必须从正文里剥干净"


def test_complete_form_still_parsed():
    """反控：首尾齐全的老格式不受影响。"""
    text = ('<tool_call>\n<function=read_file>\n<parameter=path>\n/tmp/a.txt\n'
            '</parameter>\n</function>\n</tool_call>')
    cleaned, call = _one(text)
    assert call["function"]["name"] == "read_file"
    assert json.loads(call["function"]["arguments"]) == {"path": "/tmp/a.txt"}
    assert cleaned.strip() == ""


def test_missing_closing_tag_is_parsed():
    text = ("<tool_call>\n<function=mx_query>\n<parameter=query>\n上证指数\n"
            "</parameter>\n</function>")
    cleaned, call = _one(text)
    assert call["function"]["name"] == "mx_query"
    assert json.loads(call["function"]["arguments"]) == {"query": "上证指数"}


def test_two_bare_calls_are_both_parsed():
    text = ("<function=mx_query>\n<parameter=query>\n上证\n</parameter>\n</function>\n"
            "<function=market_insight>\n<parameter=focus>\n大盘\n</parameter>\n</function>")
    _, calls = _parse_prompt_tool_calls(text)
    assert [c["function"]["name"] for c in calls] == ["mx_query", "market_insight"]


# ── 循环级：解析出来必须真的执行，并且把裸标签气泡撤掉 ──────────────────────
import asyncio  # noqa: E402
import time  # noqa: E402

import pytest  # noqa: E402

from tests.fake_engine import FakeEngine  # noqa: E402

BARE_CALL = ("<function=list_dir>\n<parameter=path>\n.\n</parameter>\n"
             "</function>\n</tool_call>")


@pytest.mark.asyncio
async def test_bare_markup_executes_tool_and_withdraws_bubble():
    import agent.context as agent_context
    import local_llm
    from agent.loop import ThinAgentLoop
    from tests.test_loop_scenarios import _StubEngine

    with FakeEngine() as engine:
        engine.push(FakeEngine.text_response(BARE_CALL))
        engine.push(FakeEngine.text_response("目录已列出。"))
        old = local_llm._engine
        local_llm._engine = _StubEngine()
        agent_context._LOCAL_NATIVE_TOOLS_OVERRIDE = True
        try:
            loop = ThinAgentLoop(
                [{"role": "user", "content": "看看当前目录"}], "fake-model", engine.url,
                {"Authorization": "Bearer fake"},
                session_id=f"bare-call-{time.time()}", access_mode="full",
                thinking_level="off")
            events = [e async for e in loop.run()]
        finally:
            local_llm._engine = old
            agent_context._LOCAL_NATIVE_TOOLS_OVERRIDE = None

    assert any(e.get("event") == "tool_end" and e.get("tool") == "list_dir"
               for e in events if isinstance(e, dict)), "裸方言没有真的执行"
    assert any(e.get("event") == "content_revised" and not str(e.get("content") or "").strip()
               for e in events if isinstance(e, dict)), "裸标签气泡没有撤回"
    assert len(engine.requests) >= 2
