"""成本可见（gap 清单第 4 步，2026-09-29）——"这轮花了多少、花在哪"。

三件事：
1. **会话状态里有数**：`context_stats.stats()["turn"]` 给出本轮输入 token / 预算比 /
   重采次数与原因 / "知识提炼"（refine）调用次数与 token（本轮 + 会话累计）；
2. **近阈显式提示**：达到预算 80% 时同时告诉用户（可见文字）与模型（尾部提示），
   而不是"要么不知道、要么被静默收口"；
3. **隐形开销计账**：每个工具执行后的 refine 调用此前完全不计账（9 次工具 = 9 次
   云端调用），现在计入 `turn.refine_calls/refine_tokens`。

这里用**桩 HTTP 服务**给 refine 记账做正控（证明仪器会响），用**种子 usage + 小预算**
给近阈提示做循环集成（不依赖真引擎）。
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import context_stats as cs  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_session():
    sid = f"cost-{time.time_ns()}"
    cs.begin_turn(sid)
    yield sid
    cs.reset(sid)


# ── ① 计数器与 stats 口径 ──
def test_turn_cost_counters_and_reset(_fresh_session):
    sid = _fresh_session
    cs.record_budget(sid, 400_000)
    cs.record_usage(sid, {"prompt_tokens": 12_000, "completion_tokens": 300})
    cs.record_retry(sid, "empty_generation")
    cs.record_retry(sid, "lang_drift")
    cs.record_refine(sid, {"prompt_tokens": 400, "completion_tokens": 60})
    cs.record_refine(sid, None, ok=False)          # 失败也计一次"发出去了"

    turn = cs.stats(sid)["turn"]
    assert turn["input_tokens"] == 12_000
    assert turn["budget"] == 400_000 and turn["budget_percent"] == 3.0
    assert turn["retries"] == 2 and turn["retry_kinds"] == ["empty_generation", "lang_drift"]
    assert turn["refine_calls"] == 2 and turn["refine_tokens"] == 460
    assert turn["refine_calls_total"] == 2

    # 新一轮：本轮清零，会话累计保留（看长期开销）
    cs.begin_turn(sid)
    turn2 = cs.stats(sid)["turn"]
    assert turn2["retries"] == 0 and turn2["refine_calls"] == 0
    assert turn2["refine_calls_total"] == 2 and turn2["refine_tokens_total"] == 460
    assert turn2["budget"] == 400_000, "预算跨轮保留（面板要显示比例）"


def test_retry_kinds_capped(_fresh_session):
    sid = _fresh_session
    for i in range(12):
        cs.record_retry(sid, f"k{i}")
    turn = cs.stats(sid)["turn"]
    assert turn["retries"] == 12 and len(turn["retry_kinds"]) == 8


# ── ② 隐形开销：refine 调用真的会被记上（正控：桩服务会回 usage）──
class _RefineStub(BaseHTTPRequestHandler):
    calls = 0

    def do_POST(self):                                    # noqa: N802
        _RefineStub.calls += 1
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        body = json.dumps({
            "choices": [{"message": {"content": "该项目用 python-docx 生成 Word 报告。"}}],
            "usage": {"prompt_tokens": 700, "completion_tokens": 40},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):                            # 静音
        pass


@pytest.mark.asyncio
async def test_refine_call_is_accounted(_fresh_session, monkeypatch):
    sid = _fresh_session
    _RefineStub.calls = 0
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _RefineStub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/v1/chat/completions"
    try:
        import main
        import memory

        async def _target(_cfg):
            return ("openai", url, {"Authorization": "Bearer x"}, False)

        monkeypatch.setattr(main, "_resolve_api_target", _target)
        await memory._refine_learnings("read_file", {"path": "/tmp/x"},
                                       "文件内容：这是一个 python-docx 报告生成脚本。" * 3, sid)
        turn = cs.stats(sid)["turn"]
        assert _RefineStub.calls == 1, "正控前提：桩服务确实收到了一次调用"
        assert turn["refine_calls"] == 1, "工具后的提炼调用必须计账（此前完全不可见）"
        assert turn["refine_tokens"] == 740, f"应取到 usage：{turn['refine_tokens']}"
    finally:
        srv.shutdown()


# ── ③ 近阈显式提示（循环集成，不依赖真引擎）──
MSG = [{"role": "user", "content": "分析一下这个仓库"}]
ANSWER = ("这个仓库分为 sidecar（Python 侧车）与 src（React 前端）两部分，"
          "具体文件清单见上方工具结果。") * 2
TEST_BUDGET = 1000


async def _run_with_seeded_usage(monkeypatch, budget: int, seeded: int):
    from tests.fake_engine import FakeEngine
    from agent.loop import ThinAgentLoop

    monkeypatch.setenv("LATIAO_TURN_TOKEN_BUDGET", str(budget))
    sid = f"costloop-{time.time_ns()}"
    cs.begin_turn(sid)
    if seeded:
        cs.record_usage(sid, {"prompt_tokens": seeded, "completion_tokens": 1})
    with FakeEngine() as engine:
        engine.push(engine.text_response(ANSWER))
        events = [e async for e in ThinAgentLoop(
            MSG, "fake-model", engine.url, {"Authorization": "Bearer fake"},
            session_id=sid, access_mode="full").run()]
    return sid, events, engine


@pytest.mark.asyncio
async def test_near_budget_warns_user_and_model(monkeypatch):
    """85% 时就显式提示（用户可见 + 模型尾部提示），而不是等到越线被静默收口。"""
    sid, events, engine = await _run_with_seeded_usage(monkeypatch, TEST_BUDGET, 850)
    shown = "".join(str(e.get("content") or "") for e in events)
    assert "已达预算的 85%" in shown, f"用户应看到近阈提示：{shown[:300]!r}"
    reqs = json.dumps(engine.requests, ensure_ascii=False)
    assert "预算已用 85%" in reqs, "模型也要收到收口要求（尾部提示）"
    assert ANSWER.strip()[:20] in shown, "提示不能顶掉正常交付"
    assert cs.stats(sid)["turn"]["budget"] == TEST_BUDGET, "面板要能显示 已用/预算"


@pytest.mark.asyncio
async def test_below_threshold_stays_silent(monkeypatch):
    """70% 不打扰（提示只该在真的要收口时出现）。"""
    _sid, events, _engine = await _run_with_seeded_usage(monkeypatch, TEST_BUDGET, 700)
    shown = "".join(str(e.get("content") or "") for e in events)
    assert "已达预算的" not in shown, f"未近阈不该提示：{shown[:200]!r}"


@pytest.mark.asyncio
async def test_warning_fires_once_per_turn(monkeypatch):
    """每轮只提示一次（不刷屏）。"""
    sid, events, _engine = await _run_with_seeded_usage(monkeypatch, TEST_BUDGET, 850)
    shown = "".join(str(e.get("content") or "") for e in events)
    assert shown.count("已达预算的 85%") == 1
