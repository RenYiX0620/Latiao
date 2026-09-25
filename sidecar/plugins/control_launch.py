#!/usr/bin/env python3
"""control_launch - 后台启动应用/命令，返回 pid 与输出文件。"""
NAME = "control_launch"
PERMISSION = "confirm"
DEFINITION = {
    "type": "function",
    "function": {
        "name": "control_launch",
        "description": "后台启动一条命令或应用（如 python3 xxx.py、open -a Safari、node server.js），立即返回 pid，stdout/stderr 落盘可后续读取。用于启动长运行的后台任务。",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要启动的完整命令（空格分隔；请用绝对路径或已在 PATH 中的可执行文件）"
                }
            },
            "required": ["command"],
        },
    },
}


def execute(args: dict) -> str:
    import sys, os as _os
    _d = _os.path.dirname(_os.path.abspath(__file__))
    if _d not in sys.path: sys.path.insert(0, _d)
    import _control_common as cc
    command = str(args.get("command") or "").strip()
    return cc.launch_bg(command)
