"""会话持久化（2026-09-23，审查⑩）。

此前会话只存在前端 localStorage：5MB 配额（超了走四级降级链，会丢旧会话）、
换设备/清缓存即失、后端无法做摘要或搜索。这里给后端一份权威存储。

写入模型：前端把**整个会话快照**（元数据 + 消息数组）PUT 上来，服务端在一个事务里
替换该会话的消息。理由：前端的真相就是那份数组（流式追加/改写都在数组里做），
快照式写入天然幂等、不需要增量协议；配合前端 1~1.5s 防抖，SQLite 完全吃得下。

列表页只需要元数据 + 预览（侧边栏显示"最后一条消息前 30 字"），所以 list 接口
不回消息体——消息按需按 id 取（lazy load），启动不用把全部历史读进内存。
"""
import json
import logging
from datetime import datetime

from db import _db_write_lock, _get_db

logger = logging.getLogger("latiao-sidecar")

# 单会话消息上限与单条消息体积上限（防前端异常把库撑爆；截断处会记日志）
MAX_MESSAGES_PER_SESSION = 2000
MAX_MESSAGE_CHARS = 200_000
MAX_SESSIONS = 1000


def _now() -> str:
    return datetime.now().isoformat()


def _clip(text: str, limit: int) -> str:
    s = str(text or "")
    return s if len(s) <= limit else s[:limit] + "…(已截断)"


def save_session(session_id: str, name: str = "", selected_model: str = "",
                 last_active: int = 0, messages: list | None = None) -> dict:
    """快照式 upsert：写元数据 + **替换**该会话的消息（一个事务内完成）。"""
    sid = str(session_id or "").strip()
    if not sid:
        return {"status": "error", "message": "session id required"}
    msgs = [m for m in (messages or []) if isinstance(m, dict)]
    if len(msgs) > MAX_MESSAGES_PER_SESSION:
        logger.info("会话 %s 消息 %d 条超上限，仅保留最近 %d 条",
                    sid[:16], len(msgs), MAX_MESSAGES_PER_SESSION)
        msgs = msgs[-MAX_MESSAGES_PER_SESSION:]
    preview = ""
    for m in reversed(msgs):
        text = str(m.get("content") or "").strip()
        if text:
            preview = _clip(" ".join(text.split()), 60)
            break
    try:
        conn = _get_db()
        with _db_write_lock:
            row = conn.execute("SELECT created_at FROM sessions WHERE id = ?", (sid,)).fetchone()
            created = row[0] if row else _now()
            conn.execute(
                """INSERT INTO sessions(id, name, selected_model, last_active, created_at,
                                        updated_at, message_count, preview)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                       selected_model=excluded.selected_model,
                       last_active=excluded.last_active,
                       updated_at=excluded.updated_at,
                       message_count=excluded.message_count,
                       preview=excluded.preview""",
                (sid, str(name or "")[:200], str(selected_model or "")[:100], int(last_active or 0),
                 created, _now(), len(msgs), preview))
            conn.execute("DELETE FROM session_messages WHERE session_id = ?", (sid,))
            rows = []
            for i, m in enumerate(msgs):
                payload = json.dumps(m, ensure_ascii=False)
                if len(payload) > MAX_MESSAGE_CHARS:
                    payload = json.dumps({**m, "content": _clip(m.get("content"), MAX_MESSAGE_CHARS)},
                                         ensure_ascii=False)
                rows.append((str(m.get("id") or f"{sid}_{i}"), sid, i,
                             str(m.get("role") or "user"), payload, _now()))
            conn.executemany(
                "INSERT OR REPLACE INTO session_messages(id, session_id, seq, role, data, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?)", rows)
            conn.commit()
    except Exception as e:
        logger.warning("保存会话失败: %s", e, exc_info=True)
        return {"status": "error", "message": f"保存失败: {e}"}
    return {"status": "ok", "id": sid, "message_count": len(msgs)}


def list_sessions(limit: int = MAX_SESSIONS, offset: int = 0) -> dict:
    """会话列表（只回元数据 + 预览，不回消息体）。"""
    try:
        conn = _get_db()
        rows = conn.execute(
            "SELECT id, name, selected_model, last_active, created_at, updated_at, "
            "message_count, preview FROM sessions ORDER BY last_active DESC, updated_at DESC "
            "LIMIT ? OFFSET ?", (int(limit), int(offset))).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    except Exception as e:
        logger.warning("读取会话列表失败: %s", e, exc_info=True)
        return {"status": "error", "message": str(e), "sessions": []}
    return {"status": "ok", "total": total, "sessions": [
        {"id": r[0], "name": r[1], "selectedModel": r[2], "lastActive": r[3],
         "created_at": r[4], "updated_at": r[5], "message_count": r[6], "preview": r[7]}
        for r in rows]}


def get_session(session_id: str) -> dict:
    """单个会话 + 全部消息（按 seq 升序）。"""
    sid = str(session_id or "").strip()
    try:
        conn = _get_db()
        meta = conn.execute(
            "SELECT id, name, selected_model, last_active, updated_at FROM sessions WHERE id = ?",
            (sid,)).fetchone()
        if not meta:
            return {"status": "error", "message": "会话不存在"}
        msgs = []
        for data, in conn.execute(
                "SELECT data FROM session_messages WHERE session_id = ? ORDER BY seq", (sid,)):
            try:
                msgs.append(json.loads(data))
            except ValueError:
                continue
    except Exception as e:
        logger.warning("读取会话失败: %s", e, exc_info=True)
        return {"status": "error", "message": str(e)}
    return {"status": "ok",
            "session": {"id": meta[0], "name": meta[1], "selectedModel": meta[2],
                        "lastActive": meta[3], "updated_at": meta[4]},
            "messages": msgs}


def delete_session(session_id: str) -> dict:
    sid = str(session_id or "").strip()
    try:
        conn = _get_db()
        with _db_write_lock:
            conn.execute("DELETE FROM session_messages WHERE session_id = ?", (sid,))
            cur = conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
            conn.commit()
    except Exception as e:
        logger.warning("删除会话失败: %s", e, exc_info=True)
        return {"status": "error", "message": str(e)}
    return {"status": "ok", "deleted": cur.rowcount}


def import_sessions(sessions: list, replace: bool = False) -> dict:
    """一次性迁移：把前端 localStorage 里的会话整体搬进后端。

    幂等：默认只补不覆盖（同 id 且已存在则跳过），除非 replace=True。
    """
    out = {"status": "ok", "imported": 0, "skipped": 0, "errors": []}
    if replace:
        try:
            conn = _get_db()
            with _db_write_lock:
                conn.execute("DELETE FROM session_messages")
                conn.execute("DELETE FROM sessions")
                conn.commit()
        except Exception as e:
            return {"status": "error", "message": str(e)}
    for s in sessions or []:
        if not isinstance(s, dict) or not s.get("id"):
            out["skipped"] += 1
            continue
        sid = str(s["id"])
        try:
            conn = _get_db()
            exists = conn.execute("SELECT 1 FROM sessions WHERE id = ?", (sid,)).fetchone()
            if exists and not replace:
                out["skipped"] += 1
                continue
        except Exception:
            pass
        r = save_session(sid, str(s.get("name") or ""), str(s.get("selectedModel") or ""),
                         int(s.get("lastActive") or 0), s.get("messages") or [])
        if r.get("status") == "ok":
            out["imported"] += 1
        else:
            out["errors"].append({"id": sid, "message": r.get("message")})
    out["total"] = len(sessions or [])
    logger.info("会话导入: 新增 %d / 跳过 %d / 失败 %d", out["imported"], out["skipped"],
                len(out["errors"]))
    return out


def prune_sessions(keep: int = MAX_SESSIONS) -> dict:
    """按最后活跃时间保留最近 keep 个会话，其余删除（会话表长期只增不减的兜底）。"""
    try:
        conn = _get_db()
        with _db_write_lock:
            victims = [r[0] for r in conn.execute(
                "SELECT id FROM sessions ORDER BY last_active DESC, updated_at DESC LIMIT -1 OFFSET ?",
                (int(keep),))]
            for sid in victims:
                conn.execute("DELETE FROM session_messages WHERE session_id = ?", (sid,))
            if victims:
                conn.execute("DELETE FROM sessions WHERE id IN (%s)" % ",".join("?" * len(victims)),
                             victims)
            conn.commit()
    except Exception as e:
        logger.warning("清理旧会话失败: %s", e, exc_info=True)
        return {"status": "error", "message": str(e)}
    return {"status": "ok", "deleted": len(victims)}
