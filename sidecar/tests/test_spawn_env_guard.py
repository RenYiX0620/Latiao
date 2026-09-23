"""子进程 env 护栏（2026-09-23，安全批次 ④ 的收尾）。

背景：④ 给 7 处 spawn 接了 `child_env()` 白名单，靠的是"人肉找齐所有 spawn 点"——
这种一次性清查会随新代码腐烂。这里改成结构检查：**每一个 spawn 调用要么显式传
env=，要么 argv 是写死的系统命令**（security/netstat/…），否则判定为"可能把
sidecar token 和云密钥漏给子进程"，直接失败。

为什么允许写死的系统命令：这类调用的 argv 完全由代码决定、不接受模型输入，且
系统二进制不需要用户凭据；env 传不传都不改变风险面（传了还容易顺手改坏）。
新增命令要进 SAFE_BINARIES 时，请在提交信息里说明它是固定 argv。
"""
import ast
import pathlib

import pytest

SIDECAR = pathlib.Path(__file__).resolve().parents[1]

# 写死 argv 的系统命令：不接受模型输入，继承 env 不构成凭据外泄
SAFE_BINARIES = {
    "security", "netstat", "taskkill", "tasklist", "sysctl", "lsof", "ps",
    "open", "xdg-open", "ffmpeg", "pmset", "vm_stat", "sw_vers",
    "screencapture",          # 固定 argv 的截屏试探（权限探测）
    "cmd",                    # Windows 分支：open_app 已先校验应用名不含 cmd 元字符
}

SPAWN_ATTRS = {"run", "Popen", "call", "check_call", "check_output", "getoutput"}
SPAWN_MODULES = {"subprocess", "os"}
ASYNC_SPAWN_ATTRS = {"create_subprocess_exec", "create_subprocess_shell"}

_SKIP_DIRS = {"tests", "python", "__pycache__", "node_modules", ".venv", "venv"}


def _iter_sources():
    for p in sorted(SIDECAR.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        yield p


def _is_spawn(call: ast.Call) -> bool:
    f = call.func
    if not isinstance(f, ast.Attribute):
        return False
    # asyncio.create_subprocess_exec / _shell（含 `import asyncio as a` 这类别名）
    if f.attr in ASYNC_SPAWN_ATTRS:
        return True
    if isinstance(f.value, ast.Name):
        if f.value.id in SPAWN_MODULES and f.attr in SPAWN_ATTRS:
            return True
        # os.system / os.popen / os.exec* / os.spawn* 一律当作 spawn
        if f.value.id == "os" and (f.attr in {"system", "popen"} or
                                   f.attr.startswith(("exec", "spawn"))):
            return True
    return False


def _first_argv_literal(call: ast.Call):
    if not call.args:
        return None
    a = call.args[0]
    if isinstance(a, (ast.List, ast.Tuple)) and a.elts:
        e = a.elts[0]
        if isinstance(e, ast.Constant) and isinstance(e.value, str):
            return e.value
    return None


def test_every_spawn_passes_env_or_is_a_fixed_system_command():
    offenders = []
    for path in _iter_sources():
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not _is_spawn(node):
                continue
            if any(k.arg == "env" for k in node.keywords):
                continue                      # 显式传了 env（应当是 child_env()）
            lit = _first_argv_literal(node)
            if lit and pathlib.Path(lit).name in SAFE_BINARIES:
                continue
            rel = path.relative_to(SIDECAR)
            offenders.append(f"{rel}:{node.lineno} argv0={lit!r}")
    assert not offenders, (
        "这些 spawn 既没有 env= 白名单，argv 也不是写死的系统命令 → "
        "子进程会继承 LATIAO_AUTH_TOKEN 与云模型密钥：\n  " + "\n  ".join(offenders))


def test_no_spawn_uses_shell_true():
    """shell=True 会把 argv 交给 /bin/sh 解释（模型可控输入即注入）。"""
    offenders = []
    for path in _iter_sources():
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not _is_spawn(node):
                continue
            for k in node.keywords:
                if k.arg == "shell" and isinstance(k.value, ast.Constant) and k.value.value is True:
                    rel = path.relative_to(SIDECAR)
                    offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, f"发现 shell=True 的 spawn：{offenders}"


@pytest.mark.parametrize("rel", [
    "plugins/run_cmd.py", "plugins/_control_common.py", "mcp_client.py",
    "local_llm.py", "plugins/mx_query.py", "tool_executor.py",
])
def test_env_passing_sites_still_use_child_env(rel):
    """传 env= 的地方必须是 child_env()，不能退回 os.environ.copy()。"""
    src = (SIDECAR / rel).read_text("utf-8")
    assert "os.environ.copy()" not in src, f"{rel} 又出现了 os.environ.copy()"
    assert src.count("child_env(") >= 1, f"{rel} 找不到 child_env("
