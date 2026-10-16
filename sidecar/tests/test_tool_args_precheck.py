"""必填参数预校验（2026-10-16）：计划轮盲猜参数 → 纠正提示、不执行、不留气泡。

真机来龙去脉（10-15「分析今天大盘」）：两段式第 1 轮不下发工具表，模型把
mx_query 猜成 {"market":"A股","type":"大盘指数"} → 工具照跑才报
`Error: query parameter is required`，白做一轮且界面留下红报错泡。

约定（改了要同步这里）：
  · 缺必填参数 → **不执行**，回一条带正确参数名的纠正提示；
  · 计划轮（probe_round=True）→ 不发任何事件（前端不留气泡）；
  · 执行轮 → 照旧发 tool_end（过程可见）；
  · 注册表里查不到的工具、或必填齐全 → 一律放行（校验绝不比执行层严）。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent_loop  # noqa: E402
from agent import tool_exec  # noqa: E402

_TOOL = {
    "type": "function",
    "function": {
        "name": "mx_query_test",
        "description": "测试用",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "查询语句"}},
            "required": ["query"],
        },
    },
}


@pytest.fixture
def fake_tool(monkeypatch):
    """注册一个带必填 query 的假工具，返回调用记录。"""
    calls: list = []

    def _impl(args):
        calls.append(args)
        return "ok-result"

    monkeypatch.setattr(agent_loop, "TOOLS", [_TOOL])
    monkeypatch.setattr(agent_loop, "TOOL_DISPATCH", {"mx_query_test": _impl})
    return calls


def _tc(args: dict) -> dict:
    return {"id": "call-1", "function": {"name": "mx_query_test",
                                         "arguments": json.dumps(args)}}


# ── 提示函数本身 ────────────────────────────────────────────────
def test_hint_none_when_required_present(fake_tool):
    assert tool_exec._required_args_hint("mx_query_test", {"query": "上证指数"}) is None


def test_hint_mentions_missing_param(fake_tool):
    hint = tool_exec._required_args_hint("mx_query_test", {"market": "A股"})
    assert hint and "query" in hint and "缺少必填参数" in hint


def test_hint_none_for_unknown_tool(fake_tool):
    assert tool_exec._required_args_hint("no_such_tool", {}) is None


def test_real_registry_mx_query_case():
    """真机原样：mx_query 的 {"market","type"} 必须被拦（10-15 事故回归）。"""
    hint = tool_exec._required_args_hint("mx_query", {"market": "A股", "type": "大盘指数"})
    assert hint and "query" in hint


def test_real_registry_no_false_positive():
    """注册表全量核对：必填齐全 → 一律不拦（校验不会误伤合法调用）。"""
    checked = 0
    for t in agent_loop.TOOLS or []:
        f = (t or {}).get("function") or {}
        name = f.get("name")
        if not name:
            continue
        params = f.get("parameters") or {}
        required = [p for p in (params.get("required") or []) if isinstance(p, str)]
        args = {p: "x" for p in required}
        assert tool_exec._required_args_hint(name, args) is None, name
        checked += 1
    assert checked > 10   # 注册表确实装配出来了


def test_probe_round_flag_wired_at_all_execution_entries():
    """tripwire：工具执行的三个入口（并行委派/只读并行/串行）都必须带计划轮标记。

    漏传会让"计划轮静默"在最常走的串行路径上失效——这条断言就是那份提醒；
    新增执行入口时同步本测试（数量刻意写死）。
    """
    src = Path(__file__).resolve().parents[1].joinpath("agent", "loop.py").read_text("utf-8")
    assert src.count("probe_round=self._thinking_first_round()") == 3


# ── 执行入口：拦下 + 是否发事件 ─────────────────────────────────
@pytest.mark.asyncio
async def test_probe_round_missing_arg_blocks_and_stays_silent(fake_tool):
    msgs: list = []
    verify_failed, events = await tool_exec._handle_tool_execution_inner(
        _tc({"market": "A股", "type": "大盘指数"}), msgs, "", "latiao",
        "full", probe_round=True)
    assert fake_tool == []                     # 没执行
    assert events == []                        # 不发事件 → 前端不留红泡
    assert msgs and msgs[-1]["role"] == "tool"
    assert "缺少必填参数" in msgs[-1]["content"]


@pytest.mark.asyncio
async def test_normal_round_missing_arg_blocks_but_reports(fake_tool):
    msgs: list = []
    verify_failed, events = await tool_exec._handle_tool_execution_inner(
        _tc({"market": "A股"}), msgs, "", "latiao", "full", probe_round=False)
    assert fake_tool == []                     # 没执行
    assert len(events) == 1 and events[0]["event"] == "tool_end"
    assert "缺少必填参数" in events[0]["result"]


@pytest.mark.asyncio
async def test_valid_args_execute_normally(fake_tool):
    msgs: list = []
    verify_failed, events = await tool_exec._handle_tool_execution_inner(
        _tc({"query": "上证指数 涨跌幅"}), msgs, "", "latiao", "full", probe_round=True)
    assert fake_tool == [{"query": "上证指数 涨跌幅"}]
    assert any(e.get("result") == "ok-result" for e in events)
