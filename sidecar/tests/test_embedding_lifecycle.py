"""嵌入服务生命周期（2026-10-19 P0）：退出 / 看门狗 / 启动回收 / 空闲循环。

真机事故：sidecar 10-18 12:09 正常关闭后，8897 上仍有一个嵌入服务（PPID=1、
已跑 1 天 9 小时、669MB）——退出路径只停了引擎和 TTS，从没停过它；唯一的空闲
回收挂在 24 小时一轮的 prune 循环里，而启动那轮又因 _last_use=0 恒不触发。

约定（改了要同步这里）：
  · 正常退出、孤儿看门狗、启动回收 三处都要调 embedding_service.stop()；
  · 空闲回收是独立的 5 分钟循环（不再挂在 24h 的 prune 循环里）；
  · 端口回收只杀身份匹配的进程（2026-10-11 误杀教训）。
"""
from __future__ import annotations

import signal
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import embedding_service as emb  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(emb, "_proc", None)


def _main_src() -> str:
    return (ROOT / "main.py").read_text("utf-8")


# ── 接线 tripwire ───────────────────────────────────────────────
def test_shutdown_and_watchdog_stop_embedding():
    src = _main_src()
    # 正常退出段（TTS 之后）
    idx = src.find("Sidecar 关闭 — 停止本机语音服务失败")
    assert idx > 0
    assert "_stop_embed" in src[idx:idx + 900], "正常退出没停嵌入服务"
    # 孤儿看门狗段（TTS 之后）
    wd = src.find("退出清理时停止语音服务失败")
    assert wd > 0
    assert "_stop_embed" in src[wd:wd + 700], "孤儿看门狗没停嵌入服务"
    # 启动回收：import 之后立刻 stop()，且要早于 warmup
    boot = src.find("import embedding_service as _emb")
    assert boot > 0
    seg = src[boot:boot + 900]
    assert "_emb.stop()" in seg, "启动没回收上次遗留的嵌入服务"
    assert seg.find("_emb.stop()") < seg.find("available()")


def test_idle_reclaim_is_its_own_loop():
    src = _main_src()
    assert "_embed_idle_loop" in src
    assert "asyncio.sleep(300)" in src
    # 唯一**调用点**在独立循环里（注释里提到名字不算）：24h prune 循环不再顺带检查
    assert src.count("run_in_threadpool(_emb.maybe_stop_idle)") == 1


# ── 端口回收的身份判定（行为）──────────────────────────────────
def _stop_with_ps(monkeypatch, ps_out: str):
    killed = []

    def fake_run(cmd, **kw):
        if cmd[0] == "lsof":
            return subprocess.CompletedProcess(cmd, 0, "424242\n", "")
        if cmd[0] == "ps":
            return subprocess.CompletedProcess(cmd, 0, ps_out, "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(emb.subprocess, "run", fake_run)
    monkeypatch.setattr(emb.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    emb.stop()
    return killed


def test_port_reap_kills_recognized_embedding_service(monkeypatch):
    killed = _stop_with_ps(monkeypatch, "llama-server --embeddings -m Qwen3-Embed.gguf")
    assert killed == [(424242, signal.SIGTERM)]


def test_port_reap_skips_unrelated_listener(monkeypatch):
    killed = _stop_with_ps(monkeypatch, "/usr/local/bin/redis-server *:8897")
    assert killed == []


def test_stop_is_quiet_when_nothing_to_stop(monkeypatch, caplog):
    monkeypatch.setattr(emb.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", ""))
    with caplog.at_level("INFO", logger="latiao-sidecar"):
        emb.stop()
    assert "嵌入服务已停止" not in caplog.text


def test_start_records_last_use_for_background_instance(monkeypatch):
    """后台冷启动（只起服务、没再检索）的实例也要记 _last_use（2026-10-19 跟进）。

    否则 maybe_stop_idle 看到 `_last_use <= 0` 永远不认领它——"空闲 15 分钟停"
    对这个最常见的路径依然不生效。
    """
    monkeypatch.setattr(emb, "_last_use", 0.0)
    monkeypatch.setattr(emb, "is_up", lambda *a, **k: True)
    assert emb.start() is True
    assert emb._last_use > 0
