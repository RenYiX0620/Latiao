"""子代理一等公民（对标 dsh subagent / Codex collab）。

原则：
- 子代理 = 同一 ThinAgentLoop 的受限实例：自己的 session、自己的事件流；
  delegate 的 bespoke 迷你循环已删除。
- 隔离 = 白名单（工具从目录消失且执行被拒）+ 深度预算（本期不递归委派，
  白名单恒排除 delegate_task；registry 保留 depth 字段为将来递归预留）。
- UI 契约不变：_SUBTASKS/_SUBTASK_EVENTS/前台返回文本，与旧实现逐字段兼容。
"""
import logging
import time as _time
import uuid
from contextvars import ContextVar

logger = logging.getLogger("latiao-sidecar")

# 发起委派的父会话 id（ThinAgentLoop.run() 入口设置；后台任务创建时随
# contextvars 快照带入，使后台子代理完成后能把结果通知回父会话）
_CURRENT_PARENT_SESSION: ContextVar[str] = ContextVar("subagent_parent_session", default="")

# 后台子代理完成结果待注入队列（parent_session -> [通知]）：
# 父循环运行中 → queue_steer 即时认领；父空闲 → 下次该会话循环启动时注入。
# 通知被任一路径消费后标记 delivered，两路不重复。
_PENDING_BG_RESULTS: dict[str, list[dict]] = {}
# 通知保留时长：超过就丢（父会话已删/父循环长期没再跑，远古结论不该再注入）
_BG_RESULT_TTL = 1800.0


def push_bg_result(parent_session: str, agent_type: str, task: str, result: str) -> None:
    """后台子代理完成：结果进待注入队列，父会话下次循环启动时注入上下文。

    （对齐 DSH subagent-settled / Codex subagent_notification 的语义：
    后台结果必须让父模型看见。v1 用确定性的"下次启动注入"，不做运行中
    steer——steer 依赖父循环恰好还在跑且能认领，链路脆（09-07 09:2x 实测
    丢失）。注入点：ThinAgentLoop.run() 开头的 claim_bg_results。"""
    if not parent_session:
        logger.warning("bg result 无父会话 id（contextvar 缺失），通知丢弃: %s", task[:60])
        return
    note = {
        "id": f"bg_{uuid.uuid4().hex[:8]}", "agent": agent_type,
        "task": task[:120], "result": result[:4000], "delivered": False,
        "ts": _time.time(),
    }
    _PENDING_BG_RESULTS.setdefault(parent_session, []).append(note)
    logger.info("bg result 已入待注入队列: parent=%s agent=%s notes=%d",
                parent_session, agent_type, len(_PENDING_BG_RESULTS[parent_session]))


def claim_bg_results(parent_session: str) -> list[dict]:
    """父循环启动时取走未投递的后台结果通知（一次性）。

    09-23：认领后**顺手清理过期项**——旧实现只标 delivered 永不出队，队列随
    会话数无限增长；且父会话被删/父循环崩掉后，那批"未投递"的远古结果会在
    下一次启动时被注入（模型看到几天前的子代理结论，答非所问）。
    """
    pending = _PENDING_BG_RESULTS.get(parent_session) or []
    fresh = [n for n in pending if not n["delivered"]]
    for n in fresh:
        n["delivered"] = True
    now = _time.time()
    kept = [n for n in pending if (now - float(n.get("ts") or 0)) < _BG_RESULT_TTL]
    if kept:
        _PENDING_BG_RESULTS[parent_session] = kept
    else:
        _PENDING_BG_RESULTS.pop(parent_session, None)
    return fresh

# ── 子任务注册表（前端活动栏/结果面板的数据源，契约不变）────────────
_SUBTASKS: dict[str, dict] = {}
_SUBTASK_EVENTS: list[dict] = []
_SUBTASK_SEQ = 0
_SUBTASK_TASKS: set = set()


def _prune_subtasks(max_keep: int = 40) -> None:
    cutoff = _time.time() - 3600
    for tid in [t for t, s in _SUBTASKS.items()
                if s.get("status") in ("done", "error")
                and s.get("updated_at", 0) < cutoff]:
        _SUBTASKS.pop(tid, None)
    if len(_SUBTASKS) > max_keep * 2:
        done_ids = [t for t, s in _SUBTASKS.items() if s.get("status") in ("done", "error")]
        done_ids.sort(key=lambda t: _SUBTASKS[t].get("updated_at", 0))
        for tid in done_ids[:len(done_ids) - max_keep]:
            _SUBTASKS.pop(tid, None)


# 每类子代理的工具白名单（从 tool_executor 迁入，语义不变）
_SUBAGENT_TOOLS: dict[str, list[str]] = {
    "code-reviewer": ["read_file", "list_dir", "search_files"],
    "doc-generator": ["read_file", "list_dir", "search_files", "write_file"],
    "debugger": ["read_file", "list_dir", "search_files", "run_cmd"],
    "translator": ["read_file", "list_dir", "search_files", "write_file"],
    "explore": ["read_file", "list_dir", "search_files", "run_cmd", "tavily_search"],
}

_FOCUS_PROMPT = """

你是子代理（类型：{agent_type}），只负责用户消息里这一个子任务：
- 结论优先：拿到足够信息立即给出结论式结果（关键事实/数字/文件:行号），不要复述过程；
- 范围纪律：只读与子任务直接相关的文件，不要顺带探索无关目录；
- 不做重复劳动：同一文件同一范围只读一次，读完就基于内容作答；
- 不提问、不寒暄、不写"我将会…"；工具额度有限（≤14 步），超限会被强制收口。"""

MAX_DEPTH = 2  # 深度预算（本期白名单恒排除 delegate_task，字段为递归预留）


def _registry_record(sub_id: str, agent_type: str, task: str,
                     parent_session: str, depth: int) -> None:
    """Registry-lite：live 子代理谱系（本期供观测，递归/互发预留）。"""
    _registry_records[sub_id] = {
        "agent_type": agent_type, "task": task,
        "parent_session": parent_session, "depth": depth,
        "status": "running", "started_at": _time.time(),
    }


_registry_records: dict[str, dict] = {}


def _child_whitelist(agent_type: str, tool_whitelist_from_parent: set | None) -> set:
    """子代理白名单 = profile 白名单 − confirm 级（只读 run_cmd 豁免在执行层）
    − 用户禁用工具 − delegate_task（本期不递归）。"""
    from agent_loop import TOOL_PERMISSIONS, TOOLS
    from capability_registry import list_capabilities
    allowed = set(_SUBAGENT_TOOLS.get(agent_type, ["read_file", "list_dir", "search_files"]))
    allowed.discard("delegate_task")  # 深度预算：本期子代理不递归委派
    known = {t.get("function", {}).get("name") for t in TOOLS}
    allowed &= known
    allowed = {n for n in allowed
               if TOOL_PERMISSIONS.get(n, "safe") != "confirm"
               or (n == "run_cmd" and agent_type in ("explore", "debugger"))}
    try:
        disabled = {c.get("name") for c in list_capabilities("tool") if not c.get("enabled")}
        if disabled:
            allowed -= disabled
    except Exception:
        logger.debug("capability disabled filter skipped", exc_info=True)
    if tool_whitelist_from_parent is not None:
        allowed &= set(tool_whitelist_from_parent)
    return allowed


async def run_sub_agent(agent_type: str, task: str, *, task_id: str | None = None,
                        parent_session: str = "", depth: int = 0,
                        parent_whitelist: set | None = None) -> str:
    """运行子代理：同一 ThinAgentLoop 的受限实例，返回最终文本结果。"""
    from agent_loop import AGENT_PROFILES, _last_cloud_config, _resolve_api_target
    from agent.loop import ThinAgentLoop
    from main import SUBAGENT_MODEL

    if not task.strip():
        return f"[Sub-agent: {agent_type}] 错误: 任务描述不能为空"
    cfg = AGENT_PROFILES.get(agent_type, AGENT_PROFILES.get("code-reviewer", {}))
    allowed = _child_whitelist(agent_type, parent_whitelist)

    sub_id = f"sub_{uuid.uuid4().hex[:10]}"
    _registry_record(sub_id, agent_type, task, parent_session, depth)

    messages = [
        {"role": "system", "content": (
            cfg.get("identity", "").strip()
            + _FOCUS_PROMPT.format(agent_type=agent_type)).strip()},
        {"role": "user", "content": task},
    ]
    protocol, api_url, headers, is_local = await _resolve_api_target(_last_cloud_config.get())
    if not api_url:
        return f"[Sub-agent: {agent_type}] 错误: 无法连接模型服务（请配置云端模型或启动本地 LLM）"
    import local_llm
    _cloud_cfg = _last_cloud_config.get()
    if is_local:
        sub_model = (getattr(local_llm._engine, "current_model_id", "")
                     or getattr(local_llm._engine, "current_model_name", "")
                     or SUBAGENT_MODEL)
    else:
        sub_model = (_cloud_cfg or {}).get("model") or SUBAGENT_MODEL

    sub_session = f"{parent_session or 'root'}:{sub_id}"
    child = ThinAgentLoop(messages, sub_model, api_url, headers,
                          session_id=sub_session, access_mode="subagent",  # 受限档（09-23 A3）：run_cmd 只放行只读∪构建/测试，其余 confirm 级拒绝
                          tool_whitelist=allowed, is_local=is_local)
    # 子代理收紧（09-13 实测：子代理与主循环共用宽松阈值，重复读同一文件、
    # 乱翻无关文件、40 分钟不收口）——步数 14、同参 3 轮即收口、6 个工具轮
    # 无正文即强制作答
    child.max_steps = 14
    child._same_sig_limit = 3
    child._tool_rounds_limit = 6
    parts: list[str] = []
    try:
        async for ev in child.run():
            if ev.get("event") == "tool_start" and task_id and task_id in _SUBTASKS:
                s = _SUBTASKS[task_id]
                s["steps"] = s.get("steps", 0) + 1
                tname = ev.get("tool", "")
                targs = ev.get("args") or {}
                _cat = ("终端" if tname == "run_cmd"
                        else "搜索" if tname in ("tavily_search", "web_search")
                        else "委派" if tname == "delegate_task"
                        else "文件")
                act = s.setdefault("activity", {})
                act[_cat] = act.get(_cat, 0) + 1
                brief = str(targs.get("query") or targs.get("cmd")
                            or targs.get("path") or targs.get("pattern") or "")[:60]
                s["last_activity"] = f"{tname}: {brief}" if brief else tname
                s["updated_at"] = _time.time()
            if "content" in ev:
                parts.append(str(ev["content"]))
    except Exception as e:
        logger.exception("subagent %s(%s) 执行异常", agent_type, task_id)
        return f"[Sub-agent: {agent_type}] 错误: {e}"
    finally:
        rec = _registry_records.get(sub_id)
        if rec is not None:
            rec["status"] = "done"
    final = "".join(parts).strip() or "无输出"
    return f"[Sub-agent: {agent_type}]\n{final}"


# ── 前台/后台入口（签名与注册表契约与旧实现逐字段兼容）────────────
async def _delegate_task(agent_type: str, task: str, task_id: str | None = None,
                         parent_session: str = "", depth: int = 0) -> str:
    result = await run_sub_agent(agent_type, task, task_id=task_id,
                                 parent_session=parent_session, depth=depth)
    if task_id and task_id in _SUBTASKS:
        s = _SUBTASKS[task_id]
        s["result"] = result
        # 只看首行是否带错误标记：正文引用工具报错（含"错误"二字）不应判失败
        _first = result.split("\n")[0]
        failed = _first.startswith("[Sub-agent") and ("错误" in _first or "HTTP" in _first)
        s["status"] = "error" if failed else "done"
        s["updated_at"] = _time.time()
        _SUBTASK_EVENTS.append({"id": task_id, "status": s["status"], "summary": result[:120]})
    return result


# 僵尸判定：running 但超过该秒数无活动 → 视为中断（09-13 事故：客户端断流
# 取消协程后收尾代码未执行，两条记录永远停在 running，界面无限转圈）
_STALE_RUNNING_SEC = 180


def _subtask_snapshot(session: str | None = None) -> list[dict]:
    """后台子任务列表快照（heartbeat 附带，前端活动栏渲染）。

    09-23：带 session 时只返回**该会话**派发的子任务（此前是所有会话混在一起，
    用户在 A 会话的活动栏里看到 B 会话的子代理）。注意：活动栏只是显示，
    不进模型输入，所以这是体验问题不是正确性问题。
    """
    out = []
    now = _time.time()
    _want = str(session or "").strip()
    for tid, s in _SUBTASKS.items():
        if _want and str(s.get("session") or "") != _want:
            continue
        status = s["status"]
        if status == "running" and now - float(s.get("updated_at") or 0) > _STALE_RUNNING_SEC:
            status = "stale"
            s["status"] = "stale"  # 回写：避免每轮心跳重复判定
        out.append({
            "id": tid, "agent": s["agent"], "task": s["task"][:60],
            "status": status, "steps": s["steps"],
            "activity": dict(s.get("activity") or {}),
            "last_activity": s.get("last_activity", ""),
            "started_at": s["started_at"], "updated_at": s["updated_at"],
            "summary": (s["result"] or "")[:160],
            "session": s.get("session", ""),
        })
    return out




async def _delegate_task_bg(agent_type: str, task: str, parent_session: str = "") -> str:
    """后台模式：立即返回任务 ID，子代理异步执行，进度/结果走注册表。

    parent_session 必须在派发时（父上下文内）捕获传入——不能在完成时读
    contextvar：子代理自己的 run() 会把它覆盖成子会话 id（09-07 09:40
    事故：通知投给了子会话，父模型永远看不到结果）。"""
    global _SUBTASK_SEQ
    _SUBTASK_SEQ += 1
    task_id = f"sub_{_time.strftime('%H%M%S')}_{_SUBTASK_SEQ}"
    _prune_subtasks()
    _SUBTASKS[task_id] = {
        "agent": agent_type, "task": task, "status": "running",
        "steps": 0, "result": "", "started_at": _time.time(), "updated_at": _time.time(),
        "session": str(parent_session or _CURRENT_PARENT_SESSION.get() or ""),
    }
    _SUBTASK_EVENTS.append({"id": task_id, "status": "started", "summary": task[:80]})
    try:
        from agent_loop import _spawn
        _spawn(_run_subtask_bg(task_id, agent_type, task, parent_session))
    except ImportError:
        import asyncio as _asyncio
        t = _asyncio.get_running_loop().create_task(_run_subtask_bg(task_id, agent_type, task, parent_session))
        _SUBTASK_TASKS.add(t)
        t.add_done_callback(_SUBTASK_TASKS.discard)
    return (f"[Sub-agent {agent_type} 后台任务已启动] task_id={task_id}\n"
            "主对话可继续；结果将自动出现在子智能体面板，也可用 task_id 查询。")


async def _delegate_task_fg(agent_type: str, task: str) -> str:
    """前台模式（阻塞等待结果），同样进注册表。"""
    global _SUBTASK_SEQ
    _SUBTASK_SEQ += 1
    task_id = f"sub_{_time.strftime('%H%M%S')}_{_SUBTASK_SEQ}"
    _prune_subtasks()
    _SUBTASKS[task_id] = {
        "agent": agent_type, "task": task, "status": "running",
        "steps": 0, "result": "", "started_at": _time.time(), "updated_at": _time.time(),
        "session": str(_CURRENT_PARENT_SESSION.get() or ""),
    }
    try:
        result = await _delegate_task(agent_type, task, task_id=task_id)
    except BaseException:
        # 父请求断流会取消本协程（CancelledError 不是 Exception）——必须落终态，
        # 否则注册表永远停在 running（09-13 僵尸行事故）
        s = _SUBTASKS.get(task_id)
        if s is not None and s.get("status") == "running":
            s["status"] = "error"
            s["result"] = "[Sub-agent] 已中断（父请求取消）"
            s["updated_at"] = _time.time()
            _SUBTASK_EVENTS.append({"id": task_id, "status": "error",
                                    "summary": "已中断（父请求取消）"})
        raise
    s = _SUBTASKS[task_id]
    # 只看首行是否带错误标记：正文引用工具报错（含"错误"二字）不应判失败
    _first = result.split("\n")[0]
    failed = _first.startswith("[Sub-agent") and ("错误" in _first or "HTTP" in _first)
    s["result"] = result
    s["status"] = "error" if failed else "done"
    s["updated_at"] = _time.time()
    _SUBTASK_EVENTS.append({"id": task_id, "status": s["status"], "summary": result[:120]})
    return result


async def _run_subtask_bg(task_id: str, agent_type: str, task: str, parent_session: str = ""):
    """后台跑子 agent（非阻塞主对话），事件实时进注册表。"""
    s = _SUBTASKS[task_id]
    try:
        s["status"] = "running"
        s["updated_at"] = _time.time()
        result = await _delegate_task(agent_type, task, task_id=task_id)
        s["result"] = result
        # 只看首行是否带错误标记：正文引用工具报错（含"错误"二字）不应判失败
        _first = result.split("\n")[0]
        failed = _first.startswith("[Sub-agent") and ("错误" in _first or "HTTP" in _first)
        s["status"] = "error" if failed else "done"
        s["updated_at"] = _time.time()
        _SUBTASK_EVENTS.append({"id": task_id, "status": s["status"], "summary": result[:120]})
        # 结果回流父会话（派发时捕获的 parent_session——不能用 contextvar，
        # 子代理 run() 会把它覆盖成子会话 id）
        if not failed:
            push_bg_result(parent_session, agent_type, task, result)
    except Exception as e:
        s["result"] = f"[Sub-agent: {agent_type}] 错误: {e}"
        s["status"] = "error"
        s["updated_at"] = _time.time()
