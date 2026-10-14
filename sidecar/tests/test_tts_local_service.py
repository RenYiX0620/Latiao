"""tts_local_service 的行为测试：假 start.sh + 假监听服务，不碰真服务/真配置。

覆盖（改了要同步这里）：
  · 未配置 service_dir / 关掉开关 → 不管理（其他装机的默认路径）
  · start() → 端口可连、句柄在手；stop() → 端口释放、句柄清空
  · 幂等：已在跑再 start() 不再拉起第二个
  · 启动失败（脚本立刻退出）→ False、进冷却、不白等满超时
"""
import socket
import sys
import textwrap
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tts_local_service as svc  # noqa: E402


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    # env 覆盖优先于用例显式传入的配置——测试里一律先清掉（见 _conf 的说明）
    monkeypatch.delenv("LATIAO_TTS_SERVICE_DIR", raising=False)
    monkeypatch.setattr(svc, "_proc", None)
    monkeypatch.setattr(svc, "_start_failed_at", 0.0)
    monkeypatch.setattr(svc, "_warm_thread", None)
    yield
    with svc._lock:
        svc._terminate_locked()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _listening_script(tmp_path: Path, port: int) -> str:
    """假 start.sh：用当前解释器起一个只监听端口、默认 SIGTERM 即退的假服务。"""
    (tmp_path / "fake_service.py").write_text(textwrap.dedent(f"""
        import socket, time
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", {port}))
        s.listen(8)
        while True:
            time.sleep(0.5)
    """), encoding="utf-8")
    return f'#!/bin/sh\nexec "{sys.executable}" "{tmp_path / "fake_service.py"}"\n'


def _make_dir(tmp_path: Path, script_body: str) -> Path:
    (tmp_path / "start.sh").write_text(script_body, encoding="utf-8")
    (tmp_path / "start.sh").chmod(0o755)
    return tmp_path


def _conf(tmp_path: Path, port: int) -> dict:
    return {"enabled": True, "base_url": f"http://127.0.0.1:{port}",
            "service_dir": str(tmp_path)}


# ── 管理范围 ────────────────────────────────────────────────────
def test_not_managed_without_service_dir():
    conf = {"enabled": True, "base_url": "http://127.0.0.1:1", "service_dir": ""}
    assert svc.service_dir(conf) is None
    assert svc.available(conf) is False
    assert svc.start(conf) is False


def test_not_managed_when_disabled(tmp_path):
    conf = {"enabled": False, "base_url": "http://127.0.0.1:1",
            "service_dir": str(tmp_path)}
    assert svc.available(conf) is False
    assert svc.start(conf) is False


def test_not_managed_when_start_sh_missing(tmp_path):
    conf = {"enabled": True, "base_url": "http://127.0.0.1:1",
            "service_dir": str(tmp_path)}   # 目录在，但没有 start.sh
    assert svc.available(conf) is False


# ── 启停 ────────────────────────────────────────────────────────
def test_start_stop_cycle(tmp_path):
    port = _free_port()
    _make_dir(tmp_path, _listening_script(tmp_path, port))
    conf = _conf(tmp_path, port)
    assert svc.available(conf) is True
    assert svc.is_up(conf) is False
    assert svc.start(conf) is True
    assert svc.is_up(conf) is True
    assert svc._proc is not None
    svc.stop(conf)
    assert svc._proc is None
    deadline = time.time() + 5
    while time.time() < deadline and svc.is_up(conf):
        time.sleep(0.1)
    assert svc.is_up(conf) is False


def test_start_is_idempotent(tmp_path):
    port = _free_port()
    _make_dir(tmp_path, _listening_script(tmp_path, port))
    conf = _conf(tmp_path, port)
    assert svc.start(conf) is True
    pid = svc._proc.pid
    assert svc.start(conf) is True
    assert svc._proc.pid == pid          # 已在跑：没再拉第二个


def test_stop_without_start_is_noop():
    svc.stop()                            # 不抛、不炸


# ── 失败与冷却 ──────────────────────────────────────────────────
def test_start_failure_enters_cooldown(tmp_path):
    _make_dir(tmp_path, "#!/bin/sh\nexit 3\n")   # 拉起即退
    conf = _conf(tmp_path, _free_port())
    t0 = time.time()
    assert svc.start(conf) is False
    assert time.time() - t0 < svc.START_TIMEOUT  # 进程早退：不等满 60s
    assert svc._start_failed_at > 0              # 进冷却
    t1 = time.time()
    assert svc.start(conf) is False              # 冷却期内直接拒绝
    assert time.time() - t1 < 0.2
