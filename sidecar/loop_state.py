"""Agent 循环相位状态机——阶段 2 地基（移植 DeepSeek Harness ReactLoopAgent 的 phase 模型）。

来源：dsh `ReactLoopAgent`（packages/core/agent-loop/src/agent.ts:38-46）——
显式 phase 状态机（idle/maintenance/running）+ 每活动一个 AbortController。
Latiao 现状（agent_loop.py:486）：`_session_cancelled: set[str]` 只有布尔标记，
没有"停止中"相位，也没有"每个 turn 一个可取消令牌"的语义——两个巨型循环
在 4 个检查点轮询布尔值。本模块补齐相位层（不替换旧标记，两套并存、语义一致，
待循环合并时旧标记即可拆除）：

- Phase 语义：
    idle     —— 无活动 turn；任何 begin_turn 成功
    running  —— 一个 turn 在跑（begin_turn 可重入调用 → StopRequested）
    stopping —— 停止已请求；在途工具允许 settle（结束原因由 end_turn 结算）
- TurnToken：一个 turn 的专用取消令牌；request_stop 幂等；check() 触发
  StopRequested 异常（循环的检查点用它替代裸布尔判断）。
- 不变式：同一会话同一时刻至多一个 active token（同 dsh runMaintenance
  "already has active work" 语义）；end_turn 幂等。

设计取舍（相对 dsh 的差异）：
- dsh 用 AbortController（Promise 取消语义）；Latiao 的循环是轮询式
  asyncio 生成器，用"标志 + 检查点"更贴合现状，令牌接口为后续
  asyncio.Event 化预留（turn_token.stop_event 换成 Event 即可）。
- stopping 相位不阻塞 begin_turn：用户停止后立即重发是现有产品行为
  （`_clear_session_cancel` 清标记），状态机同样允许（end_turn 后重开）。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Phase(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    STOPPING = "stopping"


class StopRequested(Exception):
    """检查点发现停止请求时抛出（循环在每轮迭代/工具执行前 check()）。"""

    def __init__(self, reason: str = "user", cause: str | None = None):
        self.reason = reason
        self.cause = cause
        super().__init__(f"turn stopped ({reason})")


@dataclass
class TurnToken:
    """一个 turn 的取消令牌。

    request_stop 幂等：第二次调用保留第一次的 reason（先行原因优先）。
    settle 幂等：只结算一次，重入返回 False。
    """

    turn: int
    started_at: float = field(default_factory=time.time)
    reason: str | None = None
    cause: str | None = None
    settled: bool = False

    def request_stop(self, reason: str = "user", cause: str | None = None) -> None:
        """置位停止。幂等：第二次调用不覆盖第一次的原因，但可补全 cause。

        cause 补充是刻意设计：先行原因（如 engine_error）优先，后续
        人工停止只用于补全诊断上下文，不改变原因归属。
        """
        if self.reason is None:
            self.reason = reason
            self.cause = cause
        elif self.cause is None and cause is not None:
            self.cause = cause

    @property
    def stop_requested(self) -> bool:
        return self.reason is not None

    def check(self) -> None:
        """检查点：停止已请求则抛 StopRequested（含 reason）。"""
        if self.reason is not None:
            raise StopRequested(self.reason, self.cause)

    def settle(self) -> bool:
        if self.settled:
            return False
        self.settled = True
        return True


@dataclass
class TurnState:
    """会话级相位状态机（一个会话一个实例）。

    线程安全（sync 检查点来自 asyncio 任务但可能多任务并发）；所有
    突变经锁，读路径（phase/active）无锁快照。
    """

    session_id: str
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, init=False)
    _phase: Phase = Phase.IDLE
    _active: Optional[TurnToken] = None
    _turn_seq: int = 0
    _end_reason: str | None = None   # 最近一次 end_turn 的原因（观测用）

    # ── 读路径（无锁，单次读原子）────────────────────────────────────

    @property
    def phase(self) -> Phase:
        return self._phase

    @property
    def active(self) -> Optional[TurnToken]:
        return self._active

    @property
    def end_reason(self) -> str | None:
        return self._end_reason

    def stop_requested(self) -> bool:
        active = self._active
        return active is not None and active.stop_requested

    # ── 突变路径 ──────────────────────────────────────────────────────

    def begin_turn(self) -> TurnToken:
        """开新 turn。已在运行/停止中则抛 StopRequested（同 dsh reentry 语义）。

        注意：stopping 相位下 begin_turn 会失败——用户必须先点"停止"完成
        结算（end_turn）再重发；这与现有 `_clear_session_cancel` 的
        "清除标记即可重发"语义不同，本模块刻意更严格：停止请求的内存令牌
        不保留到下一 turn（dsh wakeAfterAbort 的语义是"下个 turn 重放"，
        Latiao 的现有产品语义是"重发新消息"，不重放停止）。
        """
        with self._lock:
            if self._phase is not Phase.IDLE:
                active = self._active
                reason = active.reason if (active is not None and active.reason is not None) else "busy"
                raise StopRequested(reason, "reentry")
            self._turn_seq += 1
            token = TurnToken(turn=self._turn_seq)
            self._active = token
            self._phase = Phase.RUNNING
            return token

    def request_stop(self, reason: str = "user", cause: str | None = None) -> bool:
        """请求停止。幂等；无活动 turn 时为 no-op 返回 False。

        返回值表示"是否实际置位"（第一次置位返回 True，重复调用 False）
        ——调用方（取消注册表）借此区分"停止了一个正在跑的 turn"与
        "停止了个空气"/"重复停止"。
        """
        with self._lock:
            active = self._active
            if active is None:
                return False
            changed = active.reason is None
            active.request_stop(reason, cause)
            if self._phase is Phase.RUNNING:
                self._phase = Phase.STOPPING
            return changed

    def _record_end(self, active: TurnToken, reason: str) -> None:
        active.settle()
        self._end_reason = reason
        self._active = None
        self._phase = Phase.IDLE

    def end_turn(self, reason: str = "completed") -> TurnToken | None:
        """结算当前 turn（幂等）。返回被结算的令牌（无活动返回 None）。

        reason 由调用方给出（completed/aborted/error）；已被 request_stop
        的 turn 若调用方传 completed，这里强制改为 aborted（停止是
        最终语义，与事件日志 turn/end 的判定同源）。
        """
        with self._lock:
            active = self._active
            if active is None:
                return None
            if active.stop_requested and reason == "completed":
                reason = "aborted"
            self._record_end(active, reason)
            return active

    def abandon(self) -> TurnToken | None:
        """强制回到 IDLE（放弃语义：新请求覆盖旧请求/客户端断连）。

        与 end_turn 的差别：不走 completed→aborted 修正——放弃是外部
        事件，不构成"用户停止"证据；产品语义上"重发新消息"必须总是可用。
        """
        with self._lock:
            active = self._active
            if active is None:
                return None
            self._record_end(active, "abandoned")
            return active


def assert_state_consistent(state: TurnState) -> bool:
    """不变量自检（测试用）：idle 必须无 active；有 active 必非 idle。"""
    if state._phase is Phase.IDLE:
        return state._active is None
    return state._active is not None


# ── 会话级实例缓存（同 _event_log_for 的 LRU 模式：状态机稳定、按需恢复）──
_STATES: dict[str, TurnState] = {}
_STATES_LOCK = threading.Lock()
_STATES_MAX = 32


def turn_state_for(session_id: str) -> TurnState:
    """按会话取（或建）TurnState。缓存有界，避免长运行 sidecar 泄漏。"""
    with _STATES_LOCK:
        state = _STATES.get(session_id)
        if state is None:
            state = TurnState(session_id)
            if len(_STATES) >= _STATES_MAX:
                _STATES.pop(next(iter(_STATES)))
            _STATES[session_id] = state
        return state
