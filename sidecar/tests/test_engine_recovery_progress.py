"""引擎恢复期的进度上报（2026-09-24 用户实测"⏱ 流式响应超时"的回归守卫）。

事故链：引擎中途死亡 → transport 进入"自动重载 + 5s 重试"循环 → 这段**静默期**
后端既不向前端发数据，自己的停滞检测（首 token 前 90s / 有输出后 180s）还会把
恢复掐掉。修法三件：① 等引擎期间不累计停滞；② 心跳转成前端可见的 engine_recovering
事件（喂住前端 180s 看门狗）；③ 数据恢复时补一个 engine_recovered 收尾。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LATIAO_AUTH_TOKEN", "engine-recover-token")

_LOOP_SRC = Path(__file__).resolve().parents[1] / "agent" / "loop.py"
_TRANSPORT_SRC = Path(__file__).resolve().parents[1] / "agent" / "transport.py"
_LAUNCH_SRC = Path(__file__).resolve().parents[1] / "local_llm_launch_native.py"


def test_wait_phase_set_and_clear():
    from agent.transport import _set_wait_phase
    wi: dict = {}
    _set_wait_phase(wi, "queue")
    assert wi["phase"] == "queue"
    _set_wait_phase(wi, "reload")
    assert wi["phase"] == "reload"
    _set_wait_phase(wi, None)
    assert "phase" not in wi
    # 非 dict（没传 wait_info）不能炸
    _set_wait_phase(None, "queue")
    _set_wait_phase(None, None)


def test_transport_marks_phases_and_logs_engine_death():
    src = _TRANSPORT_SRC.read_text("utf-8")
    # 排队等槽位要标 queue；等自动重载要标 reload；两条路径都要能清
    assert '_set_wait_phase(wait_info, "queue")' in src
    assert src.count('_set_wait_phase(wait_info, "reload")') == 2, "两条重试路径都要标 reload"
    assert "_set_wait_phase(wait_info, None)" in src
    # 流中断要记录引擎死因（退出码 + stderr 尾部）
    assert "_log_engine_death(" in src
    # 成功建立连接后必须清相位（否则正常慢生成会被误报成"等引擎"）
    assert "连接已建立：等待结束" in src


def test_stall_detection_skipped_while_waiting_engine():
    src = _LOOP_SRC.read_text("utf-8")
    # 等引擎期间只发心跳、不累计停滞
    assert 'wait_info.get("phase")' in src, "停滞检测必须让位于'正在等引擎'"
    assert "等引擎期间只发心跳" in src or "不算" in src


def test_keepalive_becomes_frontend_event_and_recovered_marker():
    src = _LOOP_SRC.read_text("utf-8")
    assert '"event": "engine_recovering"' in src
    assert '"event": "engine_recovered"' in src
    # 关键：不能再把 keepalive 直接丢掉（事故根因）
    assert 'if "keepalive" in line[:30]:' in src


def test_engine_log_is_persisted_and_ready_logged():
    src = _LAUNCH_SRC.read_text("utf-8")
    # 引擎 stderr 落盘（此前只留内存 deque，成功后无人读 → 死因不可查）
    assert "_append_engine_log(line)" in src
    assert "engine_log_path()" in src
    # 暴露尾部给 transport
    assert "engine._stderr_tail = stderr_lines" in src
    # 加载成功要留痕（此前成功也一行不写，日志读起来像卡死）
    assert "引擎已就绪" in src


# ── 行为测试（不碰真引擎：用假 client / 假流驱动）─────────────────────

import asyncio  # noqa: E402
import pytest  # noqa: E402


def _bare_loop(**attrs):
    """不跑 __init__，只挂上被测代码真正会读的属性。"""
    import agent.loop as L
    lp = object.__new__(L.ThinAgentLoop)
    lp.is_local = True
    lp.api_url = "http://127.0.0.1:1235/v1/chat/completions"
    lp.headers = {}
    lp.session_id = "test-session"
    lp.user_lang = "zh"
    lp.steps = 0
    lp._lang_retried = False
    for k, v in attrs.items():
        setattr(lp, k, v)
    return lp


@pytest.mark.asyncio
async def test_stall_detection_yields_to_engine_wait(monkeypatch):
    """等引擎期间（phase 已标）静默**不**触发"输出停滞"，只发心跳。"""
    import agent.loop as L

    monkeypatch.setattr(L, "HEARTBEAT", 0.02)
    monkeypatch.setattr(L, "STALL_FIRST", 0.05)
    monkeypatch.setattr(L, "STALL_AFTER", 0.05)

    class _Resp:
        async def aiter_lines(self):
            await asyncio.sleep(0.4)
            yield 'data: {"content": "hi"}'

    class _Ctx:
        async def __aenter__(self):
            return _Resp()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(L, "_local_llm_stream", lambda *a, **kw: _Ctx())

    lp = _bare_loop()
    wait = {"phase": "reload"}
    seen = []
    async for line in lp._stream(None, {}, wait_info=wait):
        seen.append(line)
        if len(seen) >= 4:
            break
    assert all("keepalive" in ln for ln in seen), f"等引擎期间应全是心跳: {seen}"


@pytest.mark.asyncio
async def test_stall_detection_still_fires_without_engine_wait(monkeypatch):
    """没有等引擎的静默**仍然**按停滞阈值掐断（兜底不能被我改坏）。"""
    import agent.loop as L

    monkeypatch.setattr(L, "HEARTBEAT", 0.02)
    monkeypatch.setattr(L, "STALL_FIRST", 0.05)
    monkeypatch.setattr(L, "STALL_AFTER", 0.05)

    class _Resp:
        async def aiter_lines(self):
            await asyncio.sleep(0.5)
            yield 'data: {"content": "hi"}'

    class _Ctx:
        async def __aenter__(self):
            return _Resp()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(L, "_local_llm_stream", lambda *a, **kw: _Ctx())

    lp = _bare_loop()
    with pytest.raises(TimeoutError):
        async for _ in lp._stream(None, {}, wait_info={}):
            pass


@pytest.mark.asyncio
async def test_sample_forwards_keepalive_as_recovering_event(monkeypatch):
    """_sample：心跳 → engine_recovering（带相位/时长）；数据回来 → engine_recovered。"""
    import agent.loop as L

    async def _fake_stream(_client, _body, wait_info=None):
        yield ": keepalive\n\n"
        yield ": keepalive\n\n"
        yield 'data: {"content": "你好"}'

    lp = _bare_loop()
    monkeypatch.setattr(lp, "_stream", _fake_stream)
    monkeypatch.setattr(L.time, "monotonic", lambda: 1000.0)

    events = []
    async for evt in lp._sample(None, {}):
        if isinstance(evt, dict) and evt.get("event") in ("engine_recovering", "engine_recovered"):
            events.append(evt)
    kinds = [e["event"] for e in events]
    assert kinds.count("engine_recovering") == 2, kinds
    assert events[0]["phase"] == "silent"      # 无相位时的兜底标签
    assert "waited" in events[0]
    assert kinds[-1] == "engine_recovered", "数据回来必须收尾，别让相位挂在屏幕上"
