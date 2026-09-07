"""session_log 契约测试（移植自 dsh session append 契约）。

覆盖：seq 连续、surface 强制、坏数据拒绝（非有限数/函数/stem）、
快照深拷贝（事后改不动）、source_seqs 唯一、SQLite 回放往返。
"""
import json
import sqlite3

import pytest

from session_log import EventLogError, SessionLog, SessionEvent, _persist_enabled


@pytest.fixture()
def conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA journal_mode=WAL")
    yield conn
    conn.close()


@pytest.fixture()
def log(conn):
    return SessionLog("test-session", conn=conn, persist=True)


def test_basic_append_seq_contract(log):
    e1 = log.append("turn/start", {"turn": 1})
    e2 = log.append("step/start", {"turn": 1, "step": 1})
    assert e1.seq == 0 and e2.seq == 1
    assert isinstance(e1.time_ms, int) and e1.time_ms > 0
    assert len(log._events) == 2


def test_surface_requires_surface_op(log):
    with pytest.raises(EventLogError, match="must declare surface_op"):
        log.append("user/message", {"message": {"role": "user", "content": "hi"}})
    with pytest.raises(EventLogError, match="not a surface event"):
        log.append("turn/start", {"turn": 1}, surface_op="append")


def test_surface_ok_with_op_and_sources(log):
    user = log.append("user/message", {"message": {}}, surface_op="append")
    log.append(
        "assistant/message",
        {"message": {"content": "hi"}},
        surface_op="append",
        source_seqs=[user.seq],
    )


def test_nan_rejected(log):
    with pytest.raises(EventLogError, match="non-JSON-serializable"):
        log.append("turn/start", {"turn": float("nan")})


def test_function_rejected(log):
    with pytest.raises(EventLogError, match="non-JSON-serializable"):
        log.append("turn/start", {"callback": lambda x: x})


def test_non_dict_data_rejected(log):
    with pytest.raises(EventLogError, match="must be an object"):
        log.append("turn/start", [1, 2, 3])


def test_unknown_type_rejected(log):
    with pytest.raises(EventLogError, match="unknown session event type"):
        log.append("mystery/event", {})


def test_snapshot_is_deep_copy(log):
    data = {"text": "hello", "meta": {"n": 1}}
    log.append("turn/start", {"turn": 1})
    e = log.append("assistant/chunk", {"chunk": data})
    data["text"] = "MUTATED"
    data["meta"]["n"] = 999
    assert e.data["chunk"]["text"] == "hello"
    assert e.data["chunk"]["meta"]["n"] == 1


def test_source_seqs_must_be_unique(log):
    log.append("turn/start", {"turn": 1})
    with pytest.raises(EventLogError, match="distinct"):
        log.append(
            "assistant/message",
            {"message": {}},
            surface_op="append",
            source_seqs=[1, 1],
        )


def test_persist_replay_roundtrip(conn):
    log = SessionLog("replay-session", conn=conn, persist=True)
    log.append("turn/start", {"turn": 1})
    surface = log.append(
        "user/message", {"message": {"role": "user", "content": "hello"}},
        surface_op="append",
    )
    log.append(
        "tool/result",
        {"message": {"role": "tool", "content": "ok"}},
        surface_op="append",
        source_seqs=[surface.seq],
    )
    replayed = SessionLog.load("replay-session", conn=conn)
    assert [e.type for e in replayed] == ["turn/start", "user/message", "tool/result"]
    assert [e.seq for e in replayed] == [0, 1, 2]
    assert replayed[1].surface_op == "append"
    assert replayed[2].source_seqs == (1,)
    assert replayed[1].data["message"]["content"] == "hello"


def test_restart_continues_seq_not_conflict(conn):
    """P1-2：重启（新实例）后 append 必须从已持久化 seq 之后继续——否则
    INSERT OR IGNORE 会静默丢弃新事件。"""
    first = SessionLog("restart-session", conn=conn, persist=True)
    first.append("turn/start", {"turn": 1})
    first.append("step/start", {"turn": 1, "step": 1})
    # 模拟 sidecar 重启：同连接新建实例
    second = SessionLog("restart-session", conn=conn, persist=True)
    assert [e.seq for e in second._events] == [0, 1]  # 已恢复既有事件
    ev = second.append("step/end", {"turn": 1, "step": 1})
    assert ev.seq == 2  # 连续，不再与 DB 冲突
    rows = SessionLog.load("restart-session", conn=conn)
    assert [e.seq for e in rows] == [0, 1, 2]  # 三条都落库（无人被 OR IGNORE）


def test_replay_missing_session_empty(conn):
    assert SessionLog.load("no-such-session", conn=conn) == []


def test_persist_flag_respects_env(monkeypatch):
    monkeypatch.delenv("LATIAO_EVENT_LOG", raising=False)
    assert _persist_enabled() is False
    monkeypatch.setenv("LATIAO_EVENT_LOG", "1")
    assert _persist_enabled() is True
    # 任何非空值不启用（开关语义：允许未来显式 off 值）
    monkeypatch.setenv("LATIAO_EVENT_LOG", "0")
    assert _persist_enabled() is False


def test_default_conn_none_ok(monkeypatch):
    monkeypatch.setattr("session_log._default_conn", lambda: None)
    log = SessionLog("no-db")
    e = log.append("turn/start", {"turn": 1})
    assert e.seq == 0
