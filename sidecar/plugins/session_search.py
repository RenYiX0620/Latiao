"""搜索本机会话历史（session_messages 的全文索引）。

2026-10-19 新增：补上"翻旧会话"这一层——此前只有会话列表与按 id 读取，
模型无法跨会话回忆"上次我们怎么解决的 / 以前查到的结论是什么"。

索引在 sessions.py 里维护（save_session 快照重建 + 首次搜索时回填），
中文按字切分（见 sessions._fts_tokenize）；本工具只做参数收口与格式化。
"""
NAME = "session_search"
PERMISSION = "safe"

DEFINITION = {
    "type": "function",
    "function": {
        "name": "session_search",
        "description": (
            "搜索本机历史会话的聊天记录（跨会话全文检索，返回匹配片段与所在会话 ID）。"
            "用于回忆以前聊过什么、找旧结论或旧数据。只读、不联网；不含工具输出，"
            "只索引你与用户的对话正文。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "检索词（中文按字匹配；多个词用空格分隔，词之间是 AND）"},
                "limit": {"type": "integer",
                          "description": "返回条数，默认 10，最大 50"},
            },
            "required": ["query"],
        },
    },
}


def execute(args: dict) -> str:
    args = args or {}
    query = str(args.get("query") or "").strip()
    if not query:
        return "Error: query parameter is required"
    try:
        limit = int(args.get("limit") or 10)
    except (TypeError, ValueError):
        limit = 10
    try:
        import sessions as _sessions
        res = _sessions.search_sessions(query, limit)
    except Exception as e:
        return f"Error: 会话检索失败：{e}"
    if res.get("status") != "ok":
        return f"Error: 会话检索失败：{res.get('message') or '未知错误'}"
    items = res.get("results") or []
    if not items:
        return f"没有找到包含「{query}」的历史会话消息。"
    lines = [f"找到 {len(items)} 条匹配（括号里是会话 ID，需要上下文时可按 id 取会话）："]
    for i, r in enumerate(items, 1):
        name = r.get("session_name") or "（未命名会话）"
        lines.append(f"\n{i}. [{r.get('role')}] {name}（{r.get('session_id')}）"
                     f" · {r.get('created_at') or ''}\n   {r.get('snippet') or ''}")
    return "\n".join(lines)
