"""工具执行（agent_loop.py 拆出的最后一块，2026-09-23）。

主循环每次拿到 tool_calls 后做的事：先过确认闸门（agent/confirm.py）与子代理
受限闸门（agent/context.py），再分发执行（原生 dispatch / 委派 / 技能），
最后记录（进度、DB、事件）并做自动验证（agent/verify.py）。

这一块与枢纽的"注册表"（TOOLS/TOOL_DISPATCH/TOOL_PERMISSIONS/TOOL_HOOKS）仍
通过惰性导入联系——注册表由 main.py 门面在启动时装配，属于宿主职责，不从本模块
反向 import（会造出回环）。
"""
import logging

import asyncio
import inspect
import json
import time
import uuid
from agent.confirm import _await_tool_confirmation, _check_pre_hooks, _count_successful_duplicates, _wait_tool_confirmation
from agent.context import AUTO_EDIT_TOOLS, _candidate_tool_names, _check_access, _normalize_access, _recover_tool_name, subagent_tool_gate
from agent.progress import _record_progress
from agent.session_events import _event_log_for
from agent.verify import _auto_verify
from cmd_safety import redact_secrets, tool_log_preview
from datetime import datetime
from db import _db_write_lock, _get_db
from memory import _maybe_generate_skill, _quick_reflect, _record_reflection, _refine_learnings
from threat_scan import guard_tool_result
from tool_executor import _resolve_permission

logger = logging.getLogger("latiao-sidecar")   # 与 agent_loop 同名：日志格式不变

async def execute_tool(tool_name: str, arguments: dict) -> str:
    """Execute a tool with feedback verification. Supports both sync and async tool functions."""
    # 依赖仍留在 agent_loop 的定义 → 函数内惰性导入（避免循环依赖）
    from agent_loop import TOOL_DISPATCH
    fn = TOOL_DISPATCH.get(tool_name)
    if not fn:
        return f"Error: Unknown tool '{tool_name}'"
    try:
        # inspect 代替 asyncio.iscoroutinefunction：后者 3.16 移除（弃用告警）
        if inspect.iscoroutinefunction(fn):
            result = await fn(arguments)
        else:
            # 同步工具函数（如 mx_query 内含 120s subprocess）放到线程执行，
            # 避免阻塞事件循环
            result = await asyncio.to_thread(fn, arguments)
        if asyncio.iscoroutine(result):
            # 兼容：同步包装（lambda 等）返回 coroutine 的情况
            result = await result
    except KeyError as e:
        return f"Error: Missing required argument {e} for tool '{tool_name}'"
    except Exception as e:
        return f"Error executing {tool_name}: {e}"

    # ── 统一能力计数：工具与技能（use_skill）共用 capabilities 表 ──
    try:
        import capability_registry
        capability_registry.bump_usage(tool_name)
        if tool_name == "use_skill":
            capability_registry.bump_usage(str(arguments.get("skill_name") or ""))
    except Exception:
        logger.debug("bump_usage failed for %s", tool_name, exc_info=True)

    # ── Feedback subsystem: post-execution verification ──
    if tool_name == "write_file":
        path = arguments.get("path", "")
        expected = arguments.get("content", "")
        try:
            with open(path, "r", encoding="utf-8") as f:
                actual = f.read()
            if actual == expected:
                result += "\n✅ Verified: file content matches exactly."
            else:
                result += f"\n⚠️ Verification: content mismatch (expected {len(expected)} chars, got {len(actual)} chars)."
        except Exception as e:
            result += f"\n⚠️ Verification failed: could not read back file ({e})."
    elif tool_name == "run_cmd":
        # Exit code already captured; add explicit pass/fail
        if "(退出码: 0)" in result or "退出码" not in result:
            if "Error" not in result and "错误" not in result:
                result += "\n✅ Exit code: 0 (success)"

    return result

async def _handle_tool_execution(tc: dict, current_msgs: list, session_id: str,
                                 agent_id: str, access_mode: str = "confirm",
                                 pre_started: dict | None = None) -> tuple[bool, list[dict]]:
    """事件日志包装（阶段 1，灰度）：tool/call + tool/result 单点入日志。

    _handle_tool_execution_inner 是全循环唯一的工具执行入口（云/本地两个
    生成器都在此交汇），在这里配对其"调用-结果"事件最不容易漏——
    早期/拒绝/异常四条早退路径都经过同一包装。
    """
    # 依赖仍留在 agent_loop 的定义 → 函数内惰性导入（避免循环依赖）
    from agent_loop import _tool_end_result
    from agent_loop import _tool_timeout_for
    log = _event_log_for(session_id) if session_id else None
    call_seq: int | None = None
    if log is not None:
        try:
            _func = tc.get("function", {}) or {}
            call_seq = log.append(
                "tool/call",
                {
                    "call_id": tc.get("id") or "",
                    "name": _func.get("name", "unknown"),
                    "arguments": _func.get("arguments", ""),
                },
            ).seq
        except Exception:
            logger.warning("failed to log tool/call for %s", session_id, exc_info=True)
    _tname = ((tc.get("function") or {}).get("name") or "") or "unknown"
    _tlimit = _tool_timeout_for(_tname)
    try:
        verify_failed, events = await asyncio.wait_for(
            _handle_tool_execution_inner(
                tc, current_msgs, session_id, agent_id, access_mode, pre_started),
            timeout=_tlimit,
        )
    except asyncio.TimeoutError:
        _call_id = tc.get("id") or ""
        logger.warning("工具执行超时：%s 超过 %.0fs（已中止并回报模型）", _tname, _tlimit)
        from agent.messages import lang_of, msg as _msg
        result = _msg("tool_timeout", lang_of(current_msgs),
                      name=_tname, limit=_tlimit)
        verify_failed, events = False, [{"event": "tool_end", "call_id": _call_id,
                                         "tool": _tname, "result": result,
                                         "ts": int(time.time() * 1000)}]
    if log is not None and call_seq is not None:
        try:
            log.append(
                "tool/result",
                {"result": _tool_end_result(events)},
                surface_op="append",
                source_seqs=[call_seq],
            )
        except Exception:
            logger.warning("failed to log tool/result for %s", session_id, exc_info=True)
    return verify_failed, events

async def _handle_tool_execution_inner(tc: dict, current_msgs: list, session_id: str,
                                       agent_id: str, access_mode: str = "confirm",
                                       pre_started: dict | None = None) -> tuple[bool, list[dict]]:
    """Execute a single tool call within the agent loop. Returns (verify_failed, events).

    pre_started: SSE 调用方已通过 _start_tool_confirmation 启动确认并发出
    tool_confirm 事件时传入（含 event_obj）——本函数只等待结果，不再重复发事件。
    确认事件若在等待完成后才发出，前端弹窗永不出现（死锁）。"""
    # 依赖仍留在 agent_loop 的定义 → 函数内惰性导入（避免循环依赖）
    from agent_loop import _TIME_SENSITIVE_TOOLS
    from agent_loop import _REPEAT_ALLOWED_TOOLS
    from agent_loop import TOOL_HOOKS
    from agent_loop import _spawn
    call_id = tc.get("id") or str(uuid.uuid4())
    func = tc.get("function", {})
    tool_name = func.get("name", "unknown") or ""
    # 空名守卫（17:23 事故根治）：云端路径对 delta 工具名无过滤，模型流式
    # 输出 name=""（分片/格式问题）会被原样执行 → 模型只见 "Unknown tool ''"
    # 反复自我谴责死循环（8 连调）。这里在执行前拦截并回馈可操作的格式提示，
    # 让模型下一轮直接修正格式而不是猜。
    if not tool_name.strip():
        # 空名恢复（09-21 实测：deepseek-v4 名称字段为空串但参数完整）：
        # 先解析参数，若与唯一工具 schema 匹配则按推断名继续执行，否则走守卫提示。
        _recovered = ""
        try:
            _args = json.loads(func.get("arguments", "{}") or "{}")
            _recovered = _recover_tool_name(_args)
        except Exception:
            _args = {}
        if _recovered:
            logger.warning("空工具名恢复: 参数匹配 %s", _recovered)
            tool_name = _recovered
        else:
            _cands = _candidate_tool_names(_args if _args else {})
            _hint = ""
            if len(_cands) >= 2:
                _desc = {"read_file": "读文件", "list_dir": "列目录", "open_folder": "打开文件夹",
                         "write_file": "写文件", "mx_query": "查行情", "tavily_search": "联网搜索",
                         "web_search": "联网搜索", "bing_search": "联网搜索",
                         "dokobot_search": "搜索", "ak_finance": "金融数据", "run_cmd": "运行命令"}
                _hint = "。参数匹配多个工具（" + ", ".join(
                    f"{c}({_desc.get(c, c)})" for c in _cands) + "）——请明确工具名"
            result = (
                "⛔ 工具调用格式错误：工具名为空。请直接以 ```tool 工具名\n{参数JSON}\n``` "
                "形式调用（工具名后不要有空格/换行/标签），例如：\n"
                "```tool list_dir\n{\"path\": \".\"}\n```" + _hint
            )
            current_msgs.append({"role": "tool", "tool_call_id": call_id, "content": result})
            return True, [{"event": "tool_end", "call_id": call_id, "tool": "?", "result": result,
                           "ts": int(time.time() * 1000)}]
    # 权限模式拦截：read_only/workspace 下越权工具直接拒绝（不执行）
    denied = _check_access(tool_name, access_mode)
    if denied:
        current_msgs.append({"role": "tool", "tool_call_id": call_id, "content": denied})
        return True, [{"event": "tool_end", "call_id": call_id, "tool": tool_name, "result": denied, "ts": int(time.time() * 1000)}]
    try:
        args = json.loads(func.get("arguments", "{}"))
    except json.JSONDecodeError:
        # 工具参数 JSON 不完整（通常是 reasoning 模型 <think> 吃满 max_tokens，
        # trailing tool_call JSON 被截断）。绝不静默退化为空参数执行——回灌明确
        # 错误，让模型看到"我的 JSON 断了"，从而重新发起完整调用。
        raw_args = func.get("arguments", "")
        result = (
            f"⛔ 工具参数 JSON 不完整，解析失败：{raw_args[:200]}\n"
            "通常因回复达到 max_tokens 被截断。请重新调用该工具，保证参数 JSON 完整闭合。"
        )
        current_msgs.append({"role": "tool", "tool_call_id": call_id, "content": result})
        return True, [{"event": "tool_end", "call_id": call_id, "tool": tool_name, "result": result, "ts": int(time.time() * 1000)}]

    # ── 相同调用防重复（14:26 事故：模型被 nudge 后每轮重跑启动协议、
    # 反复重读 PROGRESS.md 陷入循环；今早还有同 3 条搜索词 35 连搜）──
    # 同会话内相同 (tool, args) 已成功 ≥2 次 → 不再执行，返回引导进入下一步。
    _dup_ok = _count_successful_duplicates(current_msgs, tool_name, args)
    if _dup_ok >= 2 and tool_name not in _REPEAT_ALLOWED_TOOLS:
        result = (
            f"⛔ 该调用与此前已成功执行的调用完全相同（{tool_name}，相同参数已成功 {_dup_ok} 次），"
            "已拒绝重复执行——其结果已在上方历史中。\n"
            "请直接基于已收集的数据写出完整分析（简体中文，含关键数字与结论）；"
            "或改用其他工具/其他参数补充数据。不要再次发起相同调用。"
        )
        current_msgs.append({"role": "tool", "tool_call_id": call_id, "content": result})
        return False, [{"event": "tool_end", "call_id": call_id, "tool": tool_name, "result": result, "ts": int(time.time() * 1000)}]

    # ── 子代理执行层闸门（09-23 审计 A3，必须在权限规则之前）──
    # 子代理（access_mode="subagent"）没有确认通道：run_cmd 只放行只读 ∪ 构建/测试
    # 白名单，其余 confirm 级工具一律拒绝。位置必须在 _resolve_permission 之前——
    # 用户在 permissions.json 里把 run_cmd/write_file 降成 safe 的规则不能重新打开
    # 这条路径（否则子代理又变成免确认任意执行）。
    # 判定为放行的命令在下面**跳过确认**（_sub_gate_checked）：闸门已按白名单全权
    # 判定，再进确认流程就是等一个没人能点的确认（子流事件不转发给前端）直到超时
    # ——首版没跳过，实测"只读命令"直接挂死。
    _sub_gate_checked = False
    if _normalize_access(access_mode) == "subagent":
        try:
            _sub_deny = subagent_tool_gate(tool_name, args)
        except Exception:
            logger.warning("子代理闸门判定异常，按拒绝处理", exc_info=True)
            _sub_deny = "⛔ 子代理权限判定异常，已拒绝执行（fail-closed）"
        if _sub_deny:
            result = _sub_deny
            current_msgs.append({"role": "tool", "tool_call_id": call_id, "content": result})
            return False, [{"event": "tool_end", "call_id": call_id, "tool": tool_name,
                            "result": result, "ts": int(time.time() * 1000)}]
        _sub_gate_checked = True

    # ── 权限规则拒绝（deny/danger）──
    # 自定义权限规则返回 danger/deny 时必须拦截，此前落空直接执行——
    # 权限语义严重不一致（实测 list_dir 设 danger 仍读到目录）
    _perm_level = _resolve_permission(tool_name, args)
    if _perm_level in ("deny", "danger", "blocked"):
        result = (
            f"⛔ 权限规则拒绝执行: {tool_name}（级别: {_perm_level}）。"
            "如需执行，请在设置中调整该工具的权限规则后重试。"
        )
        current_msgs.append({"role": "tool", "tool_call_id": call_id, "content": result})
        return True, [{"event": "tool_end", "call_id": call_id, "tool": tool_name, "result": result, "ts": int(time.time() * 1000)}]

    # ── User confirmation ──
    _access = _normalize_access(access_mode)
    _auto_edit_bypass = False
    if _access == "auto_edit" and tool_name in AUTO_EDIT_TOOLS:
        # 自动编辑档：文件类免确认，除非 permissions.json 有显式规则（规则优先）
        try:
            from main import _custom_permissions
            _has_rule = any(r.get("tool") == tool_name for r in _custom_permissions)
        except Exception:
            _has_rule = False
        _auto_edit_bypass = not _has_rule
    # full（完全访问）档：confirm 级工具免确认直接执行——此前 5 档中
    # confirm/plan/full 三档无门控、与默认档完全等价（审计 A2）。
    # danger/deny 规则拦截仍在上方生效，不受此豁免影响。
    _full_bypass = (_access == "full") or _sub_gate_checked
    # 事件列表必须先初始化：confirm 分支的 pre_started 路径（当前两个 SSE
    # 循环的唯一调用方式）此前从未绑定 events 就 extend → UnboundLocalError
    # 整个任务崩溃（审计 P0：每次确认弹窗路径必炸）
    events: list = []
    if _perm_level == "confirm" and not _auto_edit_bypass and not _full_bypass:
        if pre_started is not None and pre_started.get("event_obj") is not None:
            # 事件已由 SSE 调用方提前发出（死锁修复），这里只等待结果
            approved, extra = await _wait_tool_confirmation(call_id, tool_name, pre_started["event_obj"])
            events.extend(extra)
        else:
            approved, events = await _await_tool_confirmation(call_id, tool_name, args)
        if not approved:
            result = f"⛔ User denied this operation: {tool_name}"
            events.append({"event": "tool_end", "call_id": call_id, "tool": tool_name, "result": result, "ts": int(time.time() * 1000)})
            current_msgs.append({"role": "tool", "tool_call_id": call_id, "content": result})
            return True, events
    else:
        events = []

    # ── Pre-tool hooks ──
    vetoed, hook_events, veto_msg = _check_pre_hooks(tool_name, args)
    events.extend(hook_events)
    if vetoed:
        events.append({"event": "tool_end", "call_id": call_id, "tool": tool_name, "result": veto_msg, "ts": int(time.time() * 1000)})
        current_msgs.append({"role": "tool", "tool_call_id": call_id, "content": veto_msg})
        return True, events

    # ── Execute + Post-hooks ──
    events.append({"event": "tool_start", "call_id": call_id, "tool": tool_name, "args": args, "ts": int(time.time() * 1000)})
    # 参数预览也要脱敏：run_cmd 的 token、api key 常出现在参数里（09-23）
    logger.info("Tool executing: %s %s", tool_name,
                redact_secrets(json.dumps(args, ensure_ascii=False))[:120])
    result = await execute_tool(tool_name, args)
    # 结果预览脱敏：read_file 这类内容工具只记长度（真机实测原样落盘过 API key）
    logger.info("Tool result: %s → %s", tool_name, tool_log_preview(tool_name, result))

    post_hook = TOOL_HOOKS.get(tool_name, {}).get("post_tool_call")
    if post_hook:
        try:
            result = post_hook(tool_name, args, result)
        except Exception:
            logger.warning("Post-tool hook failed", exc_info=True)

    events.append({"event": "tool_end", "call_id": call_id, "tool": tool_name, "result": result, "ts": int(time.time() * 1000)})

    # ── State tracking + Verification + Reflection ──
    # 进度文件同样脱敏（它会按会话注入回提示词，密钥不该进去）
    _record_progress(f"**{tool_name}**\nArgs: `{redact_secrets(json.dumps(args, ensure_ascii=False))}`"
                     f"\nResult: {redact_secrets(result)[:200]}",
                     session_id=session_id)
    _record_tool_call_db(session_id, tool_name, args, redact_secrets(result))

    # Self-evolution: background-refine learning + auto-skill generation
    _spawn(_refine_learnings(tool_name, args, result, session_id))
    _spawn(_maybe_generate_skill(tool_name, args, result))

    verify_report = await _auto_verify(tool_name, args, result)
    verify_failed = bool(verify_report and "❌" in verify_report)
    result_lower = result.lower()
    if not verify_failed and (
        result.startswith("Error") or result.startswith("错误") or
        result.startswith("⛔") or "permission denied" in result_lower or
        "权限不足" in result or "不存在" in result or "未找到" in result
    ):
        verify_failed = True

    reflection_note = _quick_reflect(tool_name, result)
    if reflection_note:
        _record_reflection(session_id, tool_name, args, result[:200], reflection_note, True)

    tool_content = result
    # Inject reflection into conversation context so LLM benefits immediately
    if reflection_note:
        tool_content += "\n\n🔍 反思: " + reflection_note
    if verify_report:
        tool_content = f"{result}\n{verify_report}"
        if verify_failed:
            tool_content += (
                "\n\n⚠️ **验证失败！你必须立即修复以上 ❌ 项。**\n"
                "不要跳过，不要宣布完成，不要做其他事情。\n"
                "修复后重新执行相同工具，直到所有检查项变为 ✅。"
            )
    elif reflection_note:
        tool_content = f"{result}\n\n[Self-Reflection: {reflection_note}]"

    # 截断过长的工具结果:本地模型上下文有限(8K-32K tokens),
    # 39KB 的 raw.json 全塞进去会导致输入超长 -> 空响应。
    # 保留前 3000 字符(够模型理解数据结构)+ 提示完整数据已保存。
    MAX_TOOL_RESULT = 3000
    if len(tool_content) > MAX_TOOL_RESULT:
        # 保留首 2000 + 尾 800：尾部常含关键结论/错误信息（P2-16）
        tool_content = (
            tool_content[:2000]
            + f"\n\n... (中间已省略。完整结果 {len(result)} 字符已记录,"
            + "如需查看特定部分请用 read_file 分段读取对应文件。)\n\n"
            + tool_content[-800:]
        )
    # 不可信内容防护（09-21）：工具结果是外部内容进入上下文的主要入口（网页正文、
    # 搜索结果、接口返回），而工具集里有 shell / write_file。命中疑似注入句式时只加
    # 一条"这是数据、不是指令"的标注、**不删数据**（删了用户就看不懂搜索结果）；
    # 未命中时原样返回，零改动零开销。这里是全循环唯一的工具结果落库点。
    tool_content = guard_tool_result(tool_name, tool_content)
    current_msgs.append({"role": "tool", "tool_call_id": call_id,
                         "content": (_stamp_time_sensitive() + tool_content
                                     if tool_name in _TIME_SENSITIVE_TOOLS else tool_content)})
    # 启动协议防循环（17:21 事故）：read_file 成功读取 PROGRESS.md 且本会话
    # 尚未注入过该提示时，追加"协议已完成"——阻止模型每轮重跑启动协议、
    # 反复重读同一文件（本地循环由 _merge_system_messages 合并进首条 system）
    if (tool_name == "read_file"
            and not str(result).startswith(("Error", "⛔", "⚠️"))
            and str(args.get("path", "")).endswith("PROGRESS.md")
            and not any("启动协议已完成" in str(m.get("content", "")) for m in current_msgs)):
        current_msgs.append({"role": "system", "content":
            "✅ 启动协议已完成：你已了解最近工作记录（见上方摘要）。"
            "现在直接执行用户的任务（例如用 mx_query 查询行情数据），"
            "不要再读取 PROGRESS.md。"})
    return verify_failed, events

def _record_tool_call_db(session_id: str, tool_name: str, args: dict, result: str):
    """Write a tool call record to SQLite memory."""
    try:
        conn = _get_db()
        call_id = str(uuid.uuid4())
        with _db_write_lock:
            conn.execute(
                "INSERT INTO tool_calls(id, session_id, tool_name, args, result, created_at) VALUES(?, ?, ?, ?, ?, ?)",
                (call_id, session_id, tool_name, json.dumps(args, ensure_ascii=False), result, datetime.now().isoformat()),
            )
            conn.commit()
    except Exception:
        logger.warning("Failed to record tool call in DB", exc_info=True)

def _dispatch_delegate(args: dict):
    """delegate_task 分发：background=true 时以后台子任务运行（不阻塞主对话）。
    前台模式也进注册表——活动栏实时可见步数/活动摘要，与后台一致。
    parent_session 在派发时从 contextvar 捕获（此处运行在父会话上下文内，
    子代理 run() 不会覆盖它——后台完成通知靠它回寻父会话）。"""
    agent = args.get("agent", "code-reviewer")
    task = args.get("task", "")
    from agent.subagent import _CURRENT_PARENT_SESSION, _delegate_task_bg, _delegate_task_fg
    parent_session = _CURRENT_PARENT_SESSION.get()
    if args.get("background"):
        return _delegate_task_bg(agent, task, parent_session=parent_session)
    return _delegate_task_fg(agent, task)

def _dispatch_use_skill(args: dict) -> str:
    """use_skill 分发：从 registry 取技能全文。未启用/不存在时返回可用目录。"""
    import capability_registry
    name = str(args.get("skill_name") or "").strip()
    if not name:
        return "错误: 缺少 skill_name 参数"
    skill = capability_registry.get_skill_content(name)
    if skill is None:
        catalog = capability_registry.skill_catalog()
        names = "、".join(s["name"] for s in catalog) or "(空)"
        return f"技能 {name!r} 不存在或已禁用。当前可用技能: {names}"
    return (
        f"# 技能: {skill['name']}\n"
        f"描述: {skill['description'] or '(无)'}\n"
        f"安全等级: {skill['permission']}\n\n"
        f"{skill['content']}"
    )

def _stamp_time_sensitive() -> str:
    """生成当前时刻锚行，注入时间敏感工具结果头部（截断后追加，不会被截掉）。

    09-21 修措辞：原来写「[数据时刻] 当前时间」——两者并不等价（盘中查、收盘查、
    隔夜再看同一份结果，数字含义完全不同），实测用户看到"上午涨 0.89%、下午又变
    1.2%"的分歧就是这么来的。现在明确：这是**发起查询**的时刻，数据自身时点看结果
    里的 date/时间列，并要求回答时原样引用。
    """
    # 依赖仍留在 agent_loop 的定义 → 函数内惰性导入（避免循环依赖）
    from agent_loop import _WEEK_ZH
    now = datetime.now()
    return (f"⏱ [查询时刻] {now.strftime('%Y-%m-%d')} (周{_WEEK_ZH[now.weekday()]}) "
            f"{now.strftime('%H:%M:%S')} —— 这是**发起查询**的时刻，不等于数据本身的时点；"
            f"数据时点以结果里的 date/时间列为准，回答时必须原样引用（结果内日期若与此矛盾，"
            f"以当前时间为准）\n\n")

def _get_agent_tools(agent_id: str, all_tools: list[dict]) -> list[dict]:
    """Filter tools based on agent's allowed tools. 'all' means all tools.

    同时过滤 Tools 页被禁用的工具（capabilities.enabled=0）——此前禁用
    开关只写库、agent 管线从不读，禁用 run_cmd 后模型照常执行
    （审计 A1 安全项）。"""
    # 依赖仍留在 agent_loop 的定义 → 函数内惰性导入（避免循环依赖）
    from agent_loop import _get_agent_config
    cfg = _get_agent_config(agent_id)
    allowed = cfg.get("tools", "all")
    if allowed != "all":
        all_tools = [t for t in all_tools if t.get("function", {}).get("name") in allowed]
    # 工具启用/禁用开关（惰性容错：capabilities 表未初始化时不过滤）
    try:
        from capability_registry import list_capabilities
        disabled = {c.get("name") for c in list_capabilities("tool") if not c.get("enabled")}
        if disabled:
            all_tools = [t for t in all_tools
                         if t.get("function", {}).get("name") not in disabled]
    except Exception:
        pass
    return all_tools
