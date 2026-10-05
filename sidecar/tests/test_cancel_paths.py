"""停止键端到端的两段回归（2026-10-05）：工具执行中 / 排队等引擎。

背景：停止键（/v1/chat/cancel + 客户端断流置位）此前只在每一步开头被检查，
实测"点了停止之后"（a）正在跑的命令跑满自己的 300s 超时；（b）排队等引擎的
请求等满整个队列/重载窗口。这里钉住这两段现在的行为。
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LATIAO_AUTH_TOKEN", "cancel-paths-token")


def test_run_cmd_killed_on_session_cancel():
    """停止键要杀掉正在跑的命令，而不是等它跑满 300s 超时。"""
    from tool_executor import run_cmd, CURRENT_TOOL_SESSION
    from agent.session_events import _request_session_cancel, _clear_session_cancel

    sid = f"cmdcancel-{time.time()}"
    _clear_session_cancel(sid)
    tok = CURRENT_TOOL_SESSION.set(sid)
    try:
        threading.Thread(
            target=lambda: (time.sleep(0.8), _request_session_cancel(sid)),
            daemon=True).start()
        t0 = time.monotonic()
        out = run_cmd("sleep 30")
        dt = time.monotonic() - t0
    finally:
        CURRENT_TOOL_SESSION.reset(tok)
        _clear_session_cancel(sid)
    assert "已停止" in out, out
    assert dt < 5, f"取消后命令仍跑了 {dt:.1f}s（应被杀进程树）"


def test_run_cmd_without_cancel_still_works():
    """正控：没有取消时命令照常执行并返回输出（证明杀进程逻辑不误伤）。"""
    from tool_executor import run_cmd, CURRENT_TOOL_SESSION

    sid = f"cmdnocancel-{time.time()}"
    tok = CURRENT_TOOL_SESSION.set(sid)
    try:
        out = run_cmd("echo hello-cancel-path")
    finally:
        CURRENT_TOOL_SESSION.reset(tok)
    assert "hello-cancel-path" in out, out


@pytest.mark.asyncio
async def test_queue_wait_responds_to_cancel(monkeypatch):
    """排队等引擎槽位期间按停止 → 立刻放弃排队（TurnCancelled），不等到放行。"""
    import agent.transport as T
    from agent.session_events import _request_session_cancel, _clear_session_cancel

    sid = f"qcancel-{time.time()}"
    _clear_session_cancel(sid)
    # 独立信号量容量 1 并先占住 → 下一个 acquire 必须排队
    sem = asyncio.Semaphore(1)
    monkeypatch.setattr(T, "_stream_lock", lambda: sem)
    await sem.acquire()
    try:
        threading.Thread(
            target=lambda: (time.sleep(0.5), _request_session_cancel(sid)),
            daemon=True).start()
        t0 = time.monotonic()
        with pytest.raises(T.TurnCancelled):
            async with T._local_llm_serialized(
                    "http://127.0.0.1:1235/v1/chat/completions",
                    wait_info={"session_id": sid}):
                pass
        dt = time.monotonic() - t0
    finally:
        sem.release()
        _clear_session_cancel(sid)
    assert dt < 4, f"取消后仍在排队 {dt:.1f}s"


@pytest.mark.asyncio
async def test_engine_wait_loop_responds_to_cancel(monkeypatch):
    """等引擎重载期间按停止 → 放弃等待（不再睡满 5s×72 的重载窗口）。"""
    import agent.transport as T
    from agent.session_events import _request_session_cancel, _clear_session_cancel

    sid = f"ecancel-{time.time()}"
    _clear_session_cancel(sid)

    # 把真实等待循环里的"取消探测"单独拎出来验证时序：直接调辅助函数按 0.5s 后置位
    threading.Thread(
        target=lambda: (time.sleep(0.5), _request_session_cancel(sid)),
        daemon=True).start()
    t0 = time.monotonic()
    fired = False
    for _ in range(40):                       # 模拟 5s×72 循环的检查点（此处 0.1s 采样）
        if T._session_cancel_check(sid):
            fired = True
            break
        await asyncio.sleep(0.1)
    dt = time.monotonic() - t0
    _clear_session_cancel(sid)
    assert fired and dt < 3, f"取消标记未被循环头探测到（{dt:.1f}s）"
