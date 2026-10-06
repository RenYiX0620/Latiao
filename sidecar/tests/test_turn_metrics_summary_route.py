"""历史轮次用量汇总路由（/v1/turn-metrics/summary）的验收测试。

直接播种 turn_metrics 表 + 直接调 async handler（不经过 auth/HTTP 层——
那两层的接线由 test_turn_metrics_route.py 的真流式链路负责，这里只钉
聚合口径本身：合计、本地/云端拆分、TTFT 只算本地非空行、按天桶、
会话过滤、by_source 反序列化、空库形状）。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LATIAO_AUTH_TOKEN", "tms-token")


def _seed(rows: list[dict]) -> None:
    from db import _db_write_lock, _get_db
    cols = ("id, session_id, model, is_local, started_at, ended_at, duration_ms, "
            "input_tokens, gen_tokens, retries, steps, ttft_ms, refine_calls, "
            "refine_tokens, budget, by_source, ended_reason")
    with _db_write_lock:
        conn = _get_db()
        for r in rows:
            conn.execute(
                f"INSERT OR REPLACE INTO turn_metrics({cols}) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["id"], r["session_id"], r["model"], r["is_local"],
                 r["started_at"], r["started_at"], r.get("duration_ms", 1000),
                 r.get("input_tokens", 0), r.get("gen_tokens", 0),
                 r.get("retries", 0), r.get("steps", 1), r.get("ttft_ms"),
                 r.get("refine_calls", 0), r.get("refine_tokens", 0),
                 r.get("budget", 0), r.get("by_source", "{}"),
                 r.get("ended_reason", "completed")))
        conn.commit()


_DDL = ("CREATE TABLE IF NOT EXISTS turn_metrics ("
        "id TEXT PRIMARY KEY, session_id TEXT NOT NULL, model TEXT DEFAULT '', "
        "is_local INTEGER DEFAULT 0, started_at TEXT NOT NULL, ended_at TEXT NOT NULL, "
        "duration_ms INTEGER DEFAULT 0, input_tokens INTEGER DEFAULT 0, "
        "gen_tokens INTEGER DEFAULT 0, retries INTEGER DEFAULT 0, steps INTEGER DEFAULT 0, "
        "ttft_ms INTEGER, refine_calls INTEGER DEFAULT 0, refine_tokens INTEGER DEFAULT 0, "
        "budget INTEGER DEFAULT 0, by_source TEXT DEFAULT '{}', ended_reason TEXT DEFAULT '')")


def _point_db_at(tmp_path, monkeypatch, name: str) -> None:
    """把 db 模块指到 tmp 库：MEMORY_DB 换路径 + 清掉模块级缓存连接，
    再按 db.py 的同列 DDL 建表（turn_metrics 的建表语句在 db 大 init 里，
    测试里单独拉不出来，这里保持同列即可——聚合只读这些列）。"""
    import db
    monkeypatch.setattr(db, "MEMORY_DB", tmp_path / name)
    monkeypatch.setattr(db, "_db_conn", None)
    with db._db_write_lock:
        conn = db._get_db()
        conn.execute(_DDL)
        conn.commit()


def _teardown_db() -> None:
    import db
    try:
        if db._db_conn is not None:
            db._db_conn.close()
    except Exception:
        pass
    db._db_conn = None


ROWS = [
    # 两个会话 × 本地/云端 × 两天 × 一种错误结束
    {"id": "s1:1", "session_id": "s1", "model": "Hermes-35B", "is_local": 1,
     "started_at": "2026-10-03T10:00:00", "input_tokens": 1000, "gen_tokens": 500,
     "ttft_ms": 2000, "by_source": '{"main_turn": {"input_tokens": 900, "output_tokens": 400}}'},
    {"id": "s1:2", "session_id": "s1", "model": "Hermes-35B", "is_local": 1,
     "started_at": "2026-10-03T11:00:00", "input_tokens": 800, "gen_tokens": 300,
     "ttft_ms": 4000, "ended_reason": "aborted"},
    {"id": "s2:1", "session_id": "s2", "model": "mimo-v2.6-flash", "is_local": 0,
     "started_at": "2026-10-04T09:00:00", "input_tokens": 2000, "gen_tokens": 1500,
     "ttft_ms": 99999,  # 云端行：不计入本地平均首字
     "refine_calls": 1, "refine_tokens": 700, "retries": 2},
]


@pytest.fixture()
def seeded_db(tmp_path, monkeypatch):
    _point_db_at(tmp_path, monkeypatch, "tmsum.db")
    _seed(ROWS)
    yield
    _teardown_db()


def test_summary_aggregation(seeded_db):
    from api_routes_admin import get_turn_metrics_summary
    out = asyncio.run(get_turn_metrics_summary())
    assert out["status"] == "ok"
    s = out["summary"]
    assert s["n_turns_total"] == 3
    assert s["local_turns"] == 2 and s["cloud_turns"] == 1
    assert s["totals"]["input"] == 3800
    assert s["totals"]["gen"] == 2300
    assert s["totals"]["retries"] == 2
    assert s["totals"]["refine_calls"] == 1 and s["totals"]["refine_tokens"] == 700
    # 平均首字只算本地非空行：(2000+4000)//2 = 3000；云端的 99999 不掺入
    assert s["avg_ttft_ms_local"] == 3000
    assert s["ended"] == {"completed": 2, "aborted": 1}
    # 按天桶：10-03 两行（1500/800 输入），10-04 一行
    days = {d["date"]: d for d in s["per_day"]}
    assert days["2026-10-03"]["n"] == 2 and days["2026-10-03"]["input"] == 1800
    assert days["2026-10-04"]["n"] == 1 and days["2026-10-04"]["gen"] == 1500
    # 按模型聚合，多者在前
    assert s["by_model"][0]["model"] == "Hermes-35B" and s["by_model"][0]["n"] == 2
    # recent 倒序（最新在前），by_source 已反序列化为 dict
    assert out["recent"][0]["id"].endswith("s2:1") or out["recent"][0]["session_id"] == "s2"
    assert isinstance(out["recent"][0]["by_source"], dict)


def test_summary_session_filter(seeded_db):
    from api_routes_admin import get_turn_metrics_summary
    out = asyncio.run(get_turn_metrics_summary(session_id="s1"))
    s = out["summary"]
    assert s["n_turns_total"] == 2
    assert s["local_turns"] == 2 and s["cloud_turns"] == 0
    assert all(r["session_id"] == "s1" for r in out["recent"])


def test_summary_limit_and_empty(tmp_path, monkeypatch):
    _point_db_at(tmp_path, monkeypatch, "tmempty.db")
    try:
        from api_routes_admin import get_turn_metrics_summary
        out = asyncio.run(get_turn_metrics_summary())
        s = out["summary"]
        assert out["status"] == "ok"
        assert s["n_turns_total"] == 0 and s["per_day"] == []
        assert s["avg_ttft_ms_local"] is None and out["recent"] == []
        # limit 参数的钳制不抛错
        out2 = asyncio.run(get_turn_metrics_summary(limit="abc"))
        assert out2["status"] == "ok"
    finally:
        _teardown_db()


def test_clear_endpoint_empties_table(seeded_db):
    """DELETE /v1/turn-metrics：清空 turn_metrics，会话表（sessions）不碰。"""
    from api_routes_admin import clear_turn_metrics
    from db import _db_write_lock, _get_db
    with _db_write_lock:
        conn = _get_db()
        # 整文件跑时 api_routes_admin 的建表链只落在第一个 fixture 的 tmp 库上，
        # 本 fixture 的库里没有 sessions——自建最小结构（IF NOT EXISTS 兼容单跑）
        conn.execute("CREATE TABLE IF NOT EXISTS sessions("
                     "id TEXT PRIMARY KEY, name TEXT NOT NULL, "
                     "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
        _n_before = conn.execute("SELECT COUNT(*) FROM turn_metrics").fetchone()[0]
        conn.execute("INSERT OR REPLACE INTO sessions(id, name, created_at, updated_at) "
                     "VALUES('keep-me', '保留', datetime('now'), datetime('now'))")
        conn.commit()
    assert _n_before == len(ROWS)
    out = asyncio.run(clear_turn_metrics())
    assert out["status"] == "ok" and out["deleted"] == _n_before
    with _db_write_lock:
        conn = _get_db()
        assert conn.execute("SELECT COUNT(*) FROM turn_metrics").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM sessions WHERE id='keep-me'").fetchone()[0] == 1
