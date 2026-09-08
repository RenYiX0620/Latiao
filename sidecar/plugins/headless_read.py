#!/usr/bin/env python3
"""headless_read - 无头 Chromium 渲染读取网页（绕反爬/SPA，无需浏览器与扩展）。

与 dokobot_read 的区别：完全本地无头渲染（Playwright Chromium），不依赖
Chrome/Dokobot 扩展/bridge，开箱即用；代价是无登录态（读不了需登录的页面）。
未安装时返回明确的安装指引。
"""
import os
import re
import json
import urllib.parse
from pathlib import Path

# 浏览器二进制固定装在应用数据目录——不依赖会被系统定期清理的
# ~/Library/Caches/ms-playwright（09-08 17:13 事故：缓存被清后工具失效）。
# 必须在 playwright 首次加载前设置，故放模块顶层。
_os_environ = os.environ
_os_environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH",
    os.path.join(os.path.expanduser("~"), ".local-ai-os", "pw-browsers"))

NAME = "headless_read"
PERMISSION = "safe"

DEFINITION = {
    "type": "function",
    "function": {
        "name": NAME,
        "description": (
            "用本地无头 Chromium 浏览器读取任意网页的完整渲染后内容——专治普通抓取失败的页面："
            "JS 动态渲染、SPA、反爬拦截（头条/知乎/微信公众号文章等）。返回页面纯文本正文。"
            "无登录态，读不了需要登录的页面。"
            "⚠️ 首次使用前需一次性安装运行环境（Playwright + Chromium），"
            "未安装时会返回安装指引；安装后永久可用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要读取的完整网页 URL"},
                "wait_ms": {
                    "type": "integer",
                    "description": "页面加载后额外等待毫秒数（默认 3000，慢页面可加大）",
                },
            },
            "required": ["url"],
        },
    },
}

_NOT_INSTALLED = (
    "⚠️ 无头浏览器运行环境未安装（Playwright 的 Chromium 二进制缺失）。启用步骤：\n"
    "1. 打开终端执行：python3 -m playwright install chromium\n"
    "   （应用内置环境对应命令见 sidecar 提示，或直接执行："
    "'/Applications/Latiao.app/Contents/Resources/sidecar/python/bin/python3 -m playwright install chromium'）\n"
    "2. 浏览器会安装到 ~/.local-ai-os/pw-browsers（本工具固定查找该目录）\n"
    "装好后本工具即可读取反爬/动态渲染页面；若持续失败请改用 dokobot_read 或 tavily_search。"
)

_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

_MAX_CHARS = 6000


def _extract_text(html: str) -> str:
    """从渲染后 HTML 提取正文：优先 RENDER_DATA/头条正文，否则全文剥标签。"""
    m = re.search(r'<script id="RENDER_DATA" type="application/json">(.*?)</script>', html)
    if m:
        try:
            info = json.loads(urllib.parse.unquote(m.group(1)))
            ai = info.get("articleInfo") or {}
            if not ai:
                for v in info.values():
                    if isinstance(v, dict) and "articleInfo" in v:
                        ai = v["articleInfo"]
                        break
            if ai.get("title"):
                content = re.sub(r"<[^>]+>", "", ai.get("content", ""))
                author = ((ai.get("media") or {}).get("name")) or ai.get("media_name") or ""
                head = f"标题: {ai['title']}"
                if author:
                    head += f"\n作者: {author}"
                return head + "\n\n" + content
        except Exception:
            pass
    # 兜底：整页剥标签
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", body)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def execute(args: dict) -> str:
    url = (args.get("url", "") or "").strip()
    if not url.startswith(("http://", "https://")):
        return "Error: url 参数必须是完整的 http(s) 链接"
    wait_ms = max(0, min(int(args.get("wait_ms") or 3000), 15000))
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return f"Error: playwright 库未安装。\n{_NOT_INSTALLED}"

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=_UA)
                page.goto(url, timeout=30000, wait_until="domcontentloaded")
                page.wait_for_timeout(wait_ms)
                html = page.content()
            finally:
                browser.close()
    except Exception as e:
        msg = str(e)[:300]
        if ("Executable doesn't exist" in msg or "playwright install" in msg
                or "browserType.launch" in msg and "doesn't exist" in msg):
            # 二进制缺失（系统缓存清理/目录被移走）→ 明确安装指引，
            # 不再甩生涩英文报错（09-08 17:13：模型见到原生 Playwright 报错）
            return f"Error: 无头浏览器未安装。\n{_NOT_INSTALLED}"
        return f"Error: 无头浏览器读取失败（{msg}）。可加大 wait_ms 重试，或改用 dokobot_read/tavily_search。"

    if not html or len(html) < 200:
        return "Error: 页面内容为空（可能被反爬拦截）。改用 dokobot_read 或 tavily_search。"

    text = _extract_text(html)
    if not text:
        return "Error: 页面无文本内容。改用 dokobot_read 或 tavily_search。"
    return text[:_MAX_CHARS]
