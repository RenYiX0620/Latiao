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

# 命令安全不变量单点定义（审计 P0）：插件、本 fallback、插件 seed 共用，
# 消除三处漂移——fallback 此前缺解释器内联拦截（python3 -c / node -e）。
from cmd_safety import check_cmd, reject_sensitive_read

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
    Priority: path_pattern rules (permissions.json) → capability 表（统一模型）
    → legacy 无路径规则 → TOOL_PERMISSIONS 插件默认。
    use_skill 特殊处理：解析目标技能的安全等级（技能权限在 capabilities 表）。
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
    # 统一能力模型：use_skill 的技能安全等级 / 工具的表中权限
    try:
        import capability_registry
        if tool_name == "use_skill":
            skill_name = str(args.get("skill_name") or "")
            if skill_name:
                skill = capability_registry.get_skill_content(skill_name)
                return skill["permission"] if skill else "safe"
        table_perm = capability_registry.get_permission(tool_name)
        if table_perm:
            return table_perm
    except Exception:
        logger.debug("capability registry lookup failed for %s", tool_name, exc_info=True)
    # 未知工具 fail-close（审计 P0）：此前默认 "safe"——任何拼写错误/未注册
    # 工具都免确认执行。未知应降级为 confirm，由用户把关。
    return TOOL_PERMISSIONS.get(tool_name, "confirm")


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
    # 统一安全检查（cmd_safety 单点定义——插件/fallback/seed 共用）
    denied = check_cmd(cmd)
    if denied:
        return denied
    if len(cmd) > 1000:
        return f"⛔ Command too long ({len(cmd)} chars, max 1000)"
    # Redirect: if the model is trying to do web search via Python code, tell it to use the tool
    cmd_lower = cmd.lower().strip()
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
        base = os.path.expanduser(directory)
        matches = glob_mod.glob(os.path.join(base, pattern), recursive=True)
        if not pattern.startswith("."):
            # Python glob 的 * 不匹配点开头的隐藏文件/目录（.zcode、.ssh…）——
            # 09-06 14:34 事故：search_files "~/*zcode*" 找不到 ~/.zcode，
            # 模型误判"本地没装 zcode"，转去网上搜回垃圾结果。始终合并隐藏变体。
            hidden = glob_mod.glob(
                os.path.join(base, ".*" + pattern.lstrip("*")), recursive=True)
            matches = list(dict.fromkeys(matches + hidden))
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
    # 模型常给 "high"/"deep" 等非法值 → 映射为 advanced，避免 HTTP 400 整轮失败
    if search_depth not in ("basic", "advanced"):
        search_depth = "advanced"
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

# 子智能体可免确认执行的只读命令白名单（ZCode 式 Explore）。
# 审计 P0：此前只校验首 token 且含 env——"env curl ..." 可执行任意命令、
# "cat ~/.ssh/id_rsa" 可读私钥。现在校验完整 token 序列：命令名必须在
# 白名单内、参数只能是简单选项/路径形态、敏感路径拒绝。
_READONLY_CMD_WORDS = {
    "ls", "cat", "head", "tail", "find", "grep", "rg", "wc", "file",
    "stat", "du", "df", "ps", "which", "whoami", "uname", "pwd",
    "echo", "date",
}

_READONLY_ARG_RE = re.compile(r"-{1,2}[A-Za-z0-9][A-Za-z0-9_-]*|[\w./@+~:-]+")


def _is_readonly_cmd(cmd: str) -> bool:
    """判断子智能体请求的命令是否命中只读白名单（仅限单条简单命令）。

    阶段 3 优先走语义层（safety.rules.readonly_safe）：AST 展开嵌套/前缀，
    `env cat`、`bash -c 'cat x'`、$() 全部现形；语义层不可用时回退本函数
    原有的 token 形态校验（升级不改变旧环境行为）。
    """
    cmd = (cmd or "").strip()
    if not cmd:
        return False
    try:
        from safety.rules import analyze, readonly_safe
        if analyze(cmd).ts_available:
            return readonly_safe(cmd)
    except Exception:
        pass  # 语义层异常 → 回退旧路径
    if re.search(r"[;&|><`$]", cmd):
        return False
    if re.match(r"git\s+(log|status|diff|show|branch|remote|tag|blame)\b", cmd):
        return True
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return False
    if not tokens:
        return False
    first = tokens[0].rsplit("/", 1)[-1]
    if first not in _READONLY_CMD_WORDS:
        return False
    # 参数必须是简单选项/路径形态——拦截一切嵌套命令与引用花活
    if not all(_READONLY_ARG_RE.fullmatch(t) for t in tokens[1:]):
        return False
    # 读取类命令的敏感路径拒绝（与 read_file 插件黑名单对齐）
    return reject_sensitive_read(" ".join(tokens)) is None


# ── 后台子任务注册表（ZCode 式：fire-and-forget + 进度事件 + 结果查询） ──
import time as _time

# delegate 机制已迁至 agent/subagent.py（Stage 5 子代理一等公民）；
# 这里保留兼容导入（api_routes/tests 的既有引用不变）。
from agent.subagent import (  # noqa: F401
    _SUBTASKS, _SUBTASK_EVENTS, _SUBTASK_SEQ, _SUBTASK_TASKS,
    _SUBAGENT_TOOLS, _prune_subtasks, _delegate_task,
    _delegate_task_bg, _delegate_task_fg, _run_subtask_bg,
    _subtask_snapshot,)

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
            "description": "Delegate a sub-task to a specialist sub-agent. Sub-agents run independently with limited tools and return results. Use this to parallelize work — spawn multiple sub-agents for independent sub-tasks. Available agents: explore (read-only deep exploration: search codebase, run read-only commands like ls/grep, web research), code-reviewer (read-only code review), doc-generator (documentation), debugger (bug analysis), translator (translation).",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent": {
                        "type": "string",
                        "enum": ["explore", "code-reviewer", "doc-generator", "debugger", "translator"],
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
