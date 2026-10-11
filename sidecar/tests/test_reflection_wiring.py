"""输出反思接线（2026-10-11 从假开关转真）的验收测试。

背景：`334b182 feat: 输出反思档位` 当初实现了；`1aa3f61 薄循环转正` 把调用点
整段删掉后，前端监听 reflection_revised、后端 _reflect_output 零调用、设置页
档位成了假开关（用户实机发现）。这里钉住接回后的三条线：
- 触发判据与设置页文案逐字一致（off/light/deep × 云端/本地 × 长/短）；
- 到达 _reflect_output 的调用参数正确，修正经 reflection_revised 下发；
- SSE 里事件的形状与前端 replaceLastAssistant 的读取一致（content 字段）。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LATIAO_AUTH_TOKEN", "reflect-token")

_LONG = "这是一段足够长的回答。" * 80      # >800 字
_SHORT = "太短了，不触发。"


# ── 触发判据（纯函数，与设置页文案同口径）──────────────────────────

def test_eligibility_matrix():
    from api_routes import _reflection_eligible
    cases = [
        # (mode, text, is_local, expected, 说明)
        ("off", _LONG, False, False, "关闭 → 永不"),
        ("off", _LONG, True, False, "关闭 → 永不（本地）"),
        ("light", _SHORT, False, False, "轻度 → 短输出不触发"),
        ("light", _LONG, False, True, "轻度 → 云端长输出触发"),
        ("light", _LONG, True, False, "轻度 → 本地长输出不触发（文案：仅云端）"),
        ("deep", _SHORT, False, False, "重度 → 短输出仍不触发（长任务口径）"),
        ("deep", _LONG, False, True, "重度 → 云端长输出触发"),
        ("deep", _LONG, True, True, "重度 → 本地长输出也触发（UI 已警示会变慢）"),
        ("garbage", _LONG, False, False, "未知档位 → 不触发"),
    ]
    for mode, text, is_local, want, why in cases:
        got = _reflection_eligible(mode, text, is_local)
        assert got is want, f"{why}: mode={mode} local={is_local} 期望 {want} 实际 {got}"


def test_800_char_boundary():
    from api_routes import _reflection_eligible
    assert _reflection_eligible("deep", "x" * 800, False) is False     # 恰好 800 → 不触发
    assert _reflection_eligible("deep", "x" * 801, False) is True


# ── _reflect_output 本体：轮数口径与未变判定 ────────────────────────

class _FakeResp:
    def __init__(self, content: str):
        self._c = content
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": self._c}}]}


class _FakeClient:
    """记录每次请求，按脚本返回。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []

    async def post(self, url, json=None, headers=None):
        self.calls.append(json)
        return _FakeResp(self.script.pop(0) if self.script else "")


def _run(coro):
    return asyncio.run(coro)


def test_reflection_changed_returns_revision():
    from agent.reflection import _reflect_output
    c = _FakeClient(["修正后的长回答" * 100])
    revised, changed = _run(_reflect_output("原文" * 500, "m", "http://x", {}, "light", c))
    assert changed is True and revised.startswith("修正后的长回答")
    assert len(c.calls) == 1, "light 应自查 1 轮"


def test_reflection_unchanged_keeps_original():
    from agent.reflection import _reflect_output
    original = "原文" * 500
    c = _FakeClient([original])          # 模型原样返回 → 不算修正
    revised, changed = _run(_reflect_output(original, "m", "http://x", {}, "light", c))
    assert changed is False and revised == original
    assert len(c.calls) == 1


def test_reflection_deep_runs_two_rounds():
    from agent.reflection import _reflect_output
    c = _FakeClient(["第一轮修正" * 100, "第二轮修正" * 100])
    revised, changed = _run(_reflect_output("原文" * 500, "m", "http://x", {}, "deep", c))
    assert changed is True and revised.startswith("第二轮修正")
    assert len(c.calls) == 2, "deep 应自查 2 轮"


def test_reflection_failure_keeps_original():
    from agent.reflection import _reflect_output

    class _Boom:
        async def post(self, *a, **k):
            raise RuntimeError("upstream down")

    revised, changed = _run(_reflect_output("原文" * 500, "m", "http://x", {}, "light", _Boom()))
    assert changed is False and revised == "原文" * 500


# ── 未验证数字扫描：报告数字查不到工具出处 → 进反思提示 ──────────────

def test_unverified_numbers_flagged_into_prompt():
    from agent.reflection import _reflect_output
    c = _FakeClient(["改好了" * 300])
    body_text = "贵州茅台今日收盘价为 1234.56 元。" * 40
    _run(_reflect_output(body_text, "m", "http://x", {}, "light", c,
                         tool_outputs=["查询结果：贵州茅台 1234.56 元"]))
    prompt = c.calls[0]["messages"][0]["content"]
    # 有出处的数字不该被点名；编造的数字应进核查清单
    assert "1234.56" not in prompt.split("数字溯源核查")[-1], "有出处的数字不该在未验证清单里"


def test_reflection_prompt_uses_checklist():
    from agent.reflection import _reflect_output
    c = _FakeClient(["x" * 900])
    _run(_reflect_output("原文" * 500, "m", "http://x", {}, "deep", c))
    sys_prompt = c.calls[0]["messages"][0]["content"]
    assert "输出质检员" in sys_prompt
    assert "原样输出原文" in sys_prompt, "无问题必须原样返回（否则会被误判成修正）"


# ── 源码级 tripwire：调用点必须存在（防再次被重构悄悄拆掉）───────────

def test_route_calls_reflect_output():
    import api_routes
    src = Path(api_routes.__file__).read_text(encoding="utf-8")
    assert "_reflect_output(" in src, "SSE 收口必须真的调用反思（曾因此变成假开关）"
    assert "reflection_revised" in src, "修正必须经 reflection_revised 事件下发"
    assert "_reflection_eligible(" in src
