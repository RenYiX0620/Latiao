"""工具/计划确认流（agent_loop.py 拆出的第八块，2026-09-23）。

主循环每轮对 confirm 级工具做的事：先发 tool_confirm 事件给前端，再等待用户点
允许/拒绝（_start_/_wait_/_await_tool_confirmation）；plan 档还有一套计划确认。
配套：_confirm_bypassed（哪些档位免确认）、_count_successful_duplicates（同参重复守卫）、
_check_pre_hooks（工具前钩子拒绝）。
"""
import logging

import asyncio
import json
from agent.context import AUTO_EDIT_TOOLS, _normalize_access

logger = logging.getLogger("latiao-sidecar")   # 与 agent_loop 同名：日志格式不变

async def _start_tool_confirmation(call_id: str, tool_name: str, args: dict) -> dict:
    """启动工具确认：注册 pending，返回 {"event": 待发事件, "event_obj": 等待用}。

    ⚠️ SSE 生成器必须**先 yield 该事件、再等待结果**——事件若攒到确认完成后
    才发出，前端在等待期间收不到 tool_confirm，弹窗永不出现（死锁到超时）。
    此前 _await_tool_confirmation 正是这个结构，导致确认功能从未真正工作。"""
    event = asyncio.Event()
    async with _pending_lock:
        _pending_confirmations[call_id] = {"event": event, "approved": False}
    return {"event": {"event": "tool_confirm", "call_id": call_id, "tool": tool_name, "args": args},
            "event_obj": event}

async def _wait_tool_confirmation(call_id: str, tool_name: str,
                                  event_obj: asyncio.Event, timeout: float = 0) -> tuple[bool, list[dict]]:
    """等待已启动（_start_tool_confirmation）的确认结果。
    返回 (approved, events)——events 只含超时提示等补充事件（初始
    tool_confirm 已由调用方发出）。

    默认不限时（timeout=0）：确认由用户点击决定下一步，任务在等待期间
    保持暂停；停止/取消可中断（生成器取消 → finally 清理 pending）。
    09-21 用户反馈："确认超时 30 秒，改成不限时间，我点了再操作下一步"。
    """
    events = []
    try:
        if timeout and timeout > 0:
            await asyncio.wait_for(event_obj.wait(), timeout=timeout)
        else:
            await event_obj.wait()
        async with _pending_lock:
            approved = _pending_confirmations.get(call_id, {}).get("approved", False)
        logger.info("tool confirmation resolved: %s approved=%s", call_id, approved)
    except asyncio.TimeoutError:
        approved = False
        events.append({
            "content": (
                f"\n\n⚠️ 工具 `{tool_name}` 等待确认超时（2 分钟无人操作），"
                "任务已暂停，未执行该操作。可在界面中重新批准后继续。"
            ),
        })
    finally:
        async with _pending_lock:
            _pending_confirmations.pop(call_id, None)
    return approved, events

async def _await_tool_confirmation(call_id: str, tool_name: str, args: dict) -> tuple[bool, list[dict]]:
    """兼容入口：启动 + 等待一次性完成（仅限不经过 SSE 的内部调用）。"""
    started = await _start_tool_confirmation(call_id, tool_name, args)
    approved, events = await _wait_tool_confirmation(call_id, tool_name, started["event_obj"])
    return approved, [started["event"]] + events

def _confirm_bypassed(tool_name: str, access_mode: str) -> bool:
    """confirm 级工具是否免确认（full 档全免；auto_edit 档文件类免确认）。
    供 _handle_tool_execution 与 SSE 调用方（提前发确认事件）共用，避免判定漂移。"""
    _access = _normalize_access(access_mode)
    if _access == "full":
        return True
    if _access == "auto_edit" and tool_name in AUTO_EDIT_TOOLS:
        try:
            from main import _custom_permissions
            _has_rule = any(r.get("tool") == tool_name for r in _custom_permissions)
        except Exception:
            _has_rule = False
        return not _has_rule
    return False

async def _start_plan_confirmation(plan_id: str, plan: str) -> dict:
    """启动计划确认（同工具确认：先发事件再等待）。"""
    event = asyncio.Event()
    async with _pending_lock:
        _pending_confirmations[plan_id] = {"event": event, "approved": False}
    return {"event": {"event": "plan_confirm", "call_id": plan_id, "tool": "执行计划",
                      "args": {"plan": plan[:2000]}},
            "event_obj": event}

async def _wait_plan_confirmation(plan_id: str, event_obj: asyncio.Event,
                                  timeout: float = 0, lang: str = "zh") -> tuple[bool, list[dict]]:
    """等待计划确认结果（默认不限时，用户点击后继续；09-21 用户反馈）。

    `timeout=0` 是**有意**不限时（用户反馈：急着超时会打断他确认）。但超时分支本身
    必须能用：它原先引用 `_msg`/`lang_of`/`msgs` 三个名字，而这三个在该作用域都不存在
    → 一旦真传了 timeout 就会 NameError 把友好提示变成报错。`lang` 由调用方传入
    （原来是靠 `lang_of(msgs)` 猜，而 msgs 在这里根本没有）。
    """
    events = []
    try:
        if timeout and timeout > 0:
            await asyncio.wait_for(event_obj.wait(), timeout=timeout)
        else:
            await event_obj.wait()
        async with _pending_lock:
            approved = _pending_confirmations.get(plan_id, {}).get("approved", False)
        logger.info("plan confirmation resolved: %s approved=%s", plan_id, approved)
    except asyncio.TimeoutError:
        approved = False
        from agent.messages import msg as _msg
        events.append({
            "content": "\n\n" + _msg("plan_timeout", lang or "zh"),
        })
    finally:
        async with _pending_lock:
            _pending_confirmations.pop(plan_id, None)
    return approved, events

def _count_successful_duplicates(current_msgs: list, tool_name: str, args: dict) -> int:
    """统计同会话内相同 (tool_name, args) 的已成功执行次数（失败结果不计数，
    保留"失败→重试一次"的合法模式；14:26 事故：模型被 nudge 后反复重读
    PROGRESS.md 每轮重跑启动协议陷入循环）。"""
    try:
        _norm_args = dict(args)
        # 路径归一化：read_file 的 "~" 与绝对路径指向同一文件，
        # 不归一化时模型交替两种写法就能绕过护栏（17:10 重放实测）
        if tool_name == "read_file" and _norm_args.get("path"):
            from pathlib import Path as _P
            _norm_args["path"] = str(_P(_norm_args["path"]).expanduser())
        args_sig = json.dumps(_norm_args, ensure_ascii=False, sort_keys=True)
    except Exception:
        return 0
    call_ids: set[str] = set()
    for m in current_msgs:
        if m.get("role") != "assistant":
            continue
        for tc in (m.get("tool_calls") or []):
            f = tc.get("function", {})
            if f.get("name") != tool_name:
                continue
            try:
                _a = json.loads(f.get("arguments", "{}"))
                if tool_name == "read_file" and isinstance(_a, dict) and _a.get("path"):
                    from pathlib import Path as _P
                    _a["path"] = str(_P(_a["path"]).expanduser())
                same = (json.dumps(_a, ensure_ascii=False, sort_keys=True) == args_sig)
            except Exception:
                same = False
            if same and tc.get("id"):
                call_ids.add(tc["id"])
    ok = 0
    for m in current_msgs:
        if m.get("role") == "tool" and m.get("tool_call_id") in call_ids:
            if not str(m.get("content", "")).startswith(("Error", "⛔", "⚠️")):
                ok += 1
    return ok

def _check_pre_hooks(tool_name: str, args: dict) -> tuple[bool, list[dict], str]:
    """Run pre-tool hooks. Returns (vetoed, events, result_if_vetoed)."""
    # 依赖仍留在 agent_loop 的定义 → 函数内惰性导入（避免循环依赖）
    from agent_loop import TOOL_HOOKS
    hooks = TOOL_HOOKS.get(tool_name, {})
    pre_hook = hooks.get("pre_tool_call")
    if not pre_hook:
        return False, [], ""
    try:
        veto = pre_hook(tool_name, args)
        if veto is False:
            return True, [], f"⛔ Hook vetoed: {tool_name}"
    except Exception:
        logger.warning(f"Pre-tool hook failed for {tool_name}", exc_info=True)  # don't block execution
    return False, [], ""

# Pending confirmations: call_id → asyncio.Event (approve) or None (deny)
_pending_confirmations: dict[str, dict] = {}

_pending_lock = asyncio.Lock()
