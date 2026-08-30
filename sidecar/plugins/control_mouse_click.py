#!/usr/bin/env python3
"""control_mouse_click - 鼠标点击坐标（需辅助功能权限）。"""
NAME = "control_mouse_click"
PERMISSION = "confirm"
DEFINITION = {
    "type": "function",
    "function": {
        "name": "control_mouse_click",
        "description": "在坐标 (x,y) 执行鼠标点击（左键/右键，可双击）。需辅助功能权限。用于操作界面（配合 screen_capture 定位）。",
        "parameters": {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "x 坐标"},
                "y": {"type": "integer", "description": "y 坐标"},
                "button": {"type": "string", "enum": ["left", "right"], "description": "按钮，默认 left"},
                "double": {"type": "boolean", "description": "是否双击，默认 false"},
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
    button = str(args.get("button") or "left").lower()
    if button not in ("left", "right"):
        return "❌ button 仅支持 left/right"
    return mc.mouse_click(x, y, button=button, double=bool(args.get("double")))
