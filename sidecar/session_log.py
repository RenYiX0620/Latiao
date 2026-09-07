"""会话事件日志——阶段 1 地基模块（移植自 DeepSeek Harness dsh-session 的 append 契约）。

设计原则（来源：dsh `Session.append` 的契约语义，见核心循环精读文档第 3 层）：
- 事件是唯一事实源：turn/step/user/assistant/tool/cancel 的"发生"落日志；
  一切投影（会话消息、取消状态、收尾判定）从日志派生，不存第二份状态。
- append 契约：data 必须 JSON 可序列化（BigInt/函数/symbol/非有限数/循环引用拒绝），
  seq = 日志长度（连续契约），写入前完成快照——调用方事后改数据不影响日志。
- 坏事件在 append 处失败，而不是在持久化后端 flush 时（dsh 原文语义：
  "The event log is the durable source of truth, so a bad event fails at the
  append site rather than later during a backend flush"）。
- 可选持久化：SQLite（memory.db 的 session_events 表）与内存投影双写。

Latiao 落点：
- feature flag `LATIAO_EVENT_LOG=1` 才写 SQLite（双路径灰度，见升级蓝图阶段 1 的回退策略）；
- 内存投影始终在，供本进程内派生使用；
- 尚未回放的投影只读外接（inbox/header 之类的投影在后续阶段接入）。
"""

from __future__ import annotations

import copy
import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# ── 事件类型表（新增类型先加这里，append 会对未知类型直接拒绝）──
# surface_op 有值时该事件是"模型历史可派生"的来源（对应 dsh 的 SurfaceEventType）：
# 派生模型消息时必须声明如何进入 surface（positional append / revision / replacement）。
EVENT_TYPES = frozenset({
    "turn/start",          # {turn}
    "turn/end",            # {reason: completed|aborted|error}
    "step/start",          # {turn, step}
    "step/end",            # {turn, step}
    "user/message",        # {message}                    surface: append
    "assistant/chunk",     # {turn, step, chunk}          非 surface（过程，供回放）
    "assistant/message",   # {turn, step, message}        surface: append + source_seqs
    "tool/call",           # {turn, step, call_id, name, arguments}
    "tool/result",         # {turn, step, message, error?, meta?}  surface: append + source_seqs
    "cancel/request",      # {cause}
    "heartbeat",           # {} 流保活（非 surface）
})

# 这些事件会进入"模型历史 surface"（含义同 dsh SurfaceEventType）
SURFACE_TYPES = frozenset({"user/message", "assistant/message", "tool/result"})


class EventLogError(ValueError):
    """append 契约违反（坏数据/未知类型/重入）。"""


@dataclass(frozen=True)
class SessionEvent:
    type: str
    seq: int
    time_ms: int
    data: dict[str, Any]
    surface_op: str | None = None
    source_seqs: tuple[int, ...] = ()

    def __repr__(self) -> str:
        return (f"SessionEvent(type={self.type!r}, seq={self.seq}, "
                f"time_ms={self.time_ms}, surface_op={self.surface_op!r}, "
                f"source_seqs={self.source_seqs!r})")


def _snapshot_json(data: Any, type_name: str) -> dict[str, Any]:
    """单遍校验 + 快照：json 往返即保证 JSON 可序列化、深拷贝一次完成。

    `allow_nan=False` 拒绝 NaN/Infinity（json.dumps 默认放行它们，而非有限数
    是 dsh append 契约明确拒绝的）。BigInt 等价物（超大 int 超出 JSON 精度）
    一并拒绝，避免"存储一个值、读回另一个值"的竞态。
    """
    try:
        encoded = json.dumps(data, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise EventLogError(
            f'session event "{type_name}" carries non-JSON-serializable data: {exc}'
        ) from exc
    assert isinstance(encoded, str) and encoded
    try:
        snapshot = json.loads(encoded)
    except (TypeError, ValueError) as exc:  # pragma: no cover — dumps 成功则 loads 必成功
        raise EventLogError(
            f'session event "{type_name}" failed to snapshot: {exc}'
        ) from exc
    # json.loads 最外层可能是数组/标量，事件契约要求 dict
    if not isinstance(snapshot, dict):
        raise EventLogError(f'session event "{type_name}" data must be an object')
    return snapshot


def _freeze(source_seqs: list[int] | None) -> tuple[int, ...]:
    if source_seqs is None:
        return ()
    out = tuple(int(s) for s in source_seqs)
    if len(set(out)) != len(out):
        raise EventLogError("source_seqs must reference distinct earlier events")
    return out


class SessionLog:
    """一个会话的事件日志（内存投影 + 可选 SQLite 双写）。

    用法：
        log = SessionLog("session-id")            # 缺省接 db._get_db()
        ev = log.append("turn/start", {"turn": 1})
        log.append("assistant/message", {...}, surface_op="append",
                   source_seqs=[ev.seq])

    回放：
        for ev in SessionLog.load(session_id): ...

    本类不持有模型状态（inbox/header 投影后续阶段接入）；它只保证
    "发生什么"有序、可校验、可回放。
    """

    def __init__(
        self,
        session_id: str,
        *,
        conn: sqlite3.Connection | None = None,
        persist: bool | None = None,
    ) -> None:
        if not session_id:
            raise ValueError("session_id must be non-empty")
        self.session_id = session_id
        self._conn = conn
        self._persist = _persist_enabled() if persist is None else persist
        if self._persist and self._conn is None:
            # 灰度开启时接管默认连接（memory.db）；拿不到连接则退化为纯内存
            self._conn = _default_conn()
        self._events: list[SessionEvent] = []
        self._lock = threading.Lock()   # append 幂等/并发（代理层是 asyncio，但 SQLite 写是同步）
        if self._persist and self._conn is not None:
            self._ensure_table()
            # P1-2 修复：以已持久化事件为基准恢复内存日志——否则重启后
            # seq 从 0 起步与 DB 冲突，INSERT OR IGNORE 静默丢弃新事件
            # （审计完整性静默失真；LRU 逐出后回归同理会复现）。
            try:
                self._events = SessionLog.load(session_id, conn=self._conn)
                if self._events:
                    logger.debug(
                        "session log %s restored %d events (seq 0..%d)",
                        session_id, len(self._events), self._events[-1].seq,
                    )
            except Exception:
                logger.warning("failed to restore session log state", exc_info=True)

    # ── 公共 API ────────────────────────────────────────────────────────

    def append(
        self,
        type_name: str,
        data: dict[str, Any],
        *,
        surface_op: str | None = None,
        source_seqs: list[int] | None = None,
    ) -> SessionEvent:
        """追加一个事件。成功即日志已提交；失败抛 EventLogError（不入日志）。"""
        if type_name not in EVENT_TYPES:
            raise EventLogError(f"unknown session event type: {type_name!r}")
        if type_name in SURFACE_TYPES and surface_op is None:
            raise EventLogError(
                f'session event "{type_name}" must declare surface_op (append/revision/replacement)'
            )
        if type_name not in SURFACE_TYPES and surface_op is not None:
            raise EventLogError(
                f'session event "{type_name}" is not a surface event but got surface_op'
            )
        snapshot = _snapshot_json(data, type_name)
        frozen_sources = _freeze(source_seqs)
        with self._lock:
            event = SessionEvent(
                type=type_name,
                seq=len(self._events),
                time_ms=int(time.time() * 1000),
                data=snapshot,
                surface_op=surface_op,
                source_seqs=frozen_sources,
            )
            self._events.append(event)
            if self._persist and self._conn is not None:
                try:
                    self._persist_event(event)
                except Exception:
                    # 持久化失败不吞日志：事件已进内存投影，回放仍可得，
                    # 但必须可见（同 db.py extras 失败要可见的原则）
                    logger.error(
                        "session event %s seq %s failed to persist", type_name, event.seq,
                        exc_info=True,
                    )
            return copy.deepcopy(event)  # 冻结快照，调用方拿到的是不可联动的副本

    @classmethod
    def load(cls, session_id: str, *, conn: sqlite3.Connection | None = None) -> list[SessionEvent]:
        """从 SQLite 回放一个会话的事件（按 seq 升序）。无持久化时返回空。"""
        if conn is None:
            conn = _default_conn()
        if conn is None:
            return []
        _ensure_table_for(conn)  # 只读路径也要容忍"表还没建"（重启后先读后写是合法序）
        rows = conn.execute(
            "SELECT seq, type, time_ms, data, surface_op, source_seqs "
            "FROM session_events WHERE session_id=? ORDER BY seq ASC",
            (session_id,),
        ).fetchall()
        return [
            SessionEvent(
                type=row[1],
                seq=int(row[0]),
                time_ms=int(row[2]),
                data=json.loads(row[3]),
                surface_op=row[4],
                source_seqs=tuple(json.loads(row[5])) if row[5] else (),
            )
            for row in rows
        ]

    # ── 内部 ────────────────────────────────────────────────────────────

    def _ensure_table(self) -> None:
        assert self._conn is not None
        _ensure_table_for(self._conn)

    def _persist_event(self, event: SessionEvent) -> None:
        assert self._conn is not None
        self._conn.execute(
            "INSERT OR IGNORE INTO session_events "
            "(session_id, seq, type, time_ms, data, surface_op, source_seqs) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                self.session_id,
                event.seq,
                event.type,
                event.time_ms,
                json.dumps(event.data, ensure_ascii=False),
                event.surface_op,
                json.dumps(event.source_seqs) if event.source_seqs else None,
            ),
        )
        self._conn.commit()


def _ensure_table_for(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS session_events ("
        "session_id TEXT NOT NULL, "
        "seq INTEGER NOT NULL, "
        "type TEXT NOT NULL, "
        "time_ms INTEGER NOT NULL, "
        "data TEXT NOT NULL, "
        "surface_op TEXT, "
        "source_seqs TEXT, "
        "UNIQUE(session_id, seq))"
    )
    conn.commit()


def _persist_enabled() -> bool:
    """LATIAO_EVENT_LOG=1 才持久化（阶段 1 灰度开关，回退即关掉该环境变量）。"""
    return os.environ.get("LATIAO_EVENT_LOG", "").strip() in ("1", "true", "yes")


def _default_conn() -> sqlite3.Connection | None:
    try:
        from db import _get_db
        return _get_db()
    except Exception:
        logger.warning("session log: default db unavailable", exc_info=True)
        return None
