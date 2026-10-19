"""会话检索（2026-10-19）：分词、快照同步、回填、工具入口与路由顺序。

背景：记忆四层里"翻旧会话"这层此前是空的——session_messages 表早就在，但只有
列表与按 id 读取，模型无法跨会话回忆。这里补 FTS5 索引 + /v1/sessions/search
+ session_search 工具。中文必须逐字切分（unicode61 会把整句当一个 token）。
"""
import importlib.util
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db as db_mod  # noqa: E402
import sessions  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MARK = "量子猫猫"


@pytest.fixture(autouse=True, scope="module")
def _init_db():
    db_mod._init_db()


def _msg(role: str, content: str, mid: str) -> dict:
    return {"id": mid, "role": role, "content": content, "ts": 1}


def _save(sid: str, msgs: list) -> None:
    res = sessions.save_session(sid, name="检索测试", messages=msgs)
    assert res.get("status") == "ok", res


# ── 分词 ────────────────────────────────────────────────────────
def test_cjk_tokenized_per_char():
    assert sessions._fts_tokenize("今天大盘") == "今 天 大 盘"
    assert sessions._fts_tokenize("600519 跌 3%") == "600519 跌 3"


def test_query_is_phrase_per_word_and_ands():
    assert sessions._fts_query("大盘 资金") == '"大 盘" AND "资 金"'
    assert sessions._fts_query("  ") == ""


# ── 快照同步 + 检索 ─────────────────────────────────────────────
def test_saved_messages_are_searchable():
    sid = "sess-search-001"
    _save(sid, [
        _msg("user", "今天大盘怎么样", f"{sid}_u"),
        _msg("assistant", f"今天大盘跌了 1.59%，{MARK}很担心", f"{sid}_a"),
        _msg("tool", f"工具结果里也有{MARK}但不应被索引", f"{sid}_t"),
    ])
    res = sessions.search_sessions(MARK)
    assert res["status"] == "ok"
    hits = [r for r in res["results"] if r["session_id"] == sid]
    assert len(hits) == 1, "只应命中 assistant 正文（工具结果不索引）"
    assert hits[0]["role"] == "assistant"
    assert MARK in hits[0]["snippet"]
    assert "跌了 1.59%" in hits[0]["snippet"]


def test_search_is_case_insensitive_for_ascii():
    sid = "sess-search-002"
    _save(sid, [_msg("assistant", "Tesla dropped 5% today", f"{sid}_a")])
    res = sessions.search_sessions("tesla")
    assert any(r["session_id"] == sid for r in res["results"])


def test_no_match_returns_empty():
    _save("sess-search-003", [_msg("user", "随便说点什么", "sess-search-003_u")])
    res = sessions.search_sessions("绝对不存在的词xzq")
    assert res["status"] == "ok" and res["results"] == []


def test_backfill_indexes_preexisting_messages(monkeypatch):
    """索引里没有该会话时，首次检索从 session_messages 回填（老库升级路径）。"""
    sid = "sess-search-004"
    _save(sid, [_msg("assistant", f"老消息里的{MARK}标记", f"{sid}_a")])
    conn = db_mod._get_db()
    with db_mod._db_write_lock:
        conn.execute("DELETE FROM session_messages_fts WHERE session_id = ?", (sid,))
        conn.execute("DELETE FROM session_message_digests WHERE session_id = ?", (sid,))
        conn.commit()
    monkeypatch.setattr(sessions, "_fts_backfill_done", False)   # 回填每进程只跑一次
    res = sessions.search_sessions(MARK)
    assert any(r["session_id"] == sid for r in res["results"])


# ── 工具入口 ────────────────────────────────────────────────────
def _load_plugin():
    f = ROOT / "plugins" / "session_search.py"
    spec = importlib.util.spec_from_file_location("plugin_session_search_test", f)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_tool_returns_snippets_and_session_id():
    sid = "sess-search-005"
    _save(sid, [_msg("assistant", f"结论是：{MARK}方案可行", f"{sid}_a")])
    mod = _load_plugin()
    out = mod.execute({"query": MARK})
    assert MARK in out and sid in out


def test_tool_rejects_empty_query():
    mod = _load_plugin()
    assert "query parameter is required" in mod.execute({})


def test_plugin_is_registered_in_tool_registry():
    import agent_loop
    names = {(t.get("function") or {}).get("name") for t in agent_loop.TOOLS}
    assert "session_search" in names
    assert "session_search" in agent_loop.TOOL_DISPATCH
    assert agent_loop.TOOL_PERMISSIONS.get("session_search") == "safe"


def test_route_declared_before_dynamic_session_route():
    src = (ROOT / "api_routes_extensions.py").read_text("utf-8")
    assert src.find('"/v1/sessions/search"') < src.find('"/v1/sessions/{session_id}"'), \
        "search 路由必须声明在动态路由之前，否则会被当成 session_id"
