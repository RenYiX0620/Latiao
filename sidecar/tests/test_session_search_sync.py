"""会话检索的同步修正（2026-10-19 跟进）：回填盲区 / 删除清索引 / 增量同步 / 多词片段。

对应外部审计报告的 4 条：
- 回填只看"索引是否全空" → 升级后先聊一句，旧会话永远不再回填；
- 删除会话/清理/整体替换不删 FTS 行 → 已删正文仍留在索引里（空间 + 隐私残留）；
- save_session 每次全量重建索引 → 长会话 O(n²) 分词；
- 多词查询的片段退化（整串 find 失败 → 退化成取开头）。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db as db_mod  # noqa: E402
import sessions  # noqa: E402


@pytest.fixture(autouse=True, scope="module")
def _init():
    db_mod._init_db()


@pytest.fixture(autouse=True)
def _reset_backfill_flag(monkeypatch):
    # 回填每进程只跑一次；用例需要独立观察时显式复位
    monkeypatch.setattr(sessions, "_fts_backfill_done", False)


def _msg(role, content, mid):
    return {"id": mid, "role": role, "content": content, "ts": 1}


def _save(sid, msgs, last_active=0, allow_shrink=False):
    # 2026-10-24：快照变少默认被拒（审计 P0）；"删除消息"场景显式 allow_shrink=True
    res = sessions.save_session(sid, name="同步测试", last_active=last_active,
                                messages=msgs, allow_shrink=allow_shrink)
    assert res.get("status") == "ok", res


def _count(sql, *args):
    return db_mod._get_db().execute(sql, args).fetchone()[0]


def test_backfill_fills_sessions_missed_by_upgrade():
    """升级后先聊了一句（索引非空）也要补回旧会话——旧实现会永远漏掉它们。"""
    old, new = "sess-sync-old", "sess-sync-new"
    _save(old, [_msg("assistant", "旧会话里的独有标记 OLDONLY", f"{old}_a")])
    _save(new, [_msg("assistant", "新会话内容", f"{new}_a")])
    conn = db_mod._get_db()
    with db_mod._db_write_lock:
        # 模拟升级后只落了新会话的索引
        conn.execute("DELETE FROM session_messages_fts WHERE session_id = ?", (old,))
        conn.execute("DELETE FROM session_message_digests WHERE session_id = ?", (old,))
        conn.commit()
    hits = sessions.search_sessions("OLDONLY")["results"]
    assert any(r["session_id"] == old for r in hits), "旧会话没有被回填"


def test_delete_session_purges_index_and_digests():
    sid = "sess-sync-del"
    _save(sid, [_msg("assistant", "要删除的标记 DELONLY", f"{sid}_a")])
    assert any(r["session_id"] == sid for r in sessions.search_sessions("DELONLY")["results"])
    sessions.delete_session(sid)
    assert _count("SELECT COUNT(*) FROM session_messages_fts WHERE session_id = ?", sid) == 0
    assert _count("SELECT COUNT(*) FROM session_message_digests WHERE session_id = ?", sid) == 0


def test_prune_sessions_purges_index():
    keep, victim = "sess-sync-keep", "sess-sync-victim"
    _save(keep, [_msg("assistant", "保留", f"{keep}_a")], last_active=999)
    _save(victim, [_msg("assistant", "被清理的标记 PRUNEONLY", f"{victim}_a")], last_active=1)
    sessions.prune_sessions(keep=1)
    assert _count("SELECT COUNT(*) FROM session_messages_fts WHERE session_id = ?", victim) == 0
    assert _count("SELECT COUNT(*) FROM session_message_digests WHERE session_id = ?", victim) == 0


def test_incremental_sync_handles_edit_and_removal():
    """编辑消息 / 从会话里删掉消息后，索引里不能残留旧文本。"""
    sid = "sess-sync-inc"
    _save(sid, [
        _msg("assistant", "原始内容 ALPHAONLY", f"{sid}_a"),
        _msg("user", "第二条 BETAONLY", f"{sid}_u"),
    ])
    _save(sid, [_msg("assistant", "改过的内容 GAMMAONLY", f"{sid}_a")], allow_shrink=True)
    assert any(r["session_id"] == sid for r in sessions.search_sessions("GAMMAONLY")["results"])
    assert not any(r["session_id"] == sid for r in sessions.search_sessions("ALPHAONLY")["results"])
    assert not any(r["session_id"] == sid for r in sessions.search_sessions("BETAONLY")["results"])


def test_multi_word_query_snippet_points_at_real_text():
    sid = "sess-sync-mw"
    _save(sid, [_msg("assistant", "今天大盘资金明显流出，尾盘有承接", f"{sid}_a")])
    hits = sessions.search_sessions("大盘 资金")["results"]
    hit = next(r for r in hits if r["session_id"] == sid)
    assert "大盘资金" in hit["snippet"], hit["snippet"]


def test_import_replace_clears_index():
    """整体替换导入（localStorage 迁移）必须连索引一起清。"""
    sid = "sess-sync-import"
    _save(sid, [_msg("assistant", "导入替换前的标记 IMPONLY", f"{sid}_a")])
    # 2026-10-24：replace 空载荷被拒（审计 P1）。清库用一条哑会话整体替换。
    sessions.import_sessions(
        [{"id": "sess-sync-dummy", "name": "哑", "selectedModel": "", "lastActive": 0,
          "messages": [{"id": "d1", "role": "user", "content": "占位"}]}],
        replace=True)
    # 旧会话的索引/摘要必须清空；只剩哑会话自己的 1 行
    assert _count("SELECT COUNT(*) FROM session_messages_fts") == 1
    assert _count("SELECT COUNT(*) FROM session_message_digests") == 1


def test_backfill_repairs_partial_session_index():
    """会话只有部分索引行时也要补缺消息（msg 级差集；旧的 session 级会漏）。"""
    sid = "sess-sync-partial"
    _save(sid, [
        _msg("assistant", "第一条 PARTIALONLY", f"{sid}_a"),
        _msg("user", "第二条 PARTIALTWO", f"{sid}_u"),
    ])
    conn = db_mod._get_db()
    with db_mod._db_write_lock:
        conn.execute("DELETE FROM session_messages_fts WHERE msg_id = ?", (f"{sid}_u",))
        conn.execute("DELETE FROM session_message_digests WHERE msg_id = ?", (f"{sid}_u",))
        conn.commit()
    hits = sessions.search_sessions("PARTIALTWO")["results"]
    assert any(r["session_id"] == sid for r in hits), "部分索引的会话没有补缺"


def test_backfill_restores_missing_digests():
    """已索引但没有摘要的消息 → 回填时补摘要（升级组合；否则下次保存整段重分词）。"""
    sid = "sess-sync-digest"
    _save(sid, [_msg("assistant", "摘要测试 DIGESTONLY", f"{sid}_a")])
    conn = db_mod._get_db()
    with db_mod._db_write_lock:
        conn.execute("DELETE FROM session_message_digests WHERE session_id = ?", (sid,))
        conn.commit()
    sessions.search_sessions("DIGESTONLY")
    assert _count("SELECT COUNT(*) FROM session_message_digests WHERE msg_id = ?",
                  f"{sid}_a") == 1


def test_backfill_deduplicates_rows():
    """同一 msg_id 在索引里只应有一行（FTS 无唯一约束，历史并发首搜会留重复）。"""
    sid = "sess-sync-dup"
    _save(sid, [_msg("assistant", "去重测试 DUPONLY", f"{sid}_a")])
    conn = db_mod._get_db()
    with db_mod._db_write_lock:
        row = conn.execute(
            "SELECT content_tok, session_id, msg_id FROM session_messages_fts WHERE msg_id = ?",
            (f"{sid}_a",)).fetchone()
        conn.execute(
            "INSERT INTO session_messages_fts(content_tok, session_id, msg_id) VALUES (?, ?, ?)",
            row)
        conn.commit()
    assert _count("SELECT COUNT(*) FROM session_messages_fts WHERE msg_id = ?", f"{sid}_a") == 2
    sessions.search_sessions("DUPONLY")          # 触发回填 + 去重
    assert _count("SELECT COUNT(*) FROM session_messages_fts WHERE msg_id = ?", f"{sid}_a") == 1
