#!/usr/bin/env python3
"""screen_capture - 截屏保存 PNG（只读，无副作用）。"""
NAME = "screen_capture"
PERMISSION = "safe"
DEFINITION = {
    "type": "function",
    "function": {
        "name": "screen_capture",
        "description": "截取当前屏幕并保存为 PNG 文件，返回保存路径。可指定保存位置和区域（x/y/宽/高）。用于观察界面、确定点击坐标。只读，不产生副作用（需屏幕录制权限）。",
        "parameters": {
            "type": "object",
            "properties": {
                "save_path": {"type": "string", "description": "保存路径（可选，默认 ~/.local-ai-os/screens/cap_*.png）"},
                "x": {"type": "integer", "description": "区域左上角 x（可选，默认全屏）"},
                "y": {"type": "integer", "description": "区域左上角 y（可选）"},
                "width": {"type": "integer", "description": "区域宽度（可选）"},
                "height": {"type": "integer", "description": "区域高度（可选）"},
            },
            "required": [],
        },
    },
}


def execute(args: dict) -> str:
    import sys, os as _os
    _d = _os.path.dirname(_os.path.abspath(__file__))
    if _d not in sys.path: sys.path.insert(0, _d)
    import _control_mouse_common as mc
    return mc.screen_capture(
        save_path=str(args.get("save_path") or ""),
        x=int(args.get("x") or 0), y=int(args.get("y") or 0),
        w=int(args.get("width") or 0), h=int(args.get("height") or 0),
    )
