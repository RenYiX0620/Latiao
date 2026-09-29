"""用量按来源归因 + 每轮指标落库（2026-09-29，ZCode 对照后的增补）。

对着 ZCode 的两处差距补的：
- ZCode 的 `model_usage` 有 `query_source` 维度（本机实测 `subagent` 占 17%），
  我们的子代理在自己 session 里记账、父会话看不见 → `by_source` 分桶 + `credit_parent`；
- ZCode 把 `duration_ms`/`time_to_first_token_ms` 落库可查历史，我们只有内存态
  → `turn_metrics` 一行一轮（轮末写，失败只记日志）。
"""
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import context_stats as cs  # noqa: E402
import turn_metrics as tm   # noqa: E402


@pytest.fixture(autouse=True)
def _fresh():
    import db
    db._init_db()          # 平时由 main 启动时调用；临时库需要自己建表（同 test_db_retention）
    sid, sub = f"src-{time.time_ns()}", f"src-{time.time_ns()}:sub1"
    cs.begin_turn(sid)
    cs.begin_turn(sub)
    yield sid, sub
    cs.reset(sid)
    cs.reset(sub)


# ── ① 来源归因 ──
def test_subagent_usage_credited_to_parent_only(_fresh):
    sid, sub = _fresh
    cs.record_usage(sub, {"prompt_tokens": 900, "completion_tokens": 120},
                    source="subagent", parent_session_id=sid)
    cs.record_usage(sid, {"prompt_tokens": 12_000, "completion_tokens": 300})

    parent = cs.turn_cost(sid)
    assert parent["by_source"]["subagent"] == {"requests": 1, "input_tokens": 900,
                                               "output_tokens": 120}
    assert parent["by_source"]["main_turn"]["input_tokens"] == 12_000
    # 关键：子代理的 token **不能**混进父会话自己的输入量（预算守卫/缓存命中率的语义）
    assert parent["input_tokens"] == 12_000, "父会话输入量必须只含它自己的调用"


def test_parent_cache_samples_untouched_by_subagent(_fresh):
    """子代理的提示词与父会话无关，不许污染父会话的缓存命中率样本。"""
    sid, sub = _fresh
    cs.record_usage(sub, {"prompt_tokens": 900, "completion_tokens": 10,
                          "prompt_tokens_details": {"cached_tokens": 900}},
                    source="subagent", parent_session_id=sid)
    assert not (cs.stats(sid).get("cache_samples") or 0), "父会话不该因此多出缓存样本"


def test_refine_folds_into_same_shape(_fresh):
    sid, _ = _fresh
    cs.record_refine(sid, {"prompt_tokens": 400, "completion_tokens": 60})
    assert cs.turn_cost(sid)["by_source"]["refine"]["input_tokens"] == 400


def test_by_source_resets_each_turn(_fresh):
    sid, _ = _fresh
    cs.record_usage(sid, {"prompt_tokens": 100, "completion_tokens": 5})
    cs.begin_turn(sid)
    assert cs.turn_cost(sid)["by_source"] == {}, "新一轮来源桶必须清空"


# ── ② 每轮指标落库 ──
def _seed_turn(sid, *, input_tokens=12_000, gen=300, retries=2, refine_calls=3,
               ttft=0.8, steps=4):
    cs.record_usage(sid, {"prompt_tokens": input_tokens, "completion_tokens": gen})
    for k in range(retries):
        cs.record_retry(sid, f"k{k}")
    for _ in range(refine_calls):
        cs.record_refine(sid, {"prompt_tokens": 100, "completion_tokens": 10})
    cs.record_step(sid, 2.5, ttft)
    for _ in range(steps - 1):
        cs.record_step(sid, 1.0, ttft)


def test_record_turn_persists_metrics(_fresh):
    sid, _ = _fresh
    _seed_turn(sid)
    time.sleep(0.02)                       # 让 duration 可测（毫秒级）
    assert tm.record_turn(sid, model="fake-model", is_local=True, ended_reason="completed")
    rows = tm.list_turns(sid, limit=5)
    assert rows and rows[0]["session_id"] == sid
    r = rows[0]
    assert r["input_tokens"] == 12_000 and r["gen_tokens"] == 300
    assert r["retries"] == 2 and r["refine_calls"] == 3
    assert r["ttft_ms"] == 800, f"TTFT 要落库（毫秒）：{r['ttft_ms']}"
    assert r["duration_ms"] >= 10, f"轮次耗时（毫秒）要落库：{r['duration_ms']}"
    assert r["is_local"] == 1 and r["model"] == "fake-model"
    assert r["by_source"]["main_turn"]["input_tokens"] == 12_000
    assert r["ended_reason"] == "completed"


def test_record_turn_is_idempotent_per_turn(_fresh):
    """同一轮重复写只留一行（id = 会话:轮起点），刷新数值而不是追加。"""
    sid, _ = _fresh
    _seed_turn(sid, input_tokens=1000)
    tm.record_turn(sid, ended_reason="error")
    _seed_turn(sid, input_tokens=500)
    tm.record_turn(sid, ended_reason="completed")
    rows = tm.list_turns(sid, limit=10)
    assert len(rows) == 1, f"同一轮只能一行：{len(rows)}"
    assert rows[0]["input_tokens"] == 1500 and rows[0]["ended_reason"] == "completed"


def test_record_turn_skips_without_begin_turn(_fresh):
    """没有 begin_turn（直连/测试路径）→ 不写半残行。"""
    sid, _ = _fresh
    cs.reset(sid)                      # 模拟"从没开过轮"
    assert tm.record_turn(sid) is False
    assert tm.list_turns(sid) == []


def test_record_turn_never_raises_on_bad_session():
    assert tm.record_turn("") is False
    assert tm.record_turn("no-such-session") is False


def test_prune_drops_old_rows(_fresh):
    sid, _ = _fresh
    _seed_turn(sid)
    tm.record_turn(sid)
    # 手工把这一行改成 200 天前，再按 180 天保留期清理
    from db import _db_write_lock, _get_db
    old = (datetime.now() - timedelta(days=200)).isoformat()
    with _db_write_lock:
        conn = _get_db()
        conn.execute("UPDATE turn_metrics SET started_at = ? WHERE session_id = ?", (old, sid))
        conn.commit()
    assert tm.prune(days=180) >= 1
    assert tm.list_turns(sid) == []


@pytest.mark.asyncio
async def test_loop_records_subagent_source_end_to_end():
    """循环集成：子代理身份跑一回合，引擎报的 token 落进**父会话**的 subagent 桶。"""
    import agent.context as agent_context
    import local_llm
    from tests.fake_engine import FakeEngine, _sse
    from tests.test_loop_scenarios import _StubEngine
    from agent.loop import ThinAgentLoop

    parent = f"p-{time.time_ns()}"
    sub = f"{parent}:sub1"
    cs.begin_turn(parent)
    cs.begin_turn(sub)
    with FakeEngine() as engine:
        # 本地引擎语义：prompt_n 是"新评估"、cache_n 是命中的前缀 → 全量 900
        engine.push([
            _sse({"choices": [{"delta": {"role": "assistant",
                                         "content": "子代理结论：目录结构清晰。"},
                               "finish_reason": "stop", "index": 0}],
                  "timings": {"prompt_n": 700, "cache_n": 200, "predicted_n": 30}}),
            "data: [DONE]\n\n",
        ])
        old = local_llm._engine
        local_llm._engine = _StubEngine()
        agent_context._LOCAL_NATIVE_TOOLS_OVERRIDE = True
        try:
            loop = ThinAgentLoop([{"role": "user", "content": "看下这个目录"}], "fake-model",
                                 engine.url, {"Authorization": "Bearer fake"},
                                 session_id=sub, access_mode="subagent", is_local=True)
            assert loop._usage_source == "subagent", "受限档必须判定成子代理来源"
            assert loop._usage_parent == parent, "父会话 id 要从 parent:sub 里解出来"
            _ = [e async for e in loop.run()]
        finally:
            local_llm._engine = old
            agent_context._LOCAL_NATIVE_TOOLS_OVERRIDE = None

    bucket = cs.turn_cost(parent)["by_source"].get("subagent") or {}
    assert bucket.get("input_tokens") == 900, f"父会话必须看到子代理的 token：{bucket}"
    assert bucket.get("requests") == 1
    # 关键的不变量：父会话**自己的**输入量不受子代理影响（预算/缓存语义），
    # 子代理那 900 只出现在它的来源桶里；子会话自己的口径照记。
    assert cs.turn_cost(parent)["input_tokens"] == 0
    assert cs.turn_cost(sub)["input_tokens"] == 900
