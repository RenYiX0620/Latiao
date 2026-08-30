#!/usr/bin/env python3
"""control_kill_process - 杀死进程（高危，按 pid 或名称）。"""
NAME = "control_kill_process"
PERMISSION = "danger"
DEFINITION = {
    "type": "function",
    "function": {
        "name": "control_kill_process",
        "description": "杀死进程（发送 SIGTERM）。按 pid（推荐）或完整命令名。⚠️ 高危操作：系统关键进程（pid≤1）与 Latiao 自身进程会被拒绝，请在用户确认后使用。",
        "parameters": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer", "description": "目标进程 pid（优先用 pid）"},
                "pattern": {
                    "type": "string",
                    "description": "或按命令名匹配（如 python3、Safari），可选"
                },
            },
            "required": [],
        },
    },
}


def execute(args: dict) -> str:
    import sys, os as _os
    _d = _os.path.dirname(_os.path.abspath(__file__))
    if _d not in sys.path: sys.path.insert(0, _d)
    import _control_common as cc
    pid = args.get("pid")
    pattern = str(args.get("pattern") or "").strip()
    if pid is not None:
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return f"❌ pid 非法: {pid!r}"
        return cc.kill_pid(pid)
    if pattern:
        return cc.kill_by_name(pattern)
    return "❌ kill 需要 pid 或 pattern 参数"
