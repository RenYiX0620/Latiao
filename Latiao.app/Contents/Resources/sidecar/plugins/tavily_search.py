"""Search the web using Tavily Search API."""
import asyncio
import json
import os
from pathlib import Path

import httpx

NAME = "tavily_search"
PERMISSION = "safe"

DEFINITION = {
    "type": "function",
    "function": {
        "name": "tavily_search",
        "description": "Search the web for real-time information using Tavily. Use when you need current events, news, or facts beyond your training data. Returns relevant results with titles, URLs, and content summaries.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query. Be specific and use keywords."
                },
                "search_depth": {
                    "type": "string",
                    "enum": ["basic", "advanced"],
                    "description": "Search depth: 'basic' (faster, 1-2s) or 'advanced' (thorough, 5-10s). Default: basic."
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max results to return (1-10). Default: 5."
                },
            },
            "required": ["query"],
        },
    },
}

CONFIG_FILE = Path.home() / ".local-ai-os" / "config.json"


def _get_api_key() -> str | None:
    """Read Tavily API key from config file or environment variable."""
    env_key = os.environ.get("TAVILY_API_KEY")
    if env_key:
        return env_key
    try:
        if CONFIG_FILE.exists():
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return cfg.get("tavily_api_key")
    except Exception:
        pass
    return None


async def execute(args: dict) -> str:
    api_key = _get_api_key()
    if not api_key:
        return (
            "⚠️ Tavily API Key 未配置。\n"
            "请在应用的「技能」界面中找到 Web Search (Tavily)，填写 API Key。\n"
            "免费注册：https://tavily.com"
        )

    query = args["query"]
    search_depth = args.get("search_depth", "basic")
    max_results = min(args.get("max_results", 5), 10)

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30)) as client:
            resp = await client.post(
                os.environ.get("TAVILY_API_URL", "https://api.tavily.com/search"),
                json={
                    "api_key": api_key,
                    "query": query,
                    "search_depth": search_depth,
                    "max_results": max_results,
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()

            results = data.get("results", [])
            answer = data.get("answer", "")

            if not results and not answer:
                return f"🔍 Tavily 搜索: {query}\n\n未找到相关结果。"

            lines = [f"🔍 Tavily 搜索: {query}\n"]

            if answer:
                lines.append(f"📝 {answer}\n")

            if results:
                lines.append(f"📎 共 {len(results)} 条结果:\n")
                for i, r in enumerate(results, 1):
                    title = r.get("title", "No title")
                    url = r.get("url", "")
                    content = r.get("content", "")
                    if len(content) > 300:
                        content = content[:300] + "..."
                    lines.append(f"{i}. **{title}**")
                    lines.append(f"   {url}")
                    lines.append(f"   {content}\n")

            return "\n".join(lines)

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            return "⚠️ Tavily API Key 无效或已过期。请在技能设置中更新 API Key。"
        return f"⚠️ Tavily 搜索失败: HTTP {e.response.status_code}"
    except httpx.ConnectError:
        return "⚠️ 无法连接 Tavily API (api.tavily.com)。请检查网络连接。"
    except Exception as e:
        return f"⚠️ Tavily 搜索异常: {e}"
