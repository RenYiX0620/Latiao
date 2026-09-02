#!/usr/bin/env python3
"""dokobot_read - 真实浏览器渲染读取网页（绕反爬/SPA/需登录页面）。"""
import sys as _sys
_sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
from _dokobot_common import dokobot_bin, run, _NOT_INSTALLED

NAME = "dokobot_read"
PERMISSION = "safe"

DEFINITION = {
    "type": "function",
    "function": {
        "name": NAME,
        "description": (
            "用真实 Chrome 浏览器读取任意网页的完整渲染后内容——专治普通抓取失败的页面："
            "JS 动态渲染、SPA、反爬拦截、微信公众号/知乎/小红书/头条文章。返回纯文本正文。"
            "⚠️ 前置：本机需安装 Dokobot CLI + Chrome 扩展（未安装时返回安装指引）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要读取的完整网页 URL"},
                "screens": {"type": "integer", "description": "滚动屏数（长文 3-10，默认自动）"},
            },
            "required": ["url"],
        },
    },
}


def execute(args: dict) -> str:
    url = (args.get("url", "") or "").strip()
    if not url.startswith(("http://", "https://")):
        return "Error: url 参数必须是完整的 http(s) 链接"
    bin_path = dokobot_bin()
    if not bin_path:
        return f"Error: dokobot CLI 未安装。\n{_NOT_INSTALLED}"
    cmd = [bin_path, "read", url, "--local"]
    screens = args.get("screens")
    if screens:
        cmd += ["--screens", str(int(screens))]
    code, out = run(cmd, timeout=180)
    if code != 0 or not out.strip():
        low = out.lower()
        if "no supported browsers" in low or ("bridge" in low and "not" in low):
            return f"Error: 浏览器桥接未就绪。\n{_NOT_INSTALLED}"
        return f"Error (exit {code}): {out[:500]}"
    return out.strip()[:6000] or "（页面无文本内容）"
