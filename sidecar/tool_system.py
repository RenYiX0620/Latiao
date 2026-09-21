"""Tool System Module — plugin loading, seeding, and dispatch."""
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger("tool_system")

# ═══════════════════════════════════════════════════════
#  Plugin System: auto-scan sidecar/plugins/ for tool .py files
# ═══════════════════════════════════════════════════════

PLUGINS_DIR = Path(__file__).parent / "plugins"

# Embedded plugin source code for first-run seeding
_SEED_PLUGINS = {
    'read_file.py': r'''"""Read the contents of a file at the given path. Supports ~ expansion."""

import os

_BLOCKED_SUBSTRINGS = ("/.ssh", "/.aws", "/.gnupg", "/Library/Keychains", "/.kube", "/.docker")
_BLOCKED_FILE_NAMES = (".env", "id_rsa", "id_ed25519", "id_ecdsa", "known_hosts")

MAX_READ_SIZE = 10000  # chars before truncation

NAME = "read_file"
PERMISSION = "safe"

DEFINITION = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read the contents of a file at the given path. Large files are truncated.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the file."}
            },
            "required": ["path"]
        }
    }
}


def _safe_path(path: str) -> str | None:
    """返回规范化后的绝对路径；不合法返回 None。"""
    if not path:
        return None
    expanded = os.path.expanduser(path)
    # 必须本身就是绝对路径（realpath 会把相对路径变成绝对路径，所以先查）
    if not os.path.isabs(expanded):
        return None
    # Block path traversal — 两种分隔符都查
    if ".." in path.split("/") or ".." in path.split("\\"):
        return None
    # realpath 解析符号链接，防止经由 symlink 逃出校验
    return os.path.realpath(expanded)


def execute(args: dict) -> str:
    p = _safe_path(args["path"])
    if p is None:
        return "⛔ Blocked: path traversal not allowed"
    # 敏感目录（密钥/凭证）一律拒绝
    if any(s in p for s in _BLOCKED_SUBSTRINGS):
        return f"⛔ Blocked: 不允许访问敏感目录 - {p}"
    # 敏感文件名一律拒绝
    if os.path.basename(p) in _BLOCKED_FILE_NAMES:
        return f"⛔ Blocked: 不允许读取敏感文件 - {p}"
    try:
        with open(p, "r", encoding="utf-8") as f:
            content = f.read(MAX_READ_SIZE + 1)
        if len(content) > MAX_READ_SIZE:
            est_lines = content.count("\n")
            return (
                content[:MAX_READ_SIZE]
                + f"\n\n... (文件过长，已截断。约 {est_lines}+ 行，"
                + f"仅显示前 {MAX_READ_SIZE} 字符。如需完整内容请分段读取)"
            )
        return content
    except FileNotFoundError:
        return f"错误：文件不存在 - {p}"
    except Exception as e:
        return f"错误：{e}"
''',
    'write_file.py': r'''"""Write text content to a file. Creates parent directories if needed."""
import os

_BLOCKED_DIRS = ("/etc", "/System", "/usr", "/bin", "/sbin", "/var", "/private/etc")
_BLOCKED_SUBSTRINGS = ("/.ssh", "/.aws", "/.gnupg", "/Library/Keychains", "/.kube", "/.docker")
_BLOCKED_FILE_NAMES = (".env", "id_rsa", "id_ed25519", "id_ecdsa", "known_hosts")

NAME = "write_file"
PERMISSION = "confirm"

DEFINITION = {
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
}


def _safe_path(path: str) -> str | None:
    """返回规范化后的绝对路径；不合法返回 None。"""
    if not path:
        return None
    expanded = os.path.expanduser(path)
    # 必须本身就是绝对路径（realpath 会把相对路径变成绝对路径，所以先查）
    if not os.path.isabs(expanded):
        return None
    # Block path traversal — 两种分隔符都查
    if ".." in path.split("/") or ".." in path.split("\\"):
        return None
    # realpath 解析符号链接，防止经由 symlink 逃出校验
    return os.path.realpath(expanded)


def execute(args: dict) -> str:
    content = args["content"]
    p = _safe_path(args["path"])
    if p is None:
        return "⛔ Blocked: path traversal not allowed"
    # 系统目录一律拒绝写入
    if any(p == d or p.startswith(d + os.sep) for d in _BLOCKED_DIRS):
        return f"⛔ Blocked: 不允许写入系统目录 - {p}"
    # 敏感目录（密钥/凭证）一律拒绝
    if any(s in p for s in _BLOCKED_SUBSTRINGS):
        return f"⛔ Blocked: 不允许访问敏感目录 - {p}"
    # 敏感文件名一律拒绝
    if os.path.basename(p) in _BLOCKED_FILE_NAMES:
        return f"⛔ Blocked: 不允许写入敏感文件 - {p}"
    # sidecar 的 plugins/ 目录一律拒绝（写入后下次启动会被 import 执行 = RCE）
    sidecar_plugins = os.path.realpath(os.path.dirname(__file__))
    if p == sidecar_plugins or p.startswith(sidecar_plugins + os.sep):
        return f"⛔ Blocked: 不允许写入插件目录 - {p}"
    try:
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return f"✅ 已写入：{p}（{len(content)} 字符）"
    except Exception as e:
        return f"错误：{e}"
''',
    'list_dir.py': r'''"""List the contents of a directory."""
import os

NAME = "list_dir"
PERMISSION = "safe"

DEFINITION = {
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
}


def _safe_path(path: str) -> str | None:
    """返回规范化后的绝对路径；不合法返回 None。"""
    if not path:
        return None
    expanded = os.path.expanduser(path)
    # 必须本身就是绝对路径（realpath 会把相对路径变成绝对路径，所以先查）
    if not os.path.isabs(expanded):
        return None
    # Block path traversal — 两种分隔符都查
    if ".." in path.split("/") or ".." in path.split("\\"):
        return None
    # realpath 解析符号链接，防止经由 symlink 逃出校验
    return os.path.realpath(expanded)


def execute(args: dict) -> str:
    p = _safe_path(args["path"])
    if p is None:
        return "⛔ Blocked: path traversal not allowed"
    try:
        entries = os.listdir(p)
        lines = [f"  {'📁' if os.path.isdir(os.path.join(p, e)) else '📄'} {e}"
                 for e in sorted(entries)]
        return "目录内容:\n" + "\n".join(lines)
    except Exception as e:
        return f"错误：{e}"
''',
    'run_cmd.py': r'''"""Run a shell command and return its output. ⚠️ Requires user confirmation."""
import re
import shlex
import subprocess

# 安全不变量单点定义（cmd_safety）——fallback 与插件本体共用，消除漂移
from cmd_safety import (
    DESTRUCTIVE_PATTERNS,
    OBFUSCATION_PATTERNS,
    SAFE_CMD_RE,
    check_cmd,
)

# 保留旧名字导出：test_security 等引用这些名字
_DESTRUCTIVE_PATTERNS = DESTRUCTIVE_PATTERNS
_OBFUSCATION_PATTERNS = OBFUSCATION_PATTERNS
_ALWAYS_ALLOWED = SAFE_CMD_RE

NAME = "run_cmd"
PERMISSION = "confirm"

DEFINITION = {
    "type": "function",
    "function": {
        "name": "run_cmd",
        "description": "Run ONE single command and return its output (no shell). ⚠️ Requires user confirmation. Destructive commands are always blocked. Do NOT use shell operators like && | ; > < — they are not supported; call this tool once per command instead.",
        "parameters": {
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "description": "A single command with arguments, e.g. 'python3 -m pytest -v'. No shell operators (&& | ; >)."}
            },
            "required": ["cmd"]
        }
    }
}

# 破坏/混淆模式与白名单统一由 cmd_safety 提供（见文件顶部 import）

# Shell operators that are NOT supported because we run with shell=False.
# If these slip through, shlex.split() passes them as literal arguments and the
# command silently misbehaves (e.g. "cd x && pytest" only runs `cd`, exits 0,
# prints nothing — the agent then hallucinates success). Detect and reject loudly.
_SHELL_OPERATORS = re.compile(r"(&&|\|\||\||;|\$\(|>|<)")


def _reject_shell_operators(cmd: str) -> str | None:
    """Return an error string if cmd uses shell syntax we can't execute, else None."""
    m = _SHELL_OPERATORS.search(cmd)
    if not m:
        return None
    return (
        f"⛔ 不支持 shell 操作符 '{m.group(1)}'：本工具以 shell=False 直接执行，"
        f"无法解释 {m.group(1)}，否则只会运行第一个命令并把其余当参数（静默失败）。\n"
        "请拆成多次调用，每次只运行一条命令，例如：\n"
        "  ❌ cd /tmp/x && python3 -m pytest -v\n"
        "  ✅ 第1次 run_cmd: cd /tmp/x    第2次 run_cmd: python3 -m pytest -v\n"
        "重定向(>)和管道(|)同样不支持；需要输出时请让命令直接打印到 stdout。"
    )


def execute(args: dict) -> str:
    cmd = (args.get("cmd") or args.get("command", "")).strip()

    # ── Reject unsupported shell syntax FIRST (before whitelist shortcut) ──
    rejected = _reject_shell_operators(cmd)
    if rejected:
        return rejected

    # ── Whitelist fast path：整条命令完全匹配白名单形态，命中后仍走
    #    完整检查——此前首 token 命中即整条直行（"env curl ..." 绕过，P0）──
    if SAFE_CMD_RE.match(cmd) and len(cmd) < 200:
        denied = check_cmd(cmd)
        if denied:
            return denied
        try:
            r = subprocess.run(shlex.split(cmd), shell=False, capture_output=True, text=True, timeout=10)
            return r.stdout.strip() or r.stderr.strip() or "(无输出)"
        except subprocess.TimeoutExpired:
            return f"超时: {cmd}"
        except Exception as e:
            return f"错误：{e}"

    # ── 统一安全检查（cmd_safety 单点定义）──
    denied = check_cmd(cmd)
    if denied:
        return denied

    # ── Length limit ──
    if len(cmd) > 1000:
        return f"⛔ Command too long ({len(cmd)} chars, max 1000)"

    # ── Execute ──
    try:
        r = subprocess.run(shlex.split(cmd), shell=False, capture_output=True, text=True, timeout=30)
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
''',
    'open_folder.py': r'''"""Open a folder in Finder (macOS only)."""
import os
import platform
import subprocess

NAME = "open_folder"
PERMISSION = "confirm"

DEFINITION = {
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
}

IS_MACOS = platform.system() == "Darwin"


def _safe_path(path: str) -> str | None:
    """返回规范化后的绝对路径；不合法返回 None。"""
    if not path:
        return None
    expanded = os.path.expanduser(path)
    # 必须本身就是绝对路径（realpath 会把相对路径变成绝对路径，所以先查）
    if not os.path.isabs(expanded):
        return None
    # Block path traversal — 两种分隔符都查
    if ".." in path.split("/") or ".." in path.split("\\"):
        return None
    # realpath 解析符号链接，防止经由 symlink 逃出校验
    return os.path.realpath(expanded)


def execute(args: dict) -> str:
    p = _safe_path(args["path"])
    if p is None:
        return "⛔ Blocked: path traversal not allowed"
    if IS_MACOS:
        subprocess.Popen(["open", p])
        return f"✅ 已在 Finder 中打开：{p}"
    # Fallback: list directory on non-macOS
    try:
        entries = os.listdir(p)
        lines = [f"  {'📁' if os.path.isdir(os.path.join(p, e)) else '📄'} {e}"
                 for e in sorted(entries)]
        return "目录内容:\n" + "\n".join(lines)
    except Exception as e:
        return f"错误：{e}"
''',
    'open_app.py': r'''"""Open an application by name. macOS native, Windows via start."""
import platform
import subprocess

IS_WINDOWS = platform.system() == "Windows"

NAME = "open_app"
PERMISSION = "confirm"

DEFINITION = {
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
}

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


def execute(args: dict) -> str:
    name = args["name"]
    resolved = _APP_ALIASES.get(name, name)
    if IS_WINDOWS:
        try:
            subprocess.Popen(["cmd", "/c", "start", "", resolved])
            return f"✅ 已打开：{resolved}"
        except Exception as e:
            return f"无法打开 {resolved}: {e}"
    try:
        r = subprocess.run(["open", "-a", resolved], capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            err = r.stderr.strip() or "应用不存在或无法打开"
            return f"❌ 无法打开 {resolved}: {err}"
        return f"✅ 已打开应用：{resolved}"
    except subprocess.TimeoutExpired:
        return f"❌ 打开 {resolved} 超时"
    except Exception as e:
        return f"无法打开应用 {resolved}: {e}"
''',
    'search_files.py': r'''"""Search for files matching a glob pattern in a directory."""
import glob as glob_mod
import os

NAME = "search_files"
PERMISSION = "safe"

DEFINITION = {
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
}


def _safe_path(path: str) -> str | None:
    """返回规范化后的绝对路径；不合法返回 None。"""
    if not path:
        return None
    expanded = os.path.expanduser(path)
    # 必须本身就是绝对路径（realpath 会把相对路径变成绝对路径，所以先查）
    if not os.path.isabs(expanded):
        return None
    # Block path traversal — 两种分隔符都查
    if ".." in path.split("/") or ".." in path.split("\\"):
        return None
    # realpath 解析符号链接，防止经由 symlink 逃出校验
    return os.path.realpath(expanded)


def execute(args: dict) -> str:
    directory = _safe_path(args["directory"])
    if directory is None:
        return "⛔ Blocked: path traversal not allowed"
    pattern = args["pattern"]
    # pattern 不允许是绝对路径或包含 '..'（否则可逃出 directory）
    if os.path.isabs(pattern) or ".." in pattern.split("/") or ".." in pattern.split("\\"):
        return f"⛔ Blocked: pattern 不允许是绝对路径或包含 '..' - {pattern}"
    try:
        search_path = os.path.join(directory, pattern)
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
''',
    'tavily_search.py': r'''"""Search the web using Tavily Search API."""
import json
import os
import subprocess
import sys as _sys
from pathlib import Path

import httpx

# 插件目录加入 sys.path：execute() 里懒加载兄弟模块 _time_guard 用
_sys.path.insert(0, str(Path(__file__).parent))

NAME = "tavily_search"
PERMISSION = "safe"

DEFINITION = {
    "type": "function",
    "function": {
        "name": "tavily_search",
        "description": "首选网络搜索工具（Tavily API，已配置 Key）。需要实时信息、最新新闻、当前事实、或超出训练数据的内容时优先使用本工具。返回标题、URL、内容摘要、发布时间和 AI 摘要。同一任务只调用一次搜索即可，不要与 web_search/bing_search 并行调用。⚠️ 问“今天/最新/本周/盘前/收盘”这类**时效性问题**时，务必带 topic=\"news\"（或让工具自动判定），否则可能返回几个月前的旧文章，数字会严重失真。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query. Be specific and use keywords."
                },
                "search_depth": {
                    "type": "string",
                    "enum": ["basic", "advanced"],
                    "description": "Search depth: 'basic' (faster, 1-2s) or 'advanced' (thorough, 5-10s). Default: basic."
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max results to return (1-10). Default: 5."
                },
                "topic": {
                    "type": "string",
                    "enum": ["general", "news"],
                    "description": "检索类型：'news' 只返回**近期新闻**（配合 days 用，时效问题必须选它）；'general' 为通用检索。不填时由工具按问题里的时间词自动判定。"
                },
                "days": {
                    "type": "integer",
                    "description": "topic='news' 时回溯的天数（1-30，默认 7）。问“今天/昨日”用 1-3，问“本周”用 7。"
                },
            },
            "required": ["query"],
        },
    },
}

CONFIG_FILE = Path.home() / ".local-ai-os" / "config.json"

# 时效性问法：命中就自动切到 news 检索（09-20 事故：问“周五大盘”却拿到三个月前的旧新闻）
_RECENT_HINTS = (
    "今天", "今日", "最新", "昨日", "昨天", "昨晚", "本周", "这周", "上周", "盘前", "盘后",
    "收盘", "刚刚", "现在", "目前", "实时", "最新消息",
    "today", "latest", "yesterday", "tonight", "this week", "last week", "close", "closing",
    "now", "current", "breaking",
)


def _needs_recent(query: str) -> bool:
    q = (query or "").lower()
    return any(h in q for h in _RECENT_HINTS)


def _get_api_key() -> str | None:
    """Read Tavily API key from config file or environment variable."""
    env_key = os.environ.get("TAVILY_API_KEY")
    if env_key:
        return env_key
    # Try macOS Keychain via security CLI
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "com.latiao.desktop", "-a", "tavily_api_key", "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    try:
        if CONFIG_FILE.exists():
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return cfg.get("tavily_api_key")
    except Exception:
        pass
    return None


async def execute(args: dict) -> str:
    api_key = _get_api_key()
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
    try:
        n = int(args.get("max_results", 5))
    except (TypeError, ValueError):
        n = 5
    max_results = max(1, min(n, 10))

    # 时效判定：显式 topic 优先，否则按问题里的时间词自动选（09-20 修复）
    topic = str(args.get("topic") or "").strip().lower()
    if topic not in ("general", "news"):
        topic = "news" if _needs_recent(query) else "general"
    days = 0
    if topic == "news":
        try:
            days = int(args.get("days") or 0)
        except (TypeError, ValueError):
            days = 0
        if days <= 0:
            days = 3 if any(h in (query or "").lower()
                            for h in ("今天", "今日", "昨天", "昨日", "刚刚", "today", "latest")) else 7
        days = max(1, min(days, 30))

    payload = {
        "api_key": api_key,
        "query": query,
        "search_depth": search_depth,
        "max_results": max_results,
        "topic": topic,
    }
    if topic == "news":
        payload["days"] = days

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30)) as client:
            resp = await client.post(
                os.environ.get("TAVILY_API_URL", "https://api.tavily.com/search"),
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()

            results = data.get("results", [])
            answer = data.get("answer", "")

            if not results and not answer:
                return f"🔍 Tavily 搜索: {query}\n\n未找到相关结果。"

            _scope = f"（新闻检索·近 {days} 天）" if topic == "news" else ""
            lines = [f"🔍 Tavily 搜索: {query} {_scope}\n".rstrip() + "\n"]

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
                    pub = r.get("published_date") or r.get("published") or ""
                    lines.append(f"{i}. **{title}**")
                    if pub:
                        lines.append(f"   🗓 发布时间: {pub}")
                    lines.append(f"   {url}")
                    lines.append(f"   {content}\n")

            out = "\n".join(lines)
            # 数据时效提醒（09-20）：网页正文里的数字往往不是目标日期的，
            # 弱模型会直接照抄 → 必须明示"先核对发布日期"
            if topic == "news":
                out += (f"\n\n⚠️ 以上是**近 {days} 天**的新闻检索结果。引用其中任何数字前，"
                        "先核对该条的**发布时间**与你需要的日期是否一致；不一致或找不到目标日期时，"
                        "必须写明「未获取到该日数据」，不要用近似数字替代。")
            # 搜索词日期滑差自检：query 带旧日期时提示核对相对时间换算（防锚定）
            from _time_guard import check_query_date
            guard = check_query_date(query)
            if guard:
                out += "\n\n" + guard
            return out

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            return "⚠️ Tavily API Key 无效或已过期。请在技能设置中更新 API Key。"
        return f"⚠️ Tavily 搜索失败: HTTP {e.response.status_code}"
    except httpx.ConnectError:
        return "⚠️ 无法连接 Tavily API (api.tavily.com)。请检查网络连接。"
    except Exception as e:
        return f"⚠️ Tavily 搜索异常: {e}"
''',
}

def _seed_default_plugins():
    """Create default plugin files on first run; refresh stale seeds the user hasn't modified.

    Manifest (PLUGINS_DIR/.seed_manifest.json) records filename → sha256 of the seed
    source at write time. On startup:
      - file missing                    → write seed, record hash
      - file hash == manifest hash      → user didn't touch it → overwrite with new seed
      - file hash != manifest hash      → user modified it → leave it alone
    """
    try:
        PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
        manifest_path = PLUGINS_DIR / ".seed_manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                manifest = {}
        except Exception:
            manifest = {}
        for filename, source in _SEED_PLUGINS.items():
            filepath = PLUGINS_DIR / filename
            new_hash = _sha256(source)
            if not filepath.exists():
                _atomic_write(filepath, source)
                manifest[filename] = new_hash
                continue
            try:
                current_hash = _sha256(filepath.read_text(encoding="utf-8"))
            except Exception:
                continue
            old_hash = manifest.get(filename)
            if old_hash and current_hash == old_hash:
                # 用户没改过旧 seed → 用新 seed 覆盖并更新 manifest
                if current_hash != new_hash:
                    _atomic_write(filepath, source)
                manifest[filename] = new_hash
            # else: 用户改过了（或无 manifest 记录无法判断）→ 跳过不动
        _atomic_write(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2))
    except Exception:
        logger.warning("Failed to seed default plugins", exc_info=True)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _atomic_write(filepath: Path, content: str):
    """先写临时文件再 os.replace，避免崩溃留下半截文件。"""
    tmp_path = filepath.with_name(filepath.name + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, filepath)


def load_plugins(fallback_tools, fallback_dispatch, fallback_permissions):
    """
    Scan plugins/ for .py files exporting NAME, DEFINITION, PERMISSION, execute().
    Returns (tools, dispatch, permissions, hooks).
    Fallback definitions are merged in for any tool name the plugins don't provide.
    """
    _seed_default_plugins()

    plugins = []

    def _load_py(f: Path, tag: str):
        spec = importlib.util.spec_from_file_location(f"plugin_{tag}", f)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if not all(hasattr(mod, attr) for attr in ("NAME", "DEFINITION", "PERMISSION")):
            return None
        if not hasattr(mod, "execute") or not callable(mod.execute):
            return None
        return mod

    if PLUGINS_DIR.exists():
        for f in sorted(PLUGINS_DIR.glob("*.py")):
            if f.name.startswith("_"):
                continue
            try:
                mod = _load_py(f, f.stem)
                if mod is not None:
                    plugins.append(mod)
            except Exception:
                logger.warning("Failed to load plugin", exc_info=True)

    # ── 已安装扩展（~/.local-ai-os/extensions/<name>/<version>/plugin.py）──
    # 与内置插件同构加载；重名时先加载者赢（内置插件优先）。
    try:
        from extension_manager import active_extension_dirs
        for ext_dir in active_extension_dirs():
            f = ext_dir / "plugin.py"
            if not f.exists():
                continue
            try:
                mod = _load_py(f, f"{ext_dir.parent.name}_{ext_dir.name}")
                if mod is not None:
                    plugins.append(mod)
            except Exception:
                logger.warning("Failed to load extension plugin %s", f, exc_info=True)
    except Exception:
        logger.warning("Extension manager unavailable, skipping extension plugins", exc_info=True)

    tools = []
    dispatch = {}
    permissions = {}
    hooks = {}

    for mod in plugins:
        name = mod.NAME
        if name in dispatch:
            # 重名插件：先加载的赢，跳过后者
            logger.warning("Duplicate plugin NAME %r in %s — keeping the first one loaded", name, getattr(mod, "__file__", "?"))
            continue
        tools.append(mod.DEFINITION)
        dispatch[name] = mod.execute
        permissions[name] = mod.PERMISSION
        if hasattr(mod, "HOOKS") and isinstance(mod.HOOKS, dict):
            hooks[name] = mod.HOOKS

    # 合并 fallback：插件没有的工具名用 fallback 补齐（而不是有一个插件就全丢）
    for name, func in (fallback_dispatch or {}).items():
        if name not in dispatch:
            dispatch[name] = func
    for name, perm in (fallback_permissions or {}).items():
        if name not in permissions:
            permissions[name] = perm
    for tool_def in (fallback_tools or []):
        fname = tool_def.get("function", {}).get("name") if isinstance(tool_def, dict) else None
        if fname and fname in dispatch and not any(
            isinstance(t, dict) and t.get("function", {}).get("name") == fname for t in tools
        ):
            tools.append(tool_def)

    return tools, dispatch, permissions, hooks
