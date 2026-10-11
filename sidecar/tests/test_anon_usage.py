"""匿名使用数据（anon_usage）的验收测试——"真开关"与"假开关"的分界线。

钉住的口径（2026-10-11 立项时与用户确认）：
- 开关是**总闸**：关掉时 record_event/note 不落任何计数、send_if_due 不发送；
- **只收计数**：未登记的 key（尤其自由文本）一律丢弃；
- 安装 ID 随机生成、跨进程稳定；本地文件是 0600；
- **没配上报地址时永不外发**（默认状态），配了才发，失败静默保留；
- 开关状态落在 config.json privacy.anonymous_usage（前端开关的权威源）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LATIAO_AUTH_TOKEN", "anon-token")


@pytest.fixture()
def anon(tmp_path, monkeypatch):
    """每个用例一个干净的本机状态：临时 PROGRESS_DIR + 清模块缓存。

    纪律（2026-10-11 事故的教训）：reload 必须发生在环境变量**指向临时目录**时。
    这个 fixture 曾在 teardown 里 delenv 后再 reload，把 config.CONFIG_FILE 留在
    真机路径上——之后 test_app_flow 的假 cloud_config 经"兜底持久化"覆盖了用户
    真配置。现在 teardown 先恢复到会话级 TEST_PROGRESS_DIR 再 reload，模块常量
    始终指向临时区；conftest 的 _guard_real_config 兜底。
    """
    session_dir = os.environ.get("LATIAO_TEST_PROGRESS_DIR") or tempfile.mkdtemp(
        prefix="latiao-anon-fallback-")
    monkeypatch.setenv("LATIAO_TEST_PROGRESS_DIR", str(tmp_path))
    monkeypatch.delenv("LATIAO_ANON_USAGE", raising=False)
    monkeypatch.delenv("LATIAO_ANON_ENDPOINT", raising=False)
    import importlib

    import config as config_mod
    importlib.reload(config_mod)
    import anon_usage as anon_mod
    importlib.reload(anon_mod)
    yield anon_mod
    # 收口：先恢复会话级临时目录，再 reload —— 模块常量绝不落到真机路径
    monkeypatch.setenv("LATIAO_TEST_PROGRESS_DIR", session_dir)
    importlib.reload(config_mod)
    importlib.reload(anon_mod)


def _write_cfg(tmp_path: Path, privacy: dict) -> None:
    (tmp_path / "config.json").write_text(
        json.dumps({"privacy": privacy}, ensure_ascii=False), encoding="utf-8")


def _data(anon) -> dict:
    return json.loads(anon.ANON_FILE.read_text(encoding="utf-8"))


def test_disabled_shortcircuits_collect_and_send(anon, tmp_path):
    """开关关 = 总闸：不落计数、不发送。"""
    _write_cfg(tmp_path, {"anonymous_usage": False})
    assert anon.tracking_enabled() is False
    anon.record_event("turns")
    anon.note("tool_calls")
    assert not anon.ANON_FILE.exists()
    assert anon.send_if_due(force=True) == {"sent": False, "reason": "disabled"}
    st = anon.status()
    assert st["enabled"] is False and st["events_total"] == 0


def test_env_override_beats_config(anon, tmp_path):
    _write_cfg(tmp_path, {"anonymous_usage": True})
    os.environ["LATIAO_ANON_USAGE"] = "0"
    try:
        assert anon.tracking_enabled() is False
    finally:
        os.environ.pop("LATIAO_ANON_USAGE")


def test_default_is_enabled(anon):
    """无 config 无环境变量 → 默认开（与前端 localStorage 默认同一口径）。"""
    assert anon.tracking_enabled() is True


def test_counts_and_whitelist(anon):
    """已登记 key 计数递增；未登记 key（自由文本）被丢弃。"""
    anon.note("turns")
    anon.note("turns")
    anon.note("tool_calls", 3)
    anon.note("随便一段聊天内容不该被记录")
    anon.record_event("gate_idle_nudge")
    data = _data(anon)
    day = next(iter(data["days"].values()))
    assert day == {"turns": 2, "tool_calls": 3, "gate_idle_nudge": 1}
    assert anon.status()["events_total"] == 6


def test_unregistered_keys_never_reach_payload(anon):
    """上报载荷里只有登记过的计数键——不给"顺手塞内容"留口子。"""
    anon.note("turns")
    anon.note("secret-text-12345")
    payload = anon._payload()
    assert set(payload["days"][next(iter(payload["days"]))].keys()) == {"turns"}
    assert "secret-text-12345" not in json.dumps(payload, ensure_ascii=False)


def test_install_id_random_and_stable(anon):
    first = anon.install_id()
    assert first.startswith("latiao_") and len(first) == len("latiao_") + 32
    assert anon.install_id() == first          # 第二次读同一值
    reloaded = json.loads(anon.ANON_FILE.read_text(encoding="utf-8"))
    assert reloaded["install_id"] == first     # 已落盘


def test_local_file_is_0600(anon):
    anon.note("turns")
    assert (anon.ANON_FILE.stat().st_mode & 0o777) == 0o600


def test_no_endpoint_never_sends(anon):
    """默认（没配地址）永不外发——这是承诺，不是行为细节。"""
    anon.note("turns")
    assert anon.endpoint() == ""
    assert anon.send_if_due(force=True) == {"sent": False, "reason": "no_endpoint"}


def test_endpoint_from_config_and_send_due_window(anon, tmp_path, monkeypatch):
    _write_cfg(tmp_path, {"anonymous_usage": True, "anon_endpoint": "https://example.invalid/an"})
    anon.note("turns")
    sent: list = []

    class _Resp:
        status_code = 200

    class _Client:
        def __init__(self, *a, **k): ...
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, json=None):
            sent.append((url, json))
            return _Resp()

    import httpx as httpx_mod
    monkeypatch.setattr(httpx_mod, "Client", _Client)
    r1 = anon.send_if_due(force=True)
    assert r1 == {"sent": True, "reason": "ok"}
    url, payload = sent[0]
    assert url == "https://example.invalid/an"
    assert payload["install_id"].startswith("latiao_")
    assert "app_version" in payload and "platform" in payload
    # 24h 窗口：刚发过 → 不再发
    assert anon.send_if_due() == {"sent": False, "reason": "not_due"}


def test_send_failure_keeps_data(anon, tmp_path, monkeypatch):
    """发送失败静默保留：数据还在、状态不记 last_sent。"""
    _write_cfg(tmp_path, {"anonymous_usage": True, "anon_endpoint": "https://example.invalid/an"})
    anon.note("turns")

    def _boom(*a, **k):
        raise RuntimeError("network down")

    import httpx as httpx_mod
    monkeypatch.setattr(httpx_mod, "Client", _boom)
    assert anon.send_if_due(force=True) == {"sent": False, "reason": "error"}
    assert anon.status()["events_total"] == 1
    assert anon.status()["last_sent"] == ""


def test_set_enabled_writes_config_gate(anon, tmp_path):
    """设置页写开关 → config.json privacy.anonymous_usage 即刻是权威值。"""
    anon.set_enabled(False)
    cfg = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert cfg["privacy"]["anonymous_usage"] is False
    assert anon.tracking_enabled() is False
    anon.set_enabled(True)
    cfg = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert cfg["privacy"]["anonymous_usage"] is True
    assert anon.tracking_enabled() is True


def test_reset_clears_local_data(anon):
    anon.note("turns")
    assert anon.status()["events_total"] == 1
    anon.reset()
    assert anon.status()["events_total"] == 0
    assert anon.status()["install_id"] == ""


def test_prune_keeps_400_days(anon):
    with anon._lock:
        data = anon._load()
        data["days"] = {f"2020-{m:02d}-{d:02d}": {"turns": 1} for m in range(1, 13) for d in range(1, 29)}
        anon._prune(data["days"])          # 366 → 不裁剪
        assert len(data["days"]) == 336
        for i in range(100):
            data["days"][f"2019-01-{(i % 28) + 1:02d}-{i:03d}"] = {"turns": 1}
        anon._prune(data["days"])
        assert len(data["days"]) == 400


def test_note_never_raises(anon, monkeypatch):
    """记账路径绝不能因故障炸业务：note 吞掉一切异常。"""
    def _boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(anon, "record_event", _boom)
    anon.note("turns")  # 不抛即通过
