"""并行委派快路径必须走统一执行入口（2026-10-11 全盘审查 Critical）。

旧实现直接调 _delegate_task_bg/_fg，绕过 tool_whitelist 硬闸门、_check_access、
确认流、fence_untrusted——第二条工具执行入口。现在与安全并行路径同构。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LATIAO_AUTH_TOKEN", "par-del-token")


def test_parallel_delegate_uses_unified_executor():
    """源码层钉住：并行委派不得再直调 _delegate_task_bg/_fg。"""
    src = Path(__file__).resolve().parents[1].joinpath("agent", "loop.py").read_text("utf-8")
    # 找并行委派段
    idx = src.find("并行委派")
    assert idx > 0
    seg = src[idx:idx + 2500]
    assert "_handle_tool_execution(" in seg, "并行委派必须走统一执行入口"
    # 只禁**实际调用**（注释里提到旧名不算）：调用形态是 `await _delegate_task_*`
    assert "await _delegate_task_bg" not in seg and "await _delegate_task_fg" not in seg, \
        "不得再直调 _delegate_task_*（旁路闸门）"
    assert "tool_whitelist=self.tool_whitelist" in seg


def test_parallel_delegate_results_are_fenced():
    """统一入口自带 fence_untrusted（tool_exec 对 delegate_task 恒定围栏）。"""
    src = Path(__file__).resolve().parents[1].joinpath("agent", "tool_exec.py").read_text("utf-8")
    assert 'if tool_name == "delegate_task"' in src
    assert "fence_untrusted" in src
