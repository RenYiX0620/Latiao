"""拆分后的模块冒烟测试（2026-09-23）。

为什么需要：拆 agent_loop.py 时 `_MCP_LOADED` 被留在枢纽、mcp_tools 里 `global`
引用它 → **启动时 NameError**，而测试全部通过（832 全绿是假象）——没有任何测试
走到 MCP 加载那条路径。这类"拆完只有某条路径才炸"的问题，功能测试覆盖不到，
所以这里对每个拆出的模块做最小冒烟：能导入 + 便宜入口能跑通。
"""
import importlib

import pytest

EXTRACTED_MODULES = [
    "agent.progress", "agent.prompt_build", "agent.session_events", "agent.mcp_tools",
    "agent.verify", "agent.reflection", "agent.routing", "agent.confirm", "agent.tool_exec",
]


@pytest.mark.parametrize("name", EXTRACTED_MODULES)
def test_module_imports(name):
    assert importlib.import_module(name) is not None


def test_ensure_mcp_loaded_runs_without_mcp_configured():
    """没配置 MCP 时也要能跑通（此前因 _MCP_LOADED 停在别的模块而 NameError）。"""
    mcp = importlib.import_module("agent.mcp_tools")
    mcp._MCP_LOADED = False
    mcp.ensure_mcp_loaded()          # 不抛异常即通过；无 MCP 配置时应是空操作
    assert mcp._MCP_LOADED is True


def test_no_global_references_outside_module():
    """护栏：新模块里的 `global X` 必须在**本模块**有定义。

    （重构时把定义留在枢纽、函数在新模块里 global 它 → 运行时 NameError，
    测试不覆盖那条路径就发现不了。）
    """
    import ast
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1] / "agent"
    bad = []
    for f in sorted(root.glob("*.py")):
        tree = ast.parse(f.read_text("utf-8"))
        defined = {n.targets[0].id for n in tree.body
                   if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)}
        defined |= {n.target.id for n in tree.body
                    if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)}
        for n in ast.walk(tree):
            if isinstance(n, ast.Global):
                for name in n.names:
                    if name not in defined:
                        bad.append(f"{f.name}: global {name}")
    assert not bad, f"global 引用了本模块未定义的名字（会 NameError）：{bad}"
