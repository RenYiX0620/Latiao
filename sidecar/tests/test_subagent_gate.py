"""子代理执行层闸门（审计 A3）：只读 ∪ 构建/测试放行，其余 confirm 级拒绝。

背景：子代理曾以 access_mode="full" 运行 + `_is_readonly_cmd` 从未在执行层调用
（只有测试引用）→ explore/debugger 可免确认执行任意未命中破坏性黑名单的命令
（npm install / git commit / curl -o / pip install / chmod +x / git push 实测全放行），
且 delegate_task 本身是 safe 级（连"要派了"都不弹窗）。
"""
import pytest

from agent.context import (
    _normalize_access,
    subagent_cmd_allowed,
    subagent_tool_gate,
)

DENY_SAMPLES = [
    "npm install lodash",
    "npm i lodash",
    "pip install requests",
    "pip3 install -r requirements.txt",
    "git commit -m x",
    "git push origin main",
    "git checkout -b new",
    "curl -s http://x/y -o /tmp/a",
    "wget http://x/y",
    "chmod +x /tmp/a",
    "mv a b",
    "rm -rf /tmp/x",
    "python3 -c 'print(1)'",
    "python3 -m http.server 8000",
    "npm run postinstall",
    "npm run deploy",
    "make deploy",
    "make",
    "bash -c 'ls'",
    "ls; rm -rf x",
    "cat x > y",
    "echo hi > /etc/x",
]
ALLOW_SAMPLES = [
    "ls -la",
    "cat README.md",
    "head -n 20 sidecar/main.py",
    "git log --oneline",
    "git status",
    "git diff HEAD",
    "grep -rn TODO .",
    "wc -l src/App.tsx",
    "pytest -q",
    "pytest tests/test_cmd_safety.py -x",
    "python3 -m pytest -q",
    "npm test",
    "npm run test",
    "npm run build",
    "npm run typecheck",
    "pnpm run lint",
    "yarn test",
    "cargo test",
    "cargo clippy",
    "go test ./...",
    "go vet ./...",
    "make test",
    "tsc --noEmit",
    "ruff check .",
    "mypy --strict sidecar",
]


@pytest.mark.parametrize("cmd", DENY_SAMPLES)
def test_subagent_denies_non_build_commands(cmd):
    """这些命令在子代理里必须被拒（审计实测它们以前是免确认放行的）。"""
    assert subagent_cmd_allowed(cmd), f"不该放行: {cmd}"


@pytest.mark.parametrize("cmd", ALLOW_SAMPLES)
def test_subagent_allows_readonly_and_build(cmd):
    """只读与构建/测试命令放行（否则子代理干不了活）。"""
    assert subagent_cmd_allowed(cmd) is None, f"不该拒绝: {cmd}"


def test_subagent_denies_write_file_and_other_confirm_tools():
    """write_file 等 confirm 级工具：拒绝而不是"转人工确认"（子代理没有确认通道）。"""
    d = subagent_tool_gate("write_file", {"path": "/tmp/x", "content": "hi"})
    assert d and "确认" in d
    assert subagent_tool_gate("run_cmd", {"cmd": "npm install x"})
    assert subagent_tool_gate("run_cmd", {"cmd": "ls"}) is None


def test_subagent_gate_allows_safe_tools():
    """safe 级工具（读文件/列目录/搜索）不受影响。"""
    for t in ("read_file", "list_dir", "search_files", "tavily_search"):
        assert subagent_tool_gate(t, {"path": "."}) is None, t


def test_subagent_mode_normalizes():
    """"subagent" 是合法档位，且不会被归一化降级成别的档。"""
    assert _normalize_access("subagent") == "subagent"
    assert _normalize_access("full") == "full"      # 其他档位不受影响
    assert _normalize_access("workspace") == "auto_edit"


def test_gate_cannot_be_reopened_by_user_rules(monkeypatch):
    """用户自定义规则（把 run_cmd 降成 safe）不能重新打开子代理这条路。

    闸门读的是 TOOL_PERMISSIONS 规范档位而不是 _resolve_permission。
    """
    import main
    monkeypatch.setattr(main, "_custom_permissions",
                        [{"tool": "run_cmd", "permission": "safe"}], raising=False)
    assert subagent_tool_gate("run_cmd", {"cmd": "npm install x"}), \
        "用户规则不该让子代理免确认执行安装命令"


def _stub_background(monkeypatch, A):
    """桩掉执行后的后台自我进化（_refine_learnings/_maybe_generate_skill）。

    不桩会让它们真去调模型/网络 → 测试进程退出被挂住（首版就因此在 timeout 里
    看不到汇总行）。coro 需要关闭，否则报 "coroutine was never awaited"。
    """
    def _no_spawn(obj):
        try:
            if hasattr(obj, "close"):
                obj.close()
        except Exception:
            pass
    monkeypatch.setattr(A, "_spawn", _no_spawn, raising=False)
    import agent.tool_exec as TE2
    monkeypatch.setattr(TE2, "_spawn", _no_spawn, raising=False)


def test_subagent_execution_layer_denies(monkeypatch):
    """端到端：执行层对 subagent 档直接拒绝，且不进入确认流程。"""
    import asyncio
    import agent_loop as A

    # 必须桩掉真实执行：否则测试会真去跑命令（首次写这个测试时 echo 命令
    # 在 run_cmd 的 300s 超时里挂了 5 分钟）
    async def _fake_exec(tool_name, args):
        return f"[stub] {tool_name} {args}"
    monkeypatch.setattr(A, "execute_tool", _fake_exec, raising=False)
    import agent.tool_exec as TE
    monkeypatch.setattr(TE, "execute_tool", _fake_exec, raising=False)   # 执行簇已拆出
    _stub_background(monkeypatch, A)

    async def _run():
        msgs = []
        tc = {"id": "c1", "function": {"name": "run_cmd",
                                       "arguments": '{"cmd": "npm install lodash"}'}}
        ok, events = await A._handle_tool_execution(
            tc, msgs, "sess:sub_1", "latiao", "subagent")
        return msgs, events

    msgs, events = asyncio.run(_run())
    assert msgs and "子代理" in msgs[-1]["content"]
    assert "npm install" in msgs[-1]["content"] or "只允许" in msgs[-1]["content"]
    assert events and events[0]["event"] == "tool_end"
    assert "子代理" in str(events[0]["result"])


def test_subagent_execution_layer_allows_readonly(monkeypatch):
    """只读命令仍能跑通执行层（不因闸门误伤）。"""
    import asyncio
    import agent_loop as A

    async def _fake_exec(tool_name, args):
        return "[stub] ok"
    monkeypatch.setattr(A, "execute_tool", _fake_exec, raising=False)
    import agent.tool_exec as TE
    monkeypatch.setattr(TE, "execute_tool", _fake_exec, raising=False)   # 执行簇已拆出
    _stub_background(monkeypatch, A)

    async def _run():
        msgs = []
        tc = {"id": "c2", "function": {"name": "run_cmd", "arguments": '{"cmd": "echo hi"}'}}
        ok, events = await A._handle_tool_execution(
            tc, msgs, "sess:sub_2", "latiao", "subagent")
        return msgs

    msgs = asyncio.run(_run())
    # 桩执行返回了结果 = 闸门放行（没有被子代理规则拦下）
    assert msgs and "[stub] ok" in msgs[-1]["content"]


def test_confirm_mode_user_path_unaffected():
    """普通会话（confirm 档）不受子代理闸门影响——不该误拦用户自己的操作。"""
    assert subagent_cmd_allowed("npm install lodash")          # 子代理侧拒绝
    # 但 confirm 档的普通会话不过这个闸门（闸门只在 access_mode == "subagent" 时调用），
    # 这里以"闸门函数本身不认识 access_mode"来断言它没有被错误地全局启用
    import inspect
    src = inspect.getsource(subagent_tool_gate)
    assert "access_mode" not in src
