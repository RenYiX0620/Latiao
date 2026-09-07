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
import platform
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import httpx

import local_llm
from config import PROGRESS_DIR
from cron import _create_cron
from db import _db_write_lock, _get_db
from identity import _load_agent_identity, _process_identity_intents, _read_identity
from session_log import SessionLog
from loop_state import turn_state_for
from memory import (
    _get_high_confidence_preferences,
    _maybe_generate_skill,
    _quick_reflect,
    _record_reflection,
    _refine_learnings,
    _retrieve_relevant_learnings,
)
from tool_executor import (
    _FALLBACK_DISPATCH,
    _FALLBACK_PERMISSIONS,
    _FALLBACK_TOOLS,
    _resolve_permission,
)
from tool_system import load_plugins

# ── Stage 1 拆分：实现移至 agent/ 包（兼容导入，既有引用继续可用）──
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
    _extract_last_user_text, _filter_tools, _filter_tools_by_access,
    _get_localized_text, _inject_image, _inject_thinking_disabled,
    _is_chat_query, _is_light_query, _local_native_tools_ok,
    _maybe_add_inline_file_note, _merge_system_messages, _normalize_access,
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

# Pending confirmations: call_id → asyncio.Event (approve) or None (deny)
_pending_confirmations: dict[str, dict] = {}
_pending_lock = asyncio.Lock()

# 会话级取消注册表：POST /v1/chat/cancel 置位。两个循环在每轮迭代开头与
# 每次工具执行前检查——停止按钮此前只断前端流，服务端循环继续烧 GPU/
# 执行工具/扣云端费用（P0）。set 的 add/discard/contains 原子，无需锁。
_session_cancelled: set[str] = set()

# 事件日志（阶段 1，灰度）：LATIAO_EVENT_LOG=1 时取消/回合边界事件写入
# session_events 表（sidecar/session_log.py，移植 dsh append 契约）。
# 有会话级取消事件时，重放/审计能还原"用户何时按过停止"——0.3.14 审计发现
# 停止按钮此前只断前端流，这类时序信息在旧日志里是彻底丢失的。
# 日志实例按会话缓存：seq 连续性契约要求同一会话共用同一实例（否则每个
# 新实例 seq 都从 0 开始，回放时"连续序号"语义失效）。缓存有界（LRU 32），
# 会话结束不清理——事件是审计事实，实例只是写入口。
_EVENT_LOGS: dict[str, SessionLog] = {}
_EVENT_LOGS_LOCK = threading.Lock()
_EVENT_LOGS_MAX = 32


def _event_log_for(session_id: str) -> SessionLog | None:
    try:
        with _EVENT_LOGS_LOCK:
            log = _EVENT_LOGS.get(session_id)
            if log is None:
                log = SessionLog(session_id)
                if len(_EVENT_LOGS) >= _EVENT_LOGS_MAX:
                    _EVENT_LOGS.pop(next(iter(_EVENT_LOGS)))
                _EVENT_LOGS[session_id] = log
            return log
    except Exception:
        logger.warning("event log unavailable for %s", session_id, exc_info=True)
        return None


def _request_session_cancel(session_id: str) -> None:
    """置位会话取消标记（/v1/chat/cancel 调用）。"""
    if session_id:
        _session_cancelled.add(session_id)
        # 相位状态机镜像（阶段 2a，与事件日志同源）：stopping 相位 + 先行原因
        try:
            turn_state_for(session_id).request_stop("user", "button")
        except Exception:
            logger.warning("failed to mirror cancel to turn state", exc_info=True)
        log = _event_log_for(session_id)
        if log is not None:
            try:
                log.append("cancel/request", {"cause": "user"})
            except Exception:
                logger.warning("failed to log cancel/request", exc_info=True)


def _clear_session_cancel(session_id: str) -> None:
    """新请求开始时清除标记（重发消息不应被上一次停止影响）。"""
    _session_cancelled.discard(session_id)
    # 上一次停止若因断连/异常未结算，新请求强制放弃旧 turn（产品语义：重发必须可用）
    try:
        turn_state_for(session_id).abandon()
    except Exception:
        logger.warning("failed to abandon turn state", exc_info=True)


def _session_cancel_requested(session_id: str) -> bool:
    return session_id in _session_cancelled

# PROGRESS_DIR is imported from config
PROGRESS_FILE = PROGRESS_DIR / "PROGRESS.md"
AGENTS_FILE = PROGRESS_DIR / "agents.json"
CONFIG_FILE = PROGRESS_DIR / "config.json"
_merge_agents()

# ═══════════════════════════════════════════════════════
#  Self-Verification: programmatic post-tool quality checks
# ═══════════════════════════════════════════════════════

# ╔══════════════════════════════════════════════════════╗
# ║  SECTION 5: Verification & Tool Dispatch             ║
# ║  _auto_verify, execute_tool, _handle_tool_execution  ║
# ╚══════════════════════════════════════════════════════╝

async def _auto_verify(tool_name: str, args: dict, result: str) -> str:
    """Run programmatic verification after a tool executes.
    Returns a verification report to inject into the LLM context, or '' if nothing to verify."""
    checks = []
    path = ''

    if tool_name == "write_file":
        path = args.get("path") or args.get("file") or ""
        content_written = args.get("content") or ""

        # ── Read-back verification ──
        if path:
            try:
                loop = asyncio.get_running_loop()
                actual = await loop.run_in_executor(None, lambda: Path(path).read_text(encoding="utf-8"))
                if actual == content_written:
                    checks.append(("OK", "回读比对", f"内容一致 ({len(content_written)} 字符)"))
                else:
                    diff = len(actual) - len(content_written)
                    checks.append(("FAIL", "回读比对", f"内容不一致！期望 {len(content_written)} 字符，实际 {len(actual)} (差 {diff})"))
                lines = actual.split("\n")
                checks.append(("OK", "完整性", f"{len(lines)} 行, 首行: {lines[0][:60] if lines else '(空)'}"))
            except FileNotFoundError:
                checks.append(("FAIL", "文件存在", f"写入后文件不存在: {path}"))

        # ── TypeScript type-check (find nearest tsconfig.json) ──
        if path.endswith((".ts", ".tsx")):
            p = Path(path)
            for parent in [p.parent, p.parent.parent, p.parent.parent.parent]:
                if (parent / "tsconfig.json").exists():
                    try:
                        proc = await asyncio.create_subprocess_exec(
                            "npx", "tsc", "--noEmit", cwd=str(parent),
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                        )
                        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
                        if proc.returncode == 0:
                            checks.append(("OK", "TS 类型检查", "tsc --noEmit 通过"))
                        else:
                            output = (stderr or stdout or b"").decode("utf-8", errors="replace")
                            errs = [line for line in output.strip().split("\n") if line.strip()]
                            checks.append(("FAIL", "TS 类型检查", f"发现 {len(errs)} 个错误"))
                            for el in errs[:3]:
                                checks.append(("  ", "  ↳", el[:120]))
                    except FileNotFoundError:
                        pass
                    except asyncio.TimeoutError:
                        checks.append(("FAIL", "TS 类型检查", "超时"))
                    except Exception:
                        logger.warning("TypeScript check failed in auto-verify", exc_info=True)
                    break

    if tool_name == "run_cmd":
        exit_match = re.search(r'退出码:\s*(\d+)', result)
        if exit_match:
            code = int(exit_match.group(1))
            checks.append(("OK" if code == 0 else "FAIL", "退出码", f"exit {code}"))
        elif "超时" in result:
            checks.append(("FAIL", "超时", "命令执行超时 (30s)"))

    if tool_name in ("write_file", "run_cmd"):
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "diff", "--stat",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            if proc.returncode == 0 and stdout.strip():
                checks.append(("INFO", "Git 变更", "\n" + stdout.decode("utf-8", errors="replace").strip()))
        except Exception:
            logger.warning("Git diff check failed in auto-verify", exc_info=True)

        # ── ESLint check for JS/TS files ──
        if path.endswith((".ts", ".tsx", ".js", ".jsx")):
            p = Path(path)
            for parent in [p.parent, p.parent.parent, p.parent.parent.parent]:
                if (parent / "eslint.config.js").exists() or (parent / ".eslintrc").exists():
                    try:
                        proc = await asyncio.create_subprocess_exec(
                            "npx", "eslint", str(p),
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                        )
                        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
                        output = (stdout or stderr or b"").decode("utf-8", errors="replace").strip()
                        if proc.returncode == 0 and not output:
                            checks.append(("OK", "ESLint", "无警告"))
                        elif output:
                            errs = [line for line in output.split("\n") if line.strip()][:5]
                            checks.append(("FAIL", "ESLint", f"发现 {len(errs)} 个问题"))
                            for el in errs[:3]:
                                checks.append(("  ", "  ↳", el[:120]))
                    except FileNotFoundError:
                        pass
                    except asyncio.TimeoutError:
                        checks.append(("FAIL", "ESLint", "超时"))
                    except Exception:
                        logger.debug("ESLint check failed", exc_info=True)
                    break

        # ── Python syntax check ──
        if path.endswith(".py"):
            try:
                try:
                    with open(path, encoding="utf-8") as _f:
                        source = _f.read()
                    compile(source, path, "exec")
                    checks.append(("OK", "Python 语法", "编译通过"))
                except SyntaxError as _e:
                    checks.append(("FAIL", "Python 语法", str(_e)[:150]))
            except FileNotFoundError:
                pass
            except asyncio.TimeoutError:
                checks.append(("FAIL", "Python 语法", "超时"))
            except Exception:
                logger.debug("Python syntax check failed", exc_info=True)

    # ── Semgrep security scan ──
    await _enhance_auto_verify(tool_name, args, result, checks)

    if not checks:
        return ""

    report = ["\n## 🔍 自动验证"]
    all_ok = all(s in ("OK", "INFO", "  ") for s, _, _ in checks)
    report.append(f"**{'✅ 全部通过' if all_ok else '⚠️ 发现问题'}**\n")
    for status, name, detail in checks:
        icon = {"OK": "✅", "FAIL": "❌", "INFO": "📋", "  ": "  "}.get(status, status)
        report.append(f"- {icon} **{name}**: {detail}")
    return "\n".join(report)


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
    "tavily_search", "dokobot_read", "headless_read", "dokobot_search", "web_search", "bing_search",
    "mx_query", "ak_finance",
    "screen_capture", "control_list_processes", "control_process_log", "control_audit",
    "control_wait", "control_launch", "control_mouse_move", "control_mouse_click",
    "control_keyboard_type", "control_keyboard_press", "control_kill_process",
    "open_app", "open_folder", "run_cmd",
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


# ── MCP 扩展：启用扩展声明 mcpServers → 动态注册远程工具 ──
# 工具命名 mcp_<server>_<tool>；dispatch 异步转发，连接失败返回错误文本（不崩整轮）。
def _mcp_tool_name(server: str, tool: str) -> str:
    from mcp_client import sanitize_tool_name
    return sanitize_tool_name(f"mcp_{server}_{tool}")


async def _load_mcp_tools() -> None:
    """扫描启用扩展的 mcpServers 声明，把远程工具并入 TOOLS/DISPATCH。"""
    try:
        from extension_manager import active_extension_dirs, _read_manifest
        from mcp_client import get_mcp_client
        for ext_dir in active_extension_dirs():
            manifest = _read_manifest(ext_dir) or {}
            servers = manifest.get("mcpServers") or {}
            for srv_name, cfg in servers.items():
                if not isinstance(cfg, dict):
                    continue
                entry = f"{ext_dir.parent.name}/{srv_name}"
                try:
                    client = get_mcp_client(entry, cfg)
                    tools = await client.list_tools()
                except Exception as e:
                    logger.warning("MCP 连接失败 %s: %s", entry, e)
                    continue
                for t in tools:
                    tname = t.get("name", "")
                    if not tname:
                        continue
                    fname = _mcp_tool_name(srv_name, tname)
                    schema = (t.get("inputSchema") or {}).copy()
                    desc = t.get("description") or f"MCP 工具 {srv_name}:{tname}"
                    # 已有同名工具时跳过（内置优先）
                    if any(td.get("function", {}).get("name") == fname for td in TOOLS):
                        continue
                    TOOLS.append({
                        "type": "function",
                        "function": {"name": fname, "description": desc, "parameters": schema},
                    })
                    # 审计 P2-18：MCP 工具此前一律注册 safe（免确认）。MCP 服务
                    # 可暴露任意能力（网络/文件/系统命令），且扩展本身无签名
                    # 校验——默认降为 confirm，由用户把关（与未知工具 fail-close 一致）
                    TOOL_PERMISSIONS[fname] = "confirm"
                    TOOL_DISPATCH[fname] = (
                        lambda args, _srv=srv_name, _tool=tname, _entry=entry:
                        _mcp_invoke(_entry, _tool, args)
                    )
                    logger.info("MCP 工具注册: %s (%s)", fname, entry)
    except Exception:
        logger.warning("MCP 扩展扫描失败", exc_info=True)


async def _mcp_invoke(entry: str, tool: str, args: dict) -> str:
    try:
        from mcp_client import _MCP_CLIENTS
        client = _MCP_CLIENTS.get(entry)
        if client is None:
            return "⛔ MCP 连接已失效，请重启应用或重新安装扩展"
        return await client.call_tool(tool, args)
    except Exception as e:
        return f"⛔ MCP 工具调用失败: {e}"


_MCP_LOADED = False


def ensure_mcp_loaded() -> None:
    """进程内一次性 MCP 注册（幂等）。api_routes 每个请求入口调用。"""
    global _MCP_LOADED
    if _MCP_LOADED:
        return
    _MCP_LOADED = True
    try:
        import asyncio as _asyncio
        try:
            _asyncio.get_running_loop()
        except RuntimeError:
            _asyncio.run(_load_mcp_tools())
        else:
            # 已在事件循环内（理论上 api 层 async 调用前不至此）
            _asyncio.create_task(_load_mcp_tools())
    except Exception:
        logger.warning("MCP 工具注册失败", exc_info=True)


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


def _record_progress(entry: str):
    """Append a progress entry to PROGRESS.md for cross-session continuity."""
    try:
        PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
        now = datetime.now().isoformat()
        with open(PROGRESS_FILE, "a", encoding="utf-8") as f:
            f.write(f"### {now}\n{entry}\n\n")
        # 体积轮转（审计 B10）：append-only 已长到 1.1MB 且无上限。
        # 超 1MB 时保留尾部 500KB——read_file 从头截断读最旧 5 万字符，
        # 轮转同时保证最近进度仍在文件里（尾部注入读的也是最新段）
        if PROGRESS_FILE.stat().st_size > 1024 * 1024:
            _rotate_progress_file()
    except Exception:
        logger.warning("Failed to record progress", exc_info=True)


def _rotate_progress_file():
    """把 PROGRESS.md 截到最近 500KB（保留尾部，即最新进度）。

    切点必须对齐 UTF-8 字符边界：按字节直切会在多字节汉字中间断开，
    文件从此不再是合法 UTF-8 → read_file 整个拒绝读取 → 断点续作失效
    （22:46 事故根因："文件编码不是 UTF-8"）。
    """
    try:
        keep = 500 * 1024
        size = PROGRESS_FILE.stat().st_size
        if size <= keep:
            return
        with open(PROGRESS_FILE, "rb") as f:
            f.seek(size - keep)
            tail = f.read()
        # 对齐字符边界：跳过开头的残缺多字节字符（找下一个 UTF-8 合法起始字节）
        offset = 0
        while offset < len(tail):
            b = tail[offset]
            # 合法起始：ASCII(<0x80) 或 2/3/4 字节前缀(0xC0-0xF7)
            if b < 0x80 or (0xC0 <= b <= 0xF7):
                break
            offset += 1
        tail = tail[offset:]
        with open(PROGRESS_FILE, "wb") as f:
            f.write("(早期进度已轮转)\n\n".encode("utf-8") + tail)
    except Exception:
        logger.warning("PROGRESS 轮转失败", exc_info=True)


def _progress_tail(max_chars: int = 600) -> str:
    """读取 PROGRESS.md 尾部（最新进度），用于注入 system prompt。

    600 而非 2000：PROGRESS.md 是 2/3 英文的工具日志，2000 字符的英文注入
    是新会话被带偏成英文回复的最大英文源（09-03 事故）。"""
    try:
        if not PROGRESS_FILE.exists():
            return ""
        size = PROGRESS_FILE.stat().st_size
        read = min(size, max_chars * 4)  # 多读些字节再截字符，中文占 3 字节
        with open(PROGRESS_FILE, "rb") as f:
            f.seek(max(0, size - read))
            tail = f.read().decode("utf-8", errors="replace")
        return tail[-max_chars:]
    except Exception:
        return ""





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

PROGRESSIVE_DELIVERY_PROMPT = """
## 渐进式交付协议（必须遵守）

将每个任务拆为 3 个最小可验证单元，逐步交付：

**阶段 1 — 骨架**：仅生成接口定义、类型声明、空函数体。不实现逻辑。
**阶段 2 — 核心**：填充核心逻辑，跳过边界处理和异常分支。
**阶段 3 — 完善**：补充异常处理、边界检查、注释、测试用例。

每阶段 token 预算 ≤ 上下文窗口 30%。完成一阶段后明确报告进度，再进入下一阶段。
禁止单次输出完整功能——会被截断且质量下降。
"""

GOAL_MODE_PROMPT = """
## 目标导向模式

你收到的是一个**目标**而非指令。你需要：
1. 分析目标 → 拆解为可执行步骤
2. 按渐进式交付协议逐步执行
3. 遇到阻塞主动报告，不强行推进
4. 每完成一步报告进度

用户只关心目标是否达成，不关心你用什么工具。
"""

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


async def _generate_plan(user_text: str, model: str, api_url: str, headers: dict,
                         client: httpx.AsyncClient) -> str:
    """生成执行计划（3-8 步编号列表）。失败返回空串（降级为普通执行）。"""
    sys_prompt = (
        "你是任务规划器。用户给了一个复杂任务，请输出一份简洁、可执行的计划。\n"
        "要求：\n"
        "1. 用编号列表列出 3-8 个步骤\n"
        "2. 每步说明具体要做什么（可提及将使用的工具，如查询行情、读取文件、运行命令、生成报告）\n"
        "3. 步骤具体可执行，不要空话，不要重复用户原文\n"
        "4. 只输出计划本身，不要任何前后缀说明"
    )
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_text},
        ],
        "max_tokens": 1024,
        "stream": False,
        "temperature": 0.3,
    }
    try:
        resp = await client.post(api_url, json=body, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        plan = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
        return plan.strip()
    except Exception as e:
        logger.warning("Plan generation failed (fallback to direct execution): %s", e)
        return ""


def _should_reflect(mode: str, text: str, is_local: bool) -> bool:
    """反思触发条件：off 永不；light 仅云端长输出；deep 任何模型的长任务输出。"""
    if mode == "off" or not text or len(text.strip()) < 200:
        return False
    if mode == "light":
        return not is_local and len(text) > 800
    if mode == "deep":
        return len(text) > 300  # 用户主动选重度，接受任何模型的等待代价
    return False


_REFLECT_CHECKLISTS = {
    "light": (
        "1. 事实/数据与提供的上下文一致，没有编造数字\n"
        "2. 结构完整，有明确的结论\n"
        "3. 没有明显截断、乱码或格式损坏"
    ),
    "deep": (
        "1. 事实/数据与提供的上下文一致，没有编造数字\n"
        "2. 逻辑自洽，前后不矛盾\n"
        "3. 结论完整，回应了用户的所有诉求\n"
        "4. 建议/步骤可执行、无歧义\n"
        "5. 语言通顺，格式规范\n"
        "6. 篇幅合适，不啰嗦也不过于简略"
    ),
}


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


def _find_unverified_numbers(text: str, tool_outputs: list[str]) -> list[str]:
    """报告中出现的、但未能在本次工具查询结果中找到来源的数字。
    用于反思环节逐项核实——机制化防编造，不依赖模型自觉。"""
    haystack = "\n".join(tool_outputs)
    return [n for n in _extract_numbers(text) if n not in haystack]


async def _reflect_output(text: str, model: str, api_url: str, headers: dict,
                          mode: str, client: httpx.AsyncClient,
                          tool_outputs: list[str] | None = None) -> tuple[str, bool]:
    """对最终文本做一轮（light）或两轮（deep）自查反思。
    返回 (最终文本, 是否有修正)。有修正时前端替换最后一条消息。"""
    checklist = _REFLECT_CHECKLISTS.get(mode, _REFLECT_CHECKLISTS["light"])
    rounds = 2 if mode == "deep" else 1
    current = text
    changed = False
    # 机制化溯源核查：报告里的数字若在本会话工具查询结果中找不到来源，
    # 列出供反思模型逐项核实（不硬删，交给模型判断口径）
    unverified = _find_unverified_numbers(current, tool_outputs or [])
    unverified_note = ""
    if unverified:
        unverified_note = (
            "\n\n⚠️ 数字溯源核查：以下数字在本会话的**工具查询结果中未找到来源**，"
            "请逐项处理：\n"
            + "\n".join(f"- {n}" for n in unverified[:15])
            + "\n处理规则：属于查询数据（可能因口径/表述不同而未匹配）→ 保留；"
              "属于宏观/外部数据且本次**没有查询过** → 删除该数字或改为不带具体数字的定性描述。"
        )
    for _ in range(rounds):
        sys_prompt = (
            "你是输出质检员。检查下面这份回答，严格按清单逐项核对。\n"
            f"检查清单：\n{checklist}{unverified_note}\n\n"
            "规则：\n"
            "- 如果发现实质问题（数据错误、遗漏关键结论、自相矛盾、格式损坏、明显不完整），"
            "输出修正后的完整版本。\n"
            "- 如果没有问题，**原样输出原文**，不要添加任何说明。\n"
            "- 只输出最终版本本身，不要输出检查过程、不要加任何前缀。"
        )
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": current},
            ],
            "max_tokens": max(2048, len(current) + 2000),
            "stream": False,
            "temperature": 0.2,
        }
        try:
            resp = await client.post(api_url, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            revised = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
            revised = revised.strip()
            if revised and revised != current:
                current = revised
                changed = True
        except Exception as e:
            logger.warning("Reflection failed (keep original): %s", e)
            break
    return current, changed


REASONING_MODEL_HINTS = ("reasoner", "r1", "reasoning", "thinking", "o1", "o3", "o4", "gpt-5")
_session_states: dict[str, dict] = {}


async def _semgrep_scan(filepath: str) -> str | None:
    """Run semgrep on a file if available. Returns scan report or None."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "semgrep", "--config", "auto", "--quiet", filepath,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        output = (stdout or b"").decode("utf-8", errors="replace").strip()
        output += (stderr or b"").decode("utf-8", errors="replace").strip()
        if output:
            return output
        return None
    except FileNotFoundError:
        return None  # semgrep not installed
    except asyncio.TimeoutError:
        return "semgrep 扫描超时"
    except Exception:
        logger.debug("Semgrep scan failed", exc_info=True)
        return None


async def _enhance_auto_verify(tool_name: str, args: dict, result: str, checks: list):
    """Add semgrep scanning to the verification checks list."""
    if tool_name != "write_file":
        return
    path = args.get("path") or args.get("file") or ""
    if not path.endswith((".ts", ".tsx", ".js", ".jsx", ".py")):
        return
    scan_result = await _semgrep_scan(path)
    if scan_result:
        issue_count = scan_result.count("\n") + 1
        checks.append(("FAIL" if "error" in scan_result.lower() else "OK",
                       "Semgrep 安全扫描",
                       f"发现 {issue_count} 行输出" if issue_count > 1 else "通过"))
        if issue_count > 1:
            for line in scan_result.split("\n")[:3]:
                if line.strip():
                    checks.append(("  ", "  ↳", line[:120]))
    else:
        checks.append(("OK", "Semgrep", "跳过 (未安装或无可扫描内容)"))



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
                                  timeout: float = 0) -> tuple[bool, list[dict]]:
    """等待计划确认结果（默认不限时，用户点击后继续；09-21 用户反馈）。"""
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
        events.append({
            "content": "\n\n⚠️ 计划等待确认超时（5 分钟无人操作），任务已暂停未执行。可重新发起任务。",
        })
    finally:
        async with _pending_lock:
            _pending_confirmations.pop(plan_id, None)
    return approved, events


# Regex to parse prompt-based tool calls from local model output.
# Supports formats:
#   ```tool read_file\n{"path": "/home/file.txt"}\n```  (primary, taught in prompt)
#   [TOOL:read_file path="src/main.py"]
#   <tool>read_file{"path": "/home/file.txt"}</tool>
#   FUNC:read_file path=/home/file.txt
#   web_search "query string" / search "query string"  (natural language fallback)
_REPEAT_ALLOWED_TOOLS = frozenset({"screen_capture", "control_wait"})


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


# 时间敏感工具：结果自带日期数据，模型易把"昨晚/今天"等相对时间换算错后
# 被检索结果的旧日期锚定（09-03 两次事故：08:25 老会话、08:57 全新会话，
# 均把"昨晚美股"搜成 9月1日）。
_TIME_SENSITIVE_TOOLS = frozenset({
    "tavily_search", "bing_search", "dokobot_search", "dokobot_read",
    "headless_read", "mx_query", "ak_finance",
})

_WEEK_ZH = "一二三四五六日"


def _stamp_time_sensitive() -> str:
    """生成当前时刻锚行，注入时间敏感工具结果头部（截断后追加，不会被截掉）。"""
    now = datetime.now()
    return (f"⏱ [数据时刻] {now.strftime('%Y-%m-%d')} (周{_WEEK_ZH[now.weekday()]}) "
            f"{now.strftime('%H:%M:%S')} —— 下方结果内日期若与此矛盾，以当前时间为准\n\n")


def _tool_end_result(events: list[dict]) -> str:
    """从事件列表回取最后一次 tool_end 的结果（包装层写 tool/result 用）。"""
    for ev in reversed(events):
        if isinstance(ev, dict) and ev.get("event") == "tool_end":
            return str(ev.get("result", ""))
    return ""


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
    verify_failed, events = await _handle_tool_execution_inner(
        tc, current_msgs, session_id, agent_id, access_mode, pre_started,
    )
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
            f"⛔ 相同调用已成功执行 {_dup_ok} 次，不再重复执行：{tool_name}。\n"
            "不要重复同一操作——启动协议若已满足就进入下一步"
            "（例如用 mx_query 查询行情数据），或直接把完整分析写进回复正文"
            "（简体中文，含关键数字与结论）。"
        )
        current_msgs.append({"role": "tool", "tool_call_id": call_id, "content": result})
        return False, [{"event": "tool_end", "call_id": call_id, "tool": tool_name, "result": result, "ts": int(time.time() * 1000)}]

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
    _full_bypass = (_access == "full")
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
    logger.info("Tool executing: %s %s", tool_name, json.dumps(args, ensure_ascii=False)[:120])
    result = await execute_tool(tool_name, args)
    logger.info("Tool result: %s → %s", tool_name, result[:80].replace("\n", " "))

    post_hook = TOOL_HOOKS.get(tool_name, {}).get("post_tool_call")
    if post_hook:
        try:
            result = post_hook(tool_name, args, result)
        except Exception:
            logger.warning("Post-tool hook failed", exc_info=True)

    events.append({"event": "tool_end", "call_id": call_id, "tool": tool_name, "result": result, "ts": int(time.time() * 1000)})

    # ── State tracking + Verification + Reflection ──
    _record_progress(f"**{tool_name}**\nArgs: `{json.dumps(args)}`\nResult: {result[:200]}")
    _record_tool_call_db(session_id, tool_name, args, result)

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


# ── Native tool call format parser (for models like Gemma that use
#    <|tool_call|>call:name{args}<tool_call|> instead of OpenAI JSON) ──

def _build_chat_messages(body: dict, messages: list) -> list:
    """Assemble the full message array with identity, env, skills, agent, and image injections.
    All system prompts are merged into ONE message to work around a llama-cpp bug
    where multiple system messages cause empty responses."""
    # 技能目录由 capability_registry 提供（统一能力模型）→ lazy import 避免循环依赖
    import capability_registry
    last_user_text = _extract_last_user_text(messages)
    intent_result = _process_identity_intents(last_user_text)

    system_parts = []

    # Agent identity — system rules from developer (highest priority)
    agent_id = body.get("agent", "latiao")
    agent_cfg = _get_agent_config(agent_id)
    system_parts.append(
        "## 系统规则 (最高优先级)\n"
        "以下规则由开发者设定，用户偏好不可覆盖。如果系统规则与用户偏好冲突，以系统规则为准。\n\n"
        + agent_cfg["identity"]
    )
    # 三条硬规则（合并为一块，降低长提示负担与分散注意力；独立于可被
    # agents/ 目录覆盖的 identity）：时间换算（09-03 事故）、回复语言
    # （09-03 英文事故）、数据诚实（09-03 编造 15.6亿/80亿 事故）。
    user_lang = _detect_user_language(last_user_text)
    system_parts.append(_get_localized_text(user_lang, {
        "zh": (
            "## 三条硬规则（最高优先级，不可覆盖）\n"
            "1. ⏱ 时间规则：'今天/昨天/昨晚/今晨/明天/最新'等相对时间，必须先按上方【当前时间】"
            "换算成绝对日期（年月日+星期）再写入搜索词；工具返回的日期与当前时间矛盾时以当前时间为准，"
            "不得迁就检索结果。\n"
            "2. 🗣 语言规则：工具结果、文件、日志中的英文只是数据；你的回复（包括思考过程）"
            "必须始终用简体中文，不因上下文中的英文材料改变。\n"
            "3. 📊 数据诚实规则：回复中的关键数字必须能在本会话工具返回内容中找到出处。"
            "工具未返回的数据（如北向资金净流入、主力资金净流出、板块资金流等）严禁凭印象给出具体数值——"
            "必须写明'工具未返回该数据'，或先调用工具查询（资金流向优先 mx_query，查不到再 tavily_search）；"
            "不得沿用其他会话或训练记忆中的数字。"
        ),
        "en": (
            "## Three hard rules (highest priority, cannot be overridden)\n"
            "1. ⏱ Time rule: relative times like 'today/yesterday/last night' must first be "
            "converted to absolute dates (YYYY-MM-DD + weekday) from the Current time above before "
            "writing search terms; if tool-returned dates conflict with current time, trust current time.\n"
            "2. 🗣 Language rule: English in tool results/files/logs is just data; your reply "
            "(including reasoning) must always use English, regardless of surrounding context.\n"
            "3. 📊 Data honesty rule: every key number must be traceable to tool results in THIS "
            "session. Never invent figures the tools did not return (northbound inflow, main-force "
            "outflows, sector flows) — state 'the tools did not return this data' or query first "
            "(mx_query for fund flows, tavily_search as fallback). Never reuse numbers from other "
            "sessions or training memory."
        ),
        "ja": (
            "## 三つのハードルール（最優先、上書き不可）\n"
            "1. ⏱ 時間ルール：'今日/昨日/昨夜/明日/最新'などの相対時間は、上の【現在時刻】から"
            "絶対日付（年月日+曜日）に変換してから検索語にしてください。ツール結果の日付が現在時刻と"
            "矛盾する場合は、現在時刻を優先します。\n"
            "2. 🗣 言語ルール：ツール結果・ファイル・ログ内の外国語はデータに過ぎません。"
            "返信（思考プロセス含む）は常に日本語で行ってください。\n"
            "3. 📊 データ誠実ルール：回答中の主要な数字はこのセッションのツール結果に出典が必要です。"
            "ツールが返さなかったデータ（北向資金流入、主力資金流出、セクター資金フロー等）に"
            "具体的な数値をでっち上げてはいけません——「ツールはこのデータを返していない」と明記するか、"
            "先にツールで照会してください。他セッションや学習メモリの数字を使用しないこと。"
        ),
    }))

    # User identity — personal preferences (lower priority)
    user_identity = _read_identity()
    if user_identity:
        system_parts.append(
            "## 用户偏好\n"
            "以下偏好由用户自行设定。优先级低于系统规则，可与系统规则共存。"
        )
        for msg in user_identity:
            system_parts.append(msg["content"])

    if intent_result:
        system_parts.append(
            f"⚠️ 你的身份刚刚被用户更新了：{intent_result}。"
            f"从现在开始，你必须以更新后的身份回复用户。"
        )

    # Environment info
    home = str(Path.home())
    cwd = _safe_cwd()
    now = datetime.now().strftime("%Y-%m-%d (%A) %H:%M:%S")

    env_labels = _get_localized_text(user_lang, {
        "zh": {"rt": "运行环境", "time": "当前时间", "home": "用户目录", "cwd": "工作目录", "os": "操作系统", "sh": "终端"},
        "en": {"rt": "Runtime Environment", "time": "Current time", "home": "Home", "cwd": "Working dir", "os": "OS", "sh": "Shell"},
        "ja": {"rt": "実行環境", "time": "現在時刻", "home": "ホーム", "cwd": "作業ディレクトリ", "os": "OS", "sh": "シェル"},
    })
    system_parts.append(
        f"{env_labels['rt']}:\n"
        f"- {env_labels['time']}: {now}\n"
        f"- {env_labels['home']}: {home}\n"
        f"- {env_labels['cwd']}: {cwd}\n"
        f"- {env_labels['os']}: {platform.system()} ({platform.release()})\n"
        f"- {env_labels['sh']}: {os.environ.get('SHELL', os.environ.get('COMSPEC', 'unknown'))}"
    )

    # Skill catalog（统一能力模型）：只注入目录，模型按需调用 use_skill 取全文
    _catalog = capability_registry.skill_catalog()
    if _catalog:
        catalog_label = _get_localized_text(user_lang, {
            "zh": "## 可用技能（按需调用）",
            "en": "## Available skills (load on demand)",
            "ja": "## 利用可能なスキル（オンデマンド）",
        })
        lines = [catalog_label, "执行以下领域的任务时，先调用 use_skill 工具获取对应技能的完整说明，再按其执行："]
        for s in _catalog:
            desc = (s.get("description") or "").strip()
            lines.append(f"- **{s['name']}**: {desc[:120]}" if desc else f"- **{s['name']}**")
        system_parts.append("\n".join(lines))

    # 上次会话进展（审计 B10）：PROGRESS.md 尾部注入，跨会话断点续作生效
    _tail = _progress_tail()
    if _tail.strip():
        _pt_label = _get_localized_text(user_lang, {
            "zh": "## 上次会话进展（最近记录）",
            "en": "## Recent progress from previous sessions",
            "ja": "## 前回セッションの進捗（最近の記録）",
        })
        system_parts.append(f"{_pt_label}:\n{_tail}\n（以上为历史记录，仅供参考；继续当前任务时请注意衔接。）")

    # Goal mode / progressive delivery
    goal_mode = body.get("goal_mode", False)
    progressive = body.get("progressive_delivery", True)
    extra_prompts = []
    if goal_mode:
        extra_prompts.append(GOAL_MODE_PROMPT)
    if progressive:
        # P0 语境分流：渐进式交付协议（"阶段1骨架/阶段2核心/阶段3完善/每阶段≤30% token"）
        # 是纯写代码的分步规范，对"查行情/分析/聊天"类任务只会诱导模型先写一堆"我将分几步做"
        # 的声明话术，正是"意图声明未行动/8 分钟空转"的帮凶（09-03 事故）。只有任务含明显
        # 代码/文件构建语义时才注入，其余任务改为提示"直接产出完整结果，不要分步声明"。
        _detect_code_task = any(
            k in (last_user_text or "").lower() for k in
            ("代码", "编程", "写函数", "实现", "重构", "类 ", "模块", "接口",
             "compile", "refactor", "implement", "typescript", "python", "function",
             "class ", "module", "api ", "bugfix", "lint", "改代码", "修 bug")
        )
        extra_prompts.append(PROGRESSIVE_DELIVERY_PROMPT if _detect_code_task else
            "## 交付纪律\n"
            "用户等待的是完整结果。不要声明\"你将分几步/我接下来要做什么/让我先查一下\"之类的话术——"
            "要么立即调用工具，要么直接写出包含关键数据与结论的完整回答。"
            "若已有足够工具数据，直接把分析结论写入正文，不要再描述计划。")
    if extra_prompts:
        system_parts.append("\n".join(extra_prompts))

    # Cross-session memory: inject learnings semantically relevant to current query
    recent_data = _retrieve_relevant_learnings(last_user_text, limit=5) if last_user_text else []
    if recent_data:
        recent_data = [r for r in recent_data if r.get("confidence", 0) >= 0.3]
    if recent_data:
        memory_label = _get_localized_text(user_lang, {
            "zh": "以下是 AI 从过去交互学到的相关知识：",
            "en": "Relevant learnings from past interactions:",
            "ja": "過去の対話からの関連知識：",
        })
        system_parts.append(memory_label + "\n" + "\n".join(
            f"- {item['topic']}: {item['content'][:200]}" for item in recent_data
        ))

    # Always-inject high-confidence preferences (independent of query matching)
    high_prefs = _get_high_confidence_preferences()
    if high_prefs:
        pref_lines = []
        for p in high_prefs:
            pref_lines.append(f"- {p['key']}: {p['value']}")
        pref_label = _get_localized_text(user_lang, {
            "zh": "以下是用户的高置信度偏好（每次对话都必须遵守）：",
            "en": "User's high-confidence preferences (must follow every conversation):",
            "ja": "ユーザーの高信頼度設定（毎回の対話で遵守すること）：",
        })
        system_parts.append(pref_label + "\n" + "\n".join(pref_lines))

    # Language enforcement: when user speaks non-Chinese, add strong override
    if user_lang != "zh":
        lang_override = _get_localized_text(user_lang, {
            "en": "CRITICAL LANGUAGE RULE: The user is speaking English. You MUST respond in English only. Do NOT reply in Chinese even if other instructions are in Chinese. This rule overrides all other language preferences.",
            "ja": "【重要】ユーザーは日本語で話しています。必ず日本語で返信してください。他の指示が中国語でも、日本語で応答すること。このルールは他のすべての言語設定より優先されます。",
        })
        system_parts.append(lang_override)

    # Merge all system parts into ONE message (frontend may also send system messages
    # for language / plan mode). Multiple system messages trigger a llama-cpp bug
    # where the model returns empty content → no tool calls → agent stalls.
    frontend_systems = [m["content"] for m in messages if m.get("role") == "system"]
    non_system_msgs = [m for m in messages if m.get("role") != "system"]
    all_system_parts = system_parts + frontend_systems
    merged_system = "\n\n".join(all_system_parts)
    messages = [{"role": "system", "content": merged_system}] + non_system_msgs

    image_base64 = body.get("image_base64")
    image_mime = body.get("image_mime", "image/png")
    if image_base64 and messages:
        messages = _inject_image(messages, image_base64, image_mime)

    return messages


async def _resolve_api_target(cloud_config: dict | None) -> tuple[str, str, dict, bool]:
    """Resolve API URL, protocol, headers, and whether it's a local LLM (no cloud config).
    Cloud models are detected by having an endpoint (key is optional for local proxies).

    async：get_api_url 内含同步健康探测（最长 20s + 空闲复验 3s sleep），
    必须放线程池执行，否则阻塞事件循环（P2-13）。"""
    if cloud_config and cloud_config.get("endpoint"):
        protocol = cloud_config.get("protocol", "openai")
        api_url = cloud_config["endpoint"].rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        key = cloud_config.get("key", "")
        if key and protocol != "local":
            headers["Authorization"] = f"Bearer {key}"
        # If the endpoint points to a local server, treat as cloud (native function calling)
        return protocol, api_url, headers, False
    else:
        from starlette.concurrency import run_in_threadpool
        protocol = "openai"
        local_api = await run_in_threadpool(local_llm.get_api_url)
        if local_api:
            api_url = local_api + "/chat/completions"
        else:
            api_url = ""  # No local LLM running — will be caught as connection error
        headers = {"Content-Type": "application/json"}
        return protocol, api_url, headers, True


# 闲聊识别（17:11 事故根治）：任务词表里的"做"字会把"你能做什么"判成任务型
# ——model 因此进入工具结果追问链。闲聊标记优先于任务词：命中即按非任务处理。
def _get_best_cloud_config() -> dict | None:
    """Get the best available cloud model config for code tasks."""
    try:
        # First try: config.json cloud_models
        config_file = CONFIG_FILE
        if config_file.exists():
            cfg = json.loads(config_file.read_text(encoding="utf-8"))
            models = cfg.get("cloud_models", [])
            # Prefer models with "mini" or "gpt" in name for code tasks
            for m in models:
                if m.get("endpoint"):
                    return {
                        "endpoint": m["endpoint"],
                        "key": m.get("key", ""),
                        "model": m.get("name", ""),
                        "protocol": m.get("protocol", "openai"),
                    }
            # Fallback: first model with endpoint
            for m in models:
                if m.get("endpoint"):
                    return {
                        "endpoint": m["endpoint"],
                        "key": m.get("key", ""),
                        "model": m.get("name", ""),
                        "protocol": m.get("protocol", "openai"),
                    }
    except Exception:
        logger.warning("Failed to read best cloud config", exc_info=True)
    return None
