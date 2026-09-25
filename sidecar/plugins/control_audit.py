#!/usr/bin/env python3
"""control_audit - 查询工具调用历史（结果可控可查账）。"""
NAME = "control_audit"
PERMISSION = "safe"
DEFINITION = {
    "type": "function",
    "function": {
        "name": "control_audit",
        "description": "查询本机工具调用历史（哪个工具在哪个会话被调用、参数与结果摘要），可按工具名过滤。用于任务复盘、查账、追溯之前的操作。只读。",
        "parameters": {
            "type": "object",
            "properties": {
                "tool": {"type": "string", "description": "按工具名过滤（如 write_file/control_launch），可选"},
                "limit": {"type": "integer", "description": "返回条数（默认 20，最大 50）"},
            },
            "required": [],
        },
    },
}


def execute(args: dict) -> str:
    import json
    from db import _get_db
    tool = str(args.get("tool") or "").strip()
    limit = max(1, min(50, int(args.get("limit", 20) or 20)))
    try:
        conn = _get_db()
        if tool:
            rows = conn.execute(
                "SELECT id, session_id, tool_name, args, result, created_at "
                "FROM tool_calls WHERE tool_name=? ORDER BY created_at DESC LIMIT ?",
                (tool, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, session_id, tool_name, args, result, created_at "
                "FROM tool_calls ORDER BY created_at DESC LIMIT ?", (limit,),
            ).fetchall()
        if not rows:
            return "暂无工具调用记录"
        out = [f"工具调用历史（{len(rows)} 条）:"]
        for r in rows:
            _, sess, tname, args_s, result_s, ts = r
            try:
                a = json.loads(args_s or "{}")
                a_brief = json.dumps(a, ensure_ascii=False)[:90]
            except Exception:
                a_brief = (args_s or "")[:90]
            out.append(f"[{ts}] {tname} | 会话 {str(sess)[:12]} | 参数: {a_brief}")
            if result_s:
                out.append(f"    结果: {result_s[:130]}")
        return "\n".join(out)
    except Exception as e:
        return f"查询失败: {e}"
