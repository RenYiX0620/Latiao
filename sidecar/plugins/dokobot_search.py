#!/usr/bin/env python3
"""dokobot_search - 走真实浏览器的联网搜索（Dokobot）。"""
import sys as _sys
_sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
from _dokobot_common import dokobot_bin, run, _NOT_INSTALLED

NAME = "dokobot_search"
PERMISSION = "safe"

DEFINITION = {
    "type": "function",
    "function": {
        "name": NAME,
        "description": (
            "用 Dokobot 联网搜索（走真实浏览器，过部分反爬）。tavily/bing 不可用时的备选。"
            "⚠️ 前置：本机需安装 Dokobot CLI + Chrome 扩展。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
            },
            "required": ["query"],
        },
    },
}


def execute(args: dict) -> str:
    query = (args.get("query", "") or "").strip()
    if not query:
        return "Error: query 参数必填"
    bin_path = dokobot_bin()
    if not bin_path:
        return f"Error: dokobot CLI 未安装。\n{_NOT_INSTALLED}"
    code, out = run([bin_path, "search", query, "--local"], timeout=120)
    if code != 0 or not out.strip():
        low = out.lower()
        if "no supported browsers" in low or ("bridge" in low and "not" in low):
            return f"Error: 浏览器桥接未就绪。\n{_NOT_INSTALLED}"
        return f"Error (exit {code}): {out[:500]}"
    return out.strip()[:6000] or "未找到相关结果。"
