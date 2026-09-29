"""预算守卫回归（2026-09-29）—— gap 清单第 0 步"故障注入"场景之三：预算耗尽。

背景：预算守卫（每轮累计输入 token 上限，默认 400k，`LATIAO_TURN_TOKEN_BUDGET` 可调）
上线后**从未被真实触发过**（历史 0 次），参数是拍的。这里用"把阈值调小 + 让桩引擎
每步回报大 usage"等价地构造出超长回合，锁住三件事：

1. 越线时**先强制收口**（进入终答轮），而不是直接断；
2. 收口后仍越线 → **停手交付**（`budget_exhausted`），不是静默结束；
3. **绝不跑满 MAX_STEPS**——预算守卫必须比步数上限先到（长回合里它才是先撞的墙）。
"""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.fake_engine import FakeEngine, _sse  # noqa: E402

MESSAGES = [{"role": "user", "content": "把仓库里所有文件都读一遍并汇总"}]
ANSWER = "汇总：这个目录里包含 sidecar、src、docs 等目录，主要文件见上方工具结果。" * 2

# 每步回报的输入 token（小阈值下两三步就越线）
STEP_PROMPT_TOKENS = 5000
# 阈值调小 → 等价于"回合特别长"
TEST_BUDGET = 8000


def _tool_round_with_usage(name: str, args: dict, prompt_tokens: int) -> list[str]:
    """一轮：原生工具调用 + usage（同收尾包），让 turn_input_tokens 累加。"""
    return [
        _sse({"choices": [{"delta": {"role": "assistant", "content": "", "tool_calls": [
            {"index": 0, "id": f"c{time.time_ns()}", "type": "function",
             "function": {"name": name, "arguments": json.dumps(args)}}]},
            "finish_reason": "tool_calls", "index": 0}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 12}}),
        "data: [DONE]\n\n",
    ]


def _round(body) -> list[str]:
    """看到收口指令 → 给答案；否则继续调工具（把循环推向预算线）。"""
    if "收尾" in json.dumps(body.get("messages"), ensure_ascii=False):
        return FakeEngine.text_response(ANSWER)
    return _tool_round_with_usage("list_dir", {"path": "."}, STEP_PROMPT_TOKENS)


async def _run(monkeypatch, budget: int):
    import agent.context as agent_context
    import local_llm
    from tests.test_loop_scenarios import _StubEngine
    from agent.loop import ThinAgentLoop

    monkeypatch.setenv("LATIAO_TURN_TOKEN_BUDGET", str(budget))
    with FakeEngine() as engine:
        for _ in range(20):          # 脚本按需取用（一次一弹）
            engine.push(_round)
        old = local_llm._engine
        local_llm._engine = _StubEngine()
        agent_context._LOCAL_NATIVE_TOOLS_OVERRIDE = True
        try:
            loop = ThinAgentLoop(MESSAGES, "fake-model", engine.url,
                                 {"Authorization": "Bearer fake"},
                                 session_id=f"budget-{time.time()}", access_mode="full")
            events = []
            async for e in loop.run():
                events.append(e)
            return loop, events
        finally:
            local_llm._engine = old
            agent_context._LOCAL_NATIVE_TOOLS_OVERRIDE = None


@pytest.mark.asyncio
async def test_budget_forces_finalize_before_step_cap(monkeypatch):
    loop, events = await _run(monkeypatch, TEST_BUDGET)
    # ① 必须触发过守卫
    assert loop._budget_wrapped, "越线后应标记已收口"
    # ② 绝不能跑到步数上限（预算是先到的那堵墙）
    assert loop.steps < loop.max_steps, f"预算守卫应先于步数上限触发，实得 {loop.steps} 步"
    # ③ 越线时先收口（终答轮），而不是直接静默结束
    joined = json.dumps(events, ensure_ascii=False)
    assert "收尾" in joined or "预算" in joined, f"应出现收口/预算相关事件：{joined[:400]}"
    # ④ 最终要交付内容（答案或明确的预算用尽说明），不能空手
    delivered = "".join(str(e.get("content") or "") for e in events)
    assert ANSWER.strip()[:20] in delivered or "预算" in delivered, \
        f"必须交付答案或预算说明，实得：{delivered[:200]!r}"


@pytest.mark.asyncio
async def test_budget_disabled_by_zero(monkeypatch):
    """阈值 0 = 关闭：同样的脚本不应触发守卫（防止守卫误伤正常长回合）。"""
    loop, events = await _run(monkeypatch, 0)
    assert not loop._budget_wrapped, "阈值为 0 时必须完全关闭"
