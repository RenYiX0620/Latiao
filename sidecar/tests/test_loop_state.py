"""loop_state 相位状态机测试（dsh phase 模型移植）。

覆盖：状态迁移、重入拒绝、停止幂等与原因先行、end_turn 幂等与
completed→aborted 修正、无活动 no-op、并发不变式。
"""
import threading

import pytest

from loop_state import Phase, StopRequested, TurnState, assert_state_consistent


def test_begin_turn_idle_to_running():
    s = TurnState("s")
    t = s.begin_turn()
    assert s.phase is Phase.RUNNING
    assert s.active is t
    assert t.turn == 1
    assert assert_state_consistent(s)


def test_begin_turn_reentry_rejected():
    s = TurnState("s")
    s.begin_turn()
    with pytest.raises(StopRequested) as exc:
        s.begin_turn()
    assert exc.value.reason == "busy"  # 无停止请求时重入 = busy（同 dsh 语义）


def test_request_stop_running_to_stopping():
    s = TurnState("s")
    token = s.begin_turn()
    assert s.request_stop() is True
    assert s.phase is Phase.STOPPING
    assert token.stop_requested
    token.request_stop("user", "button")
    assert token.reason == "user"  # 先行原因优先
    assert token.cause == "button"


def test_check_raises_with_reason():
    s = TurnState("s")
    token = s.begin_turn()
    token.request_stop("engine_error", "dead")
    with pytest.raises(StopRequested) as exc:
        token.check()
    assert exc.value.reason == "engine_error"


def test_stop_noop_without_active():
    s = TurnState("s")
    assert s.request_stop() is False
    assert s.phase is Phase.IDLE


def test_end_turn_settles_and_resets():
    s = TurnState("s")
    token = s.begin_turn()
    settled = s.end_turn("completed")
    assert settled is token
    assert s.phase is Phase.IDLE
    assert s.active is None
    assert s.end_reason == "completed"
    assert token.settled
    assert token.settle() is False  # 幂等
    assert s.end_turn() is None     # 二次结算 no-op


def test_end_turn_after_stop_forces_aborted():
    s = TurnState("s")
    s.begin_turn()
    s.request_stop()
    s.end_turn("completed")          # 调用方误传 completed
    assert s.end_reason == "aborted"  # 停止是最终语义


def test_begin_resumes_after_end():
    s = TurnState("s")
    s.begin_turn()
    s.end_turn("completed")
    t2 = s.begin_turn()
    assert t2.turn == 2
    assert assert_state_consistent(s)


def test_concurrent_request_stop_single_settlement():
    """多线程并发 request_stop 只有一个生效原因（稳定性，非数据竞态）。"""
    s = TurnState("s")
    s.begin_turn()
    results = []
    barrier = threading.Barrier(4)

    def _stop():
        barrier.wait()
        results.append(s.request_stop())

    threads = [threading.Thread(target=_stop) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1      # 只有一个置位成功（相位迁移被锁保护）
    assert s.stop_requested() is True
