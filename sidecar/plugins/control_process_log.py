#!/usr/bin/env python3
"""control_process_log - 读取后台进程输出尾部（配合 control_launch）。"""
NAME = "control_process_log"
PERMISSION = "safe"
DEFINITION = {
    "type": "function",
    "function": {
        "name": "control_process_log",
        "description": "读取最近一次 control_launch 启动的后台进程输出（stdout 落盘文件尾部）。用于检查后台任务是否在运行/是否出错。",
        "parameters": {
            "type": "object",
            "properties": {
                "lines": {
                    "type": "integer",
                    "description": "读取尾部行数（默认 50，最大 200）"
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
    lines = args.get("lines", 50)
    return cc.read_bg_log(lines)
