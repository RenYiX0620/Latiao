"""整页使用统计聚合（turn_metrics.dashboard_stats）的验收测试。

直接播种 + 直接调函数（不经过 HTTP 层）。钉住的口径：
- 全库合计（累计 token / 最长单轮）不受窗口影响；
- 连续天数：逐日连续段算最长；"当前连续"今天没用则从昨天起算；
- 峰值单日 = 窗口内 input+gen 最大的那天；
- per_day_model 的 tokens = input+gen 合并口径。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LATIAO_AUTH_TOKEN", "tmdash-token")

from tests.test_turn_metrics_summary_route import (  # noqa: E402
    _point_db_at,
    _seed,
    _teardown_db,
)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


@pytest.fixture()
def dash_db(tmp_path, monkeypatch):
    _point_db_at(tmp_path, monkeypatch, "tmdash.db")
    now = datetime.now()
    rows = [
        # 今天 / 昨天 / 前天：连续 3 天（本地 + 云端混在一天）
        {"id": "d1", "session_id": "s", "model": "Hermes-35B", "is_local": 1,
         "started_at": _iso(now), "input_tokens": 1000, "gen_tokens": 500,
         "duration_ms": 60_000, "ttft_ms": 2000},
        {"id": "d2", "session_id": "s", "model": "mimo-flash", "is_local": 0,
         "started_at": _iso(now - timedelta(days=1)), "input_tokens": 800,
         "gen_tokens": 400, "duration_ms": 30_000},
        {"id": "d3", "session_id": "s", "model": "Hermes-35B", "is_local": 1,
         "started_at": _iso(now - timedelta(days=2)), "input_tokens": 600,
         "gen_tokens": 300, "duration_ms": 20_000},
        # 10 天前孤立一天（不连续），但单日量最大 → 峰值
        {"id": "d4", "session_id": "s", "model": "Hermes-35B", "is_local": 1,
         "started_at": _iso(now - timedelta(days=10)), "input_tokens": 5000,
         "gen_tokens": 4000, "duration_ms": 300_000},
        # 40 天前孤立一天（3 连之外的历史最长段）
        {"id": "d5", "session_id": "s", "model": "mimo-flash", "is_local": 0,
         "started_at": _iso(now - timedelta(days=40)), "input_tokens": 10,
         "gen_tokens": 5, "duration_ms": 5_000},
    ]
    _seed(rows)
    yield
    _teardown_db()


def test_dashboard_cards_and_streaks(dash_db):
    import turn_metrics
    out = turn_metrics.dashboard_stats(182)
    # 全库合计（不受窗口影响）
    assert out["all_time"]["n"] == 5
    assert out["all_time"]["input"] == 1000 + 800 + 600 + 5000 + 10
    assert out["all_time"]["gen"] == 500 + 400 + 300 + 4000 + 5
    assert out["all_time"]["longest_turn_ms"] == 300_000
    # 连续天数：最近 3 天连续 → 当前 3、最长 3
    assert out["streak_current"] == 3
    assert out["streak_longest"] == 3
    # 峰值 = 10 天前那天（9000 token）
    assert out["peak_day"]["input"] == 5000 and out["peak_day"]["gen"] == 4000
    # 按天×模型：合并口径
    pairs = {(x["date"], x["model"]): x["tokens"] for x in out["per_day_model"]}
    today_key = _iso(datetime.now())[:10]
    assert pairs[(today_key, "Hermes-35B")] == 1500
    yest_key = _iso(datetime.now() - timedelta(days=1))[:10]
    assert pairs[(yest_key, "mimo-flash")] == 1200
    # 按天桶覆盖 5 天
    assert len(out["per_day"]) == 5


def test_dashboard_empty(tmp_path, monkeypatch):
    _point_db_at(tmp_path, monkeypatch, "tmdash-empty.db")
    try:
        import turn_metrics
        out = turn_metrics.dashboard_stats(182)
        assert out["all_time"]["n"] == 0
        assert out["streak_current"] == 0 and out["streak_longest"] == 0
        assert out["peak_day"] is None and out["per_day"] == []
    finally:
        _teardown_db()


def test_dashboard_route_clamps_window(dash_db):
    from api_routes_admin import get_turn_metrics_dashboard
    out = asyncio_ok(get_turn_metrics_dashboard(window=99999))
    assert out["status"] == "ok" and out["window_days"] == 365


def asyncio_ok(coro):
    import asyncio
    return asyncio.run(coro)
