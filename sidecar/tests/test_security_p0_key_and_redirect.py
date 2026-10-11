"""两个 P0 的回归测试（2026-10-11 安全审计核验后修）。

审计原文两条，本机实测均成立：
1. `echo x > 受保护路径` 绕过写入封印——`reject_sensitive_write` 的只读动词
   白名单（echo/printf 等为"看目录"放行）在**重定向形态**下直接 return None，
   目标根本没被检查。实测 `echo pwned > ~/.local-ai-os/permissions.json`
   （含 config.json、extensions 下的 .py）全部放行 = 下次启动 RCE。
2. 云端 API key 可被诱导外传——`_lookup_cloud_key` 按 name **或** endpoint 匹配
   就回填 key，且调用方随请求自带 endpoint。构造
   {name: <已配置模型名>, endpoint: https://evil} 即把存量 key 送出去。

两条都先写"修前必红"的断言，修完即绿。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LATIAO_AUTH_TOKEN", "sec-token")

HOME = os.path.expanduser("~")


@pytest.fixture()
def prod_progress(tmp_path, monkeypatch):
    """生产口径的 PROGRESS_DIR（真机 ~/.local-ai-os）+ config.json 隔离到 tmp。

    为什么这样搭：封印判定按 _app_data_dirs()（PROGRESS_DIR + 默认 ~/.local-ai-os
    两个候选）。要测"真机受保护路径被 echo 写穿"，捕获的命令必须指向真实数据目录；
    而 key 回填测试要写 config.json——把 CONFIG_FILE 指到 tmp 避免碰真文件。

    纪律（2026-10-11 事故的教训，被 conftest 守卫当场抓到过一次）：teardown 必须
    先恢复会话级 TEST_PROGRESS_DIR 再 reload，绝不 delenv + reload——那会把
    config.CONFIG_FILE 留成真机路径。
    """
    import importlib

    import config as config_mod
    session_dir = os.environ.get("LATIAO_TEST_PROGRESS_DIR") or str(tmp_path)
    monkeypatch.setenv("LATIAO_TEST_PROGRESS_DIR", str(tmp_path))
    importlib.reload(config_mod)
    yield tmp_path
    monkeypatch.setenv("LATIAO_TEST_PROGRESS_DIR", session_dir)
    importlib.reload(config_mod)


# ── P0-1：重定向写封印绕过 ─────────────────────────────────────────

_PROTECTED_TARGETS = [
    f"{HOME}/.local-ai-os/permissions.json",
    f"{HOME}/.local-ai-os/agents.json",
    f"{HOME}/.local-ai-os/skills.json",
    f"{HOME}/.local-ai-os/config.json",
    f"{HOME}/.local-ai-os/extensions/evil/1.0/plugin.py",
]


@pytest.mark.parametrize("target", _PROTECTED_TARGETS)
@pytest.mark.parametrize("tpl", ["echo pwned > {t}", "echo pwned >> {t}",
                                 "printf x > {t}", "echo pwned>{t}"])
def test_protected_path_redirect_blocked_every_verb(tpl, target, prod_progress):
    """受保护路径的重定向写：不论哪个动词（含只读白名单里的 echo/printf）都必须拦。"""
    from cmd_safety import split_stdout_redirect
    cmd = tpl.format(t=target)
    assert split_stdout_redirect(cmd) is None, f"受保护路径不该被解析成可写重定向: {cmd}"


def test_check_cmd_blocks_echo_redirect_to_permissions(prod_progress):
    from cmd_safety import check_cmd
    denied = check_cmd(f"echo pwned > {HOME}/.local-ai-os/permissions.json")
    assert denied and "⛔" in denied, "echo 写 permissions.json 必须被拦（修前实测放行）"


def test_normal_redirect_still_works(prod_progress):
    """对照：普通路径的重定向必须仍然可用（别修成谁都拦）。"""
    from cmd_safety import split_stdout_redirect
    sp = split_stdout_redirect("echo hello > /tmp/lt-normal-out.txt")
    assert sp is not None and sp[1] == "/tmp/lt-normal-out.txt"


def test_cp_still_blocked(prod_progress):
    """既有拦截不许被改坏（cp/tee/sed -i 修前就拦得住）。"""
    from cmd_safety import check_cmd, reject_sensitive_write
    for tpl in ("cp /tmp/a {t}", "tee {t}", "sed -i s/a/b/ {t}"):
        cmd = tpl.format(t=f"{HOME}/.local-ai-os/config.json")
        assert reject_sensitive_write(cmd), cmd
        assert check_cmd(cmd), cmd


# ── P0-3：云端 key 只在 endpoint 一致时回填 ────────────────────────

def _write_cfg(tmp: Path, models: list[dict]) -> None:
    (tmp / "config.json").write_text(json.dumps({"cloud_models": models}), encoding="utf-8")


def test_key_not_leaked_to_foreign_endpoint(prod_progress):
    """攻击载荷：name 命中、endpoint 是攻击者地址 → 绝不回填 key（修前实测回填）。"""
    _write_cfg(prod_progress, [{"name": "my-model", "endpoint": "https://legit.example.com/v1",
                                "key": "sk-SECRET", "protocol": "openai"}])
    import importlib

    import agent.routing as routing
    importlib.reload(routing)
    got = routing._lookup_cloud_key({"name": "my-model", "endpoint": "https://evil.example.com",
                                     "model": "my-model"})
    assert got == "", f"跨 endpoint 不得回填 key（实测拿到 {got!r}）"


def test_key_returned_for_matching_endpoint(prod_progress):
    """对照：endpoint 一致的合法请求必须照常回填。"""
    _write_cfg(prod_progress, [{"name": "my-model", "endpoint": "https://legit.example.com/v1",
                                "key": "sk-SECRET", "protocol": "openai"}])
    import importlib

    import agent.routing as routing
    importlib.reload(routing)
    got = routing._lookup_cloud_key({"name": "my-model",
                                     "endpoint": "https://legit.example.com/v1/",
                                     "model": "my-model"})
    assert got == "sk-SECRET", "同 endpoint（含尾斜杠差异）应回填"


def test_key_not_returned_by_name_only_when_endpoint_missing(prod_progress):
    """endpoint 缺失 + 只有 name：同样不回填（按名回填是外传通道的一半）。"""
    _write_cfg(prod_progress, [{"name": "my-model", "endpoint": "https://legit.example.com/v1",
                                "key": "sk-SECRET", "protocol": "openai"}])
    import importlib

    import agent.routing as routing
    importlib.reload(routing)
    got = routing._lookup_cloud_key({"name": "my-model", "model": "my-model"})
    assert got == "", f"仅凭名称不得回填 key（实测拿到 {got!r}）"
