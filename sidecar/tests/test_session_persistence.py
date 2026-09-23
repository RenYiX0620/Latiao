"""⑩ 会话持久化（2026-09-23，审查第二梯队）。

审查原文：「会话历史只存在前端 localStorage——后端没有任何 sessions 表/端点。
5MB 配额 + 四级降级链只是止损；跨设备、备份、服务端摘要全部无从谈起」。

后端设计：快照式写入（前端把整个会话数组 PUT 上来，服务端一个事务里替换消息），
列表只回元数据 + 服务端算好的预览，消息按 id 懒加载。
"""
import importlib

import pytest


@pytest.fixture()
def sess(tmp_path, monkeypatch):
    monkeypatch.setenv("LATIAO_TEST_PROGRESS_DIR", str(tmp_path / ".local-ai-os"))
    import config
    importlib.reload(config)
    import db
    importlib.reload(db)
    import sessions
    importlib.reload(sessions)
    db._init_db()
    return sessions


def _msgs(n: int, prefix: str = "消息"):
    return [{"id": f"m{i}", "role": "user" if i % 2 == 0 else "assistant",
             "content": f"{prefix}{i}", "ts": i} for i in range(n)]


def test_snapshot_roundtrip(sess):
    r = sess.save_session("s1", "会话一", "hermes", 111, _msgs(3))
    assert r["status"] == "ok" and r["message_count"] == 3
    got = sess.get_session("s1")
    assert [m["content"] for m in got["messages"]] == ["消息0", "消息1", "消息2"]
    assert got["session"]["name"] == "会话一"
    assert got["session"]["selectedModel"] == "hermes"
    # 额外字段（thinking/toolResult…）整体保留：前端 Message 字段多，逐列建表会漂移
    sess.save_session("s1", "会话一", "hermes", 111,
                      [{"id": "a", "role": "assistant", "content": "x",
                        "thinking": "思考", "toolResult": "结果", "type": "tool_call"}])
    m = sess.get_session("s1")["messages"][0]
    assert m["thinking"] == "思考" and m["toolResult"] == "结果" and m["type"] == "tool_call"


def test_snapshot_replaces_messages(sess):
    sess.save_session("s1", "标题A", "", 1, _msgs(5))
    sess.save_session("s1", "标题B", "", 2, _msgs(2))
    got = sess.get_session("s1")
    assert len(got["messages"]) == 2, "快照语义：写几次就是几次的内容，不追加"
    assert got["session"]["name"] == "标题B"


def test_list_returns_metadata_preview_and_orders_by_activity(sess):
    sess.save_session("old", "旧", "", 100, [{"id": "a", "role": "user", "content": "旧内容"}])
    sess.save_session("new", "新", "", 200, [{"id": "b", "role": "user", "content": "  "},
                                             {"id": "c", "role": "assistant", "content": "最新一条有内容的"}])
    out = sess.list_sessions()
    assert out["total"] == 2
    assert [s["id"] for s in out["sessions"]] == ["new", "old"], "按 lastActive 倒序"
    top = out["sessions"][0]
    assert top["preview"].startswith("最新一条有内容"), "预览取最后一条**有内容**的消息"
    assert top["message_count"] == 2
    assert "messages" not in top, "列表不回消息体（懒加载）"


def test_delete_session(sess):
    sess.save_session("s1", "x", "", 1, _msgs(2))
    assert sess.delete_session("s1")["deleted"] == 1
    assert sess.get_session("s1")["status"] == "error"
    assert sess.list_sessions()["total"] == 0
    assert sess.delete_session("s1")["deleted"] == 0   # 删除不存在的不报错


def test_import_is_idempotent_and_can_replace(sess):
    payload = [{"id": "a", "name": "A", "lastActive": 5, "messages": _msgs(2)},
               {"id": "b", "name": "B", "lastActive": 6, "messages": _msgs(1)}]
    r1 = sess.import_sessions(payload)
    assert (r1["imported"], r1["skipped"]) == (2, 0)
    r2 = sess.import_sessions(payload)
    assert (r2["imported"], r2["skipped"]) == (0, 2), "重复导入应全部跳过（幂等）"
    r3 = sess.import_sessions([{"id": "c", "name": "C", "messages": _msgs(1)}], replace=True)
    assert r3["imported"] == 1
    assert [s["id"] for s in sess.list_sessions()["sessions"]] == ["c"], "replace=True 先清空"


def test_prune_keeps_most_recent(sess):
    for i in range(6):
        sess.save_session(f"s{i}", f"会话{i}", "", i, _msgs(1))
    assert sess.prune_sessions(keep=2)["deleted"] == 4
    left = [s["id"] for s in sess.list_sessions()["sessions"]]
    assert left == ["s5", "s4"], f"应保留最近活跃的两个，实际 {left}"


def test_message_cap_trims_oldest(sess, monkeypatch):
    monkeypatch.setattr(sess, "MAX_MESSAGES_PER_SESSION", 3)
    r = sess.save_session("s1", "x", "", 1, _msgs(5))
    assert r["message_count"] == 3
    assert [m["content"] for m in sess.get_session("s1")["messages"]] == ["消息2", "消息3", "消息4"]


def test_oversized_message_is_clipped(sess, monkeypatch):
    monkeypatch.setattr(sess, "MAX_MESSAGE_CHARS", 500)
    sess.save_session("s1", "x", "", 1, [{"id": "m", "role": "user", "content": "字" * 5000}])
    content = sess.get_session("s1")["messages"][0]["content"]
    assert len(content) < 1000 and "已截断" in content


def test_empty_id_rejected(sess):
    assert sess.save_session("", "x", "", 1, [])["status"] == "error"
    assert sess.get_session("")["status"] == "error"
