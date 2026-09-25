#!/usr/bin/env python3
"""cron_history - 查询定时任务的历史产出（跑完的报告终于能被读到）。

背景（2026-09-23 修）：每次定时任务跑完，完整 AI 产出都写进 memory 表
（type='cron_job'），但**全库没有一个读取点**——从 5 月底起 155 行"跑完即失"，
前端只显示每个任务最近一次的 60 字摘要。现在有三个读者：本工具（模型按需查）、
/v1/cron/history 端点、前端历史区。
"""
NAME = "cron_history"
PERMISSION = "safe"
DEFINITION = {
    "type": "function",
    "function": {
        "name": "cron_history",
        "description": (
            "查询定时任务（cron）的历史执行结果：什么时候跑过、任务是什么、AI 当时产出的"
            "完整报告。用于回答\"上次那个定时任务跑出什么了\"这类问题，或复盘历史报告。只读。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "job": {"type": "string", "description": "按任务描述里的关键字过滤（如\"资金\"、\"大盘\"），可选"},
                "limit": {"type": "integer", "description": "返回条数（默认 3，最大 10）"},
                "full": {"type": "boolean", "description": "是否返回完整报告（默认 false，只给摘要；需要细读时再开）"},
            },
            "required": [],
        },
    },
}


def execute(args: dict) -> str:
    try:
        limit = int(args.get("limit") or 3)
    except (TypeError, ValueError):
        limit = 3
    limit = max(1, min(limit, 10))
    job = str(args.get("job") or "").strip()
    full = bool(args.get("full"))

    import cron as _cron
    out = _cron.list_cron_history(limit=limit, job=job)
    if out.get("status") != "ok":
        return f"查询失败：{out.get('message', '未知错误')}"
    items = out.get("items") or []
    if not items:
        return ("没有找到定时任务的历史结果"
                + (f"（过滤条件：{job}）" if job else "")
                + "。可以用 /v1/cron/history 或前端定时任务页确认。")
    lines = [f"共 {out['total']} 条历史（显示最近 {len(items)} 条）："]
    for i, it in enumerate(items, 1):
        body = (it.get("result") or "").strip()
        if not full:
            body = (it.get("summary") or "")[:200]
        lines.append(f"\n{i}. [{it.get('created_at', '')[:16]}] {it.get('task', '')[:60]}\n{body}")
    if not full:
        lines.append("\n（需要某条的完整内容时，带 full=true 再查一次）")
    return "\n".join(lines)
