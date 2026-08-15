"""bing_search - 免 API Key 的网页搜索（抓取必应中国版 cn.bing.com）"""
import asyncio
import re
from urllib.parse import quote_plus

import httpx

NAME = "bing_search"
PERMISSION = "safe"

DEFINITION = {
    "type": "function",
    "function": {
        "name": "bing_search",
        "description": "免费网页搜索（无需 API Key）。搜索中文网络内容、新闻、行情、百科等实时信息。当 tavily_search 未配置 Key 时使用本工具。返回标题、链接和摘要。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词，尽量具体（如：今日A股大盘走势 上证指数）"
                },
                "max_results": {
                    "type": "integer",
                    "description": "最多返回结果数 (1-8)。默认 5。"
                },
            },
            "required": ["query"],
        },
    },
}

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# <li class="b_algo"> 块内: <h2><a href="URL">标题</a></h2> ... <p>摘要</p>
_ALGO_RE = re.compile(r'<li class="b_algo".*?</li>', re.S)
_H2_A_RE = re.compile(r'<h2[^>]*><a[^>]*href="([^"]+)"[^>]*>(.*?)</a></h2>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(html: str) -> str:
    text = _TAG_RE.sub("", html)
    return text.replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'").replace("&lt;", "<").replace("&gt;", ">").strip()


async def execute(args: dict) -> str:
    """Search cn.bing.com and parse results."""
    query = args.get("query", "")
    if not query:
        return "Error: query parameter is required"
    max_results = int(args.get("max_results", 5))
    if max_results < 1 or max_results > 8:
        max_results = 5

    url = f"https://cn.bing.com/search?q={quote_plus(query)}&count={max_results}&setlang=zh-CN"
    try:
        async with httpx.AsyncClient(timeout=20, headers=_HEADERS, follow_redirects=True) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return f"Error: Bing 返回 HTTP {resp.status_code}（可能被限流，稍后再试）"
            html = resp.text
    except Exception as e:
        return f"Error: 无法连接必应搜索: {e}"

    blocks = _ALGO_RE.findall(html)
    if not blocks:
        # 可能是验证页或页面结构变化
        if "验证" in html or "captcha" in html.lower():
            return "Error: Bing 触发了人机验证，稍后再试"
        return "未找到相关结果。"

    lines = [f"🌐 必应搜索: {query}\n"]
    for i, block in enumerate(blocks[:max_results], 1):
        m = _H2_A_RE.search(block)
        if not m:
            continue
        link, title = m.group(1), _clean(m.group(2))
        if not title or link.startswith("javascript:"):
            continue
        # 摘要：取 <p> 内容
        p_m = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        snippet = _clean(p_m.group(1)) if p_m else ""
        lines.append(f"{i}. {title}\n   {link}\n   {snippet}\n")

    if len(lines) == 1:
        return "未找到相关结果。"
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    asyncio.run(execute({"query": " ".join(sys.argv[1:])}))
