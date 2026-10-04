"""任务清单（todo 面板，2026-10-04）——主循环的进度可见性。

对照结论（ZCode 的 TodoWrite / Codex 的 update_plan / Claude Code 的 TodoWrite）：
模型用工具维护一份结构化清单，界面实时显示 N/M 进度。本模块只做"存储 + 快照"
（照 subagent registry-lite 的模式），传输复用 /v1/heartbeat 的 5s 轮询通道——
不新增推送机制。
"""
from __future__ import annotations

import re
import time

# 会话级清单：session_id → {"items": [...], "updated_at": ts}
_TODOS: dict[str, dict] = {}
_MAX_SESSIONS = 32      # 只保留最近 N 个会话的清单（防长跑进程内存无界）
_MAX_ITEMS = 12         # 条数上限：清单是给用户扫的，不是文档
_MAX_TEXT = 80          # 单条长度上限

_VALID_STATUS = ("pending", "in_progress", "completed")


def _norm_items(raw: list) -> list[dict]:
    out: list[dict] = []
    seen_in_progress = False
    for it in raw or []:
        if not isinstance(it, dict):
            continue
        text = re.sub(r"\s+", " ", str(it.get("step") or it.get("text") or "")).strip()
        if not text:
            continue
        status = str(it.get("status") or "pending").strip().lower()
        if status not in _VALID_STATUS:
            status = "pending"
        # 最多一个 in_progress（模型偶尔标多个——保留第一个，其余回落 pending；
        # 与 CC/Codex 的"同一时间只有一个进行中"纪律一致）
        if status == "in_progress":
            if seen_in_progress:
                status = "pending"
            seen_in_progress = True
        out.append({"step": text[:_MAX_TEXT], "status": status})
        if len(out) >= _MAX_ITEMS:
            break
    return out


def update(session_id: str, raw_items: list) -> str:
    """工具入口：写入/覆盖本会话的清单。返回给模型的一行确认。"""
    sid = str(session_id or "root")
    items = _norm_items(raw_items)
    if not items:
        return "错误: todos 为空——至少给一条 step（含 status: pending/in_progress/completed）"
    _TODOS[sid] = {"items": items, "updated_at": time.time()}
    if len(_TODOS) > _MAX_SESSIONS:
        oldest = sorted(_TODOS, key=lambda k: _TODOS[k]["updated_at"])[0]
        _TODOS.pop(oldest, None)
    done = sum(1 for i in items if i["status"] == "completed")
    return f"清单已更新（{done}/{len(items)} 完成）"


def snapshot(session_id: str | None = None) -> list[dict]:
    """心跳快照：只回指定会话的清单（空 session 返回空——清单是会话私有的）。"""
    sid = str(session_id or "")
    if not sid:
        return []
    s = _TODOS.get(sid)
    return [dict(it) for it in s["items"]] if s else []


def clear(session_id: str) -> None:
    _TODOS.pop(str(session_id or "root"), None)
