"""本地引擎闸门（agent/transport.py）：容量 = 引擎并发槽位。

09-23 起：闸门由"全局互斥锁（容量恒 1）"改为"容量 = 引擎实际槽位（--parallel N）"，
两个会话才可能真并行；超出容量的请求 FIFO 排队且等待有上限（不再无限挂起）。
"""
import asyncio
import types

import pytest

import local_llm
from agent import transport

LOCAL = "http://127.0.0.1:1234/v1/chat/completions"
CLOUD = "https://api.example.com/v1/chat/completions"


@pytest.fixture(autouse=True)
def _restore_engine():
    """本文件会把 local_llm._engine 换成桩引擎：跑完必须还原。

    不还原会污染后续测试文件（全量跑时 test_thin_loop 的 AttributeError
    'SimpleNamespace' object has no attribute 'mark_engine_busy' 就是这么来的）。
    """
    old = getattr(local_llm, "_engine", None)
    yield
    local_llm._engine = old


def _engine(slots: int):
    return types.SimpleNamespace(_launched_slots=slots, server_port=1234)


def test_slots_2_allows_concurrent():
    """容量 2：两个请求同时在闸门内（第二个不必等第一个释放）。"""
    async def _main():
        local_llm._engine = _engine(2)
        entered = []

        async def _hold(tag, hold_s=0.15):
            async with transport._local_llm_serialized(LOCAL):
                entered.append(tag)
                await asyncio.sleep(hold_s)

        await asyncio.gather(_hold("a", 0.3), _hold("b", 0.05))
        return entered

    assert asyncio.run(_main()) == ["a", "b"]


def test_slots_1_serializes():
    """容量 1（python 引擎/自定义引擎）：仍然串行——第二个必须等第一个退出。"""
    async def _main():
        local_llm._engine = _engine(1)
        order = []

        async def _hold(tag, hold_s):
            async with transport._local_llm_serialized(LOCAL):
                order.append(("in", tag))
                await asyncio.sleep(hold_s)
                order.append(("out", tag))

        await asyncio.gather(_hold("a", 0.2), _hold("b", 0.0))
        return order

    assert asyncio.run(_main()) == [("in", "a"), ("out", "a"), ("in", "b"), ("out", "b")]


def test_fifo_order():
    """排队按等待顺序放行（asyncio.Semaphore 语义），不是随机/后进先出。"""
    async def _main():
        local_llm._engine = _engine(1)
        entered = []

        async def _hold(tag):
            async with transport._local_llm_serialized(LOCAL):
                entered.append(tag)
                await asyncio.sleep(0.02)

        await asyncio.gather(_hold("a"), _hold("b"), _hold("c"))
        return entered

    assert asyncio.run(_main()) == ["a", "b", "c"]


def test_wait_info_and_snapshot():
    """wait_info 回填等待时长；快照能看到容量/在飞/排队。"""
    async def _main():
        local_llm._engine = _engine(1)
        info = {}
        seen = {}

        async def _holder():
            async with transport._local_llm_serialized(LOCAL):
                await asyncio.sleep(0.25)

        async def _waiter():
            await asyncio.sleep(0.05)          # 确保排在 _holder 后面
            async with transport._local_llm_serialized(LOCAL, wait_info=info):
                seen.update(transport.stream_gate_snapshot())

        await asyncio.gather(_holder(), _waiter())
        return info, seen

    info, seen = asyncio.run(_main())
    assert info["waited"] >= 0.15              # 确实等过
    assert seen["slots"] == 1 and seen["in_flight"] == 1


def test_queue_timeout_raises_and_leaves_clean_state(monkeypatch):
    """超过等待上限 → EngineQueueTimeout，且排队计数不残留。"""
    monkeypatch.setattr(transport, "_queue_wait_max", lambda: 0.15)

    async def _main():
        local_llm._engine = _engine(1)
        raised = None

        async def _holder():
            async with transport._local_llm_serialized(LOCAL):
                await asyncio.sleep(0.5)

        async def _waiter():
            nonlocal raised
            await asyncio.sleep(0.02)
            try:
                async with transport._local_llm_serialized(LOCAL, wait_info={}):
                    pass
            except transport.EngineQueueTimeout as e:
                raised = e

        await asyncio.gather(_holder(), _waiter())
        # 放行 _holder 退出后才看快照：排队计数必须归零
        await asyncio.sleep(0)
        return raised, transport.stream_gate_snapshot()

    raised, snap = asyncio.run(_main())
    assert isinstance(raised, transport.EngineQueueTimeout)
    assert "900" in str(raised) or "秒" in str(raised)   # 错误文案给用户看得懂
    assert snap["waiting"] == 0 and snap["in_flight"] == 0


def test_capacity_follows_engine_slots():
    """容量跟着引擎实际槽位走（切模型后容量随之变化）。"""
    async def _main():
        entered = []
        local_llm._engine = _engine(3)

        async def _hold(tag):
            async with transport._local_llm_serialized(LOCAL):
                entered.append(tag)
                await asyncio.sleep(0.15)

        await asyncio.gather(*[_hold(t) for t in "abc"])
        snap_after = transport.stream_gate_snapshot()
        return entered, snap_after

    entered, snap = asyncio.run(_main())
    assert sorted(entered) == ["a", "b", "c"]      # 3 个槽 → 三个都不用等
    assert snap["slots"] == 3


def test_cloud_not_gated():
    """云端地址完全不进闸门（不受本地槽位限制）。"""
    async def _main():
        local_llm._engine = _engine(1)
        entered = []

        async def _hold(tag):
            async with transport._local_llm_serialized(CLOUD):
                entered.append(tag)
                await asyncio.sleep(0.15)

        await asyncio.gather(_hold("a"), _hold("b"))
        return entered

    assert sorted(asyncio.run(_main())) == ["a", "b"]


def test_engine_slots_reads_launched_value(monkeypatch):
    """_engine_slots 取引擎**实际**启动槽位；取不到时保守退化 1。"""
    local_llm._engine = _engine(2)
    assert transport._engine_slots() == 2
    local_llm._engine = types.SimpleNamespace()          # 老引擎对象没有该属性
    assert transport._engine_slots() == 1
    local_llm._engine = types.SimpleNamespace(_launched_slots=0)
    assert transport._engine_slots() == 1


def test_serialized_context_releases_on_exception():
    """闸门内抛异常也必须释放（否则槽位永久泄漏）。"""
    async def _main():
        local_llm._engine = _engine(1)

        async def _boom():
            async with transport._local_llm_serialized(LOCAL):
                raise ValueError("boom")

        with pytest.raises(ValueError):
            await _boom()
        async with transport._local_llm_serialized(LOCAL):   # 还能立刻拿到槽位
            ok = True
        return ok

    assert asyncio.run(_main()) is True
