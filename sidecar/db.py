"""Database connection and schema management for Latiao sidecar."""
import logging
import re
import sqlite3
import threading

from config import PROGRESS_DIR

logger = logging.getLogger(__name__)

# Database path
MEMORY_DB = PROGRESS_DIR / "memory.db"

# Connection and lock management
_db_conn: sqlite3.Connection | None = None
_db_init_lock = threading.Lock()   # protects lazy connection init
_db_write_lock = threading.Lock()  # protects sync write paths


def _get_db() -> sqlite3.Connection:
    """Return a module-level SQLite connection (lazy-init, reused across calls)."""
    global _db_conn
    if _db_conn is None:
        with _db_init_lock:
            # double-checked locking：避免多线程并发首次调用时重复建连接
            if _db_conn is None:
                PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
                _db_conn = sqlite3.connect(str(MEMORY_DB), check_same_thread=False)
                _db_conn.execute("PRAGMA journal_mode=WAL")
    return _db_conn



# Only allow simple SQL identifiers (table/column names) in DDL to satisfy
# static analysis tools — _create_table is always called with hardcoded literals.
_VALID_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _create_table(conn: sqlite3.Connection, name: str, columns: str, extras: list[str] | None = None):
    """Create a table + FTS5 virtual table + triggers if they don't exist."""
    try:
        if not _VALID_IDENTIFIER.match(name):
            raise ValueError(f"Invalid table name: {name!r}")
        conn.execute(f"CREATE TABLE IF NOT EXISTS {name} ({columns})")
    except Exception:
        # 主表失败才整体放弃该表（extras 依附于主表，建了也没意义）
        logger.error("Failed to create table %s", name, exc_info=True)
        return
    if extras:
        for stmt in extras:
            try:
                conn.execute(stmt)
            except Exception:
                # extras 失败不阻断其他表，但必须可见——否则 FTS 永久缺失且无人察觉
                logger.error("Failed to create FTS/trigger for %s: %.60s", name, stmt, exc_info=True)



def _init_db():
    """Create memory.db tables + FTS5 triggers if they don't exist."""
    try:
        conn = _get_db()

        _create_table(conn, "tool_calls",
            "id TEXT PRIMARY KEY, session_id TEXT NOT NULL, tool_name TEXT NOT NULL, "
            "args TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL",
            [
                "CREATE VIRTUAL TABLE IF NOT EXISTS tool_calls_fts USING fts5("
                "tool_name, args, result, content='tool_calls', content_rowid='rowid')",
                "CREATE TRIGGER IF NOT EXISTS tool_calls_ai AFTER INSERT ON tool_calls BEGIN "
                "INSERT INTO tool_calls_fts(rowid, tool_name, args, result) "
                "VALUES (new.rowid, new.tool_name, new.args, new.result); END",
                "CREATE TRIGGER IF NOT EXISTS tool_calls_ad AFTER DELETE ON tool_calls BEGIN "
                "INSERT INTO tool_calls_fts(tool_calls_fts, rowid, tool_name, args, result) "
                "VALUES ('delete', old.rowid, old.tool_name, old.args, old.result); END",
            ])

        _create_table(conn, "learnings",
            "id TEXT PRIMARY KEY, session_id TEXT NOT NULL, topic TEXT NOT NULL, "
            "content TEXT NOT NULL, confidence REAL DEFAULT 0.5, source_type TEXT DEFAULT 'extracted', "
            "hit_count INTEGER DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL",
            [
                "CREATE VIRTUAL TABLE IF NOT EXISTS learnings_fts USING fts5("
                "topic, content, content='learnings', content_rowid='rowid')",
                "CREATE TRIGGER IF NOT EXISTS learnings_ai AFTER INSERT ON learnings BEGIN "
                "INSERT INTO learnings_fts(rowid, topic, content) "
                "VALUES (new.rowid, new.topic, new.content); END",
                "CREATE TRIGGER IF NOT EXISTS learnings_ad AFTER DELETE ON learnings BEGIN "
                "INSERT INTO learnings_fts(learnings_fts, rowid, topic, content) "
                "VALUES ('delete', old.rowid, old.topic, old.content); END",
                "CREATE TRIGGER IF NOT EXISTS learnings_au AFTER UPDATE ON learnings BEGIN "
                "INSERT INTO learnings_fts(learnings_fts, rowid, topic, content) "
                "VALUES ('delete', old.rowid, old.topic, old.content); "
                "INSERT INTO learnings_fts(rowid, topic, content) "
                "VALUES (new.rowid, new.topic, new.content); END",
            ])

        _create_table(conn, "preferences",
            "id TEXT PRIMARY KEY, key TEXT UNIQUE NOT NULL, value TEXT NOT NULL, "
            "source TEXT DEFAULT 'inferred', confidence REAL DEFAULT 0.5, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL",
            [
                "CREATE VIRTUAL TABLE IF NOT EXISTS preferences_fts USING fts5("
                "key, value, content='preferences', content_rowid='rowid')",
            ])

        # ── 统一能力模型：工具与技能合并为一张能力表（capability registry）──
        # kind: 'tool'（代码插件）| 'skill'（markdown 提示词）
        # source: 'builtin' | 'extension:<名>' | 'user'
        # 工具的执行代码仍在内存 dispatch，本表是其目录/开关/权限/计数的唯一事实源
        # perm_override: 1 = 用户经 API 设置的权限覆盖（优先于插件默认 TOOL_PERMISSIONS）
        _create_table(conn, "capabilities",
            "name TEXT PRIMARY KEY, kind TEXT NOT NULL, display_name TEXT NOT NULL, "
            "description TEXT DEFAULT '', definition TEXT DEFAULT '{}', "
            "content TEXT DEFAULT '', permission TEXT DEFAULT 'safe', "
            "perm_override INTEGER DEFAULT 0, "
            "enabled INTEGER DEFAULT 1, source TEXT DEFAULT 'builtin', "
            "source_path TEXT DEFAULT '', usage_count INTEGER DEFAULT 0, "
            "created_at TEXT DEFAULT (datetime('now')), "
            "updated_at TEXT DEFAULT (datetime('now'))")
        # 兼容早期开发库：缺 perm_override 列时补上（幂等）
        try:
            conn.execute("ALTER TABLE capabilities ADD COLUMN perm_override INTEGER DEFAULT 0")
        except Exception:
            pass

        try:
            conn.execute("CREATE TABLE IF NOT EXISTS reflections ("
                "id TEXT PRIMARY KEY, session_id TEXT NOT NULL, tool_name TEXT NOT NULL, "
                "tool_args TEXT NOT NULL, tool_result_summary TEXT NOT NULL, "
                "reflection TEXT NOT NULL, was_useful INTEGER DEFAULT 1, created_at TEXT NOT NULL)")
        except Exception:
            logger.error("Failed to create table reflections", exc_info=True)

        try:
            conn.execute("CREATE TABLE IF NOT EXISTS memory ("
                "session_id TEXT NOT NULL, type TEXT NOT NULL, topic TEXT NOT NULL, "
                "content TEXT NOT NULL, meta TEXT NOT NULL, "
                "created_at TEXT DEFAULT (datetime('now')))")
        except Exception:
            logger.error("Failed to create table memory", exc_info=True)

        conn.commit()
    except Exception:
        logger.error("Failed to initialize memory DB", exc_info=True)

# ── 工具调用历史的保留策略（2026-09-23，用户定的 30 天）──────────────
# 为什么需要：tool_calls 每次工具调用写一行（含参数/结果），只增不减——
# 本机 9 月已长到 6283 行 / 35.6MB，其中 57% 是 30 天前的。检索
# （/v1/memory/search）用 FTS5 external-content 表，删主表会由 AFTER DELETE
# 触发器自动同步，不需要额外清 FTS。
DEFAULT_TOOL_CALLS_RETENTION_DAYS = 30


def tool_calls_retention_days() -> int:
    """保留天数：环境变量 LATIAO_TOOL_CALLS_RETENTION_DAYS > config.json
    memory.tool_calls_retention_days > 30。<=0 表示不清理。"""
    import json
    import os
    raw = os.environ.get("LATIAO_TOOL_CALLS_RETENTION_DAYS", "")
    if not raw:
        try:
            cfg = json.loads((PROGRESS_DIR / "config.json").read_text(encoding="utf-8")) or {}
            section = cfg.get("memory") if isinstance(cfg, dict) else None
            if isinstance(section, dict) and section.get("tool_calls_retention_days") is not None:
                raw = str(section.get("tool_calls_retention_days"))
        except Exception:
            raw = ""
    try:
        days = int(raw)
    except (TypeError, ValueError):
        days = DEFAULT_TOOL_CALLS_RETENTION_DAYS
    return days


def prune_tool_calls(retention_days: int | None = None, vacuum: bool = True) -> dict:
    """删除超过保留期的工具调用历史。返回 {days, deleted, kept}。

    - 清理在写锁内做（与 _record_tool_call_db 互斥）
    - 删完做一次 wal_checkpoint(TRUNCATE)，并在确实删了行时 VACUUM 回收空间
      （35MB 级库上是毫秒级；VACUUM 失败不影响删除结果，只记日志）
    """
    days = tool_calls_retention_days() if retention_days is None else int(retention_days)
    if days <= 0:
        return {"days": days, "deleted": 0, "kept": 0, "skipped": "retention disabled"}
    conn = _get_db()
    with _db_write_lock:
        try:
            cur = conn.execute(
                "DELETE FROM tool_calls WHERE created_at != '' AND created_at < datetime('now', ?)",
                (f"-{days} days",),
            )
            deleted = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            conn.commit()
        except Exception:
            logger.warning("工具历史清理失败", exc_info=True)
            return {"days": days, "deleted": 0, "kept": -1, "skipped": "error"}
        try:
            kept = conn.execute("SELECT count(*) FROM tool_calls").fetchone()[0]
        except Exception:
            kept = -1
    if deleted and vacuum:
        try:
            with _db_write_lock:
                conn.execute("VACUUM")
        except Exception:
            logger.debug("VACUUM 失败（空间回收留到下次）", exc_info=True)
    # checkpoint 放在 VACUUM **之后**：VACUUM 自己会写一大截 WAL（实测 19.8MB），
    # 不回收的话目录看起来没瘦（首次实现就踩了这个顺序）
    try:
        with _db_write_lock:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        logger.debug("wal_checkpoint 失败（不影响清理结果）", exc_info=True)
    if deleted:
        logger.info("工具历史清理：保留 %d 天，删除 %d 行，剩余 %d 行", days, deleted, kept)
    return {"days": days, "deleted": deleted, "kept": kept}
