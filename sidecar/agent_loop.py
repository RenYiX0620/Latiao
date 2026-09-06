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
import tempfile
import threading
import time
import uuid
from contextlib import asynccontextmanager
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
    _extract_learnings_heuristic,
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
                "background": {"type": "boolean", "description": "Run in background without blocking the main conversation. Progress appears in the sub-agent panel. Default false."},
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
    前台模式也进注册表——活动栏实时可见步数/活动摘要，与后台一致。"""
    agent = args.get("agent", "code-reviewer")
    task = args.get("task", "")
    if args.get("background"):
        from tool_executor import _delegate_task_bg
        return _delegate_task_bg(agent, task)
    from tool_executor import _delegate_task_fg
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


def _track_progress(session_id: str, phase: str, action: str):
    """Record agent progress for stagnation detection."""
    if session_id not in _session_states:
        _session_states[session_id] = {"phase": "init", "round": 0, "stalled_rounds": 0, "last_action": "", "history": [], "ts": time.time()}
        # Cap to last 20 sessions to prevent memory leak
        if len(_session_states) > 20:
            # 按最近访问时间淘汰最老的会话（UUID 字典序与活跃度无关）
            oldest = sorted(_session_states, key=lambda k: _session_states[k].get("ts", 0))[:len(_session_states) - 20]
            for k in oldest:
                del _session_states[k]
    s = _session_states[session_id]
    s["ts"] = time.time()  # 每次访问刷新时间戳，淘汰时按 ts 最小（LRU）
    s["round"] += 1
    prev_phase = s["phase"]
    s["phase"] = phase
    s["last_action"] = action
    s["history"].append({"round": s["round"], "phase": phase, "action": action[:100]})
    s["history"] = s["history"][-50:]  # cap to prevent memory leak
    # Detect stall: same phase for 3+ rounds with no tool calls
    if phase == prev_phase and action == "text_only":
        s["stalled_rounds"] += 1
    else:
        s["stalled_rounds"] = 0
    # Prune history to last 20 entries
    if len(s["history"]) > 20:
        s["history"] = s["history"][-20:]


def _check_stagnation(session_id: str) -> str:
    """Return a stagnation warning if agent is stuck, or empty string."""
    s = _session_states.get(session_id)
    if not s or s["stalled_rounds"] < 3:
        return ""
    return (
        f"⚠️ 停滞告警：已连续 {s['stalled_rounds']} 轮无实质推进。"
        f"当前阶段: {s['phase']}。建议：1) 换一个工具 2) 缩小任务范围 3) 直接报告遇到的问题。"
    )


# ═══════════════════════════════════════════════════════
#  Semgrep Security Scan (integrated into auto-verify)
# ═══════════════════════════════════════════════════════

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

async def _agent_loop_stream(messages: list, model: str, api_url: str, headers: dict, session_id: str = "", agent_id: str = "latiao", reflection_mode: str = "off", access_mode: str = "confirm", thinking_level: str = "high"):
    """Agent loop: call LLM with tools. If tool_calls → execute → loop. If text → yield & done."""
    current_msgs = _strip_transient_reminders([dict(m) for m in messages])
    # Two-level compression: keep head + tail, prune middle (MUSE-Autoskill style)
    if len(current_msgs) > 30:
        system_msgs = [m for m in current_msgs if m.get("role") == "system"]
        other_msgs = [m for m in current_msgs if m.get("role") != "system"]
        # Level 1: Prune old tool results beyond the last 5
        tool_count = 0
        for m in reversed(other_msgs):
            if m.get("role") == "tool":
                tool_count += 1
                if tool_count > 5:
                    m["content"] = "[已裁剪旧工具输出]"
        # Level 2: Keep first 3 non-system messages + last 15 (head+tail, discard middle)
        if len(other_msgs) > 25:
            head = other_msgs[:3]
            tail = other_msgs[-15:]
            current_msgs = system_msgs + head + [
                {"role": "system", "content": "[中间对话已压缩。继续当前任务。]"}
            ] + tail
        else:
            current_msgs = system_msgs + other_msgs[-20:]
    if not session_id:
        session_id = str(uuid.uuid4())

    # Detect user language for localized system messages
    last_user_text = _extract_last_user_text(current_msgs)
    lang = _detect_user_language(last_user_text) if last_user_text else "zh"

    max_retries = 3
    retry_count = 0
    last_verify_failed = False
    stagnation = 0             # consecutive unproductive iterations
    has_called_tool = False
    max_stagnation = 10          # exit after this many dead-end rounds
    recent_tool_calls: set[str] = set()  # signature = "tool_name:arg_hash"
    iteration = 0
    text_only_streak = 0   # 与 local loop 对齐，消除空响应分支 (text_only_streak += 1) 的 NameError 崩溃
    text_output_delivered = False  # nudge 重试期间抑制已交付文本的重复流式输出
    _empty_name_streak = 0  # 空名连续失败计数（云端曾缺初始化 → 3 次中止静默失效）
    lang_retry_done = False
    _empty_name_seen = False  # 空名发生→下一轮并行调用关闭（序列化修复假设）
    # 进展感知看门狗：无进展静默期硬上限 15 分钟。此前模型服务器偶发 hold
    # 连接滴灌字节可绕过单次 read timeout（180s×N），用户面对 18 分钟无响应。
    # 纯墙钟一刀切会误杀正常推进的长任务（如多轮深度研究），改为
    # 每轮有实质进展（内容产出/工具执行）就顺延——只有连续 15 分钟
    # 完全无进展才中止。
    loop_deadline = time.monotonic() + 900

    # ── Self-Learning: Heuristic extraction + learning_context via _build_chat_messages ──
    if last_user_text:
        _extract_learnings_heuristic(last_user_text, session_id)

    # ── Dynamic Tool Filtering + Agent restrictions ──
    agent_tools = _get_agent_tools(agent_id, TOOLS)
    active_tools = _filter_tools(last_user_text, agent_tools) if last_user_text else agent_tools
    active_tools = _filter_tools_by_access(active_tools, access_mode)
    # Cap tools to prevent overflowing model context
    if len(active_tools) > 5:
        active_tools = _cap_tools(active_tools, 8)
    active_tools = _ensure_market_tools(active_tools, last_user_text)

    async with httpx.AsyncClient(timeout=httpx.Timeout(120)) as client:
        # ── 规划模式：复杂任务先生成执行计划（显示给用户，等确认后执行） ──
        if _should_plan(last_user_text, _is_local_llm_url(api_url)) or _normalize_access(access_mode) == "plan":
            _plan = await _generate_plan(last_user_text, model, api_url, headers, client)
            if _plan:
                yield {"event": "agent_plan", "content": _plan}
                # 计划门控：先发 plan_confirm 事件（前端渲染确认卡），再等待结果。
                # 事件若攒到确认完成后才发，前端在等待期间收不到 → 弹窗死锁。
                plan_id = f"plan_{uuid.uuid4()}"
                plan_started = await _start_plan_confirmation(plan_id, _plan)
                yield plan_started["event"]
                approved, plan_events = await _wait_plan_confirmation(plan_id, plan_started["event_obj"])
                for ev in plan_events:
                    yield ev
                if not approved:
                    _track_progress(session_id, "plan_rejected", "user_denied_plan")
                    yield {"content": "\n\n⏹️ 计划已被拒绝，任务未执行。你可以调整要求后重新发起。"}
                    return
                current_msgs.insert(0, {"role": "system",
                    "content": "以下是已确认（用户批准）的执行计划，请严格按计划逐步执行（可调用工具）：\n" + _plan})
            elif _normalize_access(access_mode) == "plan":
                # 计划模式档下计划生成失败 → 中止而非裸执行（用户明确要求先出计划）；
                # 自动触发的规划（非计划模式档）保留降级直执行的旧行为
                yield {"content": "\n\n⚠️ 计划模式：计划生成失败，任务未执行。请重试或切换到其他模式。"}
                _track_progress(session_id, "plan_generate_failed", "cloud")
                return

        while iteration < 50:  # hard cap at 50, dynamic exit via stagnation
            iteration += 1
            if _session_cancel_requested(session_id):
                _track_progress(session_id, "cancelled", "user_stop")
                logger.info(f"[AGENT] Iteration {iteration}: 会话取消（新消息/手动停止），任务中止")
                yield {"content": "\n\n⏹️ 任务已停止。"}
                return
            if time.monotonic() > loop_deadline:
                logger.error("[AGENT] 连续 15 分钟无进展，中止任务")
                yield {"content": "\n\n⚠️ 任务连续 15 分钟无进展（未产出内容或执行工具），已中止。模型服务可能异常（如响应停滞）。可重试或检查网络。"}
                _track_progress(session_id, "stalled", "total_duration_limit")
                return
            # 轮次透明化（09-05 23:52 与本地循环同口径）：多轮拉锯不再静默黑盒
            yield {"event": "round_start", "iteration": iteration}
            _maybe_add_inline_file_note(current_msgs, last_user_text)
    # Re-evaluate tool set every 3 iterations for multi-step tasks
            if iteration > 1 and iteration % 3 == 0:
                # 恢复全量工具，但仍须套用权限过滤（read_only 等模式不可绕过）
                # （本函数为云端循环，无 is_local 变量；本地循环独立实现）
                active_tools = _cap_tools(_filter_tools_by_access(agent_tools, access_mode), 8)
            # ── Auto-Fix: if last verify failed, include error context ──
            if last_verify_failed and retry_count < max_retries:
                current_msgs.append({
                    "role": "system",
                    "content": (
                        f"⚠️ 上一轮验证失败（第 {retry_count}/{max_retries} 次重试）。"
                        f"请分析验证报告中的 ❌ 项，修正问题后重新执行。"
                        f"如果 tsc 报错，请 read_file 查看错误文件 → 修复 → 重新 write_file → 再次验证。"
                    ),
                })
                retry_count += 1
                last_verify_failed = False

            # ── Stagnation detection ──
            stagnation_warning = _check_stagnation(session_id)
            if stagnation_warning:
                current_msgs.append({"role": "system", "content": stagnation_warning})

            # DeepSeek 推理模型: 每轮迭代前确保 tool_calls 的 assistant
            # 消息带 reasoning_content(旧消息或思考文本场景)
            for _m in current_msgs:
                if (_m.get("role") == "assistant" and _m.get("tool_calls")
                        and "reasoning_content" not in _m):
                    _m["reasoning_content"] = ""
            # cloud_config 指向本地引擎（如本地 MLX 代理）时，多个 system
            # 消息同样会被 mlx 拒绝——发送前统一合并（P2-11）
            _msgs_for_body = _merge_system_messages(_sanitize_tool_messages(current_msgs))
            body = {
                "model": model, "messages": _msgs_for_body,
                "tools": active_tools, "tool_choice": "auto",
                "max_tokens": _resolve_max_tokens(model), "stream": True,
                # 工具调用确定性（Nous Hermes 指引：function calling 建议 0 温度，
                # 防 JSON/结构损坏——09-21 实测空名/坏参数的概率来源）
                "temperature": 0.0,
                "frequency_penalty": 0.6,
                "stop": ["<|im_end|>", "<|endoftext|>", "<end_of_turn>", "<eos>"],
            }
            _inject_thinking_disabled(body, model, thinking_level)
            if _empty_name_seen:
                # 并行关闭重试（09-21 22:15 实测：并行多 tool_calls 名称字段全空）
                body["parallel_tool_calls"] = False
            # 私有标记（_thinking_*）仅为内部审计/提示用，绝不能发给 API（未知字段 400）
            body.pop("_thinking_level", None)
            body.pop("_thinking_unsupported", None)

            streamed_text = ""
            reasoning_text = ""  # 累积 reasoning_content——DeepSeek 推理模型要求传回
            tool_call_bufs: dict[int, dict] = {}
            _raw_delta_count = 0  # 复读循环检测节流计数（每 40 个 delta 检查一次）
            _dedup_fired = False  # 去重一次性截断标志（审计 A5）

            # 流式总超时保护：Qwen3.8 等 27B 推理模型在流式推理时可能让
            # mlx_lm.server 挂起（端口活、不吐响应头，httpx read timeout 不触发
            # ——22:27 事故：卡 8 分钟无进展）。响应头等待 + 后续迭代统一受
            # 180s 硬超时约束，超时抛 TimeoutError 由外层零交付重试接管。
            stream_ctx = client.stream("POST", api_url, json=body, headers=headers)
            try:
                async with asyncio.timeout(180):
                    async with stream_ctx as r:
                        if r.status_code != 200:
                            try:
                                err_body = (await r.aread()).decode("utf-8", errors="replace")[:800]
                            except Exception:
                                err_body = "<read failed>"
                            logger.error("Agent stream HTTP %d body: %s", r.status_code, err_body)
                        r.raise_for_status()  # httpx 不自动抛 4xx/5xx，必须显式检查
                        # 流式停顿检测：连续 180s 无数据视为僵死（大模型可能缓慢滴灌，120s 超时永不触发）
                        # 60s 分片等待 + 静默期心跳（P0-4，与本地循环 3465-3480 对齐）：
                        # 推理模型（DeepSeek 等）长思考时流中无字节，前端 180s 看门狗会
                        # 掐断连接——每 60s 静默推一个 heartbeat 续命，累计达到停滞阈值
                        # （首 token 90s / 之后 180s）才判僵死。此前云端循环无任何心跳，
                        # 长静默期前端必然断流（09-04 云端"执行一半停"根因）。
                        aiter = r.aiter_lines()
                        _fence_filter = _ThinkFenceFilter()
                        _body_out = False  # 本轮是否产出过正文增量（收尾全量修正用）
                        _silent_secs = 0  # 连续静默秒数（心跳与停滞判定共用，P0-4）
                        while True:
                            try:
                                # 60s 分片：静默超 60s 发一次心跳保活，累计静默达
                                # 停滞阈值（首 token 90s / 之后 180s）才判僵死
                                _stall_timeout = 90 if (not _body_out and _raw_delta_count == 0) else 180
                                line = await asyncio.wait_for(anext(aiter), timeout=60)
                                _silent_secs = 0
                            except asyncio.TimeoutError:
                                _silent_secs += 60
                                if _silent_secs < _stall_timeout:
                                    # 尚未到停滞阈值：发心跳，前端看门狗续命
                                    yield {"event": "heartbeat"}
                                    continue
                                raise TimeoutError(f"模型输出停滞超 {_stall_timeout:.0f} 秒（模型可能过大或未加载完）：{model[:60]}")
                            except StopAsyncIteration:
                                break
                            if line and line.startswith("data: "):
                                try:
                                    _done, delta = _parse_delta_line(line)
                                    if _done:
                                        break
                                    if delta is None:
                                        continue  # usage-only chunk（仅 token 统计，无 delta）
                                    _raw_delta_count += 1

                                    content = delta.get("content", "")
                                    reasoning = delta.get("reasoning", "")
                                    if content:
                                        streamed_text += content
                                        # 复读循环检测（节流）——放在 dedup 过滤之前，
                                        # 复读被 dedup 过滤时也要能截断
                                        if _raw_delta_count % 40 == 0 and _detect_text_loop(streamed_text):
                                            logger.warning("[AGENT] 检测到输出复读循环，截断本轮生成")
                                            streamed_text = _strip_repeat_tail(streamed_text)
                                            raise _GenerationLoopError("输出复读循环，已截断")
                                        # 自我介绍去重：只截断一次，之后照常流式输出
                                        # （审计 A5：此前命中后永久吞掉后续真实内容）
                                        if not _dedup_fired:
                                            _ded = _deduplicate_response(streamed_text)
                                            if len(_ded) < len(streamed_text):
                                                _dedup_fired = True
                                                streamed_text = _ded + content
                                        if text_output_delivered:
                                            # 追问续写轮：替换上一条而非追加（重复堆叠修复）
                                            if _raw_delta_count % 40 == 0:
                                                yield {"event": "content_revised", "content": _strip_think_fences(streamed_text)}
                                            continue
                                        # Filter native control tokens so the UI doesn't show
                                        # raw <|tool_call|> / <|channel> / <|channel|> markers
                                        clean = _NATIVE_CONTROL_RE.sub("", content)
                                        # 剥掉 think 围栏标记（```think>/```think<）——流式渲染时
                                        # ReactMarkdown 把它当未闭合代码块 → 后续正文灰框；
                                        # 用缓冲过滤器捕获被 tokenizer 拆分的围栏
                                        clean = _fence_filter.feed(clean)
                                        if clean:
                                            _body_out = True
                                            yield {"content": clean}
                                        if len(streamed_text) < 5:
                                            _track_progress(session_id, "generating", "text_start")
                                    elif reasoning:
                                        # Reasoning model (Qwen3.6, DeepSeek-R1, etc.) — stream thinking as content
                                        # so the UI doesn't appear frozen during the thinking phase
                                        reasoning_text += reasoning
                                        streamed_text += reasoning
                                        if _raw_delta_count % 40 == 0 and _detect_text_loop(streamed_text):
                                            logger.warning("[AGENT] 检测到输出复读循环，截断本轮生成")
                                            streamed_text = _strip_repeat_tail(streamed_text)
                                            raise _GenerationLoopError("输出复读循环，已截断")
                                        if not _dedup_fired:
                                            _ded = _deduplicate_response(streamed_text)
                                            if len(_ded) < len(streamed_text):
                                                _dedup_fired = True
                                                streamed_text = _ded + reasoning
                                        if text_output_delivered:
                                            if _raw_delta_count % 40 == 0:
                                                yield {"event": "content_revised", "content": _strip_think_fences(streamed_text)}
                                            continue
                                        yield {"reasoning": reasoning, "ts": int(time.time() * 1000)}

                                    for tc_delta in delta.get("tool_calls", []):
                                        idx = tc_delta.get("index", 0)
                                        if idx not in tool_call_bufs:
                                            tool_call_bufs[idx] = {
                                                "id": "", "type": "function",
                                                "function": {"name": "", "arguments": ""},
                                            }
                                        buf = tool_call_bufs[idx]
                                        if "id" in tc_delta:
                                            buf["id"] = tc_delta["id"]
                                        if "function" in tc_delta:
                                            if "name" in tc_delta["function"]:
                                                buf["function"]["name"] = tc_delta["function"]["name"]
                                            if "arguments" in tc_delta["function"]:
                                                buf["function"]["arguments"] += tc_delta["function"]["arguments"]
                                except (json.JSONDecodeError, KeyError, TypeError, IndexError):
                                    pass  # Malformed SSE delta — skip this event, try next
                                except _GenerationLoopError:
                                    # 复读截断：停止消费本轮流，已产出（裁尾后）文本
                                    # 交给后续工具解析/收尾闸门，会话继续（18:01 事故）
                                    break
                                except Exception:
                                    logger.error("SSE tool_call parse error", exc_info=True)
                                    raise  # Real errors (network, memory) must surface
            except TimeoutError:
                # 引擎挂起/超时（22:27 事故：Qwen3.8 端口活但流不出数据，
                # httpx read timeout 不触发）。不能只抛错——引擎还挂着，
                # 下一轮还会超时。这里杀进程+触发重载，让后续请求自愈。
                # ⚠️ 仅本地引擎路径可杀——云端请求超时/复读截断也抛
                # TimeoutError，若误杀本地引擎会让已加载的模型白重载一轮
                # （审计 P1：云端 stall 杀死本地引擎 + 内存尖峰）。
                if _is_local_llm_url(api_url):
                    logger.warning("本地流 180s 超时，判定引擎挂起，杀进程并触发重载")
                    try:
                        import local_llm as _llm_mod
                        _eng = _llm_mod._engine
                        if _eng.current_model_id:
                            _eng._kill_port(_eng.server_port)
                            _eng.server_status = "stopped"
                            _eng._request_reload(_eng.current_model_id)
                    except Exception:
                        logger.warning("超时后引擎重载触发失败", exc_info=True)
                else:
                    logger.warning("云端流超时/复读截断，不影响本地引擎")
                raise TimeoutError(f"流式响应超时（180s 无进展）：{model[:60]}")

            # 收尾修正：本轮产过正文就发一次全量替换（前端支持任意时刻
            # content_revised），把流中可能残留的围栏在收尾统一剥干净（双保险）
            if (text_output_delivered or _body_out) and streamed_text.strip():
                yield {"event": "content_revised", "content": _strip_think_fences(streamed_text)}

            if tool_call_bufs:
                tool_calls = [tool_call_bufs[i] for i in sorted(tool_call_bufs.keys())]
            elif streamed_text and _NATIVE_TOOL_RE.search(streamed_text):
                # Native tool call format from models like Gemma —
                # parse <|tool_call|>call:name{args}<tool_call|> → OpenAI tool_calls
                tool_calls = _parse_native_tool_calls(streamed_text)
                if tool_calls:
                    streamed_text = _strip_native_tool_calls(streamed_text)
                else:
                    tool_calls = []
            else:
                tool_calls = []

            if not tool_calls:
                # 纯文本轮计入停滞计数（工具轮/完成轮会复位，P2-12）
                _track_progress(session_id, "text_round", "text_only")
                if streamed_text.strip():
                    loop_deadline = time.monotonic() + 900  # 产出内容=实质进展，顺延看门狗

            if tool_calls:
                loop_deadline = time.monotonic() + 900  # 实质进展：顺延无进展看门狗
                _append_loop_log(f"Iteration {iteration}: found {len(tool_calls)} tool(s): {[tc.get('function',{}).get('name') for tc in tool_calls]}\n")
                _track_progress(session_id, "tool_calling", f"{len(tool_calls)} tool(s)")
                logger.info(f"[AGENT] Iteration {iteration}: {len(tool_calls)} tool(s) called, msgs_in_context={len(current_msgs)}")

                current_msgs.append({
                    "role": "assistant",
                    "content": _deduplicate_response(streamed_text) if streamed_text else None,
                    # DeepSeek 推理模型: tool_calls 的 assistant 消息必须带
                    # reasoning_content,否则下一轮 400
                    "reasoning_content": reasoning_text,
                    "tool_calls": tool_calls,
                })
                has_called_tool = True
                text_output_delivered = False  # 工具被调用=实质推进，后续文本是新的最终回复，恢复流式输出

                # Stagnation check: reset if new tool calls, else count toward limit
                any_new = False
                round_failed = False
                for tc in tool_calls:
                    sig = f"{tc.get('function',{}).get('name','')}:{hash(str(tc.get('function',{}).get('arguments','')))}"
                    if sig not in recent_tool_calls:
                        recent_tool_calls.add(sig)
                        any_new = True
                    if _session_cancel_requested(session_id):
                        _track_progress(session_id, "cancelled", "user_stop")
                        logger.info(f"[AGENT] tool 执行前检测到取消（新消息/手动停止），任务中止")
                        yield {"content": "\n\n⏹️ 任务已停止。"}
                        return
                    # 先发确认事件再等待（死锁修复）：confirm 级工具的
                    # tool_confirm 必须在执行前到达前端，弹窗才会出现
                    if not tc.get("id"):
                        tc["id"] = str(uuid.uuid4())
                    pre_started = None
                    try:
                        _tname = tc.get("function", {}).get("name", "unknown")
                        _targs = json.loads(tc.get("function", {}).get("arguments", "{}") or "{}")
                        # 空名快速中止（09-21 实测：云端模型连续 11 轮输出空工具名，
                        # 守卫逐轮弹回但循环不收敛——3 次即中止并给出可操作诊断）。
                        if not _tname.strip():
                            _empty_name_streak += 1
                            _empty_name_seen = True
                            logger.warning("empty tool name streak=%s args=%s raw_calls=%s",
                                           _empty_name_streak, json.dumps(_targs, ensure_ascii=False)[:200],
                                           json.dumps([tc.get("function", {}) for tc in tool_calls], ensure_ascii=False)[:400])
                            if _empty_name_streak >= 3:
                                yield {"content": ("\n\n⛔ 模型连续 3 次输出空工具名（工具调用格式异常）。"
                                                   "任务已中止。请切换为其他模型，或在模型页重新加载后重试。")}
                                _track_progress(session_id, "stalled", f"empty_tool_name x{_empty_name_streak}")
                                return
                            # 1-2 次不 continue：守卫反馈（空名提示）必须进模型上下文，
                            # 让模型下一轮改格式；只有第 3 次才中止。
                        if _resolve_permission(_tname, _targs) == "confirm" \
                                and not _confirm_bypassed(_tname, access_mode) \
                                and not _check_access(_tname, access_mode):
                            pre_started = await _start_tool_confirmation(tc["id"], _tname, _targs)
                            yield pre_started["event"]
                    except Exception:
                        pre_started = None
                    verify_failed, events = await _handle_tool_execution(
                        tc, current_msgs, session_id, agent_id, access_mode, pre_started=pre_started)
                    logger.info(f"[AGENT] Iteration {iteration}: tool={tc.get('function',{}).get('name','')} executed, result_len={len(current_msgs[-1].get('content','')) if current_msgs else 0}")
                    for evt in events:
                        yield evt
                    denied = any(
                        isinstance(e, dict) and str(e.get("result", "")).startswith("⛔ User denied")
                        for e in events)
                    if verify_failed and not denied:
                        round_failed = True
                        last_verify_failed = True
                # 新的调用签名，或本轮有工具失败（模型正在尝试修复）都算实质推进，不计停滞
                if any_new or round_failed:
                    stagnation = 0
                    if any_new:
                        _empty_name_streak = 0  # 合法工具调用复位空名计数
                else:
                    stagnation += 1
                    if stagnation >= max_stagnation:
                        yield {"content": f"\n\n⚠️ 连续 {stagnation} 轮无新进展，Agent 停止。如需继续请发新消息。"}
                        return
                continue

            # Text response — already streamed word-by-word
            # Check if there are pending tasks: model returned text after tool result
            has_recent_tool_result = any(
                m.get("role") == "tool" or (isinstance(m.get("content"), str) and m["content"].startswith("[工具结果]"))
                for m in current_msgs[-3:]
            )
            # 只补问一次：模型已交付最终文字后，再拖一轮确认“没有未完工具”，
            # 之后直接结束——此前最多空转 10 轮（每轮 30-60s）→ 用户看到
            # "答案有了但任务 1 分钟才结束"。
            # 非任务消息门槛（17:11 事故同根）：闲聊 + 模型乱调了工具 → 直接收官
            _has_task_kw = any(kw in (last_user_text or "").lower() for kw in (
                "运行", "执行", "做", "帮我", "写", "创建", "查", "搜", "找", "分析",
                "修复", "构建", "部署", "安装", "配置", "列出", "读取", "总结", "告诉",
                "run", "build", "fix", "create", "search", "analyze", "deploy")) \
                and not _is_chat_query(last_user_text)
            if has_recent_tool_result and not _has_task_kw and streamed_text.strip():
                _deliver, _lang_retry = await _ensure_final_language_with_retry(client, api_url, headers, model, streamed_text.strip(), last_user_text, current_msgs, lang_retry_done=lang_retry_done)
                if _lang_retry and not lang_retry_done:
                    lang_retry_done = True
                    text_output_delivered = True
                    text_only_streak += 1
                    yield {"event": "heartbeat"}
                    continue
                current_msgs.append({"role": "assistant", "content": _deliver})
                if _deliver != streamed_text.strip():
                    yield {"event": "content_revised", "content": _deliver}
                _track_progress(session_id, "completed", f"text_response ({len(_deliver)} chars)")
                return
            if has_recent_tool_result and text_only_streak < 1 and streamed_text.strip():
                if len(streamed_text.strip()) >= 200:
                    # 工具结果后已给出实质性回答——接受为最终答案直接收尾
                    # （与 local 循环同口径，防追问后重答堆叠）
                    # 语言确保：不符则以 content_revised 整体替换上一条（云端仍直播，
                    # 遵循度好，属兜底；16:50 事故同款保护）
                    _deliver, _lang_retry = await _ensure_final_language_with_retry(client, api_url, headers, model, streamed_text.strip(), last_user_text, current_msgs, lang_retry_done=lang_retry_done)
                    if _lang_retry and not lang_retry_done:
                        lang_retry_done = True
                        text_output_delivered = True
                        text_only_streak += 1
                        yield {"event": "heartbeat"}
                        continue
                    if _deliver != streamed_text.strip():
                        yield {"event": "content_revised", "content": _deliver}
                    current_msgs.append({"role": "assistant", "content": _deliver})
                    if _should_reflect(reflection_mode, _deliver, _is_local_llm_url(api_url)):
                        _tool_outs = [str(m.get("content") or "") for m in current_msgs if m.get("role") == "tool"]
                        _revised, _changed = await _reflect_output(_deliver, model, api_url, headers, reflection_mode, client, _tool_outs)
                        if _changed and _revised.strip():
                            streamed_text = _revised
                            yield {"event": "reflection_revised", "content": _revised}
                    _track_progress(session_id, "completed", f"text_response ({len(_deliver)} chars)")
                    return
                current_msgs.append({
                    "role": "system",
                    "content": (
                        "⚠️ 你刚才收到了工具的执行结果，但只回复了文字而没有继续调用工具。\n"
                        "请检查：用户的任务是否真的完全完成了？\n"
                        "如果还没完成，请继续调用工具。如果确实完成了，请回复最终结果。"
                    ),
                })
                text_output_delivered = True  # 文本已交付，nudge 重试不再重复输出
                text_only_streak += 1
                continue
            if not has_called_tool and text_only_streak < 3 and streamed_text.strip():
                # Model gave a text response without calling tools.
                # Record the response so the model knows it already replied.
                # ≥200 字符视为实质回答直接交付（09-21 实测：任务词 nudge 会让
                # R2 正文被抑制、用户看不到分析——本地同口径的 ≥200 规则）
                if len(streamed_text.strip()) >= 200:
                    _deliver, _lang_retry = await _ensure_final_language_with_retry(client, api_url, headers, model, streamed_text.strip(), last_user_text, current_msgs, lang_retry_done=lang_retry_done)
                    if _lang_retry and not lang_retry_done:
                        lang_retry_done = True
                        text_output_delivered = True
                        text_only_streak += 1
                        yield {"event": "heartbeat"}
                        continue
                    current_msgs.append({"role": "assistant", "content": _deliver})
                    if _deliver != streamed_text.strip():
                        yield {"event": "content_revised", "content": _deliver}
                    _track_progress(session_id, "completed", f"text_response ({len(_deliver)} chars)")
                    return
                current_msgs.append({"role": "assistant", "content": streamed_text.strip()})
                # 非任务型消息（闲聊/陈述/提问/长回复）→ 文本已交付给用户，直接结束，不再 nudge 重发
                user_q = last_user_text.strip().rstrip("?？") if last_user_text else ""
                has_task_kw = any(kw in user_q for kw in ["运行", "执行", "做", "帮我", "写", "创建", "查", "搜", "找", "分析", "修复", "构建", "部署", "安装", "配置", "run", "build", "fix", "create", "search", "analyze", "deploy"])
                if not has_task_kw:
                    # ── 输出反思（可选档位）：修正后前端替换最后一条消息 ──
                    if _should_reflect(reflection_mode, streamed_text, _is_local_llm_url(api_url)):
                        _tool_outs = [str(m.get("content") or "") for m in current_msgs if m.get("role") == "tool"]
                        _revised, _changed = await _reflect_output(streamed_text, model, api_url, headers, reflection_mode, client, _tool_outs)
                        if _changed and _revised.strip():
                            streamed_text = _revised
                            yield {"event": "reflection_revised", "content": _revised}
                    # 语言兜底：不符则以 content_revised 整体替换（同 2448 路径）
                    _deliver, _lang_retry = await _ensure_final_language_with_retry(client, api_url, headers, model, streamed_text.strip(), user_q, current_msgs, lang_retry_done=lang_retry_done)
                    if _lang_retry and not lang_retry_done:
                        lang_retry_done = True
                        text_output_delivered = True
                        text_only_streak += 1
                        yield {"event": "heartbeat"}
                        continue
                    if _deliver != streamed_text.strip():
                        yield {"event": "content_revised", "content": _deliver}
                    _track_progress(session_id, "completed", f"text_response ({len(_deliver)} chars)")
                    return
                # 任务型请求但模型只回文字不调工具 → nudge 促其行动（不再向用户重复流式输出）
                current_msgs.append({
                    "role": "user",
                    "content": (
                        "不要写执行计划，直接行动。需要用什么工具就立即调用；"
                        "若本次任务基于用户消息里已提供的资料即可完成，请直接给出完整回答，不要做声明或收尾。"
                    ),
                })
                text_output_delivered = True
                text_only_streak += 1
                continue
            if not streamed_text.strip() and text_only_streak < max_stagnation:
                logger.warning(f"[AGENT] Iteration {iteration}: empty response from cloud model, retrying")
                nudge_text = _get_localized_text(lang, {
                    "zh": "⚠️ 你上一轮的回复是空的。请直接回复用户，或者使用工具完成任务。",
                    "en": "⚠️ Your last response was empty. Please respond to the user directly, or use a tool.",
                    "ja": "⚠️ 前回の応答が空でした。ユーザーに直接返信するか、ツールを使用してください。",
                })
                # nudge 用 user 角色（09-05 23:52 与本地循环同口径：追加在
                # 末尾的 system 语义错误且各引擎容忍度不一）
                current_msgs.append({"role": "user", "content": nudge_text})
                text_only_streak += 1
                continue
            # ── Empty-response exhaustion：streak 耗尽且模型仍无输出。
            # 不能静默 completed 返回——用户会看到"执行一半就停了"且零提示。
            # 与本地循环的诊断分支对齐（同一 bug 只修过一边）。
            if not streamed_text.strip():
                logger.warning(f"[AGENT] Iteration {iteration}: {text_only_streak} consecutive empty responses, aborting with diagnostic")
                yield {"content": (
                    "\n\n⚠️ **模型连续多次返回空响应，任务已中止。**\n"
                    "可能原因：\n"
                    "1. 云端服务限流或降级（如上下文超限被截断）\n"
                    "2. 模型服务异常\n"
                    "建议：检查云端配置或稍后重试；若反复出现请缩短对话长度。"
                )}
                _track_progress(session_id, "stalled", f"empty_response x{text_only_streak}")
                return
            # ── 输出反思（可选档位）：修正后前端替换最后一条消息 ──
            if _should_reflect(reflection_mode, streamed_text, _is_local_llm_url(api_url)):
                _tool_outs = [str(m.get("content") or "") for m in current_msgs if m.get("role") == "tool"]
                _revised, _changed = await _reflect_output(streamed_text, model, api_url, headers, reflection_mode, client, _tool_outs)
                if _changed and _revised.strip():
                    streamed_text = _revised
                    yield {"event": "reflection_revised", "content": _revised}

            _track_progress(session_id, "completed", f"text_response ({len(streamed_text)} chars)")
            return

        # Hard cap reached (50 iterations) — extremely rare with dynamic stagnation
        tool_count = sum(1 for m in current_msgs if m.get("role") == "tool")
        yield {"content": f"\n\n⚠️ 已达到硬上限 (50 轮)。本会话共执行了 {tool_count} 次工具调用。如需继续，请发送新消息。"}


# ═══════════════════════════════════════════════════════
# ╔══════════════════════════════════════════════════════╗
# ║  SECTION 8: Local Agent Loop                         ║
# ║  Prompt-based tool calling for local models           ║
# ╚══════════════════════════════════════════════════════╝

#  Local Agent Loop — Prompt-based tool calling
#  For local models that don't support OpenAI function calling.
#  Injects tools as formatted text in a system message, and
#  parses the model's textual tool invocation commands.
# ═══════════════════════════════════════════════════════

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



def _append_unique_system(current_msgs: list, content: str) -> None:
    """nudge 性质的多轮系统提醒只保留最新一条。

    此前每条 nudge 都追加一条 system 消息，3 轮打回后上下文里堆积多条
    "任务尚未完成/你必须继续"——27B 模型据此判定"用户在同一问题上反复
    发消息，对话被循环构造"，陷入元认知死循环（09-04 23:02 事故）：
    模型在思考段里与"循环"搏斗 3 分钟，正文只给出 30 字。同文案只留
    最新一条，模型看到的是"一次提醒"，不再产生"重复模式"解读。
    """
    for i in range(len(current_msgs) - 1, -1, -1):
        m = current_msgs[i]
        if m.get("role") == "system" and isinstance(m.get("content"), str)                 and m["content"][:40] == content[:40]:
            current_msgs[i] = {"role": "system", "content": content}
            return
    current_msgs.append({"role": "system", "content": content})

async def _local_agent_loop_stream(messages: list, model: str, api_url: str, headers: dict,
                                    session_id: str = "", agent_id: str = "latiao", reflection_mode: str = "off", access_mode: str = "confirm", thinking_level: str = "high"):
    """Local model agent loop: inject tools as prompt, parse tool calls from text."""
    current_msgs = _strip_transient_reminders([dict(m) for m in messages])
    # Truncate long history to prevent context overflow.
    # Keeps system messages + last 20 user/assistant pairs.
    # Also estimates token count to warn before overflow.
    if len(current_msgs) > 50:
        system_msgs = [m for m in current_msgs if m.get("role") == "system"]
        other_msgs = [m for m in current_msgs if m.get("role") != "system"]
        # Level 1: Prune old tool results: keep last 10, truncate older ones
        tool_count = 0
        for m in reversed(other_msgs):
            if m.get("role") == "tool" or (isinstance(m.get("content"), str) and m["content"].startswith("[工具结果]")):
                tool_count += 1
                if tool_count > 10:
                    m["content"] = "[已裁剪旧工具输出]"
        # Level 2: Keep head (first 5) + tail (last 25), discard middle (MUSE-Autoskill style)
        if len(other_msgs) > 40:
            head = other_msgs[:5]
            tail = other_msgs[-25:]
            current_msgs = system_msgs + head + [
                {"role": "system", "content": "[中间对话已压缩。继续当前任务。]"}
            ] + tail
        else:
            current_msgs = system_msgs + other_msgs[-30:]
    # Rough token estimate: ~2 chars per token for Chinese
    total_chars = sum(len(str(m.get("content", ""))) for m in current_msgs)
    if total_chars > 80000:
        # 强警告：上下文随时可能溢出（必须先于 60000 判断，否则此分支不可达）
        logger.warning(f"[LOCAL-AGENT] Context may overflow: ~{total_chars} chars (~{total_chars//2} tokens)")
    if total_chars > 60000:
        # Context Anxiety prevention: save progress and suggest restart (Harness pattern)
        logger.warning(f"[LOCAL-AGENT] Context near limit: ~{total_chars} chars (~{total_chars//2} tokens). Saving progress.")
        # Write PROGRESS.md with current state
        try:
            last_user = _extract_last_user_text(current_msgs)
            _record_progress(f"⚠️ 自动存档（上下文 {total_chars//2} tokens）\n最后用户消息: {last_user[:200] if last_user else '(无)'}")
        except Exception:
            logger.warning("Failed to save progress during context-anxiety", exc_info=True)
        _ctx_lang = _detect_user_language(_extract_last_user_text(current_msgs)) if 'current_msgs' in dir() and current_msgs else "zh"
        _ctx_msg = _get_localized_text(_ctx_lang, {
            "zh": f"💡 **上下文接近上限**（~{total_chars//2} tokens）。建议：\n1. 当前进度已自动保存到 PROGRESS.md\n2. 开一个新会话，Agent 会从断点继续\n3. 或继续在本会话中完成（质量可能下降）",
            "en": f"💡 **Context limit approaching** (~{total_chars//2} tokens). Suggestions:\n1. Progress auto-saved to PROGRESS.md\n2. Start a new session — Agent continues from checkpoint\n3. Or continue here (quality may degrade)",
            "ja": f"💡 **コンテキスト上限に近づいています**（~{total_chars//2} tokens）。提案：\n1. 進捗は PROGRESS.md に自動保存済み\n2. 新しいセッションを開始 — エージェントは中断から続行\n3. このまま続行（品質が低下する可能性があります）",
        })
        yield {"content": "\n\n" + _ctx_msg}
    if not session_id:
        session_id = str(uuid.uuid4())

    max_iterations = 50
    iteration = 0
    _empty_name_streak = 0  # 本地循环空名计数（与云端口径一致，3 次中止）
    lang_retry_done = False
    _empty_name_seen = False  # 空名发生→下一轮并行调用关闭（序列化修复假设）
    recent_tool_calls: set[str] = set()
    stagnation = 0
    max_stagnation = 3  # cap empty-response/dead-end retries to avoid hammering the model server
    text_only_streak = 0
    has_called_tool = False
    text_output_delivered = False  # nudge 重试期间抑制已交付文本的重复流式输出
    _pending_tool_analysis = False  # 工具结果已产出，但尚未收到实质性文字回答
    _intent_nudges = 0              # “只声明不动手/只道歉”的追问计数
    _fabrication_nudges = 0        # 无来源数字拦截计数（19:05 编造事故，≤2 次有界）
    _think_only_nudges = 0         # 思考-only 轮计数（13:29 事故：思考型模型只产思考不写正文）
    _continuation_round_done = False  # native 完成确认轮：一轮内零工具调用的第一条纯文本会被确认一次
    # 编造拦截上限：闸门与兜底路径共用同一口径（此前两处不一致：闸门本地=1、
    # 兜底无条件=2——同一回复走不同分支行为不同）
    _fab_cap = 1 if _is_local_llm_url(api_url) else 2
    _brief_answer_nudged = False  # "资料充足却短回答"的追问只触发一次，防死循环
    # Build tool prompt
    last_user_text = _extract_last_user_text(current_msgs)
    # 学习提取与云端循环同口径（审计 B9）：此前只在云端入口调用，
    # 纯本地用户偏好/知识永不入库，"记住你的偏好"完全失效
    if last_user_text:
        _extract_learnings_heuristic(last_user_text, session_id)
    agent_tools = _get_agent_tools(agent_id, TOOLS)
    # 09-06 13:47 事故：全部权限下"打开相册"被意图筛选+8/12 裁剪剥掉了
    # run_cmd/control_launch——关键词猜不准意图（"打开"匹配不到控制类），
    # 模型只能诚实回答"我没有命令工具"。
    # 09-06 15:40 彻底重构：mlx 自管引擎（原生 function calling）在【全部权限
    # 模式】下都不再筛选裁剪——原生 schema 下 20+ 工具成本很低，判断力交还
    # 模型（与 Codex/ZCode 的薄调度同构）；只按权限模式做访问过滤（执行期
    # _check_access 仍逐次把关）。legacy 围栏路径（外部引擎/回退）维持筛选+cap。
    if _local_native_tools_ok():
        active_tools = list(agent_tools)
        if _normalize_access(access_mode) != "full":
            active_tools = _filter_tools_by_access(active_tools, access_mode)
    else:
        active_tools = _filter_tools(last_user_text, agent_tools) if last_user_text else agent_tools
        active_tools = _filter_tools_by_access(active_tools, access_mode)
        if len(active_tools) > 8:
            active_tools = _cap_tools(active_tools, 12)
    active_tools = _ensure_market_tools(active_tools, last_user_text)
    tools_prompt = _build_local_tools_prompt(active_tools)
    # 4B-class models often have only ~8K ctx. If the system prompt + tool list
    # blows past it, llama.cpp silently truncates the prompt and the model
    # responds empty — which looks like the task "stopped halfway". Keep the
    # first-round prompt small: trim tool list before building if needed.
    if len(tools_prompt) > 4500 and len(active_tools) > 4:
        # tavily_search 必须保住：裁剪后模型还得能联网搜索（否则“查资料”类任务直接废掉）
        _core_tools = {"read_file", "write_file", "list_dir", "run_cmd", "search_files", "tavily_search"}
        trimmed = [t for t in active_tools if t.get("function", {}).get("name") in _core_tools]
        if len(trimmed) < 2:
            trimmed = active_tools[:4]
        logger.info(f"[LOCAL-AGENT] Tools prompt too long ({len(tools_prompt)} chars), trimming to {len(trimmed)} core tools")
        tools_prompt = _build_local_tools_prompt(trimmed) + (
            "\n(其他工具可按需在对话中说明,需要时再调用。)"
        )

    # Detect continuation: if session has tool results but no final answer,
    # inject a strong continuation nudge in the first system message
    tool_result_count = sum(1 for m in current_msgs if m.get("role") == "tool" or (isinstance(m.get("content"), str) and m["content"].startswith("[工具结果]")))
    has_final_answer = any(
        m.get("role") == "assistant" and isinstance(m.get("content"), str) and len(m["content"]) > 100
        for m in current_msgs[-5:]
    ) if len(current_msgs) > 5 else False
    is_continuation = tool_result_count >= 1 and not has_final_answer
    if is_continuation:
        tools_prompt += (
            "\n\n⚠️⚠️⚠️ 注意：这不是用户的新消息，而是系统提醒（你的上一轮回复未完成）！\n"
            f"会话中已有 {tool_result_count} 条工具执行结果，但你尚未给出实质回答。\n"
            "用户只需要一次完整回答——不要再声明将要做什么：若需要更多数据就立即调用工具；"
            "若数据已足够，直接把完整分析（关键数字与结论）写进回复正文。\n"
            "不要重复已经给过的段落，也不要分析'为什么系统在重复提问'。\n"
            "\n📌 输出纪律（必须遵守）：\n"
            "1. 不要把思考过程用 ```think> 代码块围栏输出，也不要在正文里描述'我将要做什么'。\n"
            "2. 思考完成后，把完整分析结论直接写在正文：关键数据、要点、结论。\n"
            "3. 正文禁止出现'让我用…''我先…''接下来我要…'等待办话术——要么立刻调用工具，要么直接写出完整分析。\n"
            "4. 工具执行后不要再声明步骤，直接写结论。"
        )

    # Inject tools into the first user message context
    for m in current_msgs:
        if m.get("role") == "user":
            # Insert tools prompt as a system message right before the last user message
            break

    async with httpx.AsyncClient(timeout=httpx.Timeout(120)) as client:
        # ── 规划模式（本地模型）：计划模式档强制生成计划并等待用户确认 ──
        # 此前本地循环完全没有计划门控：计划模式档形同虚设，模型直接执行
        if _normalize_access(access_mode) == "plan" and last_user_text:
            _plan = await _generate_plan(last_user_text, model, api_url, headers, client)
            if _plan:
                yield {"event": "agent_plan", "content": _plan}
                # 先发 plan_confirm 事件再等待（与云端循环同口径，避免弹窗死锁）
                plan_id = f"plan_{uuid.uuid4()}"
                plan_started = await _start_plan_confirmation(plan_id, _plan)
                yield plan_started["event"]
                approved, plan_events = await _wait_plan_confirmation(plan_id, plan_started["event_obj"])
                for ev in plan_events:
                    yield ev
                if not approved:
                    _track_progress(session_id, "plan_rejected", "user_denied_plan")
                    yield {"content": "\n\n⏹️ 计划已被拒绝，任务未执行。你可以调整要求后重新发起。"}
                    return
                current_msgs.insert(0, {"role": "system",
                    "content": "以下是已确认（用户批准）的执行计划，请严格按计划逐步执行（可调用工具）：\n" + _plan})
            else:
                # 计划模式档下计划生成失败 → 中止而非裸执行（用户明确要求先出计划）
                yield {"content": "\n\n⚠️ 计划模式：计划生成失败，任务未执行。请重试或切换到其他模式。"}
                _track_progress(session_id, "plan_generate_failed", "local")
                return

        # 进展感知无进展看门狗（与云端循环同口径）：连续 15 分钟完全
        # 无进展（未产出内容/未执行工具）才中止；正常推进的长时间
        # 研究任务不受影响
        _no_progress_deadline = time.monotonic() + 900
        # 会话总时长预算（17:52 事故：模型持续换新查询，任何工具调用都重置
        # nudge 计数，无限循环 15 分钟+ 不收尾）——超预算强制终答提取收口。
        # 720s：27B 本地模型 3 次工具 + 写总结实际需 8-11 分钟，480s 会在
        # 模型即将写总结时截断（09-03 20:02 实测 490s 中止），放宽到 12 分钟
        # 09-04 实测 MLX 27B 每轮生成 2-4 分钟，3 轮 + 总结逼近 13 分钟仍被
        # 720s 预算截断（15:34 任务 788s 中止）——本地模型放宽到 30 分钟；
        # 无限循环/僵死由 _no_progress_deadline（15 分钟无进展）看门狗兜底，
        # 有进展的长时间研究任务不应被总时长预算掐断。
        _session_start = time.monotonic()
        _session_budget = time.monotonic() + 1800
        _maybe_add_inline_file_note(current_msgs, last_user_text)
        # 历史预算：旧轮截断（近 3 轮完整），治重历史的首 token 延迟与注意力稀释
        current_msgs = _slim_history_for_local(current_msgs)
        # 09-06 三档模式（原生 function calling 落地）：
        # - light：闲聊快车道——不传工具、不注入提示、关思考（27B 实测 1.3s）
        # - native：自管 mlx 引擎原生 tools 参数（27B 工具轮实测 4.5s，
        #   围栏文字扮演分钟级）——400 回退 legacy
        # - legacy：围栏提示词（外部引擎/GGUF/回退）
        _light_query = _is_light_query(last_user_text, current_msgs)
        _native_tools = (not _light_query) and _local_native_tools_ok() and bool(active_tools)
        _native_fallback_used = False
        while iteration < max_iterations:
            iteration += 1
            if _session_cancel_requested(session_id):
                _track_progress(session_id, "cancelled", "user_stop")
                logger.info(f"[AGENT] Iteration {iteration}: 会话取消（新消息/手动停止），任务中止")
                yield {"content": "\n\n⏹️ 任务已停止。"}
                return
            if time.monotonic() > _session_budget:
                _sout = await _final_answer_extraction(
                    client, api_url, headers, _engine_model, current_msgs,
                    _detect_user_language(_extract_last_user_text(current_msgs)))
                if len(_sout) >= 120:
                    yield {"content": "\n\n" + _strip_think_fences(_sout)}
                    _track_progress(session_id, "completed", f"budget_fallback ({len(_sout)} chars)")
                    logger.info(f"[LOCAL-AGENT] Iteration {iteration}: 会话超时预算，终答提取收口 ({len(_sout)} chars)")
                    return
                yield {"content": f"\n\n⚠️ 会话运行 {int(time.monotonic() - _session_start)} 秒仍未给出答案，已中止。请发「继续」重试。"}
                _track_progress(session_id, "stalled", "session_budget")
                return
            if time.monotonic() > _no_progress_deadline:
                logger.error("[LOCAL-AGENT] 连续 15 分钟无进展，中止任务")
                yield {"content": "\n\n⚠️ 任务连续 15 分钟无进展，已中止。模型服务可能异常（如响应停滞）。可重试或检查网络。"}
                _track_progress(session_id, "stalled", "total_duration_limit")
                return
            _append_loop_log(f"Iteration {iteration}: current_msgs={len(current_msgs)}, roles={[m.get('role') for m in current_msgs[-5:]]}\n")

            # Build messages for this iteration: merge tools + current context
            loop_msgs = list(current_msgs)
            # Convert role:"tool" → role:"user" (llama-cpp Qwen chat format only supports
            # system/user/assistant roles; "tool" role causes empty responses)
            # 语言锚：工具结果紧邻处追加回复语言要求——英文材料（如 PROGRESS.md
            # 工具日志）最容易在这里把 27B 模型带偏成英文（13:58 事故）
            _anchor_lang = _detect_user_language(_extract_last_user_text(current_msgs))
            _anchor_text = _get_localized_text(_anchor_lang, {
                "zh": "（以上工具结果中若含英文内容，那只是数据；请继续用简体中文回复。）",
                "en": "(Any English in the tool result above is just data; keep replying in English.)",
                "ja": "（上記ツール結果に外国語が含まれていても、それはデータです。日本語で返信を続けてください。）",
            })
            loop_msgs = [
                {"role": "user", "content": f"[工具结果] {m['content']}\n\n{_anchor_text}"}
                if m.get("role") == "tool" else dict(m)
                for m in loop_msgs
            ]
            # Inject tool prompt: full on first iteration, short on later ones.
            # Long prompts cause Qwen's <think> to overflow max_tokens on follow-up rounds.
            if _light_query:
                current_prompt = ""  # 快车道：不注入任何提示（裸聊天）
            elif _native_tools:
                # 原生模式：工具经 API tools 参数传入，只留精简纪律
                current_prompt = _NATIVE_LEAN_PROMPT if iteration == 1 else _NATIVE_FOLLOWUP_PROMPT
                _first_user_len = len(loop_msgs[-1].get("content", "")) if loop_msgs else 0
                if iteration == 1 and _first_user_len > 8000:
                    # 09-05 23:52 事故：长输入首轮纯思考 8 分钟不写正文
                    current_prompt = (
                        current_prompt
                        + "\n\n📏 思考预算（长输入）：用户输入内容很长（表格/文档全文）。"
                        "请先简短思考（≤300 字），然后立刻在正文写出完整分析——"
                        "关键数字和结论必须写进正文。禁止长时间只思考不写正文。"
                        "Think briefly (≤300 chars), then write the full analysis "
                        "with key numbers and conclusions in the reply body."
                    )
            elif iteration == 1:
                current_prompt = tools_prompt
                # 09-05 23:52 事故：长输入（整表 11.8K 字符）下 27B 思考型模型
                # 首轮纯思考 8 分钟不写正文（13:29 同款复现）。长输入首轮追加
                # 思考预算指令，先给结论再写正文。
                _first_user_len = len(loop_msgs[-1].get("content", "")) if loop_msgs else 0
                if _first_user_len > 8000:
                    current_prompt = (
                        current_prompt
                        + "\n\n📏 思考预算（长输入）：用户输入内容很长（表格/文档全文）。"
                        "请先简短思考（≤300 字），然后立刻在正文写出完整分析——"
                        "关键数字和结论必须写进正文。禁止长时间只思考不写正文。"
                        "Think briefly (≤300 chars), then write the full analysis "
                        "with key numbers and conclusions in the reply body."
                    )
            else:
                # Build lightweight tool reminder that still lists available tools by name
                tool_names = [t.get("function", {}).get("name", "") for t in active_tools if t.get("function", {}).get("name")]
                names_str = ", ".join(tool_names) if tool_names else "无"
                current_prompt = (
                    f"⚠️ 任务尚未完成，你必须继续！可用工具: {names_str}。\n"
                    "格式：```tool 工具名\n{\"参数\":\"值\"}\n```\n"
                    "如果当前任务的所有步骤都已完成，才可以直接回复用户。否则必须继续使用工具。"
                )
            if current_prompt:
                for m in loop_msgs:
                    if m.get("role") == "system":
                        m["content"] = m["content"] + "\n\n" + current_prompt
                        break
                else:
                    loop_msgs.insert(0, {"role": "system", "content": current_prompt})

            # mlx_lm.server v0.31 只接受一个 system 消息且必须在最前面，
            # 多个 system 直接 404 "System message must be at the beginning"
            # （15:25 后任务迭代 2+ 全部 404 的真凶：nudge 轮会追加第二个
            # system 消息）。发送前把开头连续的 system 合并为一个。
            loop_msgs = _merge_system_messages(loop_msgs)

            # 本地引擎把任意 model 名当 HuggingFace repo 解析 → 404（21:06 事故：
            # 用户选了 gpt-4o-mini 但走本地循环，mlx server 对未知名前
            # Hub 解析 SSL 失败回 404）。必须用引擎实际加载的模型 id。
            _engine_model = getattr(local_llm._engine, "current_model_id", "") or model
            body = {
                "model": _engine_model,
                "messages": loop_msgs,
                # 恢复完整生成预算（09-03 15:05 曾砍到 4096——推理模型思考+总结
                # 挤不下，致"查完不写总结/潦草收尾"并引发 nudge 压力下的数字
                # 编造；复读截断(09-03 e27ba50)+预算收口(07e9460)已兜住原问题）
                "max_tokens": _resolve_max_tokens(model), "stream": True,
                # 工具调用确定性（0 温度；本地模型同样受益）
                "temperature": 0.0,
                "frequency_penalty": 0.6,
                "stop": ["<|im_end|>", "<|endoftext|>", "<end_of_turn>", "<eos>"],
            }
            if _light_query or has_called_tool:
                # 关思考两档：闲聊快车道（27B 实测 7.0s→1.3s）；工具后续轮
                # （结果=数据，直接写答案——09-06 13:23 事故：工具后仍开思考，
                # 两轮纯思考 97s 零交付收尾）
                body["chat_template_kwargs"] = {"enable_thinking": False}
            if not _light_query and _native_tools:
                # 原生 function calling（mlx_lm.server 0.31：模板渲染工具 +
                # ToolParser 输出 OpenAI 格式 delta.tool_calls，真机实测通过）
                body["tools"] = [dict(t) for t in active_tools]

            streamed_text = ""
            body_text = ""  # 09-05 13:29 事故：只累计正文(content)delta——思考(reasoning)混进
            # streamed_text 后，所有 ≥200 判定、终答提取验收(len*3)、收尾回退交付
            # 全部被几万字的思考污染：提取结果永远不达标，最后把 4 分 34 秒的
            # 思考全文（结尾悬着半截脚本）当最终答案发给用户。正文与思考必须分离。
            _raw_delta_count = 0  # 诊断: 统计收到的 delta 数(空响应时判断是模型真空还是解析丢了)
            logger.info(f"[LOCAL-AGENT] Iteration {iteration}: calling LLM, msgs={len(loop_msgs)}, first_user_content_len={len(loop_msgs[-1].get('content','')) if loop_msgs else 0}")
            # 轮次透明化（09-05 23:52：8 分钟无正文用户以为卡死）：
            # 每轮开始通知前端"第 N 轮"，多轮拉锯不再是静默黑盒
            yield {"event": "round_start", "iteration": iteration}
            # 本地 llama.cpp 并发流式请求会崩溃 -> _local_llm_stream 内部串行化
            # 流式停顿检测：模型过大/未加载完时可能极慢滴灌（120s 超时永不触发），
            # 连续 180s 无任何数据视为僵死，中止不再无限挂起
            #
            # 零交付断裂重试：引擎在本轮 LLM 调用中途死亡（健康处置杀进程/
            # 系统内存压力/OOM）时流会中断。若本轮尚未向用户交付任何内容
            # （streamed_text 为空 <=> 未 yield 过任何 content/reasoning），
            # 等待引擎自动重载后重发本轮调用--任务无感继续，不再
            # "执行一半停止"。已有部分输出时重发会重复，直接抛出。
            _stream_break_retries = 0
            _any_output = False  # 本轮是否产出过 content/reasoning（role-only 空块不算，P1-6）
            _dedup_fired = False  # 去重一次性截断标志（审计 A5：命中后不再永久吞输出）
            _native_tcs: dict[int, dict] = {}  # 原生 delta.tool_calls 累积（mlx ToolParser）
            _retry_round_400 = False  # 本轮 400 回退标志（消费后必须复位，防死循环）
            # 单轮生成墙钟上限（20:11 事故：12288 预算下 27B 单轮可跑 7 分半，
            # 2 轮即撞 720s 总预算）——超时截断本轮（同复读截断机制），
            # 已产出文本交给闸门；截断后模型下一轮带着"请直接收尾"继续
            # 09-04 实测：Qwen3.8-27B-MLX-4bit 一轮完整分析（1-2 千字）需
            # 4-8 分钟，300s 截断导致分析永远写不完（15:34 任务三轮全被
            # 300s 截断后 788s 预算中止）——放宽到 600s；总时长由
            # _session_budget（1800s）与 15 分钟无进展看门狗兜底
            _gen_deadline = time.monotonic() + 900  # 09-21 22:48：27B 大输入单轮 8-15 分钟
            # （原 600s 仍会被掐；停滞/零交付护栏并行兜底）
            while True:
                try:
                    async with _local_llm_stream(client, api_url, body, headers) as r:
                        aiter = r.aiter_lines()
                        _fence_filter = _ThinkFenceFilter()
                        _silent_secs = 0  # 连续静默秒数（心跳与停滞判定共用，P0-4）
                        while True:
                            try:
                                # 60s 分片等待：静默超 60s 发一次心跳保活（前端
                                # 看门狗 180s 无事件会掐连接），累计静默达到
                                # 自适应停滞阈值（首 token 90s / 之后 180s，
                                # role-only 空块不算输出，P1-6）才判停滞
                                _stall_timeout = 90 if (not _any_output) else 180
                                line = await asyncio.wait_for(anext(aiter), timeout=60)
                                _silent_secs = 0
                            except asyncio.TimeoutError:
                                _silent_secs += 60
                                if _silent_secs < _stall_timeout:
                                    # 尚未到停滞阈值：发心跳，前端看门狗续命
                                    yield {"event": "heartbeat"}
                                    continue
                                mark_llm_suspect()
                                if not _any_output and _is_local_llm_url(api_url):
                                    # 零交付停滞 = 引擎挂起（接受连接但不出字，
                                    # 15:10 事故同款）。杀掉+重载，并抛传输异常
                                    # 让外层零交付重试接管：引擎恢复后本轮调用
                                    # 自动重跑，任务不再以错误中断。
                                    logger.warning(
                                        "[LOCAL-AGENT] Iteration %s: 零交付停滞 %.0fs，"
                                        "判定引擎挂起，强制重载后重试本轮",
                                        iteration, _stall_timeout)
                                    _eng = local_llm._engine
                                    try:
                                        _eng._kill_port(_eng.server_port)
                                        _eng.server_status = "stopped"
                                    except Exception:
                                        pass
                                    if (_eng.current_model_id
                                            and not getattr(_eng, "_explicit_stop", False)
                                            and not _eng._auto_reloading
                                            and _eng.server_status != "error"):
                                        _eng._request_reload(_eng.current_model_id)
                                    raise httpx.ReadTimeout(
                                        "本地模型输出停滞（引擎挂起），已自动重载，任务将自动重试。")
                                raise TimeoutError(
                                    f"本地模型输出停滞超 {_stall_timeout:.0f} 秒（模型可能过大或未加载完）：{model[:60]}"
                                )
                            except StopAsyncIteration:
                                break
                            if line and line.startswith("data: "):
                                try:
                                    _done, delta = _parse_delta_line(line)
                                    if _done:
                                        break
                                    if delta is None:
                                        continue  # usage-only chunk（仅 token 统计，无 delta）
                                    _raw_delta_count += 1
                                    # 原生 tool_calls 增量累积（mlx ToolParser 在
                                    # 收尾包整体送达 delta.tool_calls，OpenAI 格式）
                                    for _tc_d in delta.get("tool_calls", []) or []:
                                        _tidx = _tc_d.get("index", 0)
                                        _tbuf = _native_tcs.setdefault(_tidx, {
                                            "id": "", "type": "function",
                                            "function": {"name": "", "arguments": ""}})
                                        if _tc_d.get("id"):
                                            _tbuf["id"] = _tc_d["id"]
                                        _tfn = _tc_d.get("function") or {}
                                        if _tfn.get("name"):
                                            _tbuf["function"]["name"] += _tfn["name"]
                                        if _tfn.get("arguments"):
                                            _tbuf["function"]["arguments"] += _tfn["arguments"]
                                    content = delta.get("content", "")
                                    # LM Studio/方舟等返回 reasoning_content,OpenAI o 系列返回 reasoning
                                    reasoning = delta.get("reasoning") or delta.get("reasoning_content") or ""
                                    # 单轮生成墙钟超时（300s）：与复读同机制截断，
                                    # 防一轮 7 分钟把总预算耗尽（20:11 事故）
                                    if time.monotonic() > _gen_deadline:
                                        logger.warning("[LOCAL-AGENT] 单轮生成 900s 超时，截断本轮")
                                        streamed_text = _strip_repeat_tail(streamed_text)
                                        raise _GenerationLoopError("单轮生成超时(900s)，已截断")
                                    if content:
                                        _any_output = True
                                        streamed_text += content
                                        body_text += content
                                        # 复读循环检测（节流：每 40 个 delta 一次）：
                                        # 必须放在 dedup 过滤之前——此前 dedup 命中后
                                        # continue 会跳过本检查，模型复读时全程无输出
                                        # 无截断、引擎 100% CPU 转到 max_tokens
                                        # （16:29 任务"停在尾端没反应"的帮凶之一）
                                        if _raw_delta_count % 40 == 0 and _detect_text_loop(streamed_text):
                                            logger.warning("[LOCAL-AGENT] 检测到输出复读循环，截断本轮生成")
                                            streamed_text = _strip_repeat_tail(streamed_text)
                                            raise _GenerationLoopError("输出复读循环，已截断")
                                        # 自我介绍去重：只截断一次，保留首段后继续流式
                                        # 输出——此前命中后永久 continue，模型第二次
                                        # "我是辣条"之后的所有真实内容被静默丢弃
                                        # （审计 A5：用户在推理模型里看到回复停在中间）
                                        if not _dedup_fired:
                                            _ded = _deduplicate_response(streamed_text)
                                            if len(_ded) < len(streamed_text):
                                                _dedup_fired = True
                                                streamed_text = _ded + content
                                                body_text = _deduplicate_response(body_text) + content
                                        # 缓冲交付（16:50 事故）：本地模型内容不再逐字直播——
                                        # 英文会在收尾闸门/翻译轮运行之前就漏给用户。
                                        # 本轮内容全部累积，由各交付 return 路径统一做
                                        # 语言确保后一次性交付；reasoning 通道仍直播。
                                        _fence_filter.feed(content)
                                    elif reasoning:
                                        _any_output = True
                                        streamed_text += reasoning
                                        if _raw_delta_count % 40 == 0 and _detect_text_loop(streamed_text):
                                            logger.warning("[LOCAL-AGENT] 检测到输出复读循环，截断本轮生成")
                                            streamed_text = _strip_repeat_tail(streamed_text)
                                            raise _GenerationLoopError("输出复读循环，已截断")
                                        if not _dedup_fired:
                                            _ded = _deduplicate_response(streamed_text)
                                            if len(_ded) < len(streamed_text):
                                                _dedup_fired = True
                                                streamed_text = _ded + reasoning
                                        yield {"reasoning": reasoning, "ts": int(time.time() * 1000)}
                                except (json.JSONDecodeError, KeyError, TypeError, IndexError):
                                    pass
                                except _GenerationLoopError:
                                    # 复读截断：停止消费本轮流，已产出（裁尾后）文本
                                    # 交给后续工具解析/收尾闸门，会话继续（18:01 事故）
                                    break
                                except Exception:
                                    logger.error("Local agent SSE parse error", exc_info=True)
                                    raise
                    break
                except httpx.TransportError as e:
                    # 与 _local_llm_stream 同口径：任何传输层错误都算流中断。
                    if body_text.strip() or streamed_text.strip():
                        # 部分输出已交付且引擎死亡：把已交付正文落为 assistant
                        # 消息并注入断点续写提示，重发本轮让模型从断点继续，
                        # 不再让用户拿着半截回答收错误（P0-3，限一次）
                        if _stream_break_retries >= 1:
                            raise
                        logger.warning(
                            f"[LOCAL-AGENT] Iteration {iteration}: 部分输出中断"
                            f"({type(e).__name__})，记录已交付文本后重发本轮续写")
                        current_msgs.append({"role": "assistant", "content": (body_text.strip() or "（思考中断，未输出正文）")})
                        current_msgs.append({
                            "role": "system",
                            "content": "你刚才的回答被中断了（模型服务异常）。"
                                       "请从断点继续完成回答，不要从头重复。",
                        })
                        body["messages"] = _merge_system_messages(current_msgs)
                        streamed_text = ""
                        body_text = ""
                        _raw_delta_count = 0
                        _any_output = False
                        _dedup_fired = False
                        _stream_break_retries += 1
                        await asyncio.sleep(10)
                        continue
                    if _stream_break_retries >= 1:
                        raise
                    _stream_break_retries += 1
                    logger.warning(
                        f"[LOCAL-AGENT] Iteration {iteration}: LLM 流零交付中断"
                        f"({type(e).__name__})，等待引擎恢复后重发本轮调用 ({_stream_break_retries}/1)")
                    await asyncio.sleep(10)
                    continue
                except httpx.HTTPStatusError as e:
                    # 原生 tools 参数被引擎拒绝（模板不支持工具调用，mlx server
                    # 对无工具能力模型回 400）→ 本轮起回退围栏提示词，任务继续
                    if (_native_tools and not _native_fallback_used
                            and getattr(e.response, "status_code", 0) == 400):
                        _native_fallback_used = True
                        _native_tools = False
                        _retry_round_400 = True
                        logger.warning("[LOCAL-AGENT] 引擎拒绝 tools 参数（HTTP 400），"
                                       "回退围栏提示词格式重跑本轮")
                        break
                    raise

            if _retry_round_400:
                # 400 回退：重走本轮（无 tools + 完整围栏提示词——回退迭代号，
                # 让重试轮拿到带示例的首轮提示，而非轻量提醒）
                iteration -= 1
                continue

            # 收尾修正已移除（16:50 事故）：此前每轮流结束都发一次 content_revised
            # 全量替换，等于把模型文本在闸门/翻译之前直播给用户。缓冲交付下
            # 文本只由各交付 return 路径一次性 yield。

            # Check for tool calls in the streamed text
            clean_text, tool_calls = _parse_prompt_tool_calls(streamed_text)
            # Also check for native tool call format (Gemma 4 <|tool_call|>)
            if not tool_calls and _NATIVE_TOOL_RE.search(streamed_text):
                native_tcs = _parse_native_tool_calls(streamed_text)
                if native_tcs:
                    streamed_text = _strip_native_tool_calls(streamed_text)
                    body_text = _strip_native_tool_calls(body_text)
                    tool_calls = native_tcs
            # 原生 function calling 优先（09-06：API 结构化 tool_calls，最高保真）
            if _native_tcs:
                native_api_tcs = [_native_tcs[i] for i in sorted(_native_tcs.keys())]
                if any(tc.get("function", {}).get("name") for tc in native_api_tcs):
                    tool_calls = native_api_tcs
                    # 原生路径下正文不含围栏文本，无需清理；仅剥离模型可能
                    # 复读出来的 <tool_call> 文本标签
                    if _NATIVE_TOOL_RE.search(streamed_text):
                        streamed_text = _strip_native_tool_calls(streamed_text)
                        body_text = _strip_native_tool_calls(body_text)


            # Whitelist: only execute tools in the active set
            tool_names = {t.get("function", {}).get("name") for t in active_tools}
            tool_calls = [tc for tc in tool_calls if tc.get("function", {}).get("name") in tool_names]
            if tool_calls:
                _no_progress_deadline = time.monotonic() + 900  # 工具执行=实质进展
                _track_progress(session_id, "tool_calling", f"{len(tool_calls)} tool(s)")

                # Add assistant message (cleaned text)
                _asst_msg: dict = {"role": "assistant", "content": clean_text or ""}
                if _native_tools or _native_tcs:
                    # 原生模式：assistant 消息必须携带 tool_calls——Qwen 模板
                    # 据此渲染 <tool_call> 块，工具结果才有正确的对话上下文
                    _asst_msg["tool_calls"] = tool_calls
                    current_msgs.append(_asst_msg)
                else:
                    _asst_msg["content"] = clean_text or "正在调用工具..."
                    current_msgs.append(_asst_msg)
                has_called_tool = True
                text_output_delivered = False  # 工具被调用=实质推进，后续文本是新的最终回复，恢复流式输出

                any_new = False
                round_failed = False
                for tc in tool_calls:
                    sig = f"{tc.get('function',{}).get('name','')}:{hash(str(tc.get('function',{}).get('arguments',''))) } "
                    if sig not in recent_tool_calls:
                        recent_tool_calls.add(sig)
                        any_new = True
                    if _session_cancel_requested(session_id):
                        _track_progress(session_id, "cancelled", "user_stop")
                        logger.info(f"[AGENT] tool 执行前检测到取消（新消息/手动停止），任务中止")
                        yield {"content": "\n\n⏹️ 任务已停止。"}
                        return
                    # 先发确认事件再等待（死锁修复）：confirm 级工具的
                    # tool_confirm 必须在执行前到达前端，弹窗才会出现
                    if not tc.get("id"):
                        tc["id"] = str(uuid.uuid4())
                    pre_started = None
                    try:
                        _tname = tc.get("function", {}).get("name", "unknown")
                        _targs = json.loads(tc.get("function", {}).get("arguments", "{}") or "{}")
                        # 空名快速中止（09-21 实测：云端模型连续 11 轮输出空工具名，
                        # 守卫逐轮弹回但循环不收敛——3 次即中止并给出可操作诊断）。
                        if not _tname.strip():
                            _empty_name_streak += 1
                            _empty_name_seen = True
                            logger.warning("empty tool name streak=%s args=%s raw_calls=%s",
                                           _empty_name_streak, json.dumps(_targs, ensure_ascii=False)[:200],
                                           json.dumps([tc.get("function", {}) for tc in tool_calls], ensure_ascii=False)[:400])
                            if _empty_name_streak >= 3:
                                yield {"content": ("\n\n⛔ 模型连续 3 次输出空工具名（工具调用格式异常）。"
                                                   "任务已中止。请切换为其他模型，或在模型页重新加载后重试。")}
                                _track_progress(session_id, "stalled", f"empty_tool_name x{_empty_name_streak}")
                                return
                            # 1-2 次不 continue：守卫反馈（空名提示）必须进模型上下文，
                            # 让模型下一轮改格式；只有第 3 次才中止。
                        if _resolve_permission(_tname, _targs) == "confirm" \
                                and not _confirm_bypassed(_tname, access_mode) \
                                and not _check_access(_tname, access_mode):
                            pre_started = await _start_tool_confirmation(tc["id"], _tname, _targs)
                            yield pre_started["event"]
                    except Exception:
                        pre_started = None
                    verify_failed, events = await _handle_tool_execution(
                        tc, current_msgs, session_id, agent_id, access_mode, pre_started=pre_started)
                    for evt in events:
                        yield evt
                    # 用户拒绝 ≠ 验证失败：拒绝不应清零停滞预算、诱导模型反复
                    # 重试同一操作（每次重试都会再弹确认框）。与云端循环对齐。
                    denied = any(
                        isinstance(e, dict) and str(e.get("result", "")).startswith("⛔ User denied")
                        for e in events)
                    if verify_failed and not denied:
                        round_failed = True

                # 新的调用签名，或本轮有工具失败（模型正在尝试修复）都算实质推进，不计停滞
                if any_new or round_failed:
                    stagnation = 0
                    if any_new:
                        _empty_name_streak = 0  # 合法工具调用复位空名计数
                    text_only_streak = 0
                    if any_new:
                        # 工具产出了新结果：在收到实质性文字回答（≥200 字符）
                        # 之前，不允许用一句话收尾（“让我读取数据再分析”式
                        # 声明/道歉不算完成）。
                        # 注意：不重置 _intent_nudges——此前任何新工具调用都
                        # 重置计数，模型不断换查询即可无限拖延（17:52 事故），
                        # 改为全会话累计：3 次"声明不动手"后强制终答提取收口。
                        _pending_tool_analysis = True
                else:
                    stagnation += 1
                if stagnation >= max_stagnation:
                    # 停滞兜底（17:21 事故：模型重读文件后空转 3 轮直接停止，
                    # 用户等了 6 分钟拿不到任何答案）：先做一次非流式"终答提取"——
                    # 强制基于已有工具结果直接写出最终回答；成功即中文交付，
                    # 失败才给停止提示。
                    _sout = await _final_answer_extraction(
                        client, api_url, headers, _engine_model, current_msgs,
                        _detect_user_language(_extract_last_user_text(current_msgs)))
                    if len(_sout) >= 120:
                        yield {"content": "\n\n" + _strip_think_fences(_sout)}
                        _track_progress(session_id, "completed", f"stagnation_fallback ({len(_sout)} chars)")
                        logger.info(f"[LOCAL-AGENT] Iteration {iteration}: 停滞兜底终答交付 ({len(_sout)} chars)")
                        return
                    yield {"content": f"\n\n⚠️ 连续 {stagnation} 轮无新进展，Agent 停止。如需继续请发新消息。"}
                    return
                continue

            # No tool calls — pure text response done
            # 停滞计数：纯文本轮计入 stalled_rounds（工具轮/完成轮会复位，P2-12）
            _track_progress(session_id, "text_round", "text_only")
            if body_text.strip():
                _no_progress_deadline = time.monotonic() + 900  # 产出正文=实质进展（思考不算）
            # ═══ native 新契约（09-06 彻底重构）：结构规则，零文本猜测 ═══
            #   tool_calls → 已在上面执行完并 continue，不会到这里
            #   有正文 → 本轮零工具调用且未确认过 → 一次完成确认轮（模型要么
            #            立即调工具、要么重发最终回答）；否则 = 最终回答，交付收尾
            #   仅思考无正文 → 唯一兜底：终答提取一次 → 交付或干净中止
            #   完全空 → 落到下方 empty-response 重试/中止（与 legacy 共用）
            # 下方旧启发式网（关键词/规划/意图/数字闸门）仅在 legacy 围栏路径执行。
            if _native_tools:
                if not body_text.strip() and streamed_text.strip():
                    _sout = await _final_answer_extraction(
                        client, api_url, headers, _engine_model, current_msgs,
                        _detect_user_language(_extract_last_user_text(current_msgs)))
                    if len(_sout) >= 120:
                        yield {"content": "\n\n" + _strip_think_fences(_sout)}
                        _track_progress(session_id, "completed", f"think_only_extract ({len(_sout)} chars)")
                        logger.info(f"[LOCAL-AGENT] Iteration {iteration}: 思考-only，终答提取交付 ({len(_sout)} chars)")
                        return
                    yield {"content": "\n\n⚠️ 本地模型只输出了思考过程，未能生成回答正文。请回复「继续」重试，或换云端模型。"}
                    _track_progress(session_id, "completed", "think_only_no_body")
                    logger.warning(f"[LOCAL-AGENT] Iteration {iteration}: 思考-only 且终答提取失败，干净中止")
                    return
                if body_text.strip():
                    if not has_called_tool and not _continuation_round_done:
                        _continuation_round_done = True
                        current_msgs.append({"role": "assistant", "content": body_text.strip()})
                        current_msgs.append({"role": "system", "content": _NATIVE_COMPLETION_CHECK_PROMPT})
                        text_output_delivered = True
                        text_only_streak += 1
                        yield {"event": "heartbeat"}
                        logger.info(f"[LOCAL-AGENT] Iteration {iteration}: 零工具纯文本 → 完成确认轮")
                        continue
                    _deliver = await _ensure_final_language(
                        client, api_url, headers, _engine_model,
                        body_text.strip(), last_user_text)
                    yield {"content": "\n\n" + _strip_think_fences(_deliver)}
                    _track_progress(session_id, "completed", f"text_response ({len(_deliver)} chars)")
                    logger.info(f"[LOCAL-AGENT] Iteration {iteration}: 纯文本最终回答交付 ({len(_deliver)} chars)")
                    return
                # body 为空 → 继续往下走 empty-response 重试/中止（共用逻辑）
            # 工具已产出结果但模型尚未给出实质性回答时，不允许一句话收尾。
            # 此前依赖“最近 3 条消息里有工具结果”这个窗口，nudge 消息一多
            # 工具结果就被挤出窗口，模型连说两轮“让我读取数据再分析”都能
            # 被当作最终答案返回（16:45 大盘事故）。改为用 _pending_tool_analysis
            # 状态贯穿：只有 ≥200 字符的实质回答或新的工具调用才算推进。
            _recent_tool_failed = any(
                m.get("role") == "tool" and ("Error" in str(m.get("content", "")) or "⚠️" in str(m.get("content", "")) or "失败" in str(m.get("content", "")))
                for m in current_msgs[-4:]
            )
            # 非任务消息门槛（17:11 事故根治）：闲聊/能力介绍时工具是模型自己
            # 乱调的（演示式 list_dir），绝不能进入工具结果追问链——否则
            # "思考→演示工具→nudge→再思考"循环、思考闪现三次 + 重复能力清单。
            # 闲聊零追问：直接交付（语言确保后），立即结束。
            _has_task_kw = any(kw in (last_user_text or "").lower() for kw in (
                "运行", "执行", "做", "帮我", "写", "创建", "查", "搜", "找", "分析",
                "修复", "构建", "部署", "安装", "配置", "列出", "读取", "总结", "告诉",
                "run", "build", "fix", "create", "search", "analyze", "deploy")) \
                and not _is_chat_query(last_user_text)
            if _pending_tool_analysis and body_text.strip() and not _has_task_kw:
                # 闲聊交付不依赖 _recent_tool_failed（⚠️ 常驻系统提示词恒真）
                _deliver, _lang_retry = await _ensure_final_language_with_retry(client, api_url, headers, _engine_model, body_text.strip(), last_user_text, current_msgs, lang_retry_done=lang_retry_done)
                if _lang_retry and not lang_retry_done:
                    lang_retry_done = True
                    text_output_delivered = True
                    text_only_streak += 1
                    yield {"event": "heartbeat"}
                    continue
                current_msgs.append({"role": "assistant", "content": _deliver})
                yield {"content": "\n\n" + _strip_think_fences(_deliver)}
                _track_progress(session_id, "completed", f"text_response ({len(_deliver)} chars)")
                return
            if _pending_tool_analysis and body_text.strip() and not _recent_tool_failed:
                # 推理模型(Muse/Qwen3.5/Ornith)常先输出规划文字、下一轮才调工具——
                # 但“只声明不动手”的回复（让我读取/我再查一下…）不能当完成。
                _is_planning = _looks_like_planning(body_text)
                # 模型明确说"我改用/我要调用/我再单独查"但整轮没有实际工具调用
                # （21:42 事故：模型说"改用联网搜索"却直接收尾，未调 tavily）——
                # 这类带未执行意图的 ≥200 字符正文不算实质完成，继续 nudge。
                _pending_intent = any(k in body_text.lower() for k in _PENDING_INTENT_PATTERNS)
                # 纯外文长回复（如英文规划 891 字符）不算完成（13:58 事故）：
                # 用户说中文就必须中文交付，拦截后走 nudge 用中文重写。
                _lang_mismatch = _reply_lang_mismatch(
                    _extract_last_user_text(current_msgs), body_text)
                # 无来源数字校验（19:05 事故：模型编造'北向15.6亿/主力80亿'，
                # 全库工具结果从未返回）——少量可容忍，2 次后放行（有界）
                # 本地弱模型过分依赖数字换算/转写，这道防幻觉门比云端更易
                # 误触发 → 本地引擎只打回 1 次即放行，避免把"帮它兜底"的门
                # 变成"把它拖进 180s 空转断流"的门（09-04 美股事故根因）。
                _unsourced = _find_unsourced_numbers(body_text, current_msgs)
                if _unsourced and _fabrication_nudges < _fab_cap:
                    current_msgs.append({"role": "assistant", "content": body_text.strip()})
                    current_msgs.append({"role": "system", "content":
                        f"⚠️ 数据来源校验：你回复中的这些数字未出现在本会话用户消息或任何工具结果中："
                        f"{'、'.join(_unsourced[:8])}。关键数字必须来自用户消息或工具结果——"
                        f"若这些数字是工具数据的换算（如百万→亿），请注明'按工具数据折算'；"
                        f"否则删除该数值，或明确写'工具未返回该数据'。请修正后重新作答。"})
                    _fabrication_nudges += 1
                    text_output_delivered = True
                    text_only_streak += 1
                    logger.info(f"[LOCAL-AGENT] Iteration {iteration}: 无来源数字拦截（{_fabrication_nudges}/2）: {_unsourced[:5]}")
                    # 空转打回 → 下一轮重生成期间前端收不到任何字节，180s 看门狗会
                    # 误断流（09-04 美股事故）：先推一个心跳给前端续命（P0-4 根治）。
                    yield {"event": "heartbeat"}
                    continue
                if (len(body_text.strip()) >= 200 and not _is_meta_wrapup(body_text)
                        and not _pending_intent and not _is_planning and not _lang_mismatch):
                    # 模型在工具结果后已给出实质性回答（≥200 字符）——接受为
                    # 最终答案直接收尾，不再追问。此前无差别追问导致模型
                    # 从头再答一遍，UI 里重复堆叠（17:07/17:08 两任务的
                    # "已经查完了👆"式重复）。
                    # 例外：元评论式收尾（"上面的分析已覆盖…任务完成"）不算
                    # 实质回答——分析只在模型思考里，正文从未交付（19:38
                    # 事故），继续走追问轮。
                    # 缓冲交付：语言确保后一次性交付（16:50 事故后不再直播）
                    _deliver, _lang_retry = await _ensure_final_language_with_retry(client, api_url, headers, _engine_model, body_text.strip(), _extract_last_user_text(current_msgs), current_msgs, lang_retry_done=lang_retry_done)
                    if _lang_retry and not lang_retry_done:
                        lang_retry_done = True
                        text_output_delivered = True
                        text_only_streak += 1
                        yield {"event": "heartbeat"}
                        continue
                    _pending_tool_analysis = False
                    current_msgs.append({"role": "assistant", "content": _deliver})
                    yield {"content": "\n\n" + _strip_think_fences(_deliver)}
                    _track_progress(session_id, "completed", f"text_response ({len(_deliver)} chars)")
                    logger.info(f"[LOCAL-AGENT] Iteration {iteration}: no tools, returning text ({len(_deliver)} chars)")
                    return
                if _lang_mismatch and not _is_planning and not _pending_intent:
                    # 实质回答（≥200 字、非规划）但语言不符 → 不追问重分析
                    # （会丢数据），直接翻译轮交付（09-03 事故最后一公里：
                    # 14:45 重放中 598 字母/42 汉字 的混合英文分析即走此路）
                    _deliver = await _force_translate(
                        client, api_url, headers, _engine_model,
                        body_text.strip(),
                        _detect_user_language(_extract_last_user_text(current_msgs)))
                    _pending_tool_analysis = False
                    yield {"content": "\n\n" + _strip_think_fences(_deliver)}
                    current_msgs.append({"role": "assistant", "content": _deliver})
                    _track_progress(session_id, "completed", f"translated_response ({len(_deliver)} chars)")
                    logger.info(f"[LOCAL-AGENT] Iteration {iteration}: 语言不符，翻译轮交付 ({len(_deliver)} chars)")
                    return
                # 思考段提取兜底：正文是半截/声明（<200 字符、未执行意图或元评论），
                # 但思考段里有 ≥200 字符的实质分析（27B 模型把分析全写进思考段，
                # 21:13 事故）——直接以思考段内容作为最终回答交付，不再追问。
                # 语言不符（英文思考）不在此交付，走 nudge/翻译兜底（09-03 事故）。
                # 09-05 13:29 事故追加护栏：思考里若在"角色扮演"跑工具/脚本
                # （python3 -c、run_cmd、openpyxl、``` 等执行痕迹——模型幻想
                # "工具输出显示…"但从未真正调用），绝不当作答案交付。
                _think_body = _extract_think_body(streamed_text)
                if (len(_think_body) >= 200 and not _is_meta_wrapup(_think_body)
                        and not _looks_like_tool_fantasy(_think_body)
                        and not _reply_lang_mismatch(_extract_last_user_text(current_msgs), _think_body)):
                    _pending_tool_analysis = False
                    current_msgs.append({"role": "assistant", "content": _think_body})
                    yield {"event": "content_revised", "content": _think_body}
                    _track_progress(session_id, "completed", f"think_body ({len(_think_body)} chars)")
                    logger.info(f"[LOCAL-AGENT] Iteration {iteration}: 正文半截，交付思考段内容 ({len(_think_body)} chars)")
                    return
                # 意图声明未行动 nudge（本地弱模型常反复声明不动手）：云端 3 次、
                # 本地 1 次即收尾，避免把"帮它兜底"变成"拖进 180s 空转断流"（09-04 事故）。
                _intent_cap = 1 if _is_local_llm_url(api_url) else 3
                if _intent_nudges < _intent_cap:
                    # 只把正文记进上下文（09-05 13:29 事故：思考也被存为
                    # assistant 消息，模型看到自己的"角色扮演"思考被当成
                    # 真实回复，进一步强化了元叙述循环）
                    current_msgs.append({"role": "assistant", "content": (body_text.strip() or "（未输出正文）")})
                    if _is_planning:
                        _append_unique_system(current_msgs,
                            _get_localized_text(_detect_user_language(_extract_last_user_text(current_msgs)), {
                                "zh": "⚠️ 这不是用户的新消息，而是系统提醒（上一轮回复未完成）：\n"
                                       "必须用简体中文回复。\n"
                                       "不要只发声明或道歉。你刚才说还要继续——现在就调用工具去执行；如果数据其实已经足够，就把完整分析写进回复正文（含关键数字与结论）。\n"
                                       "⚠️ 重要：如果连续两次查询都只返回完全相同的汇总（没有明细），说明该查询词无解——请换成更具体的查询词或换其他工具，不要重复相同的查询。（此提醒只针对行情查询任务，与本任务无关时忽略。）",
                                "en": "You MUST reply in English.\n"
                                      "⚠️ This is a system reminder (your previous reply was incomplete), NOT a new user message.\n"
                                      "Don't just announce or apologize. You said you would continue — call the tool NOW; if the data is sufficient, write the full analysis in your reply body.\n"
                                      "IMPORTANT: If two consecutive queries return the same aggregate only (no detail), that query is unsolved — switch to a more specific query or another tool. Do NOT repeat the same query. (This applies to market-data tasks only; ignore if irrelevant.)",
                                "ja": "必ず日本語で返信してください。\n"
                                      "⚠️ これはユーザーの新規メッセージではなく、システム通知です（前の回答が未完了）。\n"
                                      "宣言や謝罪だけでなく、続けると言ったなら今すぐツールを呼び出してください。データが十分なら完全な分析を本文に書いてください。\n"
                                      "重要：連続2回同じ集計のみしか返らない場合、そのクエリは無解です。より具体的なクエリまたは別のツールに切り替えてください。同じクエリを繰り返さないでください。（市場データ関連のタスクのみ対象。無関係なら無視してください。）",
                            }))
                        logger.info(f"[LOCAL-AGENT] Iteration {iteration}: 意图声明未行动，nudge 立即调用工具（{_intent_nudges + 1}/3）")
                    else:
                        _append_unique_system(current_msgs,
                            _get_localized_text(_detect_user_language(_extract_last_user_text(current_msgs)), {
                                "zh": "⚠️ 必须用简体中文回复。\n"
                                       "⚠️ 这不是用户的新消息，而是系统提醒：你刚才收到了工具的执行结果，但你的回复里没有给出实质内容"
                                       "（只有收尾话或道歉，真正的分析还留在你的思考里）。\n"
                                       "如果还需要数据，直接调用工具；否则把完整的分析写进回复正文："
                                       "包含从工具结果中得到的关键数据与结论，让用户直接读到。\n"
                                       "调用工具格式：```tool 工具名\n{\"参数\":\"值\"}\n```",
                                "en": "You MUST reply in English.\n"
                                      "⚠️ This is a system reminder, NOT a new user message: you received tool results but your reply contained no real content"
                                      " (only meta-commentary or apologies).\n"
                                      "If you still need data, call a tool directly; otherwise write"
                                      " the FULL analysis into your reply body with key data and"
                                      " conclusions from the tool results.\n"
                                      "Tool format: ```tool tool_name\n{\"param\":\"value\"}\n```",
                                "ja": "必ず日本語で返信してください。\n"
                                      "⚠️ これはユーザーの新規メッセージではなく、システム通知です：ツール実行結果を受け取りましたが、返信に実質的な内容がありません"
                                      "（メタコメントや謝罪のみ）。\n"
                                      "データがまだ必要なら直接ツールを呼び出し、そうでなければ主要データと"
                                      "結論を含む完全な分析を本文に書いてください。\n"
                                      "形式：```tool ツール名\n{\"パラメータ\":\"値\"}\n```",
                            }))
                        logger.info(f"[LOCAL-AGENT] Iteration {iteration}: model returned text after tool result, pushing for continuation（{_intent_nudges + 1}/3）")
                    text_output_delivered = True  # 文本已交付，nudge 重试不再重复输出
                    text_only_streak += 1
                    _intent_nudges += 1
                    # 空转 nudge → 下一轮重生成期间前端收不到字节，180s 看门狗会误断流：
                    # 先推心跳续命（P0-4 根治），语义同"无来源数字拦截"分支。
                    yield {"event": "heartbeat"}
                    continue
                # 追问到上限仍只有声明/道歉：做一次"终答提取"兜底——不带工具、
                # 单一指令"写出完整分析"。本地 27B 级模型常被自身思维链卡住：
                # 思考里已有分析但正文只回意图声明（09-02 09:56 事故：mx_query
                # 数据到手，3 轮 nudge 模型仍只回 9 字符声明）。
                # 终答提取：统一走 _final_answer_extraction（09-05 13:06 后重构：
                # system 置顶 + stop 去 </think>——本地引擎对这两点极敏感，旧实现
                # 静默失败，导致元收尾被当作最终答案交付）
                final_answer = await _final_answer_extraction(
                    client, api_url, headers, _engine_model, current_msgs,
                    _detect_user_language(_extract_last_user_text(current_msgs)))
                # 语言兜底：终答仍为外文时，做一轮强制翻译（模型对翻译任务执行
                # 稳定，保证用户永远收到母语回复，09-03 事故最后一公里）
                if final_answer and _reply_lang_mismatch(_extract_last_user_text(current_msgs), final_answer):
                    final_answer = await _force_translate(
                        client, api_url, headers, _engine_model, final_answer, _user_lang)
                # 终答有效（≥200 字符，与正文长度无关——09-05 13:29 事故：
                # 旧条件 len(streamed_text)*3 被几万字的思考污染，提取结果
                # 永远不达标，最后把思考全文当答案交付）→ 交付终答
                if len(final_answer) >= 200:
                    yield {"content": "\n\n" + _strip_think_fences(final_answer)}
                    _track_progress(session_id, "completed", f"final_answer ({len(final_answer)} chars)")
                    return
                # 正文尚可（≥80 字符且非元收尾）→ 语言确保后交付正文；
                # 正文空/思考-only → 干净提示，绝不把思考 dump 给用户
                if len(body_text.strip()) >= 80 and not _is_meta_wrapup(body_text):
                    _deliver, _lang_retry = await _ensure_final_language_with_retry(client, api_url, headers, _engine_model, body_text.strip(), _extract_last_user_text(current_msgs), current_msgs, lang_retry_done=lang_retry_done)
                    if _lang_retry and not lang_retry_done:
                        lang_retry_done = True
                        text_output_delivered = True
                        text_only_streak += 1
                        yield {"event": "heartbeat"}
                        continue
                    yield {"content": "\n\n" + _strip_think_fences(_deliver)}
                    current_msgs.append({"role": "assistant", "content": _deliver})
                    _track_progress(session_id, "completed", f"text_response ({len(_deliver)} chars)")
                    logger.warning(f"[LOCAL-AGENT] Iteration {iteration}: {_intent_nudges} 轮追问无实质回答，交付正文收尾")
                    return
                yield {"content": (
                    "\n\n⚠️ 本地模型本轮未能生成有效的分析正文（只输出了思考过程）。"
                    "请直接回复「继续」让我再试一次；或换一个模型（如云端模型）重发本任务。")}
                _track_progress(session_id, "completed", "body_empty_fallback")
                logger.warning(f"[LOCAL-AGENT] Iteration {iteration}: {_intent_nudges} 轮追问仍无实质回答，收尾返回")
                # 必须 return：之前这里只打日志不返回，落回"短回答追问"分支
                # 再白送一轮（20:48 事故：收尾后又进"追问充分回答一轮"，迭代 6 重复跑）
                return
            # 思考-only 轮（有思考流、无正文）：思考型模型的典型失败形态
            # （09-05 13:29 事故：4分34秒思考 + 空正文）。不空转 nudge，
            # 直接终答提取；失败给干净收尾提示，绝不把思考 dump 给用户。
            if not body_text.strip() and streamed_text.strip():
                _final = await _final_answer_extraction(
                    client, api_url, headers, _engine_model, current_msgs,
                    _detect_user_language(_extract_last_user_text(current_msgs)))
                if len(_final) >= 200:
                    yield {"content": "\n\n" + _strip_think_fences(_final)}
                    _track_progress(session_id, "completed", f"think_only_extract ({len(_final)} chars)")
                    logger.info(f"[LOCAL-AGENT] Iteration {iteration}: 思考-only 轮，终答提取交付 ({len(_final)} chars)")
                    return
                _think_only_nudges += 1
                if _think_only_nudges >= 2:
                    yield {"content": (
                        "\n\n⚠️ 本地模型连续两轮只输出了思考过程、没有生成分析正文。"
                        "请回复「继续」重试，或换云端模型重发本任务。")}
                    _track_progress(session_id, "completed", "think_only_abort")
                    return
                current_msgs.append({"role": "assistant", "content": "（未输出正文）"})
                # 09-05 23:52：nudge 用 system 追加在消息末尾——语义错误（这是
                # 用户的补写要求，不是系统预设），且 mlx 对末尾 system 敏感
                # （虽有 _merge_system_messages 兜底，仍应保持 role 正确）
                current_msgs.append({"role": "user", "content":
                    _get_localized_text(_detect_user_language(_extract_last_user_text(current_msgs)), {
                        "zh": "你上一轮只输出了思考过程，回复正文是空的。请在正文中直接输出完整回答（含关键数字与结论）。不要只思考不输出正文。",
                        "en": "Your last turn produced only reasoning with an empty reply body. Write the full answer (with key numbers and conclusions) directly in your reply body. Do not think without writing.",
                        "ja": "前回は思考のみで本文が空でした。主要な数字と結論を含む完全な回答を本文に直接書いてください。",
                    })})
                text_output_delivered = True
                text_only_streak += 1
                yield {"event": "heartbeat"}
                logger.info(f"[LOCAL-AGENT] Iteration {iteration}: 思考-only 轮，nudge 写正文 ({_think_only_nudges}/2)")
                continue
            if not has_called_tool and text_only_streak < 3 and body_text.strip():
                # Model gave a text response without calling tools.
                # Record the response so the model knows it already replied.
                current_msgs.append({"role": "assistant", "content": body_text.strip()})
                # 非任务型消息（闲聊/陈述/提问/长回复）→ 文本已交付给用户，直接结束，不再 nudge 重发
                user_q = _extract_last_user_text(current_msgs).strip().rstrip("?？") if current_msgs else ""
                has_task_kw = any(kw in user_q for kw in ["运行", "执行", "做", "帮我", "写", "创建", "查", "搜", "找", "分析", "修复", "构建", "部署", "安装", "配置", "run", "build", "fix", "create", "search", "analyze", "deploy"])
                if not has_task_kw:
                    # 缓冲交付：语言确保 + 显式 yield（闲聊回复也可能英文）
                    _deliver, _lang_retry = await _ensure_final_language_with_retry(client, api_url, headers, _engine_model, body_text.strip(), user_q, current_msgs, lang_retry_done=lang_retry_done)
                    if _lang_retry and not lang_retry_done:
                        lang_retry_done = True
                        text_output_delivered = True
                        text_only_streak += 1
                        yield {"event": "heartbeat"}
                        continue
                    yield {"content": "\n\n" + _strip_think_fences(_deliver)}
                    _track_progress(session_id, "completed", f"text_response ({len(_deliver)} chars)")
                    return
                # 任务型消息但数据就在用户消息里（09-05 13:06 事故："分析这个文件"+
                # 内联数据根本不需要工具）：≥200 字符、非规划/声明/元收尾的正文
                # 视为实质回答直接交付，不再误判为"规划未执行"空转 5 轮，把模型
                # 推到"系统在持续推动我"的元叙述收尾。规划话术仍走下方 nudge。
                if (len(body_text.strip()) >= 200
                        and not _looks_like_planning(body_text)
                        and not _is_meta_wrapup(body_text)
                        and not any(k in body_text.lower() for k in _PENDING_INTENT_PATTERNS)):
                    _deliver, _lang_retry = await _ensure_final_language_with_retry(client, api_url, headers, _engine_model, body_text.strip(), user_q, current_msgs, lang_retry_done=lang_retry_done)
                    if _lang_retry and not lang_retry_done:
                        lang_retry_done = True
                        text_output_delivered = True
                        text_only_streak += 1
                        yield {"event": "heartbeat"}
                        continue
                    yield {"content": "\n\n" + _strip_think_fences(_deliver)}
                    _track_progress(session_id, "completed", f"text_response ({len(_deliver)} chars)")
                    return
                # 任务型请求但模型只回文字不调工具 → nudge 促其行动（不再向用户重复流式输出）
                logger.info(f"[LOCAL-AGENT] Iteration {iteration}: model planning instead of calling tools, nudging (streak={text_only_streak})")
                _stag = _check_stagnation(session_id)
                if _stag:
                    current_msgs.append({"role": "user", "content": _stag})
                current_msgs.append({
                    "role": "user",
                    "content": (
                        "不要写执行计划，直接行动。需要用什么工具就立即调用；"
                        "若本次任务基于用户消息里已提供的资料即可完成，请直接给出完整回答，不要做声明或收尾。"
                    ),
                })
                text_output_delivered = True
                text_only_streak += 1
                continue
            if not streamed_text.strip() and text_only_streak < max_stagnation:
                # Empty response from local model - retry with a nudge
                logger.warning(f"[LOCAL-AGENT] Iteration {iteration}: empty response, retrying (streamed_text={len(streamed_text)} chars, raw_deltas={_raw_delta_count}, tool_calls={len(tool_calls)}, msgs={len(current_msgs)}, last_role={current_msgs[-1].get('role') if current_msgs else '?'})")
                if _raw_delta_count <= 1 and _is_local_llm_url(api_url):
                    # 流正常结束却只收到 role 空块 → 引擎疑似损坏（残留线程竞争）。
                    # 标记后下一次请求会在锁内先验证健康，坏引擎会被杀掉。
                    mark_llm_suspect()
                nudge_text = _get_localized_text(_detect_user_language(_extract_last_user_text(current_msgs)), {
                    "zh": "⚠️ 你上一轮的回复是空的。请直接回复用户，或者使用工具完成任务。如果需要调用工具，使用 ```tool 格式。",
                    "en": "⚠️ Your last response was empty. Please respond to the user directly, or use a tool. To call a tool, use the ```tool format.",
                    "ja": "⚠️ 前回の応答が空でした。ユーザーに直接返信するか、ツールを使用してください。ツールを使用するには ```tool 形式を使ってください。",
                })
                current_msgs.append({"role": "system", "content": nudge_text})
                text_only_streak += 1
                continue
            # ── Empty-response exhaustion: streak cap reached and model still
            # produced nothing. Do NOT silently finish the task — the user would
            # see "执行一半就停了" with no explanation.
            if not streamed_text.strip():
                logger.warning(f"[LOCAL-AGENT] Iteration {iteration}: {text_only_streak} consecutive empty responses, aborting with diagnostic")
                yield {"content": (
                    "\n\n⚠️ **本地模型连续多次无响应，任务已中止。**\n"
                    "可能原因：\n"
                    "1. 模型上下文不足——系统提示+工具列表超过了模型的上下文窗口，"
                    "输入被截断后模型输出为空（4B 小模型常见）\n"
                    "2. 该模型不支持工具调用格式，或对长指令敏感\n"
                    "建议：换用更大的模型（7B+），或重启模型服务后重试。"
                )}
                _track_progress(session_id, "stalled", f"empty_response x{text_only_streak}")
                return
            # ── 输出反思（可选档位）：修正后前端替换最后一条消息 ──
            if _should_reflect(reflection_mode, body_text, _is_local_llm_url(api_url)):
                _tool_outs = [str(m.get("content") or "") for m in current_msgs if m.get("role") == "tool"]
                _revised, _changed = await _reflect_output(body_text, model, api_url, headers, reflection_mode, client, _tool_outs)
                if _changed and _revised.strip():
                    body_text = _revised
                    yield {"event": "reflection_revised", "content": _revised}

            # 短回答 + 有工具失败：模型很可能因工具报错而放弃（如 400 参数错误）。
            # 追加一轮提示让它绕过失败的工具重试/换工具，而不是 112 字符草草收场。
            if (len(body_text.strip()) < 200 and not has_called_tool
                    and any(m.get("role") == "tool" and ("Error" in str(m.get("content", "")) or "⚠️" in str(m.get("content", "")) or "失败" in str(m.get("content", "")))
                            for m in current_msgs[-4:])):
                current_msgs.append({
                    "role": "system",
                    "content": "上一个工具调用失败了。请换一个工具或调整参数重试，"
                               "不要因一次失败就直接给简短结论；若全部工具不可用，再如实告知。",
                })
                text_output_delivered = True  # 追问轮替换上一条，不堆叠
                logger.info(f"[LOCAL-AGENT] Iteration {iteration}: 短回答+工具失败，继续追问一轮")
                continue
            # 短回答 + 工具成功且有实质资料（如查大盘指数拿到 45 行预览，
            # 最终却只回 84 字符"草草收场"）：提示模型基于已有资料给出
            # 充分、带数据的回答。阈值 600：低于它的大多是"几点了"这类
            # 小工具查询，逼长回答反而奇怪。只追加一次，防止死循环。
            _tool_out_total = sum(len(str(m.get("content") or "")) for m in current_msgs if m.get("role") == "tool")
            if (len(body_text.strip()) < 200 and has_called_tool
                    and _tool_out_total > 600 and not _brief_answer_nudged):
                _brief_answer_nudged = True
                current_msgs.append({
                    "role": "system",
                    "content": "你已通过工具获得了实质数据（见上方工具结果），"
                               "但刚才的回答太简短。请基于这些数据给出充分的回答："
                               "包含关键数字与必要的展开说明，让用户不需要再追问。"
                               "若任务确已完成且无需展开，再如实收尾。",
                })
                text_output_delivered = True  # 追问轮替换上一条，不堆叠
                logger.info(f"[LOCAL-AGENT] Iteration {iteration}: 短回答+有实质资料({_tool_out_total} chars)，追问充分回答一轮")
                continue

            # 无检查兜底路径（门控被 _recent_tool_failed 跳过时落在这里，
            # 16:55 事故英文 425 字符、19:36 未执行意图规划话术即从此交付）——
            # 与闸门同款拦截：规划话术/未执行意图/元评论不得当最终答案。
            # 本地模型只打回 1 次即放行（09-04 美股/15:34 事故：打回越多，
            # 模型空转重写越久，反而拖进 788s/817s 预算中止——打回 1 次
            # 后放行，内容至少到手，必要时用户可点"继续"再要更多）
            _pending_intent2 = any(k in body_text.lower() for k in _PENDING_INTENT_PATTERNS)
            _fb_cap = 1 if _is_local_llm_url(api_url) else 3
            if ((_looks_like_planning(body_text) or _pending_intent2 or _is_meta_wrapup(body_text))
                    and _intent_nudges < _fb_cap):
                current_msgs.append({"role": "assistant", "content": (body_text.strip() or "（未输出正文）")})
                current_msgs.append({"role": "system", "content":
                    "⚠️ 你上一轮只说了计划/声明而没有执行。刚才的查询可能失败或未覆盖全部数据——"
                    "如果需要数据，立即调用相应的工具；如果数据已足够（包括用户消息里已提供的内容），"
                    "就把完整分析（含关键数字与结论）写进回复正文。不要只重复计划。"})
                _intent_nudges += 1
                text_output_delivered = True
                text_only_streak += 1
                logger.info(f"[LOCAL-AGENT] Iteration {iteration}: 兜底路径规划/未执行意图拦截（{_intent_nudges}/{_fb_cap}）")
                continue
            # 无来源数字同样拦截（19:05 编造事故；本地 1 次即放行，云端 2 次）
            _unsourced = _find_unsourced_numbers(body_text, current_msgs)
            if _unsourced and _fabrication_nudges < _fab_cap:
                current_msgs.append({"role": "assistant", "content": (body_text.strip() or "（未输出正文）")})
                current_msgs.append({"role": "system", "content":
                    f"⚠️ 数据来源校验：回复中的数字 {'、'.join(_unsourced[:8])} "
                    f"未出现在本会话用户消息或工具结果中——若为换算请注明'按工具数据折算'，"
                    f"否则删除或写'工具未返回该数据'。请修正后重新作答。"})
                _fabrication_nudges += 1
                text_output_delivered = True
                text_only_streak += 1
                logger.info(f"[LOCAL-AGENT] Iteration {iteration}: 兜底路径无来源数字拦截（{_fabrication_nudges}/2）")
                continue
            # cap 已满（此路径 1 次即放行）、正文仍是元收尾/规划话术 → 终答提取：
            # 非流式单轮回合（含用户消息里的内联资料与工具结果），强制产出实质
            # 回答再交付，而非把"分析任务已完成…回复 1/2/3"式元收尾直接发给用户
            # （09-05 13:06 事故：Ornith 被连续 nudge 推入元叙述循环）。
            if (_is_meta_wrapup(body_text) or _looks_like_planning(body_text)
                    or any(k in body_text.lower() for k in _PENDING_INTENT_PATTERNS)):
                _sout = await _final_answer_extraction(
                    client, api_url, headers, _engine_model, current_msgs,
                    _detect_user_language(_extract_last_user_text(current_msgs)))
                if (len(_sout) >= 200
                        and not _is_meta_wrapup(_sout)
                        and not _looks_like_planning(_sout)
                        and not any(k in _sout.lower() for k in _PENDING_INTENT_PATTERNS)):
                    body_text = _sout
                    logger.info(f"[LOCAL-AGENT] Iteration {iteration}: 元收尾兜底替换为终答提取 ({len(_sout)} chars)")
            # 最终交付：只交付正文（body_text）。正文空/思考-only 时绝不把
            # 思考流发给用户（09-05 13:29 事故），给干净提示。
            if not body_text.strip():
                yield {"content": (
                    "\n\n⚠️ 本地模型未能生成有效的分析正文（只输出了思考过程）。"
                    "请回复「继续」重试，或换云端模型重发本任务。")}
                _track_progress(session_id, "completed", "body_empty_final")
                return
            _deliver, _lang_retry = await _ensure_final_language_with_retry(client, api_url, headers, _engine_model, body_text.strip(), _extract_last_user_text(current_msgs), current_msgs, lang_retry_done=lang_retry_done)
            if _lang_retry and not lang_retry_done:
                lang_retry_done = True
                text_output_delivered = True
                text_only_streak += 1
                yield {"event": "heartbeat"}
                continue
            yield {"content": "\n\n" + _strip_think_fences(_deliver)}
            _track_progress(session_id, "completed", f"text_response ({len(_deliver)} chars)")
            logger.info(f"[LOCAL-AGENT] Iteration {iteration}: no tools, returning text ({len(_deliver)} chars)")
            return

        tool_count = sum(1 for m in current_msgs if m.get("role") == "tool")
        yield {"content": f"\n\n⚠️ 已达到硬上限 ({max_iterations} 轮)。本会话共执行了 {tool_count} 次工具调用。如需继续，请发送新消息。"}


# ╔══════════════════════════════════════════════════════╗
# ║  SECTION 9: Chat Building & LLM Config               ║
# ║  _build_chat_messages, _resolve_api_target, etc.     ║
# ╚══════════════════════════════════════════════════════╝

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
