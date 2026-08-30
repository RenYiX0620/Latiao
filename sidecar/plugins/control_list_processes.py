#!/usr/bin/env python3
"""control_list_processes - 列出运行中的进程（只读）。"""
from pathlib import Path

NAME = "control_list_processes"
PERMISSION = "safe"
DEFINITION = {
    "type": "function",
    "function": {
        "name": "control_list_processes",
        "description": "列出正在运行的进程（pid、CPU、内存、命令），可按名称过滤。用于了解本机当前运行了什么、查找进程 pid。只读无副作用。",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "按名称过滤（如 Safari、python、node），可选"
                }
            },
            "required": [],
        },
    },
}


def execute(args: dict) -> str:
    import sys, os as _os
    _d = _os.path.dirname(_os.path.abspath(__file__))
    if _d not in sys.path: sys.path.insert(0, _d)
    import _control_common as cc
    pattern = str(args.get("pattern") or "").strip()
    try:
        if cc.is_windows():
            rows = cc.tasklist_table(pattern)
            header = f"进程列表（匹配 {len(rows)} 个）："
        else:
            rows = cc.ps_table(pattern)
            header = f"进程列表（匹配 {len(rows)} 个）："
        return header + "\n" + "\n".join(rows[:100])
    except Exception as e:
        return f"列出进程失败: {e}"
