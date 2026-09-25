"""Search the web using Tavily Search API."""
import json
import os
import subprocess
import sys as _sys
from pathlib import Path

import httpx

# 插件目录加入 sys.path：execute() 里懒加载兄弟模块 _time_guard 用
_sys.path.insert(0, str(Path(__file__).parent))

NAME = "tavily_search"
PERMISSION = "safe"

DEFINITION = {
    "type": "function",
    "function": {
        "name": "tavily_search",
        "description": "首选网络搜索工具（Tavily API，已配置 Key）。需要实时信息、最新新闻、当前事实、或超出训练数据的内容时优先使用本工具。返回标题、URL、内容摘要、发布时间和 AI 摘要。同一任务只调用一次搜索即可，不要与 web_search/bing_search 并行调用。⚠️ 问“今天/最新/本周/盘前/收盘”这类**时效性问题**时，务必带 topic=\"news\"（或让工具自动判定），否则可能返回几个月前的旧文章，数字会严重失真。",
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
                "topic": {
                    "type": "string",
                    "enum": ["general", "news"],
                    "description": "检索类型：'news' 只返回**近期新闻**（配合 days 用，时效问题必须选它）；'general' 为通用检索。不填时由工具按问题里的时间词自动判定。"
                },
                "days": {
                    "type": "integer",
                    "description": "topic='news' 时回溯的天数（1-30，默认 7）。问“今天/昨日”用 1-3，问“本周”用 7。"
                },
            },
            "required": ["query"],
        },
    },
}

CONFIG_FILE = Path.home() / ".local-ai-os" / "config.json"

# 时效性问法：命中就自动切到 news 检索（09-20 事故：问“周五大盘”却拿到三个月前的旧新闻）
_RECENT_HINTS = (
    "今天", "今日", "最新", "昨日", "昨天", "昨晚", "本周", "这周", "上周", "盘前", "盘后",
    "收盘", "刚刚", "现在", "目前", "实时", "最新消息",
    "today", "latest", "yesterday", "tonight", "this week", "last week", "close", "closing",
    "now", "current", "breaking",
)


def _needs_recent(query: str) -> bool:
    q = (query or "").lower()
    return any(h in q for h in _RECENT_HINTS)


def _get_api_key() -> str | None:
    """Read Tavily API key from config file or environment variable."""
    env_key = os.environ.get("TAVILY_API_KEY")
    if env_key:
        return env_key
    # Try macOS Keychain via security CLI
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "com.latiao.desktop", "-a", "tavily_api_key", "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
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

    query = str(args.get("query") or args.get("q") or "")
    search_depth = args.get("search_depth", "basic")
    # 模型常给 "high"/"deep" 等非法值 → 映射为 advanced，避免 HTTP 400 整轮失败
    if search_depth not in ("basic", "advanced"):
        search_depth = "advanced"
    try:
        n = int(args.get("max_results", 5))
    except (TypeError, ValueError):
        n = 5
    max_results = max(1, min(n, 10))

    # 时效判定：显式 topic 优先，否则按问题里的时间词自动选（09-20 修复）
    topic = str(args.get("topic") or "").strip().lower()
    if topic not in ("general", "news"):
        topic = "news" if _needs_recent(query) else "general"
    days = 0
    if topic == "news":
        try:
            days = int(args.get("days") or 0)
        except (TypeError, ValueError):
            days = 0
        if days <= 0:
            days = 3 if any(h in (query or "").lower()
                            for h in ("今天", "今日", "昨天", "昨日", "刚刚", "today", "latest")) else 7
        days = max(1, min(days, 30))

    payload = {
        "api_key": api_key,
        "query": query,
        "search_depth": search_depth,
        "max_results": max_results,
        "topic": topic,
    }
    if topic == "news":
        payload["days"] = days

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30)) as client:
            resp = await client.post(
                os.environ.get("TAVILY_API_URL", "https://api.tavily.com/search"),
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()

            results = data.get("results", [])
            answer = data.get("answer", "")

            if not results and not answer:
                return f"🔍 Tavily 搜索: {query}\n\n未找到相关结果。"

            _scope = f"（新闻检索·近 {days} 天）" if topic == "news" else ""
            lines = [f"🔍 Tavily 搜索: {query} {_scope}\n".rstrip() + "\n"]

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
                    pub = r.get("published_date") or r.get("published") or ""
                    lines.append(f"{i}. **{title}**")
                    if pub:
                        lines.append(f"   🗓 发布时间: {pub}")
                    lines.append(f"   {url}")
                    lines.append(f"   {content}\n")

            out = "\n".join(lines)
            # 数据时效提醒（09-20）：网页正文里的数字往往不是目标日期的，
            # 弱模型会直接照抄 → 必须明示"先核对发布日期"
            if topic == "news":
                out += (f"\n\n⚠️ 以上是**近 {days} 天**的新闻检索结果。引用其中任何数字前，"
                        "先核对该条的**发布时间**与你需要的日期是否一致；不一致或找不到目标日期时，"
                        "必须写明「未获取到该日数据」，不要用近似数字替代。")
            # 搜索词日期滑差自检：query 带旧日期时提示核对相对时间换算（防锚定）
            from _time_guard import check_query_date
            guard = check_query_date(query)
            if guard:
                out += "\n\n" + guard
            return out

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            return "⚠️ Tavily API Key 无效或已过期。请在技能设置中更新 API Key。"
        return f"⚠️ Tavily 搜索失败: HTTP {e.response.status_code}"
    except httpx.ConnectError:
        return "⚠️ 无法连接 Tavily API (api.tavily.com)。请检查网络连接。"
    except Exception as e:
        return f"⚠️ Tavily 搜索异常: {e}"
