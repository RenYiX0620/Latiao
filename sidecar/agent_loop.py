"""Agent Loop — agent loops, tool call parsing, tool dispatch state, chat building.

Split from main.py (Sections 2/3/5/6/7/8/9). Code is a verbatim move from
main.py — only imports were adjusted for the module split. Mutable state that
must stay visible through the main.py facade (TOOLS, TOOL_DISPATCH, etc.) is
defined here and re-exported by main.py.
"""
import asyncio
import contextvars
import json
import logging
import os
import re
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
from agent.tool_exec import (  # noqa: F401  —— re-export（旧导入路径继续可用）
    _dispatch_delegate,
    _dispatch_use_skill,
    _get_agent_tools,
    _handle_tool_execution,
    _handle_tool_execution_inner,
    _record_tool_call_db,
    _stamp_time_sensitive,
    execute_tool,
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
from identity import _load_agent_identity
from tool_executor import (
    _FALLBACK_DISPATCH,
    _FALLBACK_PERMISSIONS,
    _FALLBACK_TOOLS,
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












