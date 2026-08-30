#!/usr/bin/env python3
"""control_mouse_move - 移动鼠标到坐标（需辅助功能权限）。"""
NAME = "control_mouse_move"
PERMISSION = "confirm"
DEFINITION = {
    "type": "function",
    "function": {
        "name": "control_mouse_move",
        "description": "把系统鼠标移动到屏幕坐标 (x,y)。需辅助功能权限。用于鼠标控制的第一步（配合 screen_capture 确定坐标）。",
        "parameters": {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "x 坐标（屏幕像素）"},
                "y": {"type": "integer", "description": "y 坐标（屏幕像素）"},
            },
            "required": ["x", "y"],
        },
    },
}


def execute(args: dict) -> str:
    import sys, os as _os
    _d = _os.path.dirname(_os.path.abspath(__file__))
    if _d not in sys.path: sys.path.insert(0, _d)
    import _control_mouse_common as mc
    try:
        x, y = int(args.get("x")), int(args.get("y"))
    except (TypeError, ValueError):
        return "❌ x/y 必须是整数"
    return mc.mouse_move(x, y)
