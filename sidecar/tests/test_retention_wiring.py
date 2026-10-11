"""memory 表保留 + prune_sessions 接线（2026-10-11，保守默认值）。

此前 memory（cron 产出全文）只增不减、prune_sessions 写好了但零调用。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LATIAO_AUTH_TOKEN", "retain-token")


def test_prune_memory_by_age(tmp_path, monkeypatch):
    import db
    monkeypatch.setattr(db, "MEMORY_DB", tmp_path / "m.db")
    monkeypatch.setattr(db, "_db_conn", None)
    db._init_db()
    conn = db._get_db()
    with db._db_write_lock:
        conn.execute("INSERT INTO memory (session_id, type, topic, content, meta, created_at) "
                     "VALUES ('s','cron_job','old','正文','{}','2020-01-01 00:00:00')")
        conn.execute("INSERT INTO memory (session_id, type, topic, content, meta, created_at) "
                     "VALUES ('s','cron_job','new','正文','{}','2099-01-01 00:00:00')")
        conn.commit()
    r = db.prune_memory(retention_days=30)
    assert r["deleted"] == 1, r
    n = conn.execute("SELECT count(*) FROM memory").fetchone()[0]
    assert n == 1
    row = conn.execute("SELECT topic FROM memory").fetchone()
    assert row[0] == "new"


def test_prune_memory_disabled():
    import db
    r = db.prune_memory(retention_days=0)
    assert r["skipped"] == "retention disabled"


def test_default_retention_is_conservative():
    import db
    assert db.DEFAULT_MEMORY_RETENTION_DAYS == 180
    assert db.memory_retention_days() == 180


def test_wired_into_startup_prune_loop():
    src = Path(__file__).resolve().parents[1].joinpath("main.py").read_text("utf-8")
    assert "prune_memory" in src
    assert "prune_sessions" in src


def test_prune_sessions_wired_and_conservative(tmp_path, monkeypatch):
    """prune_sessions 只删超出 keep 的最旧会话。"""
    import db
    import sessions
    monkeypatch.setattr(db, "MEMORY_DB", tmp_path / "m.db")
    monkeypatch.setattr(db, "_db_conn", None)
    db._init_db()
    conn = db._get_db()
    with db._db_write_lock:
        for i in range(5):
            conn.execute("INSERT INTO sessions(id, name, last_active, created_at, updated_at) "
                         "VALUES (?,?,?,?,?)",
                         (f"s{i}", f"n{i}", i, "2026-01-01", "2026-01-01"))
        conn.commit()
    r = sessions.prune_sessions(keep=2)
    assert r["deleted"] == 3, r
    n = conn.execute("SELECT count(*) FROM sessions").fetchone()[0]
    assert n == 2
    ids = [r[0] for r in conn.execute("SELECT id FROM sessions ORDER BY last_active DESC")]
    assert ids == ["s4", "s3"], ids
