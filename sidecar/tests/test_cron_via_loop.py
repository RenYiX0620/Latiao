"""定时任务走**唯一循环**（ThinAgentLoop）的接线回归（2026-09-29 审计①）。

背景：此前 cron 自带一套 10 轮工具循环，于是聊天侧的任务级验证器 / 预算守卫与近阈提示 /
停滞与同错升级 / 压缩与旧结果回收 / 用量记账**定时任务全都没有**——而"无人值守长跑"
恰恰最需要这些闸门，它的开销在面板与 turn_metrics 里也完全不可见。

这里钉住换过来之后的契约（cron 只该保留自己特有的四件事）：
1. 真的走 ThinAgentLoop（不是又长出一套循环）；
2. 工具白名单：**禁 delegate_task**、按任务相关性收窄、最多 5 个；
3. 工具档位来自 job.access_mode（默认 full＝原行为；无人值守可显式设 read_only）；
4. 结果落库 + 事件推送 + turn_metrics（reason=cron）；
5. 云端 429 → 回退本地重跑一次；
6. 非交互：定时任务不得触发首启引导/身份意图（否则短任务名会被记成用户称呼）。
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cron  # noqa: E402


@pytest.fixture()
def patched(monkeypatch):
    """避免写盘 + 记录结果（与 test_cron.py 同款做法：存原值还原，别用 del）。"""
    _orig_save, _orig_state = cron._save_cron, cron._save_cron_state
    recorded: list[tuple] = []
    monkeypatch.setattr(cron, "_save_cron", lambda jobs: None)
    monkeypatch.setattr(cron, "_save_cron_state", lambda: None)
    _orig_record = cron._record_cron_result

    def _record(job, status, summary):
        recorded.append((status, summary))
        return _orig_record(job, status, summary)

    monkeypatch.setattr(cron, "_record_cron_result", _record)
    monkeypatch.setitem(cron.__dict__, "_recorded_log", recorded)
    yield recorded
    cron._save_cron, cron._save_cron_state = _orig_save, _orig_state


def _job(**kw):
    j = {"id": f"cron-test-{time.time_ns()}", "schedule": "0 9 * * *",
         "task": "分析今天大盘并总结", "action": "notify", "enabled": True}
    j.update(kw)
    return j


class _FakeLoop:
    """替身：记录构造参数，并把一段正文当"交付内容"吐出来。"""

    instances: list = []

    def __init__(self, messages, model, api_url, headers, session_id="",
                 access_mode="confirm", thinking_level="high", is_local=None,
                 tool_whitelist=None):
        self.messages, self.model, self.session_id = messages, model, session_id
        self.access_mode, self.tool_whitelist = access_mode, tool_whitelist
        self.max_steps = 40
        self.steps = 3
        _FakeLoop.instances.append(self)

    async def run(self):
        yield {"content": "这里是定时任务的最终报告正文。"}


def _patch_loop(monkeypatch, text="这里是定时任务的最终报告正文。"):
    async def _run(self):
        yield {"content": text}
    monkeypatch.setattr(_FakeLoop, "run", _run)
    _FakeLoop.instances = []
    import agent.loop
    monkeypatch.setattr(agent.loop, "ThinAgentLoop", _FakeLoop)
    return _FakeLoop


def _patch_target(monkeypatch, *, is_local=False, model="fake-model"):
    """把模型选择钉死（不碰真实引擎/云端配置）。"""
    from agent import routing

    async def _target(_cloud):
        return ("openai", "http://127.0.0.1:9/v1/chat/completions", {}, is_local)

    monkeypatch.setattr(routing, "_resolve_api_target", _target)
    import agent_loop
    monkeypatch.setattr(agent_loop, "_resolve_api_target", _target)
    monkeypatch.setattr(agent_loop, "_get_best_cloud_config", lambda: {"model": model,
                                                                      "endpoint": "http://x"})
    import main
    monkeypatch.setattr(main, "SUBAGENT_MODEL", model, raising=False)


@pytest.mark.asyncio
async def test_cron_runs_through_the_single_loop(monkeypatch, patched):
    _patch_target(monkeypatch)
    fake = _patch_loop(monkeypatch)
    j = _job()
    await cron._execute_cron_job(j)

    assert len(fake.instances) == 1, "必须恰好走一次 ThinAgentLoop"
    inst = fake.instances[0]
    assert inst.session_id == f"cron:{j['id']}", "会话 id 要可辨识（面板/记账按会话聚合）"
    assert inst.max_steps == cron._CRON_MAX_STEPS, "无人值守要收紧步数"
    joined = " ".join(str(m.get("content") or "") for m in inst.messages)
    assert "分析今天大盘并总结" in joined, "任务文本必须在提示里"
    assert patched and patched[-1][0] == "success", f"结果要落库：{patched}"


@pytest.mark.asyncio
async def test_cron_tool_whitelist_excludes_delegate_and_is_capped(monkeypatch, patched):
    _patch_target(monkeypatch)
    fake = _patch_loop(monkeypatch)
    await cron._execute_cron_job(_job())
    wl = fake.instances[0].tool_whitelist
    assert isinstance(wl, set) and wl, f"必须有白名单（禁 delegate_task/收窄）：{wl}"
    assert "delegate_task" not in wl, "定时任务不得派生后台子代理（抢同一个本地引擎）"
    assert len(wl) <= 5, f"弱模型友好：最多 5 个工具，实得 {len(wl)}：{sorted(wl)}"


@pytest.mark.asyncio
async def test_cron_access_mode_comes_from_job(monkeypatch, patched):
    _patch_target(monkeypatch)
    fake = _patch_loop(monkeypatch)
    await cron._execute_cron_job(_job(access_mode="read_only"))
    assert fake.instances[0].access_mode == "read_only", "无人值守应当能显式锁成只读"
    fake.instances.clear()
    await cron._execute_cron_job(_job())
    assert fake.instances[0].access_mode == "full", "默认档＝原行为（full），不静默收紧"


@pytest.mark.asyncio
async def test_cron_records_turn_metrics(monkeypatch, patched):
    _patch_target(monkeypatch)
    _patch_loop(monkeypatch)
    import turn_metrics as tm
    j = _job()
    await cron._execute_cron_job(j)
    rows = [r for r in tm.list_turns(f"cron:{j['id']}", limit=5)]
    assert rows, "定时任务的开销必须可查（审计①：此前完全不可见）"
    assert rows[0]["ended_reason"] == "cron"


@pytest.mark.asyncio
async def test_cron_429_falls_back_to_local(monkeypatch, patched):
    _patch_target(monkeypatch)
    _patch_loop(monkeypatch, text="⚠️ 模型服务返回错误 HTTP 429，请稍后重试。")
    calls = []

    async def _spy(job, force_local=False):
        calls.append(force_local)
        if force_local:
            return None
        return await _real(job, force_local)

    _real = cron._execute_cron_job
    monkeypatch.setattr(cron, "_execute_cron_job", _spy)
    await cron._execute_cron_job(_job())
    assert calls == [False, True], f"云端 429 必须回退本地重跑一次（原语义）：{calls}"


@pytest.mark.asyncio
async def test_cron_skips_without_model(monkeypatch, patched):
    from agent import routing

    async def _no_target(_cloud):
        return ("openai", "", {}, True)

    monkeypatch.setattr(routing, "_resolve_api_target", _no_target)
    import agent_loop
    monkeypatch.setattr(agent_loop, "_resolve_api_target", _no_target)
    monkeypatch.setattr(agent_loop, "_get_best_cloud_config", lambda: None)
    await cron._execute_cron_job(_job())
    assert patched and patched[-1][0] == "skipped", f"无模型要如实记录跳过：{patched}"


def test_non_interactive_skips_onboarding(monkeypatch):
    """非交互闸：定时任务/通道消息不得被当成"用户在回答首启引导"。"""
    import onboarding
    from agent import prompt_build
    calls = []
    monkeypatch.setattr(onboarding, "process_message",
                        lambda *a, **k: (calls.append(a), (None, False))[1])
    monkeypatch.setattr(prompt_build, "_process_identity_intents",
                        lambda *a, **k: calls.append("identity") or None)
    body = {"session_id": "t", "non_interactive": True}
    prompt_build._build_chat_messages(body, [{"role": "user", "content": "定时任务: 日报"}])
    assert not calls, f"非交互调用不得触发引导/身份意图：{calls}"

    prompt_build._build_chat_messages({"session_id": "t"},
                                      [{"role": "user", "content": "你好"}])
    assert calls, "交互式调用照常走引导/身份意图（别把闸门开成永久关闭）"
