"""Tool System Module — plugin loading, seeding, and dispatch."""
import importlib.util
import logging
from pathlib import Path

logger = logging.getLogger("tool_system")

# ═══════════════════════════════════════════════════════
#  Plugin System: auto-scan sidecar/plugins/ for tool .py files
# ═══════════════════════════════════════════════════════

PLUGINS_DIR = Path(__file__).parent / "plugins"

# Embedded plugin source code for first-run seeding
_SEED_PLUGINS = {
    "read_file.py": '''"""Read the contents of a file at the given path."""
import os

NAME = "read_file"
PERMISSION = "safe"

DEFINITION = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read the contents of a file at the given path. Supports offset and limit for large files. File truncated at 50000 chars — use offset to continue reading.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the file."},
                "offset": {"type": "integer", "description": "Line number to start reading from (1-indexed)."},
                "limit": {"type": "integer", "description": "Maximum number of lines to return."}
            },
            "required": ["path"]
        }
    }
}


def execute(args: dict) -> str:
    path = args["path"]
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return f"错误：文件不存在 - {path}"
    except Exception as e:
        return f"错误：{e}"
''',
    "write_file.py": '''"""Write text content to a file. Creates parent directories if needed."""
import os

NAME = "write_file"
PERMISSION = "confirm"

DEFINITION = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "Write text content to a file. Creates parent directories if needed.",
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


def execute(args: dict) -> str:
    path = args["path"]
    content = args["content"]
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"✅ 已写入：{path}（{len(content)} 字符）"
    except Exception as e:
        return f"错误：{e}"
''',
    "list_dir.py": '''"""List the contents of a directory."""
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


def execute(args: dict) -> str:
    path = args["path"]
    try:
        entries = os.listdir(path)
        lines = [f"  {'📁' if os.path.isdir(os.path.join(path, e)) else '📄'} {e}"
                 for e in sorted(entries)]
        return "目录内容:\\n" + "\\n".join(lines)
    except Exception as e:
        return f"错误：{e}"
''',
    "run_cmd.py": '''"""Run a shell command and return its output."""
import re
import shlex
import subprocess

NAME = "run_cmd"
PERMISSION = "confirm"

DEFINITION = {
    "type": "function",
    "function": {
        "name": "run_cmd",
        "description": "Run a shell command and return its output.",
        "parameters": {
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "description": "The shell command to execute."}
            },
            "required": ["cmd"]
        }
    }
}

_DANGEROUS = [
    r"rm\\s+(-[a-z]*[rf]|--recursive|--force)",
    r">\\s*/dev/(sd|nvme|hd|disk|dm-)",
    r"\\bsudo\\b", r"\\bshutdown\\b", r"\\breboot\\b",
    r"\\bcurl\\b.*\\|\\s*(ba)?sh\\b", r"\\bwget\\b.*\\|\\s*(ba)?sh\\b",
]

def execute(args: dict) -> str:
    cmd = args["cmd"].strip()
    cmd_lower = cmd.lower()
    for pattern in _DANGEROUS:
        if re.search(pattern, cmd_lower):
            return f"⛔ Blocked unsafe command: {cmd}"
    if len(cmd) > 1000:
        return f"⛔ Command too long"
    try:
        try:
            tokens = shlex.split(cmd)
        except ValueError as e:
            return f"命令格式错误: {e}"
        r = subprocess.run(tokens, shell=False, capture_output=True, text=True, timeout=30)
        out = r.stdout.strip()
        if r.returncode != 0:
            out += f"\\n(退出码: {r.returncode})"
            if r.stderr.strip():
                out += f"\\n{r.stderr.strip()}"
        return out or "(无输出)"
    except subprocess.TimeoutExpired:
        return f"超时: {cmd}"
    except Exception as e:
        return f"错误：{e}"
''',
    "open_folder.py": '''"""Open a folder in Finder (macOS only)."""
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
IS_WINDOWS = platform.system() == "Windows"


def execute(args: dict) -> str:
    path = args["path"]
    if IS_MACOS:
        subprocess.Popen(["open", path])
    elif IS_WINDOWS:
        os.startfile(path)
    else:
        subprocess.Popen(["xdg-open", path])
    return f"✅ 已打开：{path}"
''',
    "open_app.py": '''"""Open a macOS application by name. Supports both English and Chinese names."""
import subprocess

NAME = "open_app"
PERMISSION = "confirm"

DEFINITION = {
    "type": "function",
    "function": {
        "name": "open_app",
        "description": "Open a macOS application by name.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "App name in English or Chinese."}
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
    try:
        subprocess.Popen(["open", "-a", resolved])
        return f"✅ 已打开应用：{resolved}"
    except Exception as e:
        return f"无法打开应用 {resolved}: {e}"
''',
    "search_files.py": '''"""Search for files matching a glob pattern in a directory."""
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


def execute(args: dict) -> str:
    directory = args["directory"]
    pattern = args["pattern"]
    try:
        search_path = os.path.join(os.path.expanduser(directory), pattern)
        matches = glob_mod.glob(search_path, recursive=True)
        if not matches:
            return f"No files matching '{pattern}' found in {directory}"
        lines = []
        for m in sorted(matches)[:50]:
            icon = "📁" if os.path.isdir(m) else "📄"
            lines.append(f"  {icon} {m}")
        result = f"Search results for '{pattern}' in {directory}:\\n" + "\\n".join(lines)
        if len(matches) > 50:
            result += f"\\n  ... and {len(matches) - 50} more results"
        return result
    except Exception as e:
        return f"Error searching files: {e}"
''',
    "tavily_search.py": '''"""Search the web using Tavily Search API."""
import json
import os
from pathlib import Path

import httpx

NAME = "tavily_search"
PERMISSION = "safe"

DEFINITION = {
    "type": "function",
    "function": {
        "name": "tavily_search",
        "description": "Search the web for real-time information using Tavily. Use when you need current events, news, or facts beyond your training data. Returns relevant results with titles, URLs, and content summaries.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query. Be specific and use keywords."},
                "search_depth": {"type": "string", "enum": ["basic", "advanced"], "description": "Search depth: 'basic' (faster, 1-2s) or 'advanced' (thorough, 5-10s). Default: basic."},
                "max_results": {"type": "integer", "description": "Max results to return (1-10). Default: 5."},
            },
            "required": ["query"],
        },
    },
}

CONFIG_FILE = Path.home() / ".local-ai-os" / "config.json"


def _get_api_key() -> str | None:
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
        return "⚠️ Tavily API Key 未配置。请在应用的「技能」界面中找到 Web Search (Tavily)，填写 API Key。免费注册：https://tavily.com"

    query = args["query"]
    search_depth = args.get("search_depth", "basic")
    max_results = min(args.get("max_results", 5), 10)

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30)) as client:
            resp = await client.post(
                TAVILY_API_URL,
                json={"api_key": api_key, "query": query, "search_depth": search_depth, "max_results": max_results},
            )
            resp.raise_for_status()
            data = resp.json()
        results = data.get("results", [])
        answer = data.get("answer", "")
        if not results and not answer:
            return f"🔍 Tavily 搜索: {query}\\n\\n未找到相关结果。"
        lines = [f"🔍 Tavily 搜索: {query}\\n"]
        if answer:
            lines.append(f"📝 {answer}\\n")
        if results:
            lines.append(f"📎 共 {len(results)} 条结果:\\n")
            for i, r in enumerate(results, 1):
                title = r.get("title", "No title")
                url = r.get("url", "")
                content = r.get("content", "")
                if len(content) > 300:
                    content = content[:300] + "..."
                lines.append(f"{i}. **{title}**")
                lines.append(f"   {url}")
                lines.append(f"   {content}\\n")
        return "\\n".join(lines)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            return "⚠️ Tavily API Key 无效或已过期。请在技能设置中更新 API Key。"
        return f"⚠️ Tavily 搜索失败: HTTP {e.response.status_code}"
    except Exception as e:
        return f"⚠️ Tavily 搜索异常: {e}"
''',
}

def _seed_default_plugins():
    """Create default plugin files on first run if plugins dir is empty."""
    try:
        PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
        for filename, source in _SEED_PLUGINS.items():
            filepath = PLUGINS_DIR / filename
            if not filepath.exists():
                filepath.write_text(source, encoding="utf-8")
    except Exception:
        logger.warning("Failed to seed default plugins", exc_info=True)


def load_plugins(fallback_tools, fallback_dispatch, fallback_permissions):
    """
    Scan plugins/ for .py files exporting NAME, DEFINITION, PERMISSION, execute().
    Returns (tools, dispatch, permissions, hooks).
    Falls back to provided hardcoded definitions if no plugins are found.
    """
    _seed_default_plugins()

    plugins = []
    if PLUGINS_DIR.exists():
        for f in sorted(PLUGINS_DIR.glob("*.py")):
            if f.name.startswith("_"):
                continue
            try:
                spec = importlib.util.spec_from_file_location(f"plugin_{f.stem}", f)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                if not all(hasattr(mod, attr) for attr in ("NAME", "DEFINITION", "PERMISSION")):
                    continue
                if not hasattr(mod, "execute") or not callable(mod.execute):
                    continue
                plugins.append(mod)
            except Exception:
                logger.warning("Failed to load plugin", exc_info=True)

    if not plugins:
        return (
            list(fallback_tools),
            dict(fallback_dispatch),
            dict(fallback_permissions),
            {},
        )

    tools = []
    dispatch = {}
    permissions = {}
    hooks = {}

    for mod in plugins:
        name = mod.NAME
        tools.append(mod.DEFINITION)
        dispatch[name] = mod.execute
        permissions[name] = mod.PERMISSION
        if hasattr(mod, "HOOKS") and isinstance(mod.HOOKS, dict):
            hooks[name] = mod.HOOKS

    return tools, dispatch, permissions, hooks
