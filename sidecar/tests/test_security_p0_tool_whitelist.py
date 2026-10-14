"""工具白名单硬闸门 + 无人值守 cron 默认档位（2026-10-11 深审 Critical）。

① tool_whitelist 此前只在发给模型的工具目录里过滤（loop 组 tools），执行层
   不过问——文本方言 fence 解析出的任意工具名仍会进 TOOL_DISPATCH。
② 无人值守 cron 默认 access_mode="full"，confirm 级工具被自动放行。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LATIAO_AUTH_TOKEN", "wl-token")


def _mk_call(name: str, args: dict | None = None) -> dict:
    import json
    return {
        "id": "call_1",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args or {}, ensure_ascii=False)},
    }


@pytest.mark.asyncio
async def test_tool_whitelist_rejects_outside_tool():
    """名单外的工具名在执行入口被硬拒（不再只滤目录）。"""
    from agent import tool_exec as TE

    msgs: list = []
    ok, events = await TE._handle_tool_execution(
        _mk_call("write_file", {"path": "/tmp/x", "content": "hi"}),
        msgs, "s1", "latiao", "full", tool_whitelist={"read_file", "list_dir"})
    # 拒绝即返回 False（未产生 verify_failed）且结果含白名单文案
    assert ok is False
    results = [e.get("result", "") for e in events if isinstance(e, dict)]
    assert any("白名单" in r for r in results), results
    assert any("write_file" in r for r in results), results


@pytest.mark.asyncio
async def test_tool_whitelist_allows_listed_tool():
    """名单内的工具不受影响（read_only 档下 read_file 可执行）。"""
    from agent import tool_exec as TE

    msgs: list = []
    _ok, events = await TE._handle_tool_execution(
        _mk_call("read_file", {"path": __file__}),
        msgs, "s2", "latiao", "read_only", tool_whitelist={"read_file", "list_dir"})
    results = [e.get("result", "") for e in events if isinstance(e, dict)]
    assert not any(r.startswith("⛔ 工具") and "不在本次运行的工具白名单" in r
                   for r in results), results


@pytest.mark.asyncio
async def test_tool_whitelist_none_means_unrestricted():
    """None = 不限制（主对话路径行为不变）。"""
    from agent import tool_exec as TE

    msgs: list = []
    _ok, events = await TE._handle_tool_execution(
        _mk_call("read_file", {"path": __file__}),
        msgs, "s3", "latiao", "read_only", tool_whitelist=None)
    results = [e.get("result", "") for e in events if isinstance(e, dict)]
    # 拒绝文案以固定前缀开头；读到文件内容则说明放行
    assert not any(r.startswith("⛔ 工具") and "不在本次运行的工具白名单" in r
                   for r in results), results


def test_cron_default_access_mode_is_read_only():
    """无人值守 cron 默认档位必须是 read_only，不得再默认 full。"""
    import inspect

    import cron
    src = inspect.getsource(cron._execute_cron_job)
    assert 'or "full"' not in src, "cron 默认档位仍为 full"
    assert 'or "read_only"' in src, "cron 默认档位应为 read_only"


def test_cron_create_api_defaults_read_only():
    """创建 API 显式写入 access_mode，默认 read_only。"""
    src = Path(__file__).resolve().parents[1].joinpath("api_routes_cron_local.py").read_text("utf-8")
    assert '"access_mode": body.get("access_mode", "read_only")' in src
    assert '"access_mode": body.get("access_mode", "full")' not in src


def test_identity_files_write_sealed_both_paths():
    """身份文件（IDENTITY/SOUL/AGENTS/USER.md）双通道写密封（2026-10-11 深审 High）。

    它们直通系统提示且带"一律照做"语义；此前 write_file / 命令重定向都能改写。
    普通文件（notes.md）不受影响。
    """
    import os

    from cmd_safety import (
        IDENTITY_FILE_NAMES,
        _protected_write_target,
        sensitive_write_block,
    )
    home = os.path.expanduser("~")
    assert set(IDENTITY_FILE_NAMES) == {"identity.md", "soul.md", "agents.md", "user.md"}
    for n in ("IDENTITY.md", "SOUL.md", "AGENTS.md", "USER.md"):
        p = os.path.join(home, ".local-ai-os", n)
        assert sensitive_write_block(p) is not None, f"{n} 工具路径未密封"
        assert _protected_write_target(p) is not None, f"{n} 命令路径未密封"
    free = os.path.join(home, ".local-ai-os", "notes.md")
    assert sensitive_write_block(free) is None
    assert _protected_write_target(free) is None
