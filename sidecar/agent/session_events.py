"""会话事件与取消（agent_loop.py 拆出的第三块，2026-09-23）。

两块相关的"会话级簿记"：
- 事件日志（_EVENT_LOGS）：每会话一条环形日志，模型/前端排查"这轮到底发生了什么"；
- 会话取消（_request_session_cancel / _session_cancel_requested）：停止按钮的后端落点
  （前端 /v1/chat/cancel → 这里置位 → 主循环每轮/工具执行前检查）。

拆出来的理由：这两个都是"按会话键的全局状态 + 两个纯函数"，与枢纽的工具/上下文职责
无关；而取消语义是并行改造后最容易被误改的一处（子会话要跟着父会话取消）。
"""
import logging

import threading
from loop_state import turn_state_for
from session_log import SessionLog

logger = logging.getLogger("latiao-sidecar")   # 与 agent_loop 同名：日志格式不变

# 会话级取消注册表：POST /v1/chat/cancel 置位。两个循环在每轮迭代开头与
# 每次工具执行前检查——停止按钮此前只断前端流，服务端循环继续烧 GPU/
# 执行工具/扣云端费用（P0）。set 的 add/discard/contains 原子，无需锁。
_session_cancelled: set[str] = set()


def _clear_session_cancel(session_id: str) -> None:
    """新请求开始时清除标记（重发消息不应被上一次停止影响）。"""
    _session_cancelled.discard(session_id)
    # 上一次停止若因断连/异常未结算，新请求强制放弃旧 turn（产品语义：重发必须可用）
    try:
        turn_state_for(session_id).abandon()
    except Exception:
        logger.warning("failed to abandon turn state", exc_info=True)

def _event_log_for(session_id: str) -> SessionLog | None:
    # 依赖仍留在 agent_loop 的定义 → 函数内惰性导入（避免循环依赖）
    try:
        with _EVENT_LOGS_LOCK:
            log = _EVENT_LOGS.get(session_id)
            if log is None:
                log = SessionLog(session_id)
                if len(_EVENT_LOGS) >= _EVENT_LOGS_MAX:
                    _EVENT_LOGS.pop(next(iter(_EVENT_LOGS)))
                _EVENT_LOGS[session_id] = log
            return log
    except Exception:
        logger.warning("event log unavailable for %s", session_id, exc_info=True)
        return None

def _request_session_cancel(session_id: str) -> None:
    # 依赖仍留在 agent_loop 的定义 → 函数内惰性导入（避免循环依赖）
    """置位会话取消标记（/v1/chat/cancel 调用）。"""
    if session_id:
        _session_cancelled.add(session_id)
        # 相位状态机镜像（阶段 2a，与事件日志同源）：stopping 相位 + 先行原因
        try:
            turn_state_for(session_id).request_stop("user", "button")
        except Exception:
            logger.warning("failed to mirror cancel to turn state", exc_info=True)
        log = _event_log_for(session_id)
        if log is not None:
            try:
                log.append("cancel/request", {"cause": "user"})
            except Exception:
                logger.warning("failed to log cancel/request", exc_info=True)

def _session_cancel_requested(session_id: str) -> bool:
    """会话是否已被请求取消。

    子代理会话形如 "<父会话>:sub_xxx"——父被取消（用户点停止 / 客户端断流）时
    子代理必须同步骤内停，否则断连后仍会继续烧算力（09-13 事故）。
    """
    if not session_id:
        return False
    if session_id in _session_cancelled:
        return True
    root = session_id.split(":", 1)[0]
    return root != session_id and root in _session_cancelled

_EVENT_LOGS_LOCK = threading.Lock()

_EVENT_LOGS_MAX = 32

# 事件日志（阶段 1，灰度）：LATIAO_EVENT_LOG=1 时取消/回合边界事件写入
# session_events 表（sidecar/session_log.py，移植 dsh append 契约）。
# 有会话级取消事件时，重放/审计能还原"用户何时按过停止"——0.3.14 审计发现
# 停止按钮此前只断前端流，这类时序信息在旧日志里是彻底丢失的。
# 日志实例按会话缓存：seq 连续性契约要求同一会话共用同一实例（否则每个
# 新实例 seq 都从 0 开始，回放时"连续序号"语义失效）。缓存有界（LRU 32），
# 会话结束不清理——事件是审计事实，实例只是写入口。
_EVENT_LOGS: dict[str, SessionLog] = {}
