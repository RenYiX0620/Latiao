"""工具调用历史保留策略（db.prune_tool_calls，用户定的 30 天）。

背景：tool_calls 每次工具调用写一行（含参数/结果），只增不减——本机 9 月长到
6283 行 / 35.6MB，其中 57% 是 30 天前的。FTS5 是 external-content + AFTER DELETE
触发器，所以删主表会自动同步检索索引（本测试会验证这一点）。
"""
import importlib

import pytest


@pytest.fixture()
def fresh_db(monkeypatch, tmp_path):
    """把 DB 指向临时目录并重置缓存连接（db._db_conn 是模块级单例）。"""
    monkeypatch.setenv("LATIAO_TEST_PROGRESS_DIR", str(tmp_path))
    import config
    importlib.reload(config)
    import db
    importlib.reload(db)
    db._init_db()          # 平时由 main 启动时调用；临时库需要自己建表
    yield db
    importlib.reload(db)
    importlib.reload(config)


def _insert(db, tool_name: str, days_ago: int, session: str = "s1"):
    conn = db._get_db()
    conn.execute(
        "INSERT INTO tool_calls(id, session_id, tool_name, args, result, created_at)"
        " VALUES(?,?,?,?,?, datetime('now', ?))",
        (f"id_{tool_name}_{days_ago}", session, tool_name, "{}", f"result-{tool_name}",
         f"-{days_ago} days"),
    )
    conn.commit()


def test_prune_deletes_only_old_rows(fresh_db):
    db = fresh_db
    _insert(db, "old_tool", 45)
    _insert(db, "edge_tool", 40)
    _insert(db, "recent_tool", 5)
    _insert(db, "today_tool", 0)

    result = db.prune_tool_calls(retention_days=30)

    assert result["deleted"] == 2 and result["days"] == 30
    conn = db._get_db()
    kept = {r[0] for r in conn.execute("SELECT tool_name FROM tool_calls")}
    assert kept == {"recent_tool", "today_tool"}


def test_prune_keeps_fts_in_sync(fresh_db):
    """删主表必须同步清检索索引（external-content FTS + AFTER DELETE 触发器）。"""
    db = fresh_db
    _insert(db, "searchable_old", 60)
    _insert(db, "searchable_new", 1)
    conn = db._get_db()
    before = conn.execute("SELECT count(*) FROM tool_calls_fts WHERE tool_calls_fts MATCH 'searchable'").fetchone()[0]
    assert before == 2

    db.prune_tool_calls(retention_days=30)

    after = conn.execute("SELECT count(*) FROM tool_calls_fts WHERE tool_calls_fts MATCH 'searchable'").fetchone()[0]
    assert after == 1, "旧行的 FTS 条目没被清掉（检索会命中已删除的历史）"


def test_prune_noop_when_nothing_old(fresh_db):
    db = fresh_db
    _insert(db, "fresh", 1)
    assert db.prune_tool_calls(retention_days=30) == {"days": 30, "deleted": 0, "kept": 1}


def test_retention_days_from_config_and_env(fresh_db, monkeypatch):
    import json
    db = fresh_db
    cfg = db.PROGRESS_DIR / "config.json"
    cfg.write_text(json.dumps({"memory": {"tool_calls_retention_days": 7}}), "utf-8")
    monkeypatch.delenv("LATIAO_TOOL_CALLS_RETENTION_DAYS", raising=False)
    assert db.tool_calls_retention_days() == 7
    monkeypatch.setenv("LATIAO_TOOL_CALLS_RETENTION_DAYS", "3")
    assert db.tool_calls_retention_days() == 3, "环境变量应覆盖 config"
    monkeypatch.setenv("LATIAO_TOOL_CALLS_RETENTION_DAYS", "not-a-number")
    assert db.tool_calls_retention_days() == 30, "非法值回退默认 30"


def test_retention_zero_disables_pruning(fresh_db):
    db = fresh_db
    _insert(db, "ancient", 999)
    r = db.prune_tool_calls(retention_days=0)
    assert r["deleted"] == 0 and "disabled" in r.get("skipped", "")
    assert db._get_db().execute("SELECT count(*) FROM tool_calls").fetchone()[0] == 1


def test_prune_vacuums_and_checkpoints(fresh_db):
    """VACUUM + checkpoint 之后：空闲页归零、WAL 回收（目录真的瘦下来）。

    断言的是**准确性质**：删除会留下 freelist（空闲页），VACUUM 之后必须归零；
    只比 page_count 在小库上不可靠（40 行小记录本来就没占满一页）。
    """
    import pathlib
    db = fresh_db
    conn = db._get_db()
    payload = "x" * 2048
    for i in range(120):
        conn.execute(
            "INSERT INTO tool_calls(id, session_id, tool_name, args, result, created_at)"
            " VALUES(?,?,?,?,?, datetime('now', ?))",
            (f"big_{i}", "s1", "run_cmd", payload, payload, "-90 days"),
        )
    conn.commit()
    before_pages = conn.execute("PRAGMA page_count").fetchone()[0]

    db.prune_tool_calls(retention_days=30)

    assert conn.execute("SELECT count(*) FROM tool_calls").fetchone()[0] == 0
    assert conn.execute("PRAGMA freelist_count").fetchone()[0] == 0, "VACUUM 没回收空闲页"
    assert conn.execute("PRAGMA page_count").fetchone()[0] < before_pages
    wal = pathlib.Path(str(db.MEMORY_DB) + "-wal")
    assert (not wal.exists()) or wal.stat().st_size < 64 * 1024, "WAL 未回收"
