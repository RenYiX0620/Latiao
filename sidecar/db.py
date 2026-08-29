"""Database connection and schema management for Latiao sidecar."""
import asyncio
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
_async_db_lock = asyncio.Lock()    # protects async write paths


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



