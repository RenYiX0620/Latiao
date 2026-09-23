"""Database connection and schema management for Latiao sidecar."""
import logging
import os
import re
import sqlite3
import time
from datetime import datetime
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
            [])
        # preferences_fts 是死索引（2026-09-23 审查）：建了虚表但**没有触发器**
        # （真库实测触发器只有 tool_calls_*/learnings_*），因此永不更新；全仓库也没有
        # 任何查询它（偏好是按 confidence 门槛读的，不是文本检索）。新库不再建，
        # 旧库移除——留着只会让人以为偏好有全文检索。
        try:
            conn.execute("DROP TABLE IF EXISTS preferences_fts")
        except Exception:
            logger.debug("drop legacy preferences_fts failed", exc_info=True)

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

        # ── 会话持久化（2026-09-23，审查⑩）──────────────────────────
        # 此前会话只在前端 localStorage（5MB 配额 + 四级降级链，跨设备/备份/服务端
        # 摘要都无从谈起）。这里落库：
        #   sessions          会话元数据（列表页只需读它 + preview）
        #   session_messages  消息（整条消息的 JSON 存在 data 里——前端 Message 类型
        #                     有十几个可选字段，逐列建表会随字段增删漂移）
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS sessions ("
                "id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', "
                "selected_model TEXT DEFAULT '', last_active INTEGER DEFAULT 0, "
                "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
                "message_count INTEGER DEFAULT 0, preview TEXT DEFAULT '')")
            conn.execute("CREATE TABLE IF NOT EXISTS session_messages ("
                "id TEXT PRIMARY KEY, session_id TEXT NOT NULL, seq INTEGER NOT NULL, "
                "role TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_session_messages_sid "
                "ON session_messages(session_id, seq)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_active "
                "ON sessions(last_active DESC)")
        except Exception:
            logger.error("Failed to create sessions tables", exc_info=True)

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


# ── 记忆的遗忘机制（2026-09-23，审查⑦）────────────────────────────────
# 审查原文：「全库无 TTL、无行数上限、无定期清理；置信度只涨不降……垃圾记忆进来
# 的唯一出路是被写入门槛拦住，一旦进去就是永久 1.0 满分」。
# 这里做两件**不依赖打分**的事（按质量淘汰要等 ⑥ 的检索信号，见 db.prune_learnings）：
#   1. reflections 定期清理：它是"每次工具调用的过程记录"，本来就不该永久保存
#      （tool_calls 已有 30 天策略，反思同源同理）；
#   2. learnings 容量上限：超限时淘汰"价值最低"的（置信度 × 命中次数 × 新近度），
#      给垃圾记忆一条出路——但只在超限时才动手，日常不减少任何知识。
DEFAULT_REFLECTIONS_RETENTION_DAYS = 180
DEFAULT_LEARNINGS_MAX = 3000


def reflections_retention_days() -> int:
    """反思保留天数：环境变量 > config.json memory.reflections_retention_days > 180。<=0 关闭。"""
    raw = os.environ.get("LATIAO_REFLECTIONS_RETENTION_DAYS")
    if raw is None:
        try:
            import json as _json
            from config import CONFIG_FILE
            if CONFIG_FILE.exists():
                cfg = _json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                section = cfg.get("memory") if isinstance(cfg, dict) else None
                if isinstance(section, dict) and section.get("reflections_retention_days") is not None:
                    raw = str(section.get("reflections_retention_days"))
        except Exception:
            raw = None
    try:
        return int(raw) if raw is not None else DEFAULT_REFLECTIONS_RETENTION_DAYS
    except (TypeError, ValueError):
        return DEFAULT_REFLECTIONS_RETENTION_DAYS


def learnings_max() -> int:
    """learnings 行数上限：环境变量 > config.json memory.learnings_max > 3000。<=0 关闭淘汰。"""
    raw = os.environ.get("LATIAO_LEARNINGS_MAX")
    if raw is None:
        try:
            import json as _json
            from config import CONFIG_FILE
            if CONFIG_FILE.exists():
                cfg = _json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                section = cfg.get("memory") if isinstance(cfg, dict) else None
                if isinstance(section, dict) and section.get("learnings_max") is not None:
                    raw = str(section.get("learnings_max"))
        except Exception:
            raw = None
    try:
        return int(raw) if raw is not None else DEFAULT_LEARNINGS_MAX
    except (TypeError, ValueError):
        return DEFAULT_LEARNINGS_MAX


def prune_learnings(max_rows: int | None = None) -> dict:
    """超出上限时淘汰价值最低的 learnings。返回 {deleted, kept, max}。

    价值 = confidence × (1 + hit_count) × 新近度（180 天半衰期）。
    为什么是这三项：置信度是写入侧给的（重复出现会累加），hit_count 是"被检索到过"
    （唯一的需求侧信号，目前只在 TF-IDF 主路径与 FTS 回退里加），新近度防"一次满分
    永久占位"。**只在超限时淘汰**——未超限时不动任何一行。
    """
    cap = learnings_max() if max_rows is None else int(max_rows)
    if cap <= 0:
        return {"deleted": 0, "kept": -1, "max": cap, "skipped": "cap disabled"}
    conn = _get_db()
    with _db_write_lock:
        try:
            total = conn.execute("SELECT COUNT(*) FROM learnings").fetchone()[0]
        except Exception:
            logger.warning("统计 learnings 失败", exc_info=True)
            return {"deleted": 0, "kept": -1, "max": cap, "skipped": "error"}
        if total <= cap:
            return {"deleted": 0, "kept": total, "max": cap}
        try:
            rows = conn.execute(
                "SELECT id, confidence, hit_count, MAX(created_at, updated_at) AS ts "
                "FROM learnings").fetchall()
        except Exception:
            logger.warning("读取 learnings 失败", exc_info=True)
            return {"deleted": 0, "kept": total, "max": cap, "skipped": "error"}
        now = time.time()
        scored = []
        for rid, conf, hits, ts in rows:
            try:
                t = datetime.fromisoformat(ts).timestamp()
            except (TypeError, ValueError):
                t = now
            age_days = max(0.0, (now - t) / 86400)
            recency = 0.5 ** (age_days / 180.0)          # 180 天半衰期
            scored.append(((conf or 0.0) * (1.0 + (hits or 0)) * recency, rid))
        scored.sort()                                     # 最低价值在前
        victims = [rid for _score, rid in scored[: total - cap]]
        try:
            conn.executemany("DELETE FROM learnings WHERE id = ?", [(v,) for v in victims])
            conn.commit()
        except Exception:
            logger.warning("淘汰 learnings 失败", exc_info=True)
            return {"deleted": 0, "kept": total, "max": cap, "skipped": "error"}
        deleted = len(victims)
        kept = total - deleted
    try:
        from memory import _mark_tfidf_dirty
        _mark_tfidf_dirty()                               # 淘汰后必须让检索索引失效
    except Exception:
        pass
    logger.info("learnings 超限淘汰: %d 条（上限 %d，保留 %d）", deleted, cap, kept)
    return {"deleted": deleted, "kept": kept, "max": cap}


def prune_reflections(retention_days: int | None = None) -> dict:
    """删除超过保留期的反思（与 tool_calls 同源的"过程记录"）。"""
    days = reflections_retention_days() if retention_days is None else int(retention_days)
    if days <= 0:
        return {"days": days, "deleted": 0, "kept": -1, "skipped": "retention disabled"}
    conn = _get_db()
    with _db_write_lock:
        try:
            cur = conn.execute(
                "DELETE FROM reflections WHERE created_at != '' AND created_at < datetime('now', ?)",
                (f"-{days} days",))
            deleted = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            conn.commit()
        except Exception:
            logger.warning("反思清理失败", exc_info=True)
            return {"days": days, "deleted": 0, "kept": -1, "skipped": "error"}
        try:
            kept = conn.execute("SELECT COUNT(*) FROM reflections").fetchone()[0]
        except Exception:
            kept = -1
    return {"days": days, "deleted": deleted, "kept": kept}


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
