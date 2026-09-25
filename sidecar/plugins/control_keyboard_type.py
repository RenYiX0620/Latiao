#!/usr/bin/env python3
"""control_keyboard_type - 键入文本（需辅助功能权限）。"""
NAME = "control_keyboard_type"
PERMISSION = "confirm"
DEFINITION = {
    "type": "function",
    "function": {
        "name": "control_keyboard_type",
        "description": "向当前焦点输入框键入文本（支持中英文与符号，≤500 字符）。需辅助功能权限。用于在应用表单/编辑器里输入内容（先确保目标已聚焦）。",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要输入的文本"},
            },
            "required": ["text"],
        },
    },
}


def execute(args: dict) -> str:
    import sys, os as _os
    _d = _os.path.dirname(_os.path.abspath(__file__))
    if _d not in sys.path: sys.path.insert(0, _d)
    import _control_mouse_common as mc
    return mc.keyboard_type(str(args.get("text") or ""))
