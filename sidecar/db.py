"""Database connection and schema management for Latiao sidecar."""
import asyncio
import logging
import sqlite3
import threading
from pathlib import Path

from config import PROGRESS_DIR

logger = logging.getLogger(__name__)

# Database path
MEMORY_DB = PROGRESS_DIR / "memory.db"

# Connection and lock management
_db_conn: sqlite3.Connection | None = None
_db_write_lock = threading.Lock()  # protects sync write paths
_async_db_lock = asyncio.Lock()    # protects async write paths


def _get_db() -> sqlite3.Connection:
    """Return a module-level SQLite connection (lazy-init, reused across calls)."""
    global _db_conn
    if _db_conn is None:
        PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
        _db_conn = sqlite3.connect(str(MEMORY_DB), check_same_thread=False)
        _db_conn.execute("PRAGMA journal_mode=WAL")
    return _db_conn



def _create_table(conn: sqlite3.Connection, name: str, columns: str, extras: list[str] | None = None):
    """Create a table + FTS5 virtual table + triggers if they don't exist."""
    conn.execute(f"CREATE TABLE IF NOT EXISTS {name} ({columns})")
    if extras:
        for stmt in extras:
            conn.execute(stmt)



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

        conn.execute("CREATE TABLE IF NOT EXISTS reflections ("
            "id TEXT PRIMARY KEY, session_id TEXT NOT NULL, tool_name TEXT NOT NULL, "
            "tool_args TEXT NOT NULL, tool_result_summary TEXT NOT NULL, "
            "reflection TEXT NOT NULL, was_useful INTEGER DEFAULT 1, created_at TEXT NOT NULL)")

        conn.execute("CREATE TABLE IF NOT EXISTS memory ("
            "session_id TEXT NOT NULL, type TEXT NOT NULL, topic TEXT NOT NULL, "
            "content TEXT NOT NULL, meta TEXT NOT NULL, "
            "created_at TEXT DEFAULT (datetime('now')))")

        conn.commit()
    except Exception:
        logger.warning("Failed to initialize memory DB", exc_info=True)



