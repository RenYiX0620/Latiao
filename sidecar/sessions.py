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
import re
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


# ── 会话检索（2026-10-19）──────────────────────────────────────
# 为什么不用 FTS5 自带分词：unicode61 把连续的 CJK 当一个 token（"今天大盘"整体
# 一个词），"大盘"永远搜不到。做法是入库与查询都先逐字空格切分（CJK 一字一
# token、ASCII 词保持整词，unicode61 自己处理大小写）。snippet 也不交给 FTS5
# （切分后的文本带空格），命中后在 Python 侧按真实正文截取。
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def _fts_tokenize(text: str) -> str:
    """正文 → FTS token 串：CJK 逐字、ASCII 词整词、其余作分隔。"""
    parts: list[str] = []
    buf: list[str] = []

    def _flush():
        if buf:
            parts.append("".join(buf))
            buf.clear()

    for ch in str(text or ""):
        if _CJK_RE.match(ch):
            _flush()
            parts.append(ch)
        elif ch.isalnum() or ch in "_'’":
            buf.append(ch)
        else:
            _flush()
    _flush()
    return " ".join(parts)


def _fts_query(query: str) -> str:
    """查询 → FTS 表达式：每个空白词切分后作为短语，多词之间 AND。"""
    phrases = []
    for word in str(query or "").split():
        tok = _fts_tokenize(word).strip()
        if tok:
            phrases.append('"' + tok.replace('"', '""') + '"')
    return " AND ".join(phrases)


def _fts_sync(conn, session_id: str, msgs: list) -> None:
    """重建该会话的检索索引（快照式写入 → 整段替换最省心）。"""
    conn.execute("DELETE FROM session_messages_fts WHERE session_id = ?", (session_id,))
    rows = []
    for i, m in enumerate(msgs):
        if str(m.get("role") or "") not in ("user", "assistant"):
            continue
        text = str(m.get("content") or "").strip()
        if text:
            rows.append((_fts_tokenize(text), session_id,
                         str(m.get("id") or f"{session_id}_{i}")))
    if rows:
        conn.executemany(
            "INSERT INTO session_messages_fts(content_tok, session_id, msg_id) "
            "VALUES (?, ?, ?)", rows)


def _ensure_fts_backfill(conn) -> None:
    """把历史消息灌进检索索引（只在索引为空且库里有消息时跑一次）。"""
    if conn.execute("SELECT COUNT(*) FROM session_messages_fts").fetchone()[0]:
        return
    total = conn.execute("SELECT COUNT(*) FROM session_messages").fetchone()[0]
    if not total:
        return
    logger.info("会话检索索引首次构建：%d 条消息…", total)
    rows = []
    for sid, mid, role, data in conn.execute(
            "SELECT session_id, id, role, data FROM session_messages"):
        if role not in ("user", "assistant"):
            continue
        try:
            text = str(json.loads(data).get("content") or "").strip()
        except Exception:
            continue
        if text:
            rows.append((_fts_tokenize(text), sid, mid))
    if rows:
        with _db_write_lock:
            conn.executemany(
                "INSERT INTO session_messages_fts(content_tok, session_id, msg_id) "
                "VALUES (?, ?, ?)", rows)
            conn.commit()
    logger.info("会话检索索引构建完成：%d 条", len(rows))


def _snippet(text: str, query: str, radius: int = 40) -> str:
    """在真实正文里定位查询词并截取片段（定位不到就取开头一段）。"""
    t = " ".join(str(text or "").split())
    if not t:
        return ""
    pos = t.find(query)
    if pos < 0:
        pos = t.lower().find(str(query).lower())
    if pos < 0:
        head = t[: radius * 2]
        return head + ("…" if len(t) > len(head) else "")
    start = max(0, pos - radius)
    end = min(len(t), pos + len(query) + radius)
    return ("…" if start > 0 else "") + t[start:end] + ("…" if end < len(t) else "")


def search_sessions(query: str, limit: int = 20) -> dict:
    """全文检索历史会话消息（2026-10-19）。只搜 user/assistant 正文。"""
    q = str(query or "").strip()
    if not q:
        return {"status": "error", "message": "query required", "results": []}
    expr = _fts_query(q)
    if not expr:
        return {"status": "ok", "query": q, "total": 0, "results": []}
    limit = max(1, min(int(limit or 20), 50))
    try:
        conn = _get_db()
        _ensure_fts_backfill(conn)
        hits = conn.execute(
            "SELECT session_id, msg_id FROM session_messages_fts "
            "WHERE session_messages_fts MATCH ? ORDER BY rank LIMIT ?",
            (expr, limit)).fetchall()
        results = []
        for sid, mid in hits:
            row = conn.execute(
                "SELECT role, data, created_at FROM session_messages WHERE id = ?",
                (mid,)).fetchone()
            if not row:
                continue
            try:
                text = str(json.loads(row[1]).get("content") or "")
            except Exception:
                text = ""
            meta = conn.execute("SELECT name FROM sessions WHERE id = ?", (sid,)).fetchone()
            results.append({
                "session_id": sid,
                "session_name": (meta[0] if meta else "") or "",
                "role": row[0],
                "snippet": _snippet(text, q),
                "created_at": row[2],
            })
    except Exception as e:
        logger.warning("会话检索失败: %s", e, exc_info=True)
        return {"status": "error", "message": str(e), "results": []}
    return {"status": "ok", "query": q, "total": len(results), "results": results}


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
            if row is None:
                # 匿名使用数据（2026-10-11）：只记"新会话"这个计数（会话 ID/名称
                # 一律不记）。快照保存每次消息都跑，所以必须用"首次插入"做判据。
                try:
                    import anon_usage
                    anon_usage.note("sessions_started")
                except Exception:
                    logger.debug("anon_usage 会话记账失败", exc_info=True)
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
            # 检索索引跟随快照重建（2026-10-19）。独立 try：索引失败不能拖垮
            # 会话保存——用户数据优先，索引可下次重建。
            try:
                _fts_sync(conn, sid, msgs)
            except Exception:
                logger.warning("会话检索索引更新失败（不影响保存）: %s", sid[:16], exc_info=True)
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
