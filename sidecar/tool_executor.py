"""Tool Executor — fallback tool implementations, dispatch table, permission resolution.

Split from main.py (Section 4: Tool Fallbacks + permission helpers). Code is a
verbatim move from main.py — only imports were adjusted for the module split.
"""
import fnmatch
import json
import logging
import os
import platform
import re
import shlex
import subprocess
from pathlib import Path

import httpx

logger = logging.getLogger("latiao-sidecar")

# ═══════════════════════════════════════════════════════
#  Harness: 工具权限分级 + 状态持久化
# ═══════════════════════════════════════════════════════

# Fallback definitions used when no plugins are found
_FALLBACK_PERMISSIONS = {
    "read_file": "safe",
    "list_dir": "safe",
    "search_files": "safe",
    "write_file": "confirm",
    "run_cmd": "confirm",
    "open_app": "confirm",
    "open_folder": "confirm",
    "delegate_task": "safe",
    "tavily_search": "safe",
    "web_search": "safe",
}


def _resolve_permission(tool_name: str, args: dict) -> str:
    """
    Resolve permission level for a tool call.
    Checks custom rules first (with optional path_pattern matching),
    then falls back to TOOL_PERMISSIONS default.
    Rules format: {"tool": "write_file", "path_pattern": "/tmp/*", "permission": "safe"}
    """
    # 状态由 main.py 门面持有（测试通过 main._custom_permissions 重绑定，
    # 这里必须在调用时读 main 的实时槽位）→ 函数内 lazy import 避免循环依赖
    from main import TOOL_PERMISSIONS, _custom_permissions
    for rule in _custom_permissions:
        if rule.get("tool") != tool_name:
            continue
        path_pattern = rule.get("path_pattern")
        if path_pattern:
            for val in args.values():
                if isinstance(val, str) and (
                    fnmatch.fnmatch(val, path_pattern) or
                    fnmatch.fnmatch(os.path.expanduser(val), os.path.expanduser(path_pattern))
                ):
                    return rule.get("permission", "confirm")
        else:
            return rule.get("permission", "confirm")
    return TOOL_PERMISSIONS.get(tool_name, "safe")


#  工具执行函数
# ═══════════════════════════════════════════════════════

MAX_READ_SIZE = 50000  # chars before truncation (~1500 lines)

# ╔══════════════════════════════════════════════════════╗
# ║  SECTION 4: Tool Fallbacks                           ║
# ║  Fallback impls used when plugins/ is empty           ║
# ╚══════════════════════════════════════════════════════╝

def read_file(path: str, offset: int = 0, limit: int = 0) -> str:
    """Read file contents. Supports offset/limit for large files.
    - offset: start reading from this line (1-indexed)
    - limit: max lines to return (0 = up to MAX_READ_SIZE chars)"""
    # Block path traversal
    if ".." in path.split("/") or ".." in path.split("\\"):
        return "⛔ Blocked: path traversal not allowed"
    try:
        with open(path, "r", encoding="utf-8") as f:
            if offset > 1:
                for _ in range(offset - 1):
                    if not f.readline():
                        return f"错误：偏移超出文件范围（第 {offset} 行不存在）"
            if limit > 0:
                lines = []
                for _ in range(limit):
                    line = f.readline()
                    if not line:
                        break
                    lines.append(line)
                content = "".join(lines)
                if len(lines) == limit and f.readline():
                    content += f"\n... (继续读取请使用 offset={offset + limit})"
                return content or "(空)"
            content = f.read(MAX_READ_SIZE + 1)
        if len(content) > MAX_READ_SIZE:
            est_lines = content.count("\n")
            return (
                content[:MAX_READ_SIZE]
                + f"\n\n... (文件过长，已截断。约 {est_lines}+ 行，仅显示前 {MAX_READ_SIZE} 字符。"
                + f"分段读取：read_file(path=\"{path}\", offset={est_lines + 1})"
            )
        return content
    except FileNotFoundError:
        return f"错误：文件不存在 - {path}"
    except Exception as e:
        return f"错误：{e}"


def write_file(path: str, content: str) -> str:
    # Block path traversal
    if ".." in path.split("/") or ".." in path.split("\\"):
        return "⛔ Blocked: path traversal not allowed"
    if len(content) > 10 * 1024 * 1024:  # 10 MB limit
        return f"⛔ File too large ({len(content)} bytes, max 10 MB)"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"✅ 已写入：{path}（{len(content)} 字符）"
    except Exception as e:
        return f"错误：{e}"


def list_dir(path: str) -> str:
    # Block path traversal
    if ".." in path.split("/") or ".." in path.split("\\"):
        return "⛔ Blocked: path traversal not allowed"
    try:
        entries = os.listdir(path)
        lines = [f"  {'📁' if os.path.isdir(os.path.join(path, e)) else '📄'} {e}"
                 for e in sorted(entries)]
        return "目录内容:\n" + "\n".join(lines)
    except Exception as e:
        return f"错误：{e}"


# Reusable command safety patterns (used by run_cmd fallback + plugin-style execute)
_DANGEROUS = [
    # File destruction
    r"rm\s+(-[a-z]*[rf]|--recursive|--force)", r">\s*/dev/(sd|nvme|hd|disk|dm-)",
    r">\s*/etc/", r"chmod\s+[0-7]*7", r"chown\s+-R",
    r"chattr\s+[+-]=*i", r"mv\s+/.*\s*/dev/null",
    # System modification
    r"dd\s+if=", r"mkfs", r"\bsudo\b", r"\bshutdown\b", r"\breboot\b",
    r"\bpoweroff\b", r"\binit\s+0\b", r"\binit\s+6\b",
    r"systemctl\s+(stop|disable|mask|kill)",
    r"launchctl\s+(unload|remove|bootout)",
    # Code execution
    r"\beval\s", r"\bbase64\s+(-d|--decode|--wrap)", r"`[^`]+`", r"\$\([^)]+\)",
    r"python\s+-c\s+['\"]", r"python3\s+-c\s+['\"]",
    r"perl\s+-e\s+['\"]", r"ruby\s+-e\s+['\"]",
    r"node\s+-e\s+['\"]",
    # Pipe to shell
    r"\bcurl\b.*\|\s*(ba)?sh\b", r"\bwget\b.*\|\s*(ba)?sh\b",
    r"echo.*\b\|\s*(ba)?sh\b", r"cat.*\b\|\s*(ba)?sh\b",
    r"\bbase64.*\|\b.*sh", r"openssl.*\|\b.*sh",
    # Dangerous xargs/nohup combos
    r"xargs\s+rm", r"xargs\s+kill",
    r"nohup.*rm\s", r"nohup.*kill\s",
    # Fork bomb
    r":\(\)\s*\{", r":\|:&",
]


def run_cmd(cmd: str) -> str:
    # Strip shell comment lines (models sometimes prepend "# comment\n")
    cmd = "\n".join(line for line in cmd.split("\n") if not line.strip().startswith("#")).strip()
    if not cmd:
        return "错误：命令为空（可能只包含注释行）"
    # Reject unsupported shell operators — with shell=False they are passed as
    # literal args and the command silently misbehaves (only the 1st runs).
    m = re.search(r"(&&|\|\||\||;|\$\(|>|<)", cmd)
    if m:
        return (
            f"⛔ 不支持 shell 操作符 '{m.group(1)}'：本工具以 shell=False 执行，"
            "复合命令会静默失败。请拆成多次调用，每次只运行一条命令"
            "（不要用 && | ; > < 等）。"
        )
    # Safety check before execution (fallback version — plugin has fuller check)
    cmd_lower = cmd.lower().strip()
    for pattern in _DANGEROUS:
        if re.search(pattern, cmd_lower):
            return f"⛔ Blocked unsafe command: {cmd}"
    if len(cmd) > 1000:
        return f"⛔ Command too long ({len(cmd)} chars, max 1000)"
    # Redirect: if the model is trying to do web search via Python code, tell it to use the tool
    if re.search(r'(tavily|requests\.|urllib|httpx|aiohttp)', cmd_lower) and re.search(r'(search|api|get|post)', cmd_lower):
        return (
            "⛔ 不要用 Python 代码做网络搜索或 API 请求！\n"
            "请使用 web_search 工具来做网络搜索，例如：\n"
            "  web_search({query: \"你的搜索词\"})\n"
            "对于文件操作，使用 read_file、list_dir、write_file 等工具。"
        )
    try:
        try:
            tokens = shlex.split(cmd)
        except ValueError as e:
            return f"命令格式错误: {e}"
        r = subprocess.run(tokens, shell=False, capture_output=True, text=True, timeout=30)
        out = r.stdout.strip()
        if r.returncode != 0:
            out += f"\n(退出码: {r.returncode})"
            if r.stderr.strip():
                out += f"\n{r.stderr.strip()}"
        return out or "(无输出)"
    except subprocess.TimeoutExpired:
        return f"超时: {cmd}"
    except Exception as e:
        return f"错误：{e}"


def open_folder(path: str) -> str:
    try:
        if platform.system() == "Darwin":
            subprocess.Popen(["open", path])
        elif platform.system() == "Windows":
            os.startfile(path)
        else:
            subprocess.Popen(["xdg-open", path])
        return f"✅ 已打开：{path}"
    except Exception:
        logger.debug("open_folder failed, falling back to list_dir", exc_info=True)
        return list_dir(path)


_APP_ALIASES = {
    "照片": "Photos", "相册": "Photos", "photo": "Photos",
    "音乐": "Music", "music": "Music",
    "浏览器": "Safari", "safari": "Safari",
    "邮件": "Mail", "mail": "Mail",
    "日历": "Calendar", "calendar": "Calendar",
    "备忘录": "Notes", "notes": "Notes",
    "提醒": "Reminders", "reminders": "Reminders",
    "计算器": "Calculator", "calculator": "Calculator",
    "终端": "Terminal", "terminal": "Terminal",
    "设置": "System Settings", "系统设置": "System Settings", "偏好设置": "System Settings",
    "App Store": "App Store", "app store": "App Store",
    "地图": "Maps", "maps": "Maps",
    "天气": "Weather", "weather": "Weather",
    "时钟": "Clock", "clock": "Clock",
    "查找": "Find My", "find my": "Find My",
}


def open_app(name: str) -> str:
    """Open a macOS application by name. Supports both English and Chinese names."""
    # Resolve Chinese aliases
    resolved = _APP_ALIASES.get(name, name)
    try:
        subprocess.Popen(["open", "-a", resolved])
        return f"✅ 已打开应用：{resolved}"
    except Exception as e:
        return f"无法打开应用 {resolved}: {e}"


def search_files(directory: str, pattern: str) -> str:
    """Search for files matching a glob pattern in a directory."""
    if ".." in directory.split("/"):
        return "⛔ Blocked: path traversal not allowed"
    import glob as glob_mod
    try:
        search_path = os.path.join(os.path.expanduser(directory), pattern)
        matches = glob_mod.glob(search_path, recursive=True)
        if not matches:
            return f"No files matching '{pattern}' found in {directory}"
        lines = []
        for m in sorted(matches)[:50]:
            icon = "📁" if os.path.isdir(m) else "📄"
            lines.append(f"  {icon} {m}")
        result = f"Search results for '{pattern}' in {directory}:\n" + "\n".join(lines)
        if len(matches) > 50:
            result += f"\n  ... and {len(matches) - 50} more results"
        return result
    except Exception as e:
        return f"Error searching files: {e}"


async def tavily_search(args: dict) -> str:
    """Search the web using Tavily API."""
    import json

    # 常量由 main.py 门面持有 → 函数内 lazy import 避免循环依赖
    from main import TAVILY_API_URL
    config_file = Path.home() / ".local-ai-os" / "config.json"
    # Priority: env var → macOS Keychain → config.json (legacy)
    api_key = os.environ.get("TAVILY_API_KEY")

    if not api_key:
        # Try macOS Keychain via security CLI
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-s", "com.latiao.desktop", "-a", "tavily_api_key", "-w"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                api_key = result.stdout.strip()
        except Exception:
            logger.debug("Tavily keychain read failed", exc_info=True)

    if not api_key:
        try:
            if config_file.exists():
                cfg = json.loads(config_file.read_text(encoding="utf-8"))
                api_key = cfg.get("tavily_api_key")
        except Exception:
            logger.warning("Failed to read Tavily key from config.json", exc_info=True)

    if not api_key:
        return (
            "⚠️ Tavily API Key 未配置。\n"
            "请在应用的「技能」界面中找到 Web Search (Tavily)，填写 API Key。\n"
            "免费注册：https://tavily.com"
        )

    query = args["query"]
    search_depth = args.get("search_depth", "basic")
    max_results = min(args.get("max_results", 5), 10)

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30)) as client:
            resp = await client.post(
                TAVILY_API_URL,
                json={
                    "api_key": api_key,
                    "query": query,
                    "search_depth": search_depth,
                    "max_results": max_results,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        results = data.get("results", [])
        answer = data.get("answer", "")

        if not results and not answer:
            return f"🔍 Tavily 搜索: {query}\n\n未找到相关结果。"

        lines = [f"🔍 Tavily 搜索: {query}\n"]

        if answer:
            lines.append(f"📝 {answer}\n")

        if results:
            lines.append(f"📎 共 {len(results)} 条结果:\n")
            for i, r in enumerate(results, 1):
                title = r.get("title", "No title")
                url = r.get("url", "")
                content = r.get("content", "")
                if len(content) > 300:
                    content = content[:300] + "..."
                lines.append(f"{i}. **{title}**")
                lines.append(f"   {url}")
                lines.append(f"   {content}\n")

        return "\n".join(lines)

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            return "⚠️ Tavily API Key 无效或已过期。请在技能设置中更新 API Key。"
        return f"⚠️ Tavily 搜索失败: HTTP {e.response.status_code}"
    except httpx.ConnectError:
        return "⚠️ 无法连接 Tavily API (api.tavily.com)。请检查网络连接。"
    except Exception as e:
        return f"⚠️ Tavily 搜索异常: {e}"


# ═══════════════════════════════════════════════════════
#  Sub-Agent System: delegate_task spawns specialist sub-agents
# ═══════════════════════════════════════════════════════

_SUBAGENT_TOOLS: dict[str, list[str]] = {
    "code-reviewer": ["read_file", "list_dir", "search_files"],
    "doc-generator": ["read_file", "list_dir", "search_files", "write_file"],
    "debugger": ["read_file", "list_dir", "search_files", "run_cmd"],
    "translator": ["read_file", "list_dir", "search_files", "write_file"],
}


# ── 后台子任务注册表（ZCode 式：fire-and-forget + 进度事件 + 结果查询） ──
import time as _time

_SUBTASKS: dict[str, dict] = {}   # task_id -> {agent, task, status, events, result, ...}
_SUBTASK_EVENTS: list[dict] = []  # 事件流（前端心跳拉取）
_SUBTASK_SEQ = 0


def _subtask_snapshot() -> list[dict]:
    """后台子任务列表快照（heartbeat 附带，前端活动栏渲染）。"""
    out = []
    for tid, s in _SUBTASKS.items():
        out.append({
            "id": tid, "agent": s["agent"], "task": s["task"][:60],
            "status": s["status"], "steps": s["steps"],
            "started_at": s["started_at"], "updated_at": s["updated_at"],
            "summary": (s["result"] or "")[:160],
        })
    return out


async def _run_subtask_bg(task_id: str, agent_type: str, task: str):
    """后台跑子 agent（非阻塞主对话），事件实时进注册表。"""
    s = _SUBTASKS[task_id]
    try:
        # 复用前台逻辑但拦截进度：为简洁直接跑完整 _delegate_task
        s["status"] = "running"
        s["updated_at"] = _time.time()
        result = await _delegate_task(agent_type, task)
        s["result"] = result
        s["status"] = "done" if not result.startswith("[Sub-agent") or "错误" not in result else "error"
        s["updated_at"] = _time.time()
        _SUBTASK_EVENTS.append({"id": task_id, "status": s["status"], "summary": result[:120]})
    except Exception as e:
        s["result"] = f"[Sub-agent: {agent_type}] 错误: {e}"
        s["status"] = "error"
        s["updated_at"] = _time.time()


async def _delegate_task_bg(agent_type: str, task: str) -> str:
    """后台模式：立即返回任务 ID，子 agent 异步执行，进度/结果走 heartbeat。"""
    global _SUBTASK_SEQ
    _SUBTASK_SEQ += 1
    task_id = f"sub_{_time.strftime('%H%M%S')}_{_SUBTASK_SEQ}"
    _SUBTASKS[task_id] = {
        "agent": agent_type, "task": task, "status": "running",
        "steps": 0, "result": "", "started_at": _time.time(), "updated_at": _time.time(),
    }
    _SUBTASK_EVENTS.append({"id": task_id, "status": "started", "summary": task[:80]})
    import asyncio as _asyncio
    _asyncio.get_event_loop().create_task(_run_subtask_bg(task_id, agent_type, task))
    return f"[Sub-agent {agent_type} 后台任务已启动] task_id={task_id}\n主对话可继续；结果将自动出现在子智能体面板，也可用 task_id 查询。"


async def _delegate_task(agent_type: str, task: str) -> str:
    """Spawn a specialist sub-agent to handle a delegated task.
    Uses async httpx to avoid blocking the main event loop."""
    if not task.strip():
        return "错误：任务描述不能为空"

    # 依赖 agent_loop 的全局状态/LLM 辅助 → 函数内 lazy import 避免循环依赖
    # SUBAGENT_MODEL 常量由 main.py 门面持有
    from agent_loop import (
        AGENT_PROFILES,
        TOOL_PERMISSIONS,
        TOOLS,
        _inject_thinking_disabled,
        _last_cloud_config,
        _local_llm_serialized,
        _resolve_api_target,
        _sanitize_tool_messages,
        execute_tool,
    )
    from main import SUBAGENT_MODEL
    cfg = AGENT_PROFILES.get(agent_type, AGENT_PROFILES.get("code-reviewer", {}))
    allowed = _SUBAGENT_TOOLS.get(agent_type, ["read_file", "list_dir", "search_files"])
    sub_tools = [t for t in TOOLS if t.get("function", {}).get("name") in allowed]
    # Sub-agents cannot use confirm-level tools (no user confirmation possible)
    sub_tools = [t for t in sub_tools if TOOL_PERMISSIONS.get(t.get("function", {}).get("name"), "safe") != "confirm"]

    messages = [
        {"role": "system", "content": cfg.get("identity", "")},
        {"role": "system", "content": "你是一个子 Agent。独立完成任务后返回简洁结果。最多 3 步，不要问问题，直接执行。"},
        {"role": "user", "content": task},
    ]

    protocol, api_url, sub_headers, _is_local = _resolve_api_target(_last_cloud_config.get())
    if not api_url:
        return f"[Sub-agent: {agent_type}] 错误: 无法连接模型服务（请配置云端模型或启动本地 LLM）"

    current_msgs = list(messages)

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60)) as client:
            for _ in range(3):
                body = {
                    "model": SUBAGENT_MODEL,
                    "messages": _sanitize_tool_messages(current_msgs),
                    "tools": sub_tools,
                    "tool_choice": "auto",
                    "max_tokens": 1024,
                    "stream": False,
                    "temperature": 0.5,
                    "frequency_penalty": 0.6,
                "stop": ["<|im_end|>", "<|endoftext|>", "<end_of_turn>", "<eos>"],
                }
                _inject_thinking_disabled(body, SUBAGENT_MODEL)
                async with _local_llm_serialized(api_url):
                    r = await client.post(api_url, json=body, headers=sub_headers)
                if r.status_code != 200:
                    return f"[Sub-agent: {agent_type}] HTTP {r.status_code}"

                data = r.json()
                choice = data.get("choices", [{}])[0]
                msg = choice.get("message", {})

                tool_calls = msg.get("tool_calls", [])
                if tool_calls:
                    current_msgs.append({"role": "assistant", "content": msg.get("content"), "tool_calls": tool_calls})
                    for tc in tool_calls:
                        fn = tc.get("function", {})
                        tname = fn.get("name", "")
                        try:
                            targs = json.loads(fn.get("arguments", "{}"))
                        except json.JSONDecodeError:
                            logger.warning("Sub-agent received malformed tool arguments", exc_info=True)
                            targs = {}
                        perm = _resolve_permission(tname, targs)
                        if perm == "deny":
                            tres = "⛔ 工具已被权限系统阻止: " + tname
                            logger.warning("Sub-agent attempted blocked tool: " + tname)
                        elif perm == "confirm":
                            tres = "⛔ 子 Agent 不能执行需要用户确认的工具 (" + tname + ")。跳过执行。"
                            logger.warning("Sub-agent blocked from confirm-level tool: " + tname)
                        else:
                            tres = await execute_tool(tname, targs)
                        current_msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": tres})
                else:
                    return f"[Sub-agent: {agent_type}]\n{msg.get('content', '无输出')}"

        return f"[Sub-agent: {agent_type}] 达到最大迭代次数"
    except Exception as e:
        return f"[Sub-agent: {agent_type}] 错误: {e}"


# ── Fallback OpenAI Function Calling tool definitions ──

_FALLBACK_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file at the given path. Supports offset and limit for large files. File truncated at 50000 chars — use offset to continue reading.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path to the file."},
                    "offset": {"type": "integer", "description": "Line number to start reading from (1-indexed). Use this with limit to read large files in chunks."},
                    "limit": {"type": "integer", "description": "Maximum number of lines to return. Use with offset for chunked reading."}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write text content to a file. Creates parent directories if needed. ⚠️ Requires user confirmation before executing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path where the file should be written."},
                    "content": {"type": "string", "description": "The text content to write to the file."}
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List the contents of a directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path to the directory to list."}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_cmd",
            "description": "Run ONE single command and return its output (no shell). ⚠️ Requires user confirmation. Dangerous commands (rm -rf, sudo, etc.) are always blocked. Do NOT use shell operators (&& | ; > <) — call once per command instead.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cmd": {"type": "string", "description": "A single command with arguments (no shell operators like && | ; > <)."}
                },
                "required": ["cmd"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "open_folder",
            "description": "Open a folder in Finder (macOS only).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path to the folder to open."}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "open_app",
            "description": "Open a macOS application by name. Use this when the user asks to open an app. Supports both English names (Photos, Safari, Mail) and Chinese names (照片/相册, 浏览器, 邮件).",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "App name in English or Chinese (e.g., 'Photos', 'Safari', '照片', '浏览器')."}
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search for files matching a glob pattern in a directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {"type": "string", "description": "Absolute path to the directory to search in."},
                    "pattern": {"type": "string", "description": "Glob pattern to match (e.g., '*.py', '**/*.md')."}
                },
                "required": ["directory", "pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "搜索互联网获取实时信息（tavily_search 的旧名别名，二者等价，指向同一个 Tavily 搜索）。优先使用 tavily_search；仅当工具列表中没有 tavily_search 时才用本工具，不要同时调用两者。返回标题、URL和内容摘要。不要用 run_cmd 或手写代码来做网络搜索。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query. Be specific and use keywords."},
                    "search_depth": {"type": "string", "enum": ["basic", "advanced"], "description": "Search depth: 'basic' (faster) or 'advanced' (thorough). Default: basic."},
                    "max_results": {"type": "integer", "description": "Max results to return (1-10). Default: 5."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delegate_task",
            "description": "Delegate a sub-task to a specialist sub-agent. Sub-agents run independently with limited tools and return results. Use this to parallelize work — spawn multiple sub-agents for independent sub-tasks. Available agents: code-reviewer (read-only code review), doc-generator (documentation), debugger (bug analysis), translator (translation).",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent": {
                        "type": "string",
                        "enum": ["code-reviewer", "doc-generator", "debugger", "translator"],
                        "description": "The specialist agent type to delegate to."
                    },
                    "task": {
                        "type": "string",
                        "description": "The specific task for the sub-agent. Be clear and concise — the sub-agent only sees this task description."
                    }
                },
                "required": ["agent", "task"]
            }
        }
    },
]

_FALLBACK_DISPATCH = {
    "read_file": lambda args: read_file(args["path"]),
    "write_file": lambda args: write_file(args["path"], args["content"]),
    "list_dir": lambda args: list_dir(args["path"]),
    "run_cmd": lambda args: run_cmd(args["cmd"]),
    "open_folder": lambda args: open_folder(args["path"]),
    "open_app": lambda args: open_app(args["name"]),
    "search_files": lambda args: search_files(args["directory"], args["pattern"]),
    "tavily_search": lambda args: tavily_search(args),
    "web_search": lambda args: tavily_search(args),
    "delegate_task": lambda args: _delegate_task(args.get("agent", "code-reviewer"), args.get("task", "")),
}
