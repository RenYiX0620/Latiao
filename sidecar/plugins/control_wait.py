#!/usr/bin/env python3
"""control_wait - 等待指定秒数（流程编排用）。"""
import asyncio
import time

NAME = "control_wait"
PERMISSION = "safe"
DEFINITION = {
    "type": "function",
    "function": {
        "name": "control_wait",
        "description": "等待指定秒数（0-120，默认 1）。用于流程编排：等待界面变化/后台任务产出/窗口就绪后再操作。",
        "parameters": {
            "type": "object",
            "properties": {
                "seconds": {"type": "number", "description": "等待秒数（0-120）"},
            },
            "required": ["seconds"],
        },
    },
}


async def execute(args: dict) -> str:
    try:
        seconds = float(args.get("seconds", 1))
    except (TypeError, ValueError):
        return "❌ seconds 必须是数字"
    seconds = max(0, min(120, seconds))
    await asyncio.sleep(seconds)
    return f"✅ 已等待 {seconds:.1f} 秒"
