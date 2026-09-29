"""Loop 级故障注入（2026-09-29）—— gap 清单第 0 步的三场景之二、之三。

设计原则：**先有台架再上闸门**。三条注入都必须在"预期轮数内被拦下、绝不跑满
MAX_STEPS"——跑满才停是最差的收尾（用户等到 40 步只收到一句提示）。

- 场景①(a) 同参空转（死循环的一种）：同一工具同一参数反复调 → 停滞闸门在
  5 轮内收口；
- 场景①(b) 同错（死循环的另一种）：参数换着花样但撞同一堵墙 → 同错升级在
  3 轮内交回用户。**用中文报错的工具**（read_file 缺文件 → 「错误：文件不存在」），
  防"判据只认英文"的回归；
- 场景② 假成功：收口轮交空正文 → 必须交付**已收集的数据**，而不是空手或碎片。

（场景③ 预算耗尽见 test_budget_guard.py。）
"""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.fake_engine import FakeEngine, _sse  # noqa: E402

NEUTRAL = "根据已收集的结果，这个目录里有若干文件，具体清单见上方工具输出。" * 2


async def _run_script(monkeypatch, engine, script, prompt="看看这个目录", **kw):
    import agent.context as agent_context
    import local_llm
    from tests.test_loop_scenarios import _StubEngine
    from agent.loop import ThinAgentLoop

    with engine:
        for entry in script:
            engine.push(entry)
        old = local_llm._engine
        local_llm._engine = _StubEngine()
        agent_context._LOCAL_NATIVE_TOOLS_OVERRIDE = True
        try:
            loop = ThinAgentLoop([{"role": "user", "content": prompt}], "fake-model",
                                 engine.url, {"Authorization": "Bearer fake"},
                                 session_id=f"fault-{time.time()}", access_mode="full", **kw)
            events = [e async for e in loop.run()]
            return loop, events
        finally:
            local_llm._engine = old
            agent_context._LOCAL_NATIVE_TOOLS_OVERRIDE = None


def _native(name, args, cid="c1"):
    return [
        _sse({"choices": [{"delta": {"role": "assistant", "content": "", "tool_calls": [
            {"index": 0, "id": f"{cid}-{time.time_ns()}", "type": "function",
             "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]},
            "finish_reason": "tool_calls", "index": 0}]}),
        "data: [DONE]\n\n",
    ]


def _text(s):
    return FakeEngine.text_response(s)


# ── 场景①(a)：同参空转 → 停滞闸门在 5 轮内收口 ──
@pytest.mark.asyncio
async def test_fault_same_call_loop_is_closed_before_step_cap():
    script = [_native("list_dir", {"path": "."})] * 12 + [_text(NEUTRAL)] * 4
    with FakeEngine() as engine:
        loop, events = await _run_script(None, engine, script)
    joined = json.dumps(events, ensure_ascii=False)
    assert loop.steps < loop.max_steps, f"同参空转应在步数上限前收口，实得 {loop.steps} 步"
    assert loop.steps <= 12, f"应在停滞闸门窗口（同签名 5 轮）内收口，实得 {loop.steps}"
    # 收口后必须有可交付内容（答案或明确的收口说明），不能只剩心跳
    delivered = "".join(str(e.get("content") or "") for e in events)
    assert delivered.strip(), "收口必须交付内容"
    assert "收口" in joined or delivered.strip(), joined[:200]


# ── 场景①(b)：同错（中文报错）→ 同错升级 3 轮内交回用户 ──
@pytest.mark.asyncio
async def test_fault_same_error_escalates_with_chinese_error_text(tmp_path):
    """参数每轮不同、错误每轮相同（中文文案）→ 必须升级交回，且不跑满步数。

    这条同时守住"判据与 verify_failed 同源"：read_file 缺文件返回的是
    「错误：文件不存在 - …」，只认 "Error"/"⛔" 的旧判据会漏掉它。
    """
    missing = tmp_path / "并不存在的文件.txt"
    script = [_native("read_file", {"path": f"{missing}-{i}"}) for i in range(8)]
    with FakeEngine() as engine:
        loop, events = await _run_script(None, engine, script)
    delivered = "".join(str(e.get("content") or "") for e in events)
    assert loop.steps < loop.max_steps, f"同错应升级而非跑满，实得 {loop.steps} 步"
    assert loop.steps <= 6, f"同错升级应在 3 轮左右触发，实得 {loop.steps}"
    # 断言必须**只认升级路径**：只写"文件不存在"会被数据兜底（finalize 里回显工具结果）
    # 满足 → 旧判据下也能过，测试失去判别力（A/B 实测抓到的弱断言）。
    assert "已停止重试" in delivered and "交回" in delivered, \
        f"必须走同错升级并交回用户（而非数据兜底），实得：{delivered[:200]!r}"


# ── 场景②：假成功（收口轮空正文）→ 交付已收集数据 ──
@pytest.mark.asyncio
async def test_fault_empty_finalize_delivers_collected_data(tmp_path):
    marker = "凭证-2026-09-29.xlsx"
    (tmp_path / marker).write_text("x", encoding="utf-8")
    script = [
        _native("list_dir", {"path": str(tmp_path)}),   # 收集到含 marker 的数据
        _native("list_dir", {"path": str(tmp_path)}),   # 触发重复拒绝 → 推向收口
        _native("list_dir", {"path": str(tmp_path)}),
        _native("list_dir", {"path": str(tmp_path)}),
        _native("list_dir", {"path": str(tmp_path)}),
        _native("list_dir", {"path": str(tmp_path)}),
        _sse({"choices": [{"delta": {"content": "   \n\n"}, "finish_reason": "stop", "index": 0}]}),
        "data: [DONE]\n\n",
    ] + [_sse({"choices": [{"delta": {"content": "   "}, "finish_reason": "stop", "index": 0}]}),
         "data: [DONE]\n\n"] * 2
    with FakeEngine() as engine:
        loop, events = await _run_script(None, engine, script)
    delivered = "".join(str(e.get("content") or "") for e in events)
    assert delivered.strip(), "绝不能交付空内容"
    assert len(delivered.strip()) > 20, f"空正文必须走数据兜底，实得 {delivered[:120]!r}"
    assert marker in delivered or "数据" in delivered, \
        f"应收口后交付已收集的数据（含 {marker}），实得：{delivered[:200]!r}"
