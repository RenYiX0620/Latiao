#!/usr/bin/env python3
"""control_keyboard_press - 发送快捷键组合（需辅助功能权限）。"""
NAME = "control_keyboard_press"
PERMISSION = "confirm"
DEFINITION = {
    "type": "function",
    "function": {
        "name": "control_keyboard_press",
        "description": "发送快捷键组合，如 cmd+tab、cmd+w、shift+cmd+3、cmd+space。需辅助功能权限。用于操作系统级快捷键（切换窗口、保存、复制粘贴等）。",
        "parameters": {
            "type": "object",
            "properties": {
                "combo": {
                    "type": "string",
                    "description": "快捷键组合，修饰键 cmd/ctrl/alt/option/shift + 主键，用 + 连接（如 cmd+tab、shift+cmd+s）"
                }
            },
            "required": ["combo"],
        },
    },
}


def execute(args: dict) -> str:
    import sys, os as _os
    _d = _os.path.dirname(_os.path.abspath(__file__))
    if _d not in sys.path: sys.path.insert(0, _d)
    import _control_mouse_common as mc
    return mc.keyboard_press(str(args.get("combo") or ""))
