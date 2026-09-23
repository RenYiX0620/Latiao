"""Agent Loop — agent loops, tool call parsing, tool dispatch state, chat building.

Split from main.py (Sections 2/3/5/6/7/8/9). Code is a verbatim move from
main.py — only imports were adjusted for the module split. Mutable state that
must stay visible through the main.py facade (TOOLS, TOOL_DISPATCH, etc.) is
defined here and re-exported by main.py.
"""
import asyncio
import contextvars
import inspect
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path


from config import PROGRESS_DIR

# ── 进度读写已拆到 agent/progress.py（2026-09-23 第一块接缝）──
# 保留同名 re-export：api_routes（_progress_file）、main（_record_progress）与既有
# 测试（test_language 的 _progress_tail、test_context_stats 的 monkeypatch）都按
# agent_loop 的旧路径导入；monkeypatch agent_loop._progress_tail 依然生效，因为
# _build_chat_messages 在本模块内按全局名查表。
from agent.prompt_build import (  # noqa: F401  —— re-export（api_routes/main/6 个测试按旧路径导入）
    _build_chat_messages,
)
from agent.session_events import (  # noqa: F401  —— re-export（旧导入路径继续可用）
    _clear_session_cancel,
    _EVENT_LOGS,
    _EVENT_LOGS_LOCK,
    _EVENT_LOGS_MAX,
    _event_log_for,
    _request_session_cancel,
    _session_cancel_requested,
)
from agent.mcp_tools import (  # noqa: F401  —— re-export（旧导入路径继续可用）
    _load_mcp_tools,
    _mcp_invoke,
    _mcp_tool_name,
    ensure_mcp_loaded,
)
from agent.verify import (  # noqa: F401  —— re-export（旧导入路径继续可用）
    _auto_verify,
    _enhance_auto_verify,
    _semgrep_scan,
)
from agent.reflection import (  # noqa: F401  —— re-export（旧导入路径继续可用）
    _REFLECT_CHECKLISTS,
    _find_unverified_numbers,
    _generate_plan,
    _reflect_output,
)
from agent.routing import (  # noqa: F401  —— re-export（旧导入路径继续可用）
    _get_best_cloud_config,
    _resolve_api_target,
)
from agent.confirm import (  # noqa: F401  —— re-export（旧导入路径继续可用）
    _await_tool_confirmation,
    _check_pre_hooks,
    _confirm_bypassed,
    _count_successful_duplicates,
    _pending_confirmations,
    _pending_lock,
    _start_plan_confirmation,
    _start_tool_confirmation,
    _wait_plan_confirmation,
    _wait_tool_confirmation,
)
from agent.progress import (  # noqa: F401  —— re-export（兼容旧导入路径）
    PROGRESS_FILE,        # 规范所有者在 agent.progress：进度路径只留一份定义
    _clean_progress_tail,
    _progress_file,
    _progress_tail,
    _record_progress,
    _rotate_progress_file,
)
AGENTS_FILE = PROGRESS_DIR / "agents.json"
CONFIG_FILE = PROGRESS_DIR / "config.json"

from cron import _create_cron
from db import _db_write_lock, _get_db
from identity import _load_agent_identity
from memory import (
    _maybe_generate_skill,
    _quick_reflect,
    _record_reflection,
    _refine_learnings,
)
from tool_executor import (
    _FALLBACK_DISPATCH,
    _FALLBACK_PERMISSIONS,
    _FALLBACK_TOOLS,
    _resolve_permission,
)
from tool_system import load_plugins

# ── Stage 1 拆分：实现移至 agent/ 包（兼容导入，既有引用继续可用）──
from cmd_safety import redact_secrets, tool_log_preview   # 日志/落盘脱敏（09-23）
from agent.transport import (  # noqa: F401
    _is_local_llm_url, _local_llm_serialized, _local_llm_stream,
    _safe_cwd, _verify_llm_health, clear_llm_suspect, mark_llm_suspect,
)
from agent.parsing import (  # noqa: F401
    _NATIVE_CONTROL_RE, _NATIVE_TOOL_RE, _append_loop_log,
    _gemma_args_to_json, _parse_delta_line, _parse_kv_args,
    _parse_native_tool_calls, _parse_prompt_tool_calls, _salvage_tool_args,
    _strip_native_tool_calls,
)
from agent.text_quality import (  # noqa: F401
    _GenerationLoopError, _THINK_FENCE_RE, _ThinkFenceFilter, _deduplicate_response,
    _detect_text_loop, _extract_magnitude_numbers, _extract_think_body,
    _find_unsourced_numbers, _is_meta_wrapup, _looks_like_tool_fantasy,
    _strip_repeat_tail, _strip_think_fences,
)
from agent.context import (  # noqa: F401
    AUTO_EDIT_TOOLS, INTENT_PATTERNS, TOOL_CATEGORIES,
    _ANTHROPIC_HINTS, _CHAT_MARKERS, _FORCED_REASONING, _INLINED_FILE_RE,
    _MARKET_TASK_RE, _NATIVE_COMPLETION_CHECK_PROMPT,
    _NATIVE_FOLLOWUP_PROMPT, _NATIVE_LEAN_PROMPT, _NativeToolsUnsupported,
    _TASK_KW, _TRANSIENT_REMINDER_MARKERS, _cap_tools, _candidate_tool_names,
    _check_access, _detect_user_language, _ensure_market_tools,
    detect_language_decision,
    _extract_last_user_text, _filter_tools, _filter_tools_by_access,
    _get_localized_text, _inject_image, _inject_thinking_disabled,
    _is_chat_query, _is_light_query, _local_native_tools_ok,
    _maybe_add_inline_file_note, _merge_system_messages, _normalize_access,
    subagent_tool_gate,
    _recover_tool_name, _resolve_max_tokens, _sanitize_tool_messages,
    _slim_history_for_local, _strip_transient_reminders,
)
from agent.gates import (  # noqa: F401
    _LANG_RETRY_HINT, _PENDING_INTENT_PATTERNS, _PLANNING_SIGNALS,
    _build_local_tools_prompt, _ensure_final_language,
    _ensure_final_language_with_retry, _final_answer_extraction,
    _force_translate, _looks_like_planning, _reply_lang_mismatch,
    _strip_nonprose_for_lang,
)
# 不可信内容防护（09-21）：工具结果注入扫描 + 身份文件读取护栏
from threat_scan import guard_tool_result


logger = logging.getLogger("latiao-sidecar")

# Per-request cloud config — contextvars isolates concurrent requests
_last_cloud_config: contextvars.ContextVar = contextvars.ContextVar("cloud_config", default=None)

# Fire-and-forget 后台任务集合——保存强引用，防止任务被 GC 提前回收
_background_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    """创建后台任务并保存引用，完成后自动从集合移除。"""
    t = asyncio.create_task(coro)
    _background_tasks.add(t)
    t.add_done_callback(_background_tasks.discard)
    return t


# ═══════════════════════════════════════════════════════
#  Multi-Agent System: LaTiao orchestrator + specialists
# ═══════════════════════════════════════════════════════

AGENTS_DIR = Path(__file__).parent / "agents"


AGENT_PROFILES: dict[str, dict] = {
    "latiao": {
        "name": "辣条",
        "display": "LaTiao · 总指挥",
        "role": "orchestrator",
        "identity": _load_agent_identity("latiao",
            "你是 LaTiao（辣条），本机 AI Agent 的总指挥。\n"
            "始终用与用户相同的语言回复。\n"
            "你拥有完整的工具权限。"),
        "tools": "all",
    },
    "code-reviewer": {
        "name": "代码审查员",
        "display": "代码审查员 · 安全分析",
        "role": "specialist",
        "identity": _load_agent_identity("code-reviewer",
            "你是代码审查员，专注代码审查和安全分析。权限：只读。"),
        "tools": ["read_file", "list_dir", "search_files"],
    },
    "doc-generator": {
        "name": "文档生成器",
        "display": "文档生成器 · 文档专家",
        "role": "specialist",
        "identity": _load_agent_identity("doc-generator",
            "你是文档生成器，生成项目文档、API 文档和变更日志。"),
        "tools": ["read_file", "list_dir", "search_files", "write_file"],
    },
    "debugger": {
        "name": "调试专家",
        "display": "调试专家 · Bug 猎手",
        "role": "specialist",
        "identity": _load_agent_identity("debugger",
            "你是调试专家，分析日志、定位 Bug、提供修复方案。"),
        "tools": "all",
    },
    "translator": {
        "name": "翻译助手",
        "display": "翻译助手 · 多语言",
        "role": "specialist",
        "identity": _load_agent_identity("translator",
            "你是翻译助手，负责多语言翻译与本地化。"),
        "tools": ["read_file", "list_dir", "search_files", "write_file"],
    },
    "explore": {
        "name": "探索者",
        "display": "探索者 · 深度调研",
        "role": "specialist",
        "identity": _load_agent_identity("explore",
            "你是探索者，专注快速摸清代码库/文件结构、定位关键实现，并可联网调研。\n"
            "你可以运行只读命令（ls/grep/find/cat 等白名单命令）和联网搜索，但不能修改任何东西。\n"
            "高效优先：一次读大范围信息，避免琐碎小步。返回简洁的发现摘要（含关键路径与结论）。"),
    },
}


# ╔══════════════════════════════════════════════════════╗
# ║  SECTION 2: Agent Config & Custom Agents             ║
# ║  _get_agent_config, _load_custom_agents, _merge_agents║
# ╚══════════════════════════════════════════════════════╝

def _get_agent_config(agent_id: str) -> dict:
    """Get agent profile, falling back to latiao (orchestrator)."""
    return AGENT_PROFILES.get(agent_id, AGENT_PROFILES["latiao"])


def _get_agent_tools(agent_id: str, all_tools: list[dict]) -> list[dict]:
    """Filter tools based on agent's allowed tools. 'all' means all tools.

    同时过滤 Tools 页被禁用的工具（capabilities.enabled=0）——此前禁用
    开关只写库、agent 管线从不读，禁用 run_cmd 后模型照常执行
    （审计 A1 安全项）。"""
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


def _load_custom_agents() -> dict[str, dict]:
    """Load user-created agent profiles from disk."""
    if AGENTS_FILE.exists():
        try:
            return json.loads(AGENTS_FILE.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("Failed to load custom agents", exc_info=True)
    return {}

def _save_custom_agents(agents: dict[str, dict]):
    """Persist custom agent profiles to disk."""
    AGENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    AGENTS_FILE.write_text(json.dumps(agents, indent=2, ensure_ascii=False), encoding="utf-8")

def _merge_agents():
    """Merge built-in + custom agents into AGENT_PROFILES."""
    custom = _load_custom_agents()
    for key, profile in custom.items():
        if key not in AGENT_PROFILES:
            profile["custom"] = True
            AGENT_PROFILES[key] = profile

# ╔══════════════════════════════════════════════════════╗
# ║  SECTION 3: Shared State & Plugin Registry           ║
# ║  TOOLS, TOOL_DISPATCH, TOOL_PERMISSIONS, TOOL_HOOKS  ║
# ╚══════════════════════════════════════════════════════╝

# Plugin system globals — populated by _load_plugins()
TOOL_PERMISSIONS: dict[str, str] = {}
TOOLS: list[dict] = []
TOOL_DISPATCH: dict[str, callable] = {}
TOOL_HOOKS: dict[str, dict] = {}



# ═══════════════════════════════════════════════════════
#  Self-Verification: programmatic post-tool quality checks
# ═══════════════════════════════════════════════════════

# ╔══════════════════════════════════════════════════════╗
# ║  SECTION 5: Verification & Tool Dispatch             ║
# ║  _auto_verify, execute_tool, _handle_tool_execution  ║
# ╚══════════════════════════════════════════════════════╝



# Initialize plugin system at module load (seeded inside _load_plugins)
TOOLS, TOOL_DISPATCH, TOOL_PERMISSIONS, TOOL_HOOKS = load_plugins(_FALLBACK_TOOLS, _FALLBACK_DISPATCH, _FALLBACK_PERMISSIONS)

# 去重：DeepSeek 等 API 要求 tools 名字唯一，重复直接 400
_seen_tool_names: set[str] = set()
_unique_tools: list[dict] = []
for _t in TOOLS:
    _n = _t.get("function", {}).get("name") if isinstance(_t, dict) else None
    if _n and _n not in _seen_tool_names:
        _seen_tool_names.add(_n)
        _unique_tools.append(_t)
TOOLS = _unique_tools

# Append delegate_task to TOOLS (not a plugin — built-in sub-agent system)
_delegate_tool_def = {
    "type": "function",
    "function": {
        "name": "delegate_task",
        "description": "Delegate a sub-task to a specialist sub-agent. Sub-agents run independently with limited tools and return results. Use to parallelize work — call multiple times for independent sub-tasks. Available agents: explore (read-only deep exploration: search codebase, run read-only commands like ls/grep, web research), code-reviewer (read-only code review), doc-generator (documentation), debugger (bug analysis), translator (translation).",
        "parameters": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "enum": ["explore", "code-reviewer", "doc-generator", "debugger", "translator"], "description": "The specialist agent type."},
                "task": {"type": "string", "description": "The specific task for the sub-agent. Be clear and concise."},
                "background": {"type": "boolean", "description": "Run in background without blocking the main conversation. Progress appears in the sub-agent panel. Use background=true for long-running work (research, multi-directory scans, >1min) — the result will be automatically delivered back to you when done. Default false."},
            },
            "required": ["agent", "task"],
        },
    },
}
# 防重复: fallback 合并可能已加入 delegate_task(拆分前就存在此 bug,
# 导致 Iteration 3 全量工具时 DeepSeek 报 "Tool names must be unique" 400)
if not any(t.get("function", {}).get("name") == "delegate_task" for t in TOOLS):
    TOOLS.append(_delegate_tool_def)
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

TOOL_DISPATCH["delegate_task"] = _dispatch_delegate
TOOL_PERMISSIONS["delegate_task"] = "safe"
# 描述补充 background 参数（模型需要知道才能用）

_create_cron_def = {
    "type": "function",
    "function": {
        "name": "create_cron",
        "description": "创建定时任务。schedule 用标准 5 段 cron 表达式：分 时 日 月 周。示例：每10分钟=*/10 * * * *；每小时=0 * * * *；每天9点=0 9 * * *；每30分钟=*/30 * * * *。task 是要执行的任务描述（中文）。创建后会按时自动执行并把结果推送到会话。",
        "parameters": {
            "type": "object",
            "properties": {
                "schedule": {"type": "string"},
                "task": {"type": "string"}
            },
            "required": ["schedule", "task"]
        }
    }
}
TOOLS.append(_create_cron_def)
TOOL_DISPATCH["create_cron"] = lambda a: _create_cron(a.get("schedule", "0 9 * * *"), a.get("task", ""))
TOOL_PERMISSIONS["create_cron"] = "safe"

# ── use_skill：统一能力模型的技能调用通道（ZCode 式按需加载）──
# 技能正文存 capability_registry 表，运行时调用取全文；目录注入 system prompt。
# 与普通工具走同一条执行/权限确认/计数通道，技能安全等级由 registry 解析。
_use_skill_def = {
    "type": "function",
    "function": {
        "name": "use_skill",
        "description": "加载一个技能并获取其完整使用说明。执行特定领域任务（如代码审查、git 工作流、金融分析等）时，先调用本工具取得对应技能的完整指引，再按其执行。可用技能目录见系统提示。",
        "parameters": {
            "type": "object",
            "properties": {
                "skill_name": {"type": "string", "description": "要加载的技能名（与系统提示技能目录中的名字一致）"},
            },
            "required": ["skill_name"],
        },
    },
}
TOOLS.append(_use_skill_def)


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


TOOL_DISPATCH["use_skill"] = _dispatch_use_skill
TOOL_PERMISSIONS["use_skill"] = "safe"

# 工具列表顺序 = 模型看到的"优先级"：搜索类模型明显倾向选靠前的工具。
# 插件按文件名排序加载（bing_search.py < tavily_search.py），导致 tavily 永远排在
# bing 之后，且 _cap_tools 按原序截断时 tavily_search 总被先切掉——模型根本没机会
# 看到 tavily。按语义优先级只重排一次，保证 tavily 排在搜索组最前、截断时优先保留。
_TOOL_PRIORITY = (
    "read_file", "write_file", "list_dir", "search_files",
    "tavily_search", "headless_read", "dokobot_search", "web_search", "bing_search",
    "mx_query", "ak_finance",
    "open_app", "open_folder", "run_cmd",
    "screen_capture", "control_list_processes", "control_process_log", "control_audit",
    "control_wait", "control_launch", "control_mouse_move", "control_mouse_click",
    "control_keyboard_type", "control_keyboard_press", "control_kill_process",
    "use_skill", "delegate_task", "create_cron",
)
_TOOL_RANK = {_name: _rank for _rank, _name in enumerate(_TOOL_PRIORITY)}
TOOLS.sort(key=lambda _t: _TOOL_RANK.get(_t.get("function", {}).get("name", ""), len(_TOOL_RANK)))

# 能力表初始同步（仅 upsert，不裁剪——MCP 工具稍后才加载）
try:
    import capability_registry as _cap_reg
    _cap_reg.sync_tools(TOOLS, TOOL_PERMISSIONS, TOOL_DISPATCH, prune=False)
    _cap_reg.sync_skills()
except Exception:
    logger.warning("capability sync at import failed", exc_info=True)








_MCP_LOADED = False




async def execute_tool(tool_name: str, arguments: dict) -> str:
    """Execute a tool with feedback verification. Supports both sync and async tool functions."""
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


# ═══════════════════════════════════════════════════════
#  Self-Learning: Context Injection + Knowledge Extraction + Reflection
# ═══════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════
#  Progressive Delivery + State Tracking + Stagnation Detection
# ═══════════════════════════════════════════════════════


_PLAN_KEYWORDS = (
    "分析", "报告", "调研", "研究", "构建", "搭建", "部署", "修复", "排查",
    "优化", "重构", "设计", "开发", "写一个", "写一份", "写一篇", "总结",
    "对比", "评估", "方案", "规划",
    "analyze", "report", "research", "build", "deploy", "fix", "refactor",
    "design", "develop", "compare", "evaluate", "plan",
)


def _should_plan(user_text: str, is_local: bool) -> bool:
    """复杂任务触发规划模式：任务关键词 + 消息够长。本地模型不触发（避免额外等待）。"""
    if is_local or not user_text:
        return False
    # plan 访问档强制规划（此前档位对规划无任何影响，审计 A2）
    return len(user_text.strip()) >= 30 and any(k in user_text.lower() for k in _PLAN_KEYWORDS)




def _should_reflect(mode: str, text: str, is_local: bool) -> bool:
    """反思触发条件：off 永不；light 仅云端长输出；deep 任何模型的长任务输出。"""
    if mode == "off" or not text or len(text.strip()) < 200:
        return False
    if mode == "light":
        return not is_local and len(text) > 800
    if mode == "deep":
        return len(text) > 300  # 用户主动选重度，接受任何模型的等待代价
    return False




_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?%")
_VOL_RE = re.compile(r"(?:\d+(?:\.\d+)?\s*(?:亿|万|万亿|亿元|万股|亿股|元|点|个|只|家|天|日|周|年))")


def _extract_numbers(text: str) -> list[str]:
    """提取文本中的百分数与带单位数字（去重保序）。"""
    out: list[str] = []
    for m in _NUM_RE.findall(text):
        if m not in out:
            out.append(m)
    for m in _VOL_RE.findall(text):
        if m not in out:
            out.append(m)
    return out






REASONING_MODEL_HINTS = ("reasoner", "r1", "reasoning", "thinking", "o1", "o3", "o4", "gpt-5")
_session_states: dict[str, dict] = {}



















# Regex to parse prompt-based tool calls from local model output.
# Supports formats:
#   ```tool read_file\n{"path": "/home/file.txt"}\n```  (primary, taught in prompt)
#   [TOOL:read_file path="src/main.py"]
#   <tool>read_file{"path": "/home/file.txt"}</tool>
#   FUNC:read_file path=/home/file.txt
#   web_search "query string" / search "query string"  (natural language fallback)
_REPEAT_ALLOWED_TOOLS = frozenset({"screen_capture", "control_wait"})









# 时间敏感工具：结果自带日期数据，模型易把"昨晚/今天"等相对时间换算错后
# 被检索结果的旧日期锚定（09-03 两次事故：08:25 老会话、08:57 全新会话，
# 均把"昨晚美股"搜成 9月1日）。
_TIME_SENSITIVE_TOOLS = frozenset({
    "tavily_search", "bing_search", "dokobot_search",
    "headless_read", "mx_query", "ak_finance",
})

_WEEK_ZH = "一二三四五六日"


def _stamp_time_sensitive() -> str:
    """生成当前时刻锚行，注入时间敏感工具结果头部（截断后追加，不会被截掉）。

    09-21 修措辞：原来写「[数据时刻] 当前时间」——两者并不等价（盘中查、收盘查、
    隔夜再看同一份结果，数字含义完全不同），实测用户看到"上午涨 0.89%、下午又变
    1.2%"的分歧就是这么来的。现在明确：这是**发起查询**的时刻，数据自身时点看结果
    里的 date/时间列，并要求回答时原样引用。
    """
    now = datetime.now()
    return (f"⏱ [查询时刻] {now.strftime('%Y-%m-%d')} (周{_WEEK_ZH[now.weekday()]}) "
            f"{now.strftime('%H:%M:%S')} —— 这是**发起查询**的时刻，不等于数据本身的时点；"
            f"数据时点以结果里的 date/时间列为准，回答时必须原样引用（结果内日期若与此矛盾，"
            f"以当前时间为准）\n\n")


def _tool_end_result(events: list[dict]) -> str:
    """从事件列表回取最后一次 tool_end 的结果（包装层写 tool/result 用）。"""
    for ev in reversed(events):
        if isinstance(ev, dict) and ev.get("event") == "tool_end":
            return str(ev.get("result", ""))
    return ""


# 工具执行超时（09-19）：此前工具执行**没有任何超时**，模型给出宽泛递归模式
# （实测 search_files {directory: ~, pattern: "**/tavily_*"}）后整轮卡死 21 分钟、
# 零输出零日志——现有守卫都在步与步之间检查，救不了执行中的工具。超时不抛错，
# 而是把"工具超时"作为结果回给模型，让它自己缩小范围或换路。
_TOOL_TIMEOUT_DEFAULT = float(os.environ.get("LATIAO_TOOL_TIMEOUT", "120") or 120)
_TOOL_TIMEOUTS = {
    "search_files": 20.0, "list_dir": 20.0, "read_file": 20.0, "write_file": 20.0,
    "tavily_search": 45.0, "web_search": 45.0, "bing_search": 45.0,
    "mx_query": 30.0, "ak_finance": 60.0, "headless_read": 150.0,
    "run_cmd": 300.0, "use_skill": 30.0, "open_app": 20.0, "open_folder": 20.0,
}


def _tool_timeout_for(tool_name: str) -> float:
    """单工具超时秒数（可按需扩表；未列出的用默认值）。"""
    if tool_name in _TOOL_TIMEOUTS:
        return _TOOL_TIMEOUTS[tool_name]
    return _TOOL_TIMEOUT_DEFAULT


async def _handle_tool_execution(tc: dict, current_msgs: list, session_id: str,
                                 agent_id: str, access_mode: str = "confirm",
                                 pre_started: dict | None = None) -> tuple[bool, list[dict]]:
    """事件日志包装（阶段 1，灰度）：tool/call + tool/result 单点入日志。

    _handle_tool_execution_inner 是全循环唯一的工具执行入口（云/本地两个
    生成器都在此交汇），在这里配对其"调用-结果"事件最不容易漏——
    早期/拒绝/异常四条早退路径都经过同一包装。
    """
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








