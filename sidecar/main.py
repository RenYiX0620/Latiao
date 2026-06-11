"""
Local AI OS - Python Sidecar
Stateless: frontend manages all state, sends complete messages array per request.
"""
from __future__ import annotations

import asyncio
import base64
from collections import deque
import contextvars
import fnmatch
import io
import json
import logging
import os
import platform
import re
import shlex
import sqlite3
import subprocess
import sys
import threading
import uuid

# Fix SSL certificate verification for Python 3.14 on macOS
# (httpx/huggingface_hub don't find system certs by default)
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
except ImportError:
    pass
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from tool_system import load_plugins
from config import LM_STUDIO_URL, SUBAGENT_MODEL, SKILLS_DIR, PROGRESS_DIR
from db import _get_db, _create_table, _init_db, MEMORY_DB, _db_write_lock
from memory import _tokenize_zh, _quick_reflect, _build_learning_context, _extract_learnings_heuristic, _maybe_generate_skill, _refine_learnings, _get_recent_learnings, _summarize_learning, _retrieve_preferences, _record_reflection, _get_high_confidence_preferences
import httpx
from fastapi import FastAPI, File, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

import local_llm

logger = logging.getLogger("latiao-sidecar")

# Load .env file manually
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    for _line in env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ[_k.strip()] = _v.strip().strip("'\"")

# ── MX_APIKEY: set via env var MX_APIKEY ──
if not os.environ.get("MX_APIKEY"):
    logger.info("MX_APIKEY not set — 妙想金融技能将不可用")

# In-memory ring buffer for recent log entries (accessible via /v1/logs)
_log_buffer: deque = deque(maxlen=500)


class _DequeHandler(logging.Handler):
    """Captures log records into the in-memory ring buffer."""
    def emit(self, record: logging.LogRecord) -> None:
        _log_buffer.append({
            "time": datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S"),
            "level": record.levelname,
            "message": self.format(record),
        })


_deque_handler = _DequeHandler()
_deque_handler.setFormatter(logging.Formatter("%(message)s"))
_deque_handler.setLevel(logging.INFO)
logger.addHandler(_deque_handler)
logger.setLevel(logging.INFO)

# Log key lifecycle events
logger.info("Sidecar 启动")

# huggingface — 国内网络可用 hf-mirror.com 镜像
# os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")


from identity import _load_agent_identity, IDENTITY_FILES, _read_identity, _create_default_identity, _detect_identity_intent, _apply_name_change, _apply_style_change, _apply_rule_change, _apply_pref_change, _process_identity_intents
# ═══════════════════════════════════════════════════════
#  Smart Skill System: Auto-match & load skills on demand
# ═══════════════════════════════════════════════════════

SKILL_INDEX: dict[str, dict] = {}
PROJECT_ROOT = Path(__file__).parent  # sidecar/
SKILLS_DIR = PROJECT_ROOT / "skills"

# TF-IDF cache for learnings (avoid rebuilding every search)

def _load_skill_index():
    """Scan all skills in ./skills/ directory and build index of their metadata."""
    global SKILL_INDEX
    SKILL_INDEX.clear()
    # Pre-import yaml at the top so we don't import inside the loop
    try:
        import yaml  # noqa: F811
    except ImportError:
        logger.error("PyYAML not installed — skills system disabled. Install: pip install pyyaml")
        return
    if not SKILLS_DIR.exists():
        logger.info("No skills directory found, skill system disabled")
        return
    for skill_dir in SKILLS_DIR.iterdir():
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        try:
            content = skill_md.read_text(encoding="utf-8")
            # Parse YAML frontmatter
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    frontmatter = yaml.load(parts[1], Loader=yaml.SafeLoader)
                    name = frontmatter.get("name", skill_dir.name)
                    description = frontmatter.get("description", "")
                    skill_content = parts[2].strip()
                    SKILL_INDEX[name] = {
                        "name": name,
                        "description": description,
                        "content": skill_content,
                        "path": skill_dir,
                    }
                    logger.info(f"Indexed skill: {name}")
        except Exception as e:
            logger.warning(f"Failed to load skill {skill_dir.name}: {e}")
    logger.info(f"Loaded {len(SKILL_INDEX)} skills into index")

def _match_skill_keywords(user_query: str) -> str | None:
    """Match user query against skill keywords using overlap scoring."""
    if not user_query or not SKILL_INDEX:
        return None
    query_lower = user_query.lower()
    q_words = set(query_lower.split())
    best_match = None
    best_score = 0
    for name, skill in SKILL_INDEX.items():
        if not _is_skill_enabled(name):
            continue
        kw_text = (name + " " + skill.get("description", "")).lower()
        kw_words = set(kw_text.split())
        overlap = len(q_words & kw_words)
        if name.lower() in query_lower:
            overlap += 5
        if overlap > best_score:
            best_score = overlap
            best_match = skill.get("content", "")
    return best_match if best_score >= 1 else None

async def _match_skill(user_query: str) -> str | None:
    """Intelligently match user query to the most appropriate skill."""
    if not SKILL_INDEX:
        return None
    # For local models, use keyword matching to avoid sending user text to external API
    cfg = _last_cloud_config.get()
    if not cfg or not cfg.get("key"):
        return _match_skill_keywords(user_query)
    # Build skill list for LLM to choose from
    skill_list = []
    for name, skill in SKILL_INDEX.items():
        if not _is_skill_enabled(name):
            continue
        skill_list.append(f"- {name}: {skill['description']}")
    skill_list_str = "\n".join(skill_list)
    # Lightweight prompt to match skill, no tool calls needed
    prompt = f"""用户的问题是：{user_query}
下面是所有可用的技能列表：
{skill_list_str}
请判断用户的问题是否需要用到某个技能，如果需要，返回技能的名字，如果不需要，返回NONE。
只返回一个结果，不需要解释。"""
    try:
        protocol, api_url, skill_headers, _is_local = _resolve_api_target(_last_cloud_config.get())
        if not api_url:
            return None
        async with httpx.AsyncClient(timeout=httpx.Timeout(10)) as client:
            r = await client.post(api_url, json={
                "model": SUBAGENT_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 10,
                "temperature": 0,
                "stream": False,
            }, headers=skill_headers)
            if r.status_code != 200:
                return None
            data = r.json()
            result = data.get("choices", [{}])[0].get("message", {}).get("content", "NONE").strip()
            if result in SKILL_INDEX:
                logger.info(f"Matched skill: {result} for query: {user_query[:50]}...")
                return result
            return None
    except Exception as e:
        logger.warning(f"Skill matching failed: {e}")
        return None

@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan: startup + shutdown hooks."""
    _load_permissions()
    _create_default_identity()
    _init_db()
    _seed_default_cron()
    _load_skill_index()  # Load skill index at startup
    # Write PID file so the Rust process manager can find us (after _init_db creates dir)
    SIDECAR_PID = PROGRESS_DIR / "sidecar.pid"
    SIDECAR_PID.write_text(str(os.getpid()))
    cron_task = asyncio.create_task(_cron_loop())
    logger.info("Sidecar 启动 — cron loop started")
    yield
    SIDECAR_PID.unlink(missing_ok=True)
    cron_task.cancel()
    logger.info("Sidecar 关闭")

app = FastAPI(title="Local AI OS Sidecar", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:1420",
        "http://tauri.localhost",
        "https://tauri.localhost",
        "tauri://localhost",
        "http://127.0.0.1:1420",
        "http://127.0.0.1:8000",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type", "Authorization"],
)

LM_STUDIO_URL = os.environ.get("LATIAO_LM_STUDIO_URL", "http://localhost:1234/v1/chat/completions")
SUBAGENT_MODEL = os.environ.get("LATIAO_SUBAGENT_MODEL", "gpt-4o-mini")
TAVILY_API_URL = os.environ.get("TAVILY_API_URL", "https://api.tavily.com/search")
# Per-request cloud config — contextvars isolates concurrent requests
_last_cloud_config: contextvars.ContextVar = contextvars.ContextVar("cloud_config", default=None)
MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50 MB
IS_MACOS = platform.system() == "Darwin"

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
}


def _get_agent_config(agent_id: str) -> dict:
    """Get agent profile, falling back to latiao (orchestrator)."""
    return AGENT_PROFILES.get(agent_id, AGENT_PROFILES["latiao"])


def _get_agent_tools(agent_id: str, all_tools: list[dict]) -> list[dict]:
    """Filter tools based on agent's allowed tools. 'all' means all tools."""
    cfg = _get_agent_config(agent_id)
    allowed = cfg.get("tools", "all")
    if allowed == "all":
        return all_tools
    return [t for t in all_tools if t.get("function", {}).get("name") in allowed]


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

# Plugin system globals — populated by _load_plugins()
TOOL_PERMISSIONS: dict[str, str] = {}
TOOLS: list[dict] = []
TOOL_DISPATCH: dict[str, callable] = {}
TOOL_HOOKS: dict[str, dict] = {}

# Pending confirmations: call_id → asyncio.Event (approve) or None (deny)
_pending_confirmations: dict[str, dict] = {}
_pending_lock = asyncio.Lock()

PROGRESS_DIR = Path.home() / ".local-ai-os"
PROGRESS_FILE = PROGRESS_DIR / "PROGRESS.md"
PERMISSIONS_CONFIG = PROGRESS_DIR / "permissions.json"
CRON_FILE = PROGRESS_DIR / "cron.json"
AGENTS_FILE = PROGRESS_DIR / "agents.json"
CONFIG_FILE = PROGRESS_DIR / "config.json"
_merge_agents()

# Identity files that collectively define the agent

# Custom permission rules loaded from ~/.local-ai-os/permissions.json
_custom_permissions: list[dict] = []


def _load_permissions():
    """Load custom permission rules from disk."""
    global _custom_permissions
    try:
        if PERMISSIONS_CONFIG.exists():
            data = json.loads(PERMISSIONS_CONFIG.read_text(encoding="utf-8"))
            _custom_permissions = data.get("rules", [])
        else:
            _custom_permissions = []
    except Exception:
        logger.warning("Failed to load custom permissions", exc_info=True)
        _custom_permissions = []


def _save_permissions(rules: list[dict]):
    """Save custom permission rules to disk."""
    PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
    PERMISSIONS_CONFIG.write_text(
        json.dumps({"rules": rules}, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )


def _resolve_permission(tool_name: str, args: dict) -> str:
    """
    Resolve permission level for a tool call.
    Checks custom rules first (with optional path_pattern matching),
    then falls back to TOOL_PERMISSIONS default.
    Rules format: {"tool": "write_file", "path_pattern": "/tmp/*", "permission": "safe"}
    """
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
    r"chattr\s+[+-]=*i", r"mv\s+/.*\s+/dev/null",
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
            pass

    if not api_key:
        try:
            if config_file.exists():
                cfg = json.loads(config_file.read_text(encoding="utf-8"))
                api_key = cfg.get("tavily_api_key")
        except Exception:
            pass

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


async def _delegate_task(agent_type: str, task: str) -> str:
    """Spawn a specialist sub-agent to handle a delegated task.
    Uses async httpx to avoid blocking the main event loop."""
    if not task.strip():
        return "错误：任务描述不能为空"

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
                    "messages": current_msgs,
                    "tools": sub_tools,
                    "tool_choice": "auto",
                    "max_tokens": 2048,
                    "stream": False,
                }
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
            "description": "Run a shell command and return its output. ⚠️ Requires user confirmation. Dangerous commands (rm -rf, sudo, etc.) are always blocked.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cmd": {"type": "string", "description": "The shell command to execute."}
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
            "description": "搜索互联网获取实时信息。当你需要最新新闻、事实、或超出你训练数据的信息时使用。返回标题、URL和内容摘要。不要用 run_cmd 或手写代码来做网络搜索——直接用这个工具。",
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


# ═══════════════════════════════════════════════════════
#  Self-Verification: programmatic post-tool quality checks
# ═══════════════════════════════════════════════════════

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
                            errs = [l for l in output.split("\n") if l.strip()][:5]
                            checks.append(("FAIL", "ESLint", f"发现 {len(errs)} 个问题"))
                            for el in errs[:3]:
                                checks.append(("  ", "  ↳", el[:120]))
                    except FileNotFoundError:
                        pass
                    except asyncio.TimeoutError:
                        checks.append(("FAIL", "ESLint", "超时"))
                    except Exception:
                        pass
                    break

        # ── Python syntax check ──
        if path.endswith(".py"):
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "py_compile", str(Path(path)),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
                if proc.returncode == 0:
                    checks.append(("OK", "Python 语法", "编译通过"))
                else:
                    err_text = (stderr or b"").decode("utf-8", errors="replace")[:300]
                    checks.append(("FAIL", "Python 语法", err_text[:150]))
            except FileNotFoundError:
                pass
            except asyncio.TimeoutError:
                checks.append(("FAIL", "Python 语法", "超时"))
            except Exception:
                pass

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

# Append delegate_task to TOOLS (not a plugin — built-in sub-agent system)
_delegate_tool_def = {
    "type": "function",
    "function": {
        "name": "delegate_task",
        "description": "Delegate a sub-task to a specialist sub-agent. Sub-agents run independently with limited tools and return results. Use to parallelize work — call multiple times for independent sub-tasks. Available agents: code-reviewer (read-only code review), doc-generator (documentation), debugger (bug analysis), translator (translation).",
        "parameters": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "enum": ["code-reviewer", "doc-generator", "debugger", "translator"], "description": "The specialist agent type."},
                "task": {"type": "string", "description": "The specific task for the sub-agent. Be clear and concise."},
            },
            "required": ["agent", "task"],
        },
    },
}
TOOLS.append(_delegate_tool_def)
TOOL_DISPATCH["delegate_task"] = lambda args: _delegate_task(args.get("agent", "code-reviewer"), args.get("task", ""))
TOOL_PERMISSIONS["delegate_task"] = "safe"


async def execute_tool(tool_name: str, arguments: dict) -> str:
    """Execute a tool with feedback verification. Supports both sync and async tool functions."""
    fn = TOOL_DISPATCH.get(tool_name)
    if not fn:
        return f"Error: Unknown tool '{tool_name}'"
    try:
        result = fn(arguments)
        if asyncio.iscoroutine(result):
            result = await result
    except KeyError as e:
        return f"Error: Missing required argument {e} for tool '{tool_name}'"
    except Exception as e:
        return f"Error executing {tool_name}: {e}"

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
    except Exception:
        logger.warning("Failed to record progress", exc_info=True)



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

def _extract_last_user_text(messages: list) -> str:
    """Extract text content from the last user message in the messages array."""
    for m in reversed(messages):
        if m.get("role") == "user":
            c = m.get("content", "")
            if isinstance(c, list):
                for part in c:
                    if part.get("type") == "text":
                        return part.get("text", "")
                return ""
            return c
    return ""


# ═══════════════════════════════════════════════════════
#  Skill Card System: Markdown constraints injected at startup
# ═══════════════════════════════════════════════════════

SKILLS_DIR = Path(__file__).parent / "skills"
SKILLS_CONFIG = PROGRESS_DIR / "skills.json"
_loaded_skills: list[dict] = []


def _load_skills_config() -> dict:
    """Load skills enable/disable config."""
    try:
        if SKILLS_CONFIG.exists():
            return json.loads(SKILLS_CONFIG.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("Failed to load skills config", exc_info=True)
    return {}


def _save_skills_config(cfg: dict):
    """Save skills config."""
    PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
    SKILLS_CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")




def _is_skill_enabled(skill_name: str) -> bool:
    """Check if a skill is enabled in the loaded skills list."""
    for s in _loaded_skills:
        if s.get("key") == skill_name or s.get("name") == skill_name:
            return s.get("enabled", True)
    return True  # If not listed, assume enabled


def _load_skills() -> list[dict]:
    """Load all .md skill cards. Returns list of {name, file, content, enabled}."""
    cfg = _load_skills_config()
    skills = []
    if SKILLS_DIR.exists():
        for f in sorted(SKILLS_DIR.glob("*.md")):
            try:
                content = f.read_text(encoding="utf-8").strip()
                name = f.stem.replace("-", " ").title()
                # New skills default to enabled
                enabled = cfg.get(f.stem, {}).get("enabled", True)
                skills.append({"name": name, "file": f.name, "key": f.stem, "content": content, "enabled": enabled})
            except Exception:
                logger.warning(f"Failed to load skill: {f.name if 'f' in dir() else 'unknown'}", exc_info=True)
    return skills


def _build_skill_prompt() -> str:
    """Build a lightweight skill directory for system prompt.
    Only injects name + description (progressive disclosure).
    Agent loads full SKILL.md via read_file when it needs a specific skill."""
    global _loaded_skills
    _loaded_skills = _load_skills()
    enabled = [s for s in _loaded_skills if s.get("enabled", True)]
    if not enabled:
        return ""
    lines = ["## 可用技能（按需加载）"]
    lines.append("以下技能可用。需要使用特定技能时，用 read_file 读取对应的 SKILL.md。\n")
    for s in enabled:
        # Extract first meaningful line as description
        desc = s.get("description", "")
        if not desc:
            # Fallback: first non-empty line of content
            for line in s.get("content", "").split("\n"):
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and not stripped.startswith("---"):
                    desc = stripped[:100]
                    break
            if not desc:
                desc = s["name"]
        lines.append(f"- **{s['name']}** (`{s['key']}`): {desc[:120]}")
    lines.append(f"\n技能文件路径: {SKILLS_DIR}/*.md")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════
#  Dynamic Tool Filtering: intent-based tool selection
# ═══════════════════════════════════════════════════════

TOOL_CATEGORIES = {
    "file_read": ["read_file", "list_dir", "search_files"],
    "file_write": ["write_file"],
    "command": ["run_cmd"],
    "app": ["open_app", "open_folder"],
    "web": ["tavily_search", "web_search"],
    "financial": ["mx_query"],
}

INTENT_PATTERNS = [
    (re.compile(r"读|看|查看|检查|搜索|找|列出|显示|看看|分析|审查|review|check|read|find|list|show|cat|head|tail|grep|ls|dir", re.IGNORECASE),
     ["file_read"]),
    (re.compile(r"写|创建|修改|改|删|新建|保存|生成|write|create|modify|update|delete|save|generate|make", re.IGNORECASE),
     ["file_read", "file_write"]),
    (re.compile(r"运行|执行|跑|命令|安装|测试|构建|编译|build|run|test|exec|install|npm|pip|git|docker|tsc|vite|cargo", re.IGNORECASE),
     ["file_read", "command"]),
    (re.compile(r"打开|启动|open|launch|start|应用|app|程序|finder", re.IGNORECASE),
     ["file_read", "app"]),
    (re.compile(r"大盘|A股|港股|股票|个股|股价|行情|涨停|跌停|板块|上证|深证|创业板|科创板|沪深|指数|基金|财报|财务|营收|净利润|上市公司|分红|PE|PB|ROE|股息|龙头|K线|成交量|换手率|资金流向|北向资金|龙虎榜|券商研报", re.IGNORECASE),
     ["file_read", "financial"]),
    (re.compile(r"上网|联网|搜索网络|搜一下|最新的|最新消息|新闻|热搜|汇率|天气|search|web|online|latest|news|weather|trending", re.IGNORECASE),
     ["file_read", "web"]),
]


def _filter_tools(user_text: str, all_tools: list[dict]) -> list[dict]:
    """Return a filtered tool list based on user intent. Falls back to all tools if uncertain."""
    if not user_text or len(user_text) < 3:
        return all_tools
    allowed_categories: set[str] = set()
    for pattern, cats in INTENT_PATTERNS:
        if pattern.search(user_text):
            allowed_categories.update(cats)
    if not allowed_categories:
        return all_tools  # No match = keep all tools
    allowed_tools: set[str] = set()
    for cat in allowed_categories:
        allowed_tools.update(TOOL_CATEGORIES.get(cat, []))
    # Always include read_file as fallback
    allowed_tools.add("read_file")
    allowed_tools.add("tavily_search")
    allowed_tools.add("mx_query")
    filtered = [t for t in all_tools if t.get("function", {}).get("name") in allowed_tools]
    return filtered if filtered else all_tools


def _inject_image(messages: list, image_base64: str, image_mime: str) -> list:
    """Modify the last user message to include an image attachment."""
    msgs = [dict(m) for m in messages]
    for m in reversed(msgs):
        if m.get("role") == "user":
            text = m["content"] if isinstance(m["content"], str) else _extract_last_user_text(msgs)
            m["content"] = [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": f"data:{image_mime};base64,{image_base64}", "detail": "auto"}},
            ]
            break
    return msgs


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

# Session state tracking: session_id → {phase, round, stalled_rounds, last_action}
_session_states: dict[str, dict] = {}


def _track_progress(session_id: str, phase: str, action: str):
    """Record agent progress for stagnation detection."""
    if session_id not in _session_states:
        _session_states[session_id] = {"phase": "init", "round": 0, "stalled_rounds": 0, "last_action": "", "history": []}
        # Cap to last 20 sessions to prevent memory leak
        if len(_session_states) > 20:
            oldest = sorted(_session_states.keys())[:len(_session_states) - 20]
            for k in oldest:
                del _session_states[k]
    s = _session_states[session_id]
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



async def _await_tool_confirmation(call_id: str, tool_name: str, args: dict) -> tuple[bool, list[dict]]:
    """Wait for user to approve/deny a confirm-level tool. Returns (approved, events)."""
    events = [{"event": "tool_confirm", "call_id": call_id, "tool": tool_name, "args": args}]
    event = asyncio.Event()
    async with _pending_lock:
        _pending_confirmations[call_id] = {"event": event, "approved": False}
    try:
        await asyncio.wait_for(event.wait(), timeout=120)
        async with _pending_lock:
            approved = _pending_confirmations.get(call_id, {}).get("approved", False)
    except asyncio.TimeoutError:
        approved = False
    finally:
        async with _pending_lock:
            _pending_confirmations.pop(call_id, None)
    return approved, events


def _check_skill_permission(skill_name: str, args: dict) -> tuple[bool, list[dict], str]:
    """Check skill permission level. Returns (need_confirm, events, confirm_prompt)."""
    if skill_name not in SKILL_INDEX:
        return False, [], ""
    level = SKILL_INDEX[skill_name].get("security_level", "safe")
    if level == "safe":
        return False, [], ""
    elif level == "confirm":
        call_id = str(uuid.uuid4())
        prompt = f"⚠️ 你正在使用 {skill_name} 技能，该操作会修改你的本地文件或访问外部服务，是否确认执行？"
        events = [{"event": "tool_confirm", "call_id": call_id, "tool": skill_name, "args": args, "prompt": prompt}]
        return True, events, prompt
    elif level == "danger":
        return True, [], f"⛔ {skill_name} 是高危技能，已被系统禁止调用，请联系管理员。"
    return False, [], ""

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
        pass  # hook errors don't block execution
    return False, [], ""


async def _handle_tool_execution(tc: dict, current_msgs: list, session_id: str,
                                  agent_id: str) -> tuple[bool, list[dict]]:
    """Execute a single tool call within the agent loop. Returns (verify_failed, events)."""
    call_id = tc["id"]
    func = tc.get("function", {})
    tool_name = func.get("name", "unknown")
    try:
        args = json.loads(func.get("arguments", "{}"))
    except json.JSONDecodeError:
        args = {}

    # ── User confirmation ──
    if _resolve_permission(tool_name, args) == "confirm":
        approved, events = await _await_tool_confirmation(call_id, tool_name, args)
        if not approved:
            result = f"⛔ User denied this operation: {tool_name}"
            events.append({"event": "tool_end", "call_id": call_id, "tool": tool_name, "result": result})
            current_msgs.append({"role": "tool", "tool_call_id": call_id, "content": result})
            return True, events
    else:
        events = []

    # ── Pre-tool hooks ──
    vetoed, hook_events, veto_msg = _check_pre_hooks(tool_name, args)
    events.extend(hook_events)
    if vetoed:
        events.append({"event": "tool_end", "call_id": call_id, "tool": tool_name, "result": veto_msg})
        current_msgs.append({"role": "tool", "tool_call_id": call_id, "content": veto_msg})
        return True, events

    # ── Execute + Post-hooks ──
    events.append({"event": "tool_start", "call_id": call_id, "tool": tool_name, "args": args})
    logger.info("Tool executing: %s %s", tool_name, json.dumps(args, ensure_ascii=False)[:120])
    result = await execute_tool(tool_name, args)
    logger.info("Tool result: %s → %s", tool_name, result[:80].replace("\n", " "))

    post_hook = TOOL_HOOKS.get(tool_name, {}).get("post_tool_call")
    if post_hook:
        try:
            result = post_hook(tool_name, args, result)
        except Exception:
            logger.warning("Post-tool hook failed", exc_info=True)

    events.append({"event": "tool_end", "call_id": call_id, "tool": tool_name, "result": result})

    # ── State tracking + Verification + Reflection ──
    _record_progress(f"**{tool_name}**\nArgs: `{json.dumps(args)}`\nResult: {result[:200]}")
    _record_tool_call_db(session_id, tool_name, args, result)

    # Self-evolution: background-refine learning + auto-skill generation
    asyncio.create_task(_refine_learnings(tool_name, args, result, session_id))
    asyncio.create_task(_maybe_generate_skill(tool_name, args, result))

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

    current_msgs.append({"role": "tool", "tool_call_id": call_id, "content": tool_content})
    return verify_failed, events


# ── Native tool call format parser (for models like Gemma that use
#    <|tool_call|>call:name{args}<tool_call|> instead of OpenAI JSON) ──

_NATIVE_TOOL_RE = re.compile(
    # Gemma native format: <|tool_call|>call:name{args}<tool_call|>
    # Tokenizer may strip pipe chars, so be flexible about them
    r"<\s*\|?\s*tool_call\s*\|?\s*>call:(\w+)\{(.*?)\}<\s*\|?\s*tool_call\s*\|?\s*>",
    re.DOTALL | re.IGNORECASE,
)

# Filter native control token wrappers from displayed content.
# The tool execution itself is shown via tool_start/tool_end events,
# so we just need to suppress raw <|tool_call|> / <|channel> / <channel|> markers.
_NATIVE_CONTROL_RE = re.compile(
    r"<\s*\|?\s*(?:tool_call|channel)\s*\|?\s*>",
    re.IGNORECASE,
)

def _parse_native_tool_calls(text: str) -> list[dict]:
    """Parse Gemma-style native tool calls from streamed text.
    Returns OpenAI-format tool_calls list.

    Handles formats like:
      <|tool_call|>call:list_dir{path:<|\"|>.<|\"|>}<tool_call|>
    """
    tool_calls = []
    for idx, m in enumerate(_NATIVE_TOOL_RE.finditer(text)):
        name = m.group(1)
        args_str = m.group(2).strip()
        # Gemma escapes quotes as <|"|> — restore them
        args_str = args_str.replace("<|\"|>", '"')
        # Convert Gemma's {key:value} or {key:"value"} to JSON {"key": "value"}
        args_str = _gemma_args_to_json(args_str)
        try:
            args = json.loads(args_str)
        except json.JSONDecodeError:
            args = _salvage_tool_args(args_str)
        tool_calls.append({
            "id": f"native_{name}_{idx}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
        })
    return tool_calls


def _gemma_args_to_json(raw: str) -> str:
    """Convert Gemma's {key:value} or {key:\"value\"} format to valid JSON.
    Handles flat key-value pairs only (Gemma tool args are never deeply nested)."""
    raw = raw.strip()
    if not raw.startswith("{"):
        raw = "{" + raw
    if not raw.endswith("}"):
        raw = raw + "}"
    # Quote unquoted keys: key: → "key":
    raw = re.sub(r'(?<!")(\b\w+\b)\s*:', r'"\1":', raw)
    # Quote unquoted string values: : value → : "value"
    # Only quote bare words, not already-quoted strings, not numbers, not objects/arrays
    raw = re.sub(
        r':\s*(?!["{\[\-\d])([a-zA-Z_./~][a-zA-Z0-9_./~*@+\-]*)',
        r': "\1"',
        raw,
    )
    return raw


def _salvage_tool_args(args_str: str) -> dict:
    """Last-resort parse of broken tool call arguments."""
    result: dict[str, object] = {}
    for part in args_str.split(","):
        part = part.strip().strip('"').strip("'")
        if ":" in part:
            key, _, val = part.partition(":")
            key = key.strip().strip('"').strip("'")
            val = val.strip().strip('"').strip("'")
            if key:
                result[key] = val
    return result or {"raw": args_str}


def _strip_native_tool_calls(text: str) -> str:
    """Remove native tool call blocks from text, keeping only the real content."""
    return _NATIVE_TOOL_RE.sub("", text).strip()


async def _agent_loop_stream(messages: list, model: str, api_url: str, headers: dict, session_id: str = "", agent_id: str = "latiao"):
    """Agent loop: call LLM with tools. If tool_calls → execute → loop. If text → yield & done."""
    current_msgs = [dict(m) for m in messages]
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

    # ── Self-Learning: Inject past learnings + preferences ──
    last_user_text = _extract_last_user_text(current_msgs)
    learning_context = _build_learning_context(last_user_text) if last_user_text else ""
    if learning_context:
        system_idx = 0
        for i, m in enumerate(current_msgs):
            if m.get("role") == "system":
                system_idx = i + 1
            else:
                break
        learn_label = _get_localized_text(lang, {
            "zh": "以下是 AI 从过去交互中学习的知识，请在回复时参考：",
            "en": "Here's what AI learned from past interactions — reference when responding:",
            "ja": "以下はAIが過去の対話から学んだ知識です。回答時に参考にしてください：",
        })
        current_msgs.insert(system_idx, {
            "role": "system",
            "content": f"{learn_label}\n\n{learning_context}",
        })

    # ── Self-Learning: Heuristic extraction ──
    if last_user_text:
        _extract_learnings_heuristic(last_user_text, session_id)

    # ── Dynamic Tool Filtering + Agent restrictions ──
    agent_tools = _get_agent_tools(agent_id, TOOLS)
    active_tools = _filter_tools(last_user_text, agent_tools) if last_user_text else agent_tools
    # Cap tools at 7 to prevent overflowing model context with large definitions
    if len(active_tools) > 7:
        # Keep most important: read/write/list + the first 2 matching intent tools
        essential = {"read_file", "write_file", "list_dir"}
        priority = [t for t in active_tools if t.get("function", {}).get("name") in essential]
        others = [t for t in active_tools if t.get("function", {}).get("name") not in essential]
        active_tools = priority + others[:max(0, 5 - len(priority))]

    async with httpx.AsyncClient(timeout=httpx.Timeout(120)) as client:
        while iteration < 50:  # hard cap at 50, dynamic exit via stagnation
            iteration += 1
            # Re-evaluate tool set every 3 iterations for multi-step tasks
            if iteration > 1 and iteration % 3 == 0:
                active_tools = agent_tools  # Full tool access for follow-up steps
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

            body = {
                "model": model, "messages": current_msgs,
                "tools": active_tools, "tool_choice": "auto",
                "max_tokens": 4096, "stream": True,
            }

            streamed_text = ""
            tool_call_bufs: dict[int, dict] = {}

            async with client.stream("POST", api_url, json=body, headers=headers) as r:
                async for line in r.aiter_lines():
                    if line and line.startswith("data: "):
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            break
                        try:
                            event = json.loads(data_str)
                            delta = event.get("choices", [{}])[0].get("delta", {})

                            content = delta.get("content", "")
                            reasoning = delta.get("reasoning", "")
                            if content:
                                streamed_text += content
                                # Filter native control tokens so the UI doesn't show
                                # raw <|tool_call|> / <|channel> / <channel|> markers
                                clean = _NATIVE_CONTROL_RE.sub("", content)
                                if clean:
                                    yield {"content": clean}
                                if len(streamed_text) < 5:
                                    _track_progress(session_id, "generating", "text_start")
                            elif reasoning:
                                # Reasoning model (Qwen3.6, DeepSeek-R1, etc.) — stream thinking as content
                                # so the UI doesn't appear frozen during the thinking phase
                                streamed_text += reasoning
                                yield {"content": reasoning}

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
                        except (json.JSONDecodeError, KeyError, TypeError):
                            pass  # Malformed SSE delta — skip this event, try next
                        except Exception:
                            logger.error("SSE tool_call parse error", exc_info=True)
                            raise  # Real errors (network, memory) must surface

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

            if tool_calls:
                with open("/tmp/latiao-loop.log", "a") as lf:
                    lf.write(f"Iteration {iteration}: found {len(tool_calls)} tool(s): {[tc.get('function',{}).get('name') for tc in tool_calls]}\n")
                _track_progress(session_id, "tool_calling", f"{len(tool_calls)} tool(s)")
                logger.info(f"[LOCAL-AGENT] Iteration {iteration}: {len(tool_calls)} tool(s) called, msgs_in_context={len(current_msgs)}")

                current_msgs.append({
                    "role": "assistant",
                    "content": streamed_text or None,
                    "tool_calls": tool_calls,
                })
                has_called_tool = True

                # Stagnation check: reset if new tool calls, else count toward limit
                any_new = False
                for tc in tool_calls:
                    sig = f"{tc.get('function',{}).get('name','')}:{hash(str(tc.get('function',{}).get('arguments','')))}"
                    if sig not in recent_tool_calls:
                        recent_tool_calls.add(sig)
                        any_new = True
                    verify_failed, events = await _handle_tool_execution(
                        tc, current_msgs, session_id, agent_id)
                    logger.info(f"[LOCAL-AGENT] Iteration {iteration}: tool={tc.get('function',{}).get('name','')} executed, result_len={len(current_msgs[-1].get('content','')) if current_msgs else 0}")
                    for evt in events:
                        yield evt
                    if verify_failed:
                        last_verify_failed = True
                if any_new:
                    stagnation = 0
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
            if has_recent_tool_result and iteration < 8 and streamed_text.strip():
                current_msgs.append({
                    "role": "system",
                    "content": (
                        "⚠️ 你刚才收到了工具的执行结果，但只回复了文字而没有继续调用工具。\n"
                        "请检查：用户的任务是否真的完全完成了？\n"
                        "如果还没完成，请继续调用工具。如果确实完成了，请回复最终结果。"
                    ),
                })
                continue
            if not has_called_tool and iteration <= 3 and streamed_text.strip():
                # Model hasn't called any tools yet - nudging to use tools instead of planning
                current_msgs.append({
                    "role": "system",
                    "content": (
                        "不要写执行计划，直接行动。需要用什么工具就立即调用。"
                    ),
                })
                continue
            if not streamed_text.strip() and iteration < 3:
                logger.warning(f"[AGENT] Iteration {iteration}: empty response from cloud model, retrying")
                nudge_text = _get_localized_text(lang, {
                    "zh": "⚠️ 你上一轮的回复是空的。请直接回复用户，或者使用工具完成任务。",
                    "en": "⚠️ Your last response was empty. Please respond to the user directly, or use a tool.",
                    "ja": "⚠️ 前回の応答が空でした。ユーザーに直接返信するか、ツールを使用してください。",
                })
                current_msgs.append({"role": "system", "content": nudge_text})
                continue
            _track_progress(session_id, "completed", f"text_response ({len(streamed_text)} chars)")
            return

        # Hard cap reached (50 iterations) — extremely rare with dynamic stagnation
        tool_count = sum(1 for m in current_msgs if m.get("role") == "tool")
        yield {"content": f"\n\n⚠️ 已达到硬上限 (50 轮)。本会话共执行了 {tool_count} 次工具调用。如需继续，请发送新消息。"}


# ═══════════════════════════════════════════════════════
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
_PROMPT_TOOL_FENCE_RE = re.compile(
    r'```tool\s+(\w+)\s*\n(.*?)\n```',
    re.DOTALL | re.IGNORECASE,
)

_PROMPT_TOOL_RE = re.compile(
    r'(?:\[TOOL:|<tool>|FUNC:)(\w+)\s*(?:\{(.*?)\}|"(.*?)"|(.*?))(?:\]|</tool>|$)',
    re.DOTALL | re.IGNORECASE,
)

# Natural language fallback: matches "web_search \"query\"" or "search \"query\"" etc.
_NL_TOOL_RE = re.compile(
    r'\b(web_search|tavily_search|search|read_file|write_file|list_dir|run_cmd|open_app|open_folder|search_files)\s*[\(\[""]\s*([^\")\]\.]+)\s*[\)\]""]',
    re.IGNORECASE,
)

# Bash/shell code block fallback: ```bash\nls -la /path\n``` → run_cmd
_BASH_BLOCK_RE = re.compile(
    r'```(?:bash|sh|shell|zsh)\s*\n(.*?)\n```',
    re.DOTALL | re.IGNORECASE,
)

# Common commands in bash blocks
_LS_CMD_RE = re.compile(r'^\s*ls\s+(?:-\w+\s+)*["\']?([/\~]\S+|\.\S*)\s*$', re.IGNORECASE)
_CAT_CMD_RE = re.compile(r'^\s*cat\s+["\']?([/\~]\S+)\s*$', re.IGNORECASE)
_FIND_CMD_RE = re.compile(r'^\s*find\s+["\']?([/\~]\S+)\s+(.*)', re.IGNORECASE)


def _parse_prompt_tool_calls(text: str) -> tuple[str, list[dict]]:
    """Parse tool calls from text generated by a local model (prompt-based).
    Returns (cleaned_text, tool_calls_in_openai_format)."""
    tool_calls = []
    used_ranges = []  # track char ranges to strip from text

    # For Qwen models that embed <think>...</think> blocks inside content output:
    # strip think blocks before parsing — reasoning should not contain tool invocations.
    search_text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    if not search_text:
        search_text = text  # fallback if everything was think

    # Priority 1: Fenced format ```tool name\n{json}\n```
    for idx, m in enumerate(_PROMPT_TOOL_FENCE_RE.finditer(text)):
        name = m.group(1)
        args_str = m.group(2).strip()
        try:
            args = json.loads(args_str)
        except json.JSONDecodeError:
            args = _salvage_tool_args(args_str)
        tool_calls.append({
            "id": f"local_fence_{name}_{idx}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
        })
        used_ranges.append((m.start(), m.end()))

    # Priority 2: Inline format [TOOL:name key=value ...] or <tool>name{json}</tool> or FUNC:name key=value
    if not tool_calls:
        for idx, m in enumerate(_PROMPT_TOOL_RE.finditer(text)):
            name = m.group(1)
            json_str = m.group(2)
            quoted = m.group(3)
            rest = m.group(4)
            if json_str:
                try:
                    args = json.loads("{" + json_str + "}")
                except json.JSONDecodeError:
                    args = _salvage_tool_args(json_str)
            elif quoted:
                args = {"query": quoted} if name in ("web_search", "tavily_search", "search") else {"path": quoted}
            elif rest:
                args = _parse_kv_args(rest.strip())
            else:
                args = {}
            if not args:
                continue
            tool_calls.append({
                "id": f"local_inline_{name}_{idx}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
            })
            used_ranges.append((m.start(), m.end()))

    # Priority 3: Natural language fallback — "web_search \"query\"" etc.
    if not tool_calls:
        for idx, m in enumerate(_NL_TOOL_RE.finditer(text)):
            name = m.group(1).lower()
            # Normalize tool name
            if name == "search":
                name = "web_search"
            raw_query = m.group(2).strip()
            if not raw_query:
                continue
            # Build args based on tool type
            if name in ("web_search", "tavily_search"):
                args = {"query": raw_query}
            elif name in ("read_file", "write_file", "open_app", "open_folder"):
                args = {"path": raw_query}
            elif name == "list_dir":
                args = {"path": raw_query}
            elif name == "run_cmd":
                args = {"command": raw_query}
            elif name == "search_files":
                args = {"pattern": raw_query}
            else:
                args = {"query": raw_query}
            tool_calls.append({
                "id": f"local_nl_{name}_{idx}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
            })
            used_ranges.append((m.start(), m.end()))

    # Priority 4: Bash/shell code block fallback — models often output ```bash
    # instead of the taught ```tool format; parse ls/cat/find into our tools
    if not tool_calls:
        for idx, m in enumerate(_BASH_BLOCK_RE.finditer(text)):
            cmd_text = m.group(1).strip()
            if not cmd_text:
                continue
            tool_name = ""
            args = {}
            ls_match = _LS_CMD_RE.match(cmd_text)
            cat_match = _CAT_CMD_RE.match(cmd_text)
            find_match = _FIND_CMD_RE.match(cmd_text)
            if ls_match:
                tool_name = "list_dir"
                args = {"path": ls_match.group(1) or "."}
            elif cat_match:
                tool_name = "read_file"
                args = {"path": cat_match.group(1)}
            elif find_match:
                tool_name = "search_files"
                args = {"path": find_match.group(1), "pattern": find_match.group(2).strip()}
            else:
                tool_name = "run_cmd"
                args = {"command": cmd_text}
            if tool_name:
                tool_calls.append({
                    "id": f"local_bash_{tool_name}_{idx}",
                    "type": "function",
                    "function": {"name": tool_name, "arguments": json.dumps(args, ensure_ascii=False)},
                })
                used_ranges.append((m.start(), m.end()))

    # Clean text by removing parsed tool call regions
    clean = text
    if used_ranges:
        # Remove from end to start to preserve offsets
        for start, end in sorted(used_ranges, reverse=True):
            clean = clean[:start] + clean[end:]
        clean = clean.strip()

    return clean, tool_calls


def _parse_kv_args(raw: str) -> dict:
    """Parse key=value or key=\"value\" pairs from a raw string."""
    result = {}
    # Match: key="value" or key=value
    for m in re.finditer(r'(\w+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|(\S+))', raw):
        key = m.group(1)
        val = m.group(2) or m.group(3) or m.group(4)
        result[key] = val
    return result


def _build_local_tools_prompt(active_tools: list[dict]) -> str:
    """Build a concise tool prompt for local models with strong few-shot examples."""
    lines = ["# 可用工具\n"]
    lines.append("你可以使用以下工具来完成任务。不需要工具时直接回复用户。\n")
    for t in active_tools:
        fn = t.get("function", {})
        name = fn.get("name", "")
        desc = fn.get("description", "")
        params = fn.get("parameters", {}).get("properties", {})
        param_hints = ""
        if params:
            required = fn.get("parameters", {}).get("required", [])
            parts = []
            for pk, pv in params.items():
                req = "*" if pk in required else ""
                ptype = pv.get("type", "string")
                if ptype == "string":
                    ptype = "str"
                elif ptype == "integer":
                    ptype = "int"
                elif ptype == "boolean":
                    ptype = "bool"
                parts.append(f'{pk}{req}: {ptype}')
            param_hints = "(" + ", ".join(parts) + ")"
        lines.append(f"- {name}{param_hints}: {desc}")

    lines.append("\n# 调用格式\n")
    lines.append("调用工具时，必须严格使用以下格式：\n")
    lines.append("```tool 工具名")
    lines.append('{"参数名": "参数值"}')
    lines.append("```")
    lines.append("")
    lines.append("# 示例\n")
    lines.append("用户：帮我看看当前目录有什么文件")
    lines.append("助手：```tool list_dir")
    lines.append('{"path": "."}')
    lines.append("```")
    lines.append("")
    lines.append("用户：搜索今天A股行情")
    lines.append("助手：```tool tavily_search")
    lines.append('{"query": "今天A股大盘走势 上证指数"}')
    lines.append("```")
    lines.append("")
    lines.append("用户：读取 main.py 的内容")
    lines.append("助手：```tool read_file")
    lines.append('{"path": "main.py"}')
    lines.append("```")
    lines.append("")
    lines.append("重要规则：")
    lines.append("1. 每次只调用一个工具")
    lines.append("2. 必须用 ```tool 代码块格式，不要用其他格式")
    lines.append("3. 参数必须是合法 JSON")
    lines.append("4. 等待工具结果后再决定下一步")
    lines.append("5. 不要在 ```tool 块外面写工具调用")
    lines.append("")
    lines.append("⚠️ 强制要求：当用户的问题需要搜索、读取文件、执行命令时，你必须使用工具。")
    lines.append("不可以用文字描述来代替工具调用。直接写出 ```tool 代码块。")
    lines.append("")
    return "\n".join(lines)


async def _local_agent_loop_stream(messages: list, model: str, api_url: str, headers: dict,
                                    session_id: str = "", agent_id: str = "latiao"):
    """Local model agent loop: inject tools as prompt, parse tool calls from text."""
    current_msgs = [dict(m) for m in messages]
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
    if total_chars > 60000:
        # Context Anxiety prevention: save progress and suggest restart (Harness pattern)
        logger.warning(f"[LOCAL-AGENT] Context near limit: ~{total_chars} chars (~{total_chars//2} tokens). Saving progress.")
        # Write PROGRESS.md with current state
        try:
            last_user = _extract_last_user_text(current_msgs)
            _record_progress(f"⚠️ 自动存档（上下文 {{total_chars//2}} tokens）\n最后用户消息: {last_user[:200] if last_user else '(无)'}")
        except Exception:
            pass
        yield {"content": f"\n\n💡 **上下文接近上限**（~{total_chars//2} tokens）。建议：\n1. 当前进度已自动保存到 PROGRESS.md\n2. 开一个新会话，Agent 会从断点继续\n3. 或继续在本会话中完成（质量可能下降）"}
    elif total_chars > 80000:
        logger.warning(f"[LOCAL-AGENT] Context may overflow: ~{total_chars} chars (~{total_chars//2} tokens)")
    if not session_id:
        session_id = str(uuid.uuid4())

    max_iterations = 30
    iteration = 0
    recent_tool_calls: set[str] = set()
    stagnation = 0
    max_stagnation = 10
    text_only_streak = 0
    has_called_tool = False
    # Build tool prompt
    last_user_text = _extract_last_user_text(current_msgs)
    agent_tools = _get_agent_tools(agent_id, TOOLS)
    active_tools = _filter_tools(last_user_text, agent_tools) if last_user_text else agent_tools
    if len(active_tools) > 8:
        essential = {"read_file", "write_file", "list_dir"}
        priority = [t for t in active_tools if t.get("function", {}).get("name") in essential]
        others = [t for t in active_tools if t.get("function", {}).get("name") not in essential]
        active_tools = priority + others[:max(0, 8 - len(priority))]
    tools_prompt = _build_local_tools_prompt(active_tools)

    # Detect continuation: if session has tool results but no final answer,
    # inject a strong continuation nudge in the first system message
    tool_result_count = sum(1 for m in current_msgs if m.get("role") == "tool" or (isinstance(m.get("content"), str) and m["content"].startswith("[工具结果]")))
    has_final_answer = any(
        m.get("role") == "assistant" and isinstance(m.get("content"), str) and len(m["content"]) > 100
        for m in current_msgs[-5:]
    ) if len(current_msgs) > 5 else False
    is_continuation = tool_result_count >= 2 and not has_final_answer
    if is_continuation:
        tools_prompt += (
            "\n\n⚠️⚠️⚠️ 你现在处于任务执行中途！\n"
            f"会话中已有 {tool_result_count} 条工具执行结果，但任务尚未完成。\n"
            "你必须继续使用工具完成用户的原始请求，不能只回复文字说'好的'或'正在处理'。\n"
            "直接调用工具，不要废话。"
        )

    # Inject tools into the first user message context
    for i, m in enumerate(current_msgs):
        if m.get("role") == "user":
            # Insert tools prompt as a system message right before the last user message
            break

    async with httpx.AsyncClient(timeout=httpx.Timeout(120)) as client:
        while iteration < max_iterations:
            iteration += 1
            with open("/tmp/latiao-loop.log", "a") as lf:
                lf.write(f"Iteration {iteration}: current_msgs={len(current_msgs)}, roles={[m.get('role') for m in current_msgs[-5:]]}\n")

            # Build messages for this iteration: merge tools + current context
            loop_msgs = list(current_msgs)
            # Convert role:"tool" → role:"user" (llama-cpp Qwen chat format only supports
            # system/user/assistant roles; "tool" role causes empty responses)
            loop_msgs = [
                {"role": "user", "content": f"[工具结果] {m['content']}"}
                if m.get("role") == "tool" else dict(m)
                for m in loop_msgs
            ]
            # Inject tool prompt: full on first iteration, short on later ones.
            # Long prompts cause Qwen's <think> to overflow max_tokens on follow-up rounds.
            if iteration == 1:
                current_prompt = tools_prompt
            else:
                # Build lightweight tool reminder that still lists available tools by name
                tool_names = [t.get("function", {}).get("name", "") for t in active_tools if t.get("function", {}).get("name")]
                names_str = ", ".join(tool_names) if tool_names else "无"
                current_prompt = (
                    f"⚠️ 任务尚未完成，你必须继续！可用工具: {names_str}。\n"
                    "格式：```tool 工具名\n{\"参数\":\"值\"}\n```\n"
                    "如果当前任务的所有步骤都已完成，才可以直接回复用户。否则必须继续使用工具。"
                )
            for i, m in enumerate(loop_msgs):
                if m.get("role") == "system":
                    m["content"] = current_prompt + "\n\n" + m["content"]
                    break
            else:
                loop_msgs.insert(0, {"role": "system", "content": current_prompt})

            body = {
                "model": model, "messages": loop_msgs,
                "max_tokens": 8192, "stream": True,
            }

            streamed_text = ""
            logger.info(f"[LOCAL-AGENT] Iteration {iteration}: calling LLM, msgs={len(loop_msgs)}, first_user_content_len={len(loop_msgs[-1].get('content','')) if loop_msgs else 0}")
            async with client.stream("POST", api_url, json=body, headers=headers) as r:
                async for line in r.aiter_lines():
                    if line and line.startswith("data: "):
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            break
                        try:
                            event = json.loads(data_str)
                            delta = event.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            reasoning = delta.get("reasoning", "")
                            if content:
                                streamed_text += content
                                yield {"content": content}
                            elif reasoning:
                                streamed_text += reasoning
                                yield {"content": reasoning}
                        except (json.JSONDecodeError, KeyError, TypeError):
                            pass
                        except Exception:
                            logger.error("Local agent SSE parse error", exc_info=True)
                            raise

            # Check for tool calls in the streamed text
            clean_text, tool_calls = _parse_prompt_tool_calls(streamed_text)
            # Also check for native tool call format (Gemma 4 <|tool_call|>)
            if not tool_calls and _NATIVE_TOOL_RE.search(streamed_text):
                native_tcs = _parse_native_tool_calls(streamed_text)
                if native_tcs:
                    streamed_text = _strip_native_tool_calls(streamed_text)
                    tool_calls = native_tcs


            if tool_calls:
                _track_progress(session_id, "tool_calling", f"{len(tool_calls)} tool(s)")

                # Add assistant message (cleaned text)
                current_msgs.append({
                    "role": "assistant",
                    "content": clean_text or "正在调用工具...",
                })
                has_called_tool = True

                any_new = False
                for tc in tool_calls:
                    sig = f"{tc.get('function',{}).get('name','')}:{hash(str(tc.get('function',{}).get('arguments','')))} "
                    if sig not in recent_tool_calls:
                        recent_tool_calls.add(sig)
                        any_new = True
                    verify_failed, events = await _handle_tool_execution(
                        tc, current_msgs, session_id, agent_id)
                    for evt in events:
                        yield evt

                if any_new:
                    stagnation = 0
                    text_only_streak = 0
                    stagnation += 1
                    if stagnation >= max_stagnation:
                        yield {"content": f"\n\n⚠️ 连续 {stagnation} 轮无新进展，Agent 停止。如需继续请发新消息。"}
                        return
                continue

            # No tool calls — pure text response done
            # Check if there are pending tasks: if the last message is a tool result
            # and model didn't call another tool, it might have prematurely stopped
            has_recent_tool_result = any(
                m.get("role") == "tool" or (isinstance(m.get("content"), str) and m["content"].startswith("[工具结果]"))
                for m in current_msgs[-3:]
            )
            if has_recent_tool_result and text_only_streak < max_stagnation and streamed_text.strip():
                # Model returned text after a tool result but didn't call another tool.
                # It might think the task is done when it's not. Force one more check.
                logger.info(f"[LOCAL-AGENT] Iteration {iteration}: model returned text after tool result, pushing for continuation")
                current_msgs.append({
                    "role": "system",
                    "content": (
                        "⚠️ 你刚才收到了工具的执行结果，但只回复了文字而没有继续调用工具。\n"
                        "请检查：用户的任务是否真的完全完成了？\n"
                        "如果还没完成，请继续调用工具。如果确实完成了，请回复最终结果。\n"
                        "调用工具格式：```tool 工具名\n{\"参数\":\"值\"}\n```"
                    ),
                })
                text_only_streak += 1
                continue
            if not has_called_tool and iteration <= 3 and streamed_text.strip():
                # Model hasn't called any tools yet and is outputting text (planning instead of doing)
                logger.info(f"[LOCAL-AGENT] Iteration {iteration}: model planning instead of calling tools, nudging")
                current_msgs.append({
                    "role": "system",
                    "content": (
                        "不要写执行计划，直接行动。需要用什么工具就立即调用。"
                    ),
                })
                text_only_streak += 1
                continue
            if not streamed_text.strip() and text_only_streak < max_stagnation:
                # Empty response from local model — retry with a nudge
                logger.warning(f"[LOCAL-AGENT] Iteration {iteration}: empty response, retrying")
                nudge_text = _get_localized_text(_detect_user_language(_extract_last_user_text(current_msgs)), {
                    "zh": "⚠️ 你上一轮的回复是空的。请直接回复用户，或者使用工具完成任务。如果需要调用工具，使用 ```tool 格式。",
                    "en": "⚠️ Your last response was empty. Please respond to the user directly, or use a tool. To call a tool, use the ```tool format.",
                    "ja": "⚠️ 前回の応答が空でした。ユーザーに直接返信するか、ツールを使用してください。ツールを使用するには ```tool 形式を使ってください。",
                })
                current_msgs.append({"role": "system", "content": nudge_text})
                continue
            _track_progress(session_id, "completed", f"text_response ({len(streamed_text)} chars)")
            logger.info(f"[LOCAL-AGENT] Iteration {iteration}: no tools, returning text ({len(streamed_text)} chars)")
            return

        tool_count = sum(1 for m in current_msgs if m.get("role") == "tool")
        yield {"content": f"\n\n⚠️ 已达到硬上限 ({max_iterations} 轮)。本会话共执行了 {tool_count} 次工具调用。如需继续，请发送新消息。"}


def _build_chat_messages(body: dict, messages: list, matched_skill: str|None = None) -> list:
    """Assemble the full message array with identity, env, skills, agent, and image injections.
    All system prompts are merged into ONE message to work around a llama-cpp bug
    where multiple system messages cause empty responses."""
    last_user_text = _extract_last_user_text(messages)
    intent_result = _process_identity_intents(last_user_text)

    system_parts = []

    # Agent identity
    agent_id = body.get("agent", "latiao")
    agent_cfg = _get_agent_config(agent_id)
    system_parts.append(agent_cfg["identity"])

    # Identity file
    identity_msgs = _read_identity()
    for im in identity_msgs:
        system_parts.append(im["content"])
    if intent_result:
        system_parts.append(
            f"⚠️ 你的身份刚刚被用户更新了：{intent_result}。"
            f"从现在开始，你必须以更新后的身份回复用户。"
        )

    # Environment info
    home = str(Path.home())
    cwd = os.getcwd()
    now = datetime.now().strftime("%Y-%m-%d (%A) %H:%M:%S")
    
    # Detect user language for system prompt localization
    user_lang = _detect_user_language(last_user_text)
    
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

    # Matched skill
    if matched_skill and matched_skill in SKILL_INDEX:
        skill = SKILL_INDEX[matched_skill]
        skill_intro = _get_localized_text(user_lang, {
            "zh": {"use": f"你现在可以使用以下技能：{skill['name']}", "desc": f"技能说明：{skill['description']}", "level": f"技能安全等级：{skill.get('security_level', 'safe')}", "rules": f"技能使用规则：\n{skill['content']}", "follow": "请根据技能规则来回答用户的问题。"},
            "en": {"use": f"You can now use this skill: {skill['name']}", "desc": f"Description: {skill['description']}", "level": f"Security level: {skill.get('security_level', 'safe')}", "rules": f"Rules:\n{skill['content']}", "follow": "Follow the skill rules when responding."},
            "ja": {"use": f"次のスキルを使用できます：{skill['name']}", "desc": f"説明：{skill['description']}", "level": f"セキュリティレベル：{skill.get('security_level', 'safe')}", "rules": f"ルール：\n{skill['content']}", "follow": "スキルルールに従って回答してください。"},
        })
        system_parts.append(f"{skill_intro['use']}\n{skill_intro['desc']}\n{skill_intro['level']}\n{skill_intro['rules']}\n{skill_intro['follow']}")

    # Skill prompt
    skill_prompt = _build_skill_prompt()
    if skill_prompt:
        system_parts.append(skill_prompt)

    # Goal mode / progressive delivery
    goal_mode = body.get("goal_mode", False)
    progressive = body.get("progressive_delivery", True)
    extra_prompts = []
    if goal_mode:
        extra_prompts.append(GOAL_MODE_PROMPT)
    if progressive:
        extra_prompts.append(PROGRESSIVE_DELIVERY_PROMPT)
    if extra_prompts:
        system_parts.append("\n".join(extra_prompts))

    # Cross-session memory: inject recent learnings from past conversations
    recent = _get_recent_learnings(5)
    if recent:
        memory_label = _get_localized_text(user_lang, {
            "zh": "以下是 AI 从过去交互中学到的知识：",
            "en": "Here's what AI learned from past interactions:",
            "ja": "以下はAIが過去の対話から学んだ知識です：",
        })
        system_parts.append(memory_label + "\n" + "\n".join(recent))

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


def _resolve_api_target(cloud_config: dict | None) -> tuple[str, str, dict, bool]:
    """Resolve API URL, protocol, headers, and whether it's a local LLM (no cloud config).
    Cloud models are detected by having an endpoint (key is optional for local proxies)."""
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
        protocol = "openai"
        local_api = local_llm.get_api_url()
        if local_api:
            api_url = local_api + "/chat/completions"
        else:
            api_url = ""  # No local LLM running — will be caught as connection error
        headers = {"Content-Type": "application/json"}
        return protocol, api_url, headers, True


# ── Task Intent Detection + Model Auto-Routing ──

_CODE_INTENT_PATTERNS = [
    r'(?:代码|编程|写|修复|改|review|检查|debug|优化|重构|实现|开发)',
    r'(?:code|fix|write|implement|refactor|debug|review|optimize)',
    r'(?:bug|error|报错|异常|crash)',
    r'(?:function|函数|class|类|module|模块|API|接口)',
    r'(?:read_file|write_file|list_dir|run_cmd)',
]


def _detect_user_language(text: str) -> str:
    """Detect the language of user input: 'zh', 'en', or 'ja'."""
    if not text:
        return "zh"
    # Count characters in each language range
    zh = len(re.findall(r'[\u4e00-\u9fff\u3400-\u4dbf]', text))
    ja_kana = len(re.findall(r'[\u3040-\u309f\u30a0-\u30ff]', text))
    en = len(re.findall(r'[a-zA-Z]', text))
    if ja_kana > zh and ja_kana > en:
        return "ja"
    if en > zh + ja_kana:
        return "en"
    return "zh"


def _get_localized_text(lang: str, texts: dict[str, str | dict]) -> str | dict:
    """Get localized text for a given language, falling back to zh."""
    return texts.get(lang) or texts.get("zh", "")

def _detect_task_intent(text: str) -> str:
    """Detect whether the user intent is 'code', 'chat', or 'research'.
    Used for automatic model routing."""
    text_lower = text.lower()
    for pattern in _CODE_INTENT_PATTERNS:
        if re.search(pattern, text_lower):
            return "code"
    # Research indicators
    if re.search(r'(?:搜索|查|找|论文|研究|分析|最新|news|search|research)', text_lower):
        return "research"
    return "chat"


def _has_cloud_models() -> bool:
    """Check if any cloud model is configured."""
    try:
        config_file = CONFIG_FILE
        if config_file.exists():
            cfg = json.loads(config_file.read_text(encoding="utf-8"))
            models = cfg.get("cloud_models", [])
            return any(m.get("endpoint") for m in models)
    except Exception:
        pass
    return False


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
        pass
    return None


@app.post("/v1/chat/completions")
async def chat_completion(request: Request):
    """Main chat endpoint. Routes to agent loop (OpenAI-compatible) or simple streaming.
    Auto-routes to best model based on task type when no specific model is selected."""
    body = await request.json()
    messages = body.get("messages", [])
    last_user_text = _extract_last_user_text(messages)
    
    # Async match skill (no event loop conflict)
    matched_skill = await _match_skill(last_user_text)

    # Assemble full message context (identity, env, skills, agent, image)
    messages = _build_chat_messages(body, messages, matched_skill)
    model = body.get("model") or SUBAGENT_MODEL
    
    # ── Auto-route: if no explicit model selected, pick based on task intent ──
    cloud_config = body.get("cloud_config")
    user_selected_model = body.get("model")  # User explicitly chose a model?
    if not user_selected_model and not cloud_config and last_user_text:
        intent = _detect_task_intent(last_user_text)
        if intent == "code" and _has_cloud_models():
            logger.info("Auto-route: code task → using cloud model")
            # Try to use an available cloud model for code tasks
            cloud_config = _get_best_cloud_config()
            if cloud_config:
                model = cloud_config.get("model", model)
        elif intent == "chat" and local_llm.get_api_url():
            logger.info("Auto-route: chat task → using local model (free)")
            # Keep local model for casual chat
            pass
    
    logger.info("Chat request: model=%s, msg_count=%d, stream=%s", model, len(messages), body.get("stream", False))

    skip_tools = body.get("skip_tools", False)
    agent_id = body.get("agent", "latiao")
    cloud_config = body.get("cloud_config")
    _last_cloud_config.set(cloud_config)
    use_stream = body.get("stream", False)

    # Resolve API target
    protocol, api_url, headers, is_local = _resolve_api_target(cloud_config)

    # Agent loop: LLM autonomously decides when to call tools
    if not skip_tools and protocol == "openai":
        session_id = body.get("session_id", str(uuid.uuid4()))
        if use_stream:
            async def agent_loop_wrapper():
                try:
                    if is_local:
                        # 本地模型：用 prompt-based tool calling（不依赖 OpenAI function calling API）
                        async for event in _local_agent_loop_stream(messages, model, api_url, headers, session_id, agent_id):
                            yield f"data: {json.dumps(event)}\n\n"
                    else:
                        # 云端模型：原生 OpenAI function calling
                        async for event in _agent_loop_stream(messages, model, api_url, headers, session_id, agent_id):
                            yield f"data: {json.dumps(event)}\n\n"
                    yield "data: [DONE]\n\n"
                except (httpx.ConnectError, httpx.RemoteProtocolError):
                    yield f"data: {json.dumps({'error': '无法连接模型服务。请检查后端是否已启动。'})}\n\n"
                    yield "data: [DONE]\n\n"
                except httpx.HTTPStatusError as e:
                    yield f"data: {json.dumps({'error': f'模型服务返回错误 HTTP {e.response.status_code}'})}\n\n"
                    yield "data: [DONE]\n\n"
                except httpx.TimeoutException:
                    yield f"data: {json.dumps({'error': '模型服务响应超时，请检查网络或模型是否过大。'})}\n\n"
                    yield "data: [DONE]\n\n"
                except Exception:
                    logger.error("Agent loop unexpected error", exc_info=True)
                    yield f"data: {json.dumps({'error': 'Agent 循环内部错误，请查看日志。'})}\n\n"
                    yield "data: [DONE]\n\n"
            return StreamingResponse(agent_loop_wrapper(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    # Non-streaming agent loop (for Tauri HTTP plugin compatibility)
    if not skip_tools and protocol == "openai":
        # Run agent loop synchronously — collect all content + tool results
        current_msgs = [dict(m) for m in messages]
        # Truncate to prevent context overflow (8K token limit)
        if len(current_msgs) > 30:
            # Keep system messages + last 20 exchanges
            system_msgs = [m for m in current_msgs if m.get("role") == "system"]
            other_msgs = [m for m in current_msgs if m.get("role") != "system"]
            current_msgs = system_msgs + other_msgs[-20:]
        agent_tools_ns = _get_agent_tools(agent_id, TOOLS)
        active_tools_ns = _filter_tools(last_user_text, agent_tools_ns) if last_user_text else agent_tools_ns
        use_prompt_tools = is_local  # Local models use prompt-based tool calling
        # Cap tools: 7 for native function calling, 8 for prompt-based (less overhead)
        tool_cap = 8 if use_prompt_tools else 5
        if len(active_tools_ns) > tool_cap:
            essential = {"read_file", "write_file", "list_dir"}
            priority_ns = [t for t in active_tools_ns if t.get("function", {}).get("name") in essential]
            others_ns = [t for t in active_tools_ns if t.get("function", {}).get("name") not in essential]
            active_tools = priority_ns + others_ns[:max(0, tool_cap - len(priority_ns))]
        else:
            active_tools = active_tools_ns
        full_content = ""
        tool_count = 0
        local_tools_prompt = _build_local_tools_prompt(active_tools) if use_prompt_tools else ""

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120)) as client:
                for _ in range(30):  # max iterations, non-streaming
                    loop_msgs = list(current_msgs)
                    # Convert role:"tool" → role:"user" for Qwen chat format compatibility
                    loop_msgs = [
                        {"role": "user", "content": f"[工具结果] {m['content']}"}
                        if m.get("role") == "tool" else dict(m)
                        for m in loop_msgs
                    ]
                    if use_prompt_tools:
                        # Inject tool prompt into the LAST system message (append, don't create new)
                        # Creating a second system message triggers a llama-cpp bug → empty response
                        last_sys_idx = -1
                        for i, m in enumerate(loop_msgs):
                            if m.get("role") == "system":
                                last_sys_idx = i
                        if last_sys_idx >= 0:
                            loop_msgs[last_sys_idx]["content"] += "\n\n" + local_tools_prompt
                        else:
                            loop_msgs.insert(0, {"role": "system", "content": local_tools_prompt})

                    if use_prompt_tools:
                        resp = await client.post(api_url, json={
                            "model": model, "messages": loop_msgs,
                            "max_tokens": 8192, "stream": False,
                        }, headers=headers)
                    else:
                        resp = await client.post(api_url, json={
                            "model": model, "messages": current_msgs,
                            "tools": active_tools, "tool_choice": "auto",
                            "max_tokens": 8192, "stream": False,
                        }, headers=headers)
                    resp_data = resp.json()
                    choices = resp_data.get("choices", [])
                    if not choices:
                        break
                    msg = choices[0].get("message", {})
                    content = msg.get("content", "") or ""
                    reasoning = msg.get("reasoning", "") or ""
                    tc_data = msg.get("tool_calls", [])

                    # Native tool call detection for Gemma
                    if not tc_data and content and _NATIVE_TOOL_RE.search(content):
                        native_tcs = _parse_native_tool_calls(content)
                        if native_tcs:
                            content = _strip_native_tool_calls(content)
                            tc_data = native_tcs

                    # Prompt-based tool call detection for local models
                    if not tc_data and content and use_prompt_tools:
                        clean_text, prompt_tcs = _parse_prompt_tool_calls(content)
                        if prompt_tcs:
                            content = clean_text
                            tc_data = prompt_tcs

                    if tc_data:
                        tool_count += 1
                        current_msgs.append({
                            "role": "assistant",
                            "content": content or None,
                            "tool_calls": tc_data,
                        })
                        for tc in tc_data:
                            call_id = tc.get("id", str(uuid.uuid4()))
                            tool_name = tc.get("function", {}).get("name", "")
                            tool_args_str = tc.get("function", {}).get("arguments", "{}")
                            try:
                                tool_args = json.loads(tool_args_str) if isinstance(tool_args_str, str) else tool_args_str
                            except json.JSONDecodeError:
                                tool_args = {}
                            # Respect permissions — non-streaming can't ask for user confirmation
                            perm = _resolve_permission(tool_name, tool_args)
                            if perm == "confirm":
                                result = f"⛔ 操作需要用户确认: {tool_name}。请在流式模式下重试。"
                            elif perm == "danger":
                                result = f"⛔ 高危操作已阻止: {tool_name}。请联系管理员。"
                            else:
                                logger.info("Tool executing (non-streaming): %s %s", tool_name, str(tool_args)[:100])
                                result = await execute_tool(tool_name, tool_args)
                                # Self-evolution: record + background-refine learning
                                _record_tool_call_db(session_id, tool_name, tool_args, result)
                                asyncio.create_task(_refine_learnings(tool_name, tool_args, result, session_id))
                            if len(result) > 5000:
                                result = result[:5000] + "\n...(截断)"
                            current_msgs.append({
                                "role": "tool",
                                "tool_call_id": call_id,
                                "content": result,
                            })
                        continue  # Loop again with tool results

                    # Text response
                    if content:
                        full_content += content
                    elif reasoning:
                        full_content += reasoning
                    break  # Done
        except Exception as e:
            logger.error("Non-streaming agent loop error: %s", e)
            return JSONResponse({"error": f"Agent 循环错误: {e}"}, status_code=500)

        if not full_content:
            # Model may return empty when context is too long or only thinking tokens
            logger.warning("Non-streaming agent loop: model returned empty content, tool_count=%d", tool_count)
            full_content = "（模型未生成文本回复。可能是上下文过长。请开启新会话或缩短对话历史。）"
        return {
            "id": "chatcmpl-sidecar",
            "object": "chat.completion",
            "created": int(datetime.now().timestamp()),
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": full_content},
                         "finish_reason": "stop"}],
            "usage": {"total_tokens": 0},
        }

    # Simple streaming fallback (skip_tools=True or non-OpenAI protocol or sync mode)
    if use_stream:
        async def stream():
            # Truncate long history to prevent context overflow
            msgs_for_model = messages
            if len(msgs_for_model) > 30:
                system_msgs = [m for m in msgs_for_model if m.get("role") == "system"]
                other_msgs = [m for m in msgs_for_model if m.get("role") != "system"]
                msgs_for_model = system_msgs + other_msgs[-20:]
            lm_body = {"model": model, "messages": msgs_for_model, "stream": True, "max_tokens": 4096}
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(120)) as c:
                    async with c.stream("POST", api_url, json=lm_body, headers=headers) as r:
                        async for line in r.aiter_lines():
                            if line and line.startswith("data: "):
                                data_str = line[6:]
                                if data_str == "[DONE]":
                                    yield "data: [DONE]\n\n"
                                    return
                                try:
                                    event = json.loads(data_str)
                                    delta = event.get("choices", [{}])[0].get("delta", {})
                                    text = delta.get("content", "")
                                    reasoning = delta.get("reasoning", "")
                                    if reasoning:
                                        yield f"data: {json.dumps({'content': reasoning})}\n\n"
                                    if text:
                                        yield f"data: {json.dumps({'content': text})}\n\n"
                                except (json.JSONDecodeError, KeyError, IndexError):
                                    pass  # Malformed SSE event — skip, try next
                                except Exception:
                                    logger.warning("Unexpected error in SSE stream fallback", exc_info=True)
                                    raise
            except (httpx.ConnectError, httpx.RemoteProtocolError):
                yield f"data: {json.dumps({'error': '无法连接模型服务。请检查 LM Studio 或本地 LLM 是否已启动。'})}\n\n"
                yield "data: [DONE]\n\n"
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    # Sync fallback (rarely used)
    resp_data = {}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120)) as c:
            resp = await c.post(api_url, json={
                "model": model, "messages": messages, "max_tokens": 4096,
            }, headers=headers)
            resp_data = resp.json()
    except (httpx.ConnectError, httpx.RemoteProtocolError):
        return JSONResponse(
            {"error": "无法连接模型服务。请检查 LM Studio 或本地 LLM 是否已启动。"},
            status_code=503,
        )
    except Exception:
        logger.error("Sync chat fallback failed", exc_info=True)
        return JSONResponse(
            {"error": "模型请求失败，请查看日志。"},
            status_code=500,
        )

    # Handle malformed responses (model may return only reasoning, no choices)
    choices = resp_data.get("choices", [])
    if choices:
        ai_content = choices[0].get("message", {}).get("content", "") or ""
        ai_reasoning = choices[0].get("message", {}).get("reasoning_content", "") or ""
        # Also check top-level reasoning field (used by some MLX models)
        if not ai_reasoning:
            ai_reasoning = resp_data.get("reasoning", "") or ""
    else:
        # Model returned no choices — might be an error or all-reasoning response
        ai_content = ""
        ai_reasoning = resp_data.get("reasoning", "") or ""
        if not ai_content and not ai_reasoning:
            # Check for error field
            err = resp_data.get("error", "")
            if isinstance(err, str) and err:
                return JSONResponse({"error": f"模型返回错误: {err[:300]}"}, status_code=502)
            return JSONResponse({"error": "模型返回了空的响应。"}, status_code=502)

    if not ai_content and ai_reasoning:
        ai_content = "(思考过程太长，以下是部分推理内容)\n\n" + ai_reasoning[-500:]

    return {
        "id": "chatcmpl-sidecar",
        "object": "chat.completion",
        "created": int(datetime.now().timestamp()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": ai_content, "reasoning": ai_reasoning},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


@app.post("/v1/upload_file")
async def upload_file(file: UploadFile = File(...)):
    """文件上传：图片转 base64，PDF 提取文本，文本直接读取"""
    try:
        content = await file.read()
        if len(content) > MAX_UPLOAD_SIZE:
            return {
                "status": "error",
                "message": f"文件过大: {len(content) / 1024 / 1024:.1f}MB (上限 {MAX_UPLOAD_SIZE / 1024 / 1024:.0f}MB)",
            }
        file_type = file.content_type or ""
        is_image = file_type.startswith("image/")
        is_pdf = file_type == "application/pdf" or (file.filename or "").lower().endswith(".pdf")

        if is_image:
            base64_content = base64.b64encode(content).decode("utf-8")
            return {
                "status": "success",
                "content": f"图片已上传: {file.filename}",
                "filename": file.filename,
                "is_image": True,
                "base64_data": base64_content,
                "content_type": file_type,
                "size": len(content),
            }
        elif is_pdf:
            reader = None
            try:
                from PyPDF2 import PdfReader
                reader = PdfReader(io.BytesIO(content))
                pages = []
                for page in reader.pages:
                    text = page.extract_text()
                    if text:
                        pages.append(text)
                pdf_text = "\n\n".join(pages)
                if not pdf_text.strip():
                    pdf_text = "(PDF 中没有可提取的文字，可能是扫描件或图片型 PDF)"
            except Exception as e:
                pdf_text = f"(PDF 解析失败: {e})"

            return {
                "status": "success",
                "content": pdf_text,
                "filename": file.filename,
                "is_pdf": True,
                "page_count": len(reader.pages) if reader is not None else 0,
                "size": len(content),
            }
        else:
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                text = content.decode("latin-1", errors="replace")

            return {
                "status": "success",
                "content": text,
                "filename": file.filename,
                "is_image": False,
                "size": len(content),
            }
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ── Whisper model cache (lazy-load once, reuse across requests) ──
_whisper_model = None

def _get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        _whisper_model = WhisperModel("tiny", device="cpu", compute_type="int8")
    return _whisper_model


@app.post("/v1/recognize_speech")
async def recognize_speech(request: Request):
    """语音识别：前端发 WAV base64 → faster-whisper 本地识别"""
    import tempfile

    try:
        body = await request.json()
        audio_base64 = body.get("audio_base64", "")

        if not audio_base64:
            return {"status": "error", "message": "No audio data provided"}

        audio_bytes = base64.b64decode(audio_base64)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".webm") as tmp:
            tmp.write(audio_bytes)
            wav_path = tmp.name

        try:
            model = _get_whisper_model()
            segments, info = model.transcribe(wav_path, language="zh", beam_size=5)

            text = " ".join(s.text.strip() for s in segments)

            if text:
                return {"status": "success", "text": text}
            else:
                return {"status": "success", "text": "(未识别到语音内容)"}

        finally:
            if os.path.exists(wav_path):
                os.unlink(wav_path)
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/v1/test_connection")
async def test_connection(request: Request):
    """测试云端 API 连接"""
    body = await request.json()
    key = body.get("key", "")
    endpoint = body.get("endpoint", "")
    protocol = body.get("protocol", "openai")
    model = body.get("model", "")

    if not key or not endpoint:
        return {"status": "error", "message": "Key and endpoint required"}

    timeout = httpx.Timeout(10.0)

    try:
        if protocol == "anthropic":
            api_url = endpoint.rstrip("/") + "/messages"
            headers = {
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            }
            req_body = {"model": model, "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]}
        elif protocol == "gemini":
            api_url = f"{endpoint.rstrip('/')}/models/{model}:generateContent?key={key}"
            headers = {"Content-Type": "application/json"}
            req_body = {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}
        else:
            api_url = endpoint.rstrip("/") + "/chat/completions"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            }
            req_body = {"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5}

        async with httpx.AsyncClient(timeout=timeout) as c:
            resp = await c.post(api_url, json=req_body, headers=headers)

        if resp.status_code in (200, 201):
            return {"status": "ok", "message": f"Connected (HTTP {resp.status_code})"}
        elif resp.status_code in (401, 403):
            return {"status": "error", "message": "Invalid API key"}
        else:
            return {"status": "error", "message": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except httpx.TimeoutException:
        return {"status": "error", "message": "Connection timed out"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/v1/identity")
async def get_identity():
    """Return status and content of all identity files."""
    files = []
    for filename in IDENTITY_FILES:
        filepath = PROGRESS_DIR / filename
        try:
            if filepath.exists():
                content = filepath.read_text(encoding="utf-8")
                files.append({"name": filename, "exists": True, "content": content})
            else:
                files.append({"name": filename, "exists": False, "content": ""})
        except Exception:
            files.append({"name": filename, "exists": False, "content": ""})
    return {"status": "ok", "files": files}


# ── Agent management endpoints ──

@app.get("/v1/agents")
async def get_agents():
    """Return all agent profiles (built-in + custom)."""
    agents = []
    for key, cfg in AGENT_PROFILES.items():
        agents.append({
            "id": key,
            "name": cfg.get("name", key),
            "display": cfg.get("display", ""),
            "role": cfg.get("role", "specialist"),
            "tools": cfg.get("tools", "all") if isinstance(cfg.get("tools"), list) else "all",
            "custom": cfg.get("custom", False),
        })
    return {"status": "ok", "agents": agents}

@app.post("/v1/agents/save")
async def save_agent(request: Request):
    """Create or update a custom agent profile."""
    body = await request.json()
    agent_id = body.get("id", "").strip().lower().replace(" ", "-")
    if not agent_id or agent_id in ("latiao",):  # protect built-in orchestrator
        return {"status": "error", "message": "Invalid or reserved agent id"}
    custom = _load_custom_agents()
    custom[agent_id] = {
        "name": body.get("name", agent_id),
        "display": body.get("display", body.get("name", agent_id)),
        "role": "specialist",
        "identity": body.get("identity", f"You are {body.get('name', agent_id)}."),
        "tools": body.get("tools", ["read_file", "list_dir", "search_files"]),
    }
    _save_custom_agents(custom)
    # Reload into AGENT_PROFILES
    AGENT_PROFILES[agent_id] = dict(custom[agent_id], custom=True)
    return {"status": "ok", "agent": AGENT_PROFILES[agent_id]}

@app.delete("/v1/agents/{agent_id}")
async def delete_agent(agent_id: str):
    """Delete a custom agent profile."""
    custom = _load_custom_agents()
    if agent_id not in custom:
        return {"status": "error", "message": "Agent not found or not custom"}
    del custom[agent_id]
    _save_custom_agents(custom)
    AGENT_PROFILES.pop(agent_id, None)
    return {"status": "ok"}


@app.get("/v1/tools")
async def get_tools():
    """Return all loaded tools with definitions, permissions, and usage stats."""
    tools_info = []
    # Get usage stats from memory.db
    usage: dict[str, int] = {}
    try:
        if MEMORY_DB.exists():
            conn = sqlite3.connect(str(MEMORY_DB), check_same_thread=False)
            rows = conn.execute(
                "SELECT tool_name, COUNT(*) as cnt FROM tool_calls GROUP BY tool_name"
            ).fetchall()
            for row in rows:
                usage[row[0]] = row[1]
            conn.close()
    except Exception:
        logger.warning("Failed to load usage stats", exc_info=True)

    for tool in TOOLS:
        fn = tool.get("function", {})
        name = fn.get("name", "unknown")
        # Check custom permissions first, then fall back to default
        perm = TOOL_PERMISSIONS.get(name, "safe")
        for rule in _custom_permissions:
            if rule.get("tool") == name and "path_pattern" not in rule:
                perm = rule.get("permission", perm)
                break
        tools_info.append({
            "name": name,
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters", {}),
            "permission": perm,
            "usage_count": usage.get(name, 0),
        })
    return {"status": "ok", "tools": tools_info}


@app.get("/v1/permissions")
async def get_permissions():
    """Return current custom permission rules."""
    return {"status": "ok", "rules": _custom_permissions}


@app.post("/v1/permissions")
async def set_permissions(request: Request):
    """Save custom permission rules. Accepts {rules: [...]} or {tool, permission} for single update."""
    global _custom_permissions
    body = await request.json()
    if "tool" in body and "permission" in body:
        # Single tool update — upsert into custom rules
        tool = body["tool"]
        perm = body["permission"]
        found = False
        for rule in _custom_permissions:
            if rule.get("tool") == tool and "path_pattern" not in rule:
                rule["permission"] = perm
                found = True
                break
        if not found:
            _custom_permissions.append({"tool": tool, "permission": perm})
        _save_permissions(_custom_permissions)
        return {"status": "ok", "rules": _custom_permissions}
    rules = body.get("rules", [])
    _save_permissions(rules)
    _load_permissions()
    return {"status": "ok", "rules": _custom_permissions}


@app.get("/v1/progress")
async def get_progress():
    """Return PROGRESS.md content for cross-session continuity."""
    try:
        if PROGRESS_FILE.exists():
            content = PROGRESS_FILE.read_text(encoding="utf-8")
            return {"status": "ok", "content": content}
        return {"status": "ok", "content": ""}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/v1/memory/search")
async def search_memory(q: str = Query(..., min_length=1), limit: int = Query(default=20, ge=1, le=100)):
    """Full-text search over tool call history using FTS5."""
    try:
        if not MEMORY_DB.exists():
            return {"status": "ok", "results": [], "query": q}
        conn = _get_db()
        # Sanitize FTS5 query: escape special chars, collapse whitespace
        safe_q = re.sub(r'[^\w\s"*]', '', q).strip()
        if not safe_q:
            return {"status": "ok", "results": [], "query": q}
        rows = conn.execute(
            "SELECT t.id, t.session_id, t.tool_name, t.args, t.result, t.created_at "
            "FROM tool_calls_fts f JOIN tool_calls t ON f.rowid = t.rowid "
            "WHERE tool_calls_fts MATCH ? ORDER BY rank LIMIT ?",
            (safe_q, limit),
        ).fetchall()
        results = [
            {"id": r[0], "session_id": r[1], "tool_name": r[2], "args": r[3], "result": r[4], "created_at": r[5]}
            for r in rows
        ]
        # LIKE fallback for CJK text that FTS5 unicode61 tokenizer misses
        if not results:
            like_q = f"%{q.strip()}%"
            rows = conn.execute(
                "SELECT id, session_id, tool_name, args, result, created_at FROM tool_calls "
                "WHERE tool_name LIKE ? OR args LIKE ? OR result LIKE ? ORDER BY created_at DESC LIMIT ?",
                (like_q, like_q, like_q, limit),
            ).fetchall()
            results = [
                {"id": r[0], "session_id": r[1], "tool_name": r[2], "args": r[3], "result": r[4], "created_at": r[5]}
                for r in rows
            ]
        return {"status": "ok", "results": results, "query": q}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/v1/memory/recent")
async def recent_memory(limit: int = Query(default=50, ge=1, le=200)):
    """Return most recent tool call records."""
    try:
        if not MEMORY_DB.exists():
            return {"status": "ok", "records": []}
        conn = _get_db()
        rows = conn.execute(
            "SELECT id, session_id, tool_name, args, result, created_at "
            "FROM tool_calls ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        records = [
            {"id": r[0], "session_id": r[1], "tool_name": r[2], "args": r[3], "result": r[4], "created_at": r[5]}
            for r in rows
        ]
        return {"status": "ok", "records": records}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ── Self-Learning endpoints ──

@app.get("/v1/memory/learnings")
async def search_learnings(q: str = Query(default="", min_length=0), limit: int = Query(default=20, ge=1, le=100)):
    """Search learned knowledge. Empty query returns recent high-confidence learnings."""
    try:
        if not MEMORY_DB.exists():
            return {"status": "ok", "learnings": []}
        conn = _get_db()
        if q.strip():
            safe_q = re.sub(r'[^\w\s"*]', '', q).strip()
            rows = []
            if safe_q:
                rows = conn.execute(
                    """SELECT l.id, l.topic, l.content, l.confidence, l.hit_count, l.source_type, l.created_at
                       FROM learnings l JOIN learnings_fts f ON l.rowid = f.rowid
                       WHERE learnings_fts MATCH ? ORDER BY l.confidence DESC LIMIT ?""",
                    (safe_q, limit),
                ).fetchall()
            # LIKE fallback for CJK text that FTS5 unicode61 tokenizer misses
            if not rows and q.strip():
                like_q = f"%{q.strip()}%"
                rows = conn.execute(
                    """SELECT id, topic, content, confidence, hit_count, source_type, created_at
                       FROM learnings WHERE topic LIKE ? OR content LIKE ?
                       ORDER BY confidence DESC LIMIT ?""",
                    (like_q, like_q, limit),
                ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, topic, content, confidence, hit_count, source_type, created_at
                   FROM learnings ORDER BY confidence DESC, updated_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        results = [
            {"id": r[0], "topic": r[1], "content": r[2], "confidence": r[3],
             "hit_count": r[4], "source_type": r[5], "created_at": r[6]}
            for r in rows
        ]
        return {"status": "ok", "learnings": results, "query": q}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/v1/memory/learn")
async def learn_from_conversation(request: Request):
    """Manually trigger knowledge extraction from a conversation."""
    body = await request.json()
    user_text = body.get("text", "")
    session_id = body.get("session_id", str(uuid.uuid4()))
    if not user_text.strip():
        return {"status": "error", "message": "No text provided"}
    count = _extract_learnings_heuristic(user_text, session_id)
    return {"status": "ok", "extracted": count, "session_id": session_id}


@app.post("/v1/memory/forget")
async def forget_learning(request: Request):
    """Delete a learning by id or topic. Also decrements confidence for corrections."""
    body = await request.json()
    lid = body.get("id", "")
    topic = body.get("topic", "")
    try:
        conn = _get_db()
        async with _async_db_lock:
            if lid:
                conn.execute("DELETE FROM learnings WHERE id = ?", (lid,))
            elif topic:
                conn.execute("DELETE FROM learnings WHERE topic = ?", (topic,))
            conn.commit()
        return {"status": "ok", "deleted": True}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/v1/memory/preferences")
async def get_preferences():
    """Get all learned user preferences."""
    try:
        prefs = _retrieve_preferences()
        return {"status": "ok", "preferences": prefs}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/v1/memory/reflections")
async def get_reflections(limit: int = Query(default=20, ge=1, le=100)):
    """Get recent tool execution reflections."""
    try:
        if not MEMORY_DB.exists():
            return {"status": "ok", "reflections": []}
        conn = _get_db()
        rows = conn.execute(
            """SELECT id, session_id, tool_name, tool_args, tool_result_summary, reflection, was_useful, created_at
               FROM reflections ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        results = [
            {"id": r[0], "session_id": r[1], "tool_name": r[2], "tool_args": r[3],
             "tool_result_summary": r[4], "reflection": r[5], "was_useful": bool(r[6]), "created_at": r[7]}
            for r in rows
        ]
        return {"status": "ok", "reflections": results}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/v1/skills")
async def get_skills():
    """Return loaded skill cards with enable/disable state. Injects Tavily as a built-in skill."""
    global _loaded_skills
    _loaded_skills = _load_skills()
    skills = [{"name": s["name"], "file": s["file"], "key": s["key"], "enabled": s.get("enabled", True)} for s in _loaded_skills]

    # Inject Tavily as a built-in skill (not a physical .md file)
    cfg = _load_skills_config()
    tavily_enabled = cfg.get("tavily_search", {}).get("enabled", True)
    # Check if API key is configured
    has_key = False
    try:
        if CONFIG_FILE.exists():
            config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            has_key = bool(config.get("tavily_api_key"))
    except Exception:
        pass
    skills.append({
        "name": "Web Search (Tavily)",
        "file": "sidecar/plugins/tavily_search.py",
        "key": "tavily_search",
        "enabled": tavily_enabled,
        "builtin": True,
        "has_api_key": has_key,
    })
    return {"status": "ok", "skills": skills}


@app.post("/v1/skills/{key}/toggle")
async def toggle_skill(key: str):
    """Toggle a skill enabled/disabled. Works for both physical .md skills and built-in skills (tavily_search)."""
    cfg = _load_skills_config()
    if key not in cfg:
        cfg[key] = {"enabled": True}
    cfg[key]["enabled"] = not cfg[key].get("enabled", True)
    _save_skills_config(cfg)
    # Reload so next request picks up changes
    global _loaded_skills
    _loaded_skills = _load_skills()
    return {"status": "ok", "key": key, "enabled": cfg[key]["enabled"]}


@app.post("/v1/skills")
async def create_skill(request: Request):
    """Create a new skill .md file."""
    body = await request.json()
    name = body.get("name", "").strip()
    content = body.get("content", "").strip()
    if not name or not content:
        return {"status": "error", "message": "Name and content required"}
    key = re.sub(r'[^a-z0-9-]', '', name.lower().replace(" ", "-"))[:40]
    filepath = SKILLS_DIR / f"{key}.md"
    if filepath.exists():
        return {"status": "error", "message": "Skill already exists"}
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    filepath.write_text(content, encoding="utf-8")
    global _loaded_skills
    _loaded_skills = _load_skills()
    return {"status": "ok", "skill": {"name": name, "file": f"{key}.md", "key": key, "enabled": True}}


@app.delete("/v1/skills/{key}")
async def delete_skill(key: str):
    """Delete a skill .md file. Built-in skills cannot be deleted."""
    if key == "tavily_search":
        return {"status": "error", "message": "Built-in skill cannot be deleted"}
    filepath = SKILLS_DIR / f"{key}.md"
    if not filepath.exists():
        return {"status": "error", "message": "Skill not found"}
    filepath.unlink()
    cfg = _load_skills_config()
    cfg.pop(key, None)
    _save_skills_config(cfg)
    global _loaded_skills
    _loaded_skills = _load_skills()
    return {"status": "ok"}


# ── Tavily API Key management endpoints ──


@app.get("/v1/settings/tavily-key")
async def get_tavily_key():
    """Get Tavily API key status (masked, never returns full key). Reads from keychain first, then config.json."""
    key = ""
    # Try macOS Keychain first
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "com.latiao.desktop", "-a", "tavily_api_key", "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            key = result.stdout.strip()
    except Exception:
        pass
    # Fallback to config.json
    if not key:
        try:
            if CONFIG_FILE.exists():
                cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                key = cfg.get("tavily_api_key", "")
        except Exception:
            pass
    if key:
        masked = key[:7] + "••••" + key[-4:] if len(key) > 11 else "••••"
        return {"status": "ok", "has_key": True, "masked": masked}
    return {"status": "ok", "has_key": False, "masked": None}


@app.post("/v1/settings/tavily-key")
async def set_tavily_key(request: Request):
    """Save Tavily API key to macOS Keychain (primary) + config.json (fallback)."""
    body = await request.json()
    key = body.get("key", "").strip()
    if not key:
        return {"status": "error", "message": "API key is required"}
    try:
        # Store in macOS Keychain
        subprocess.run(
            ["security", "add-generic-password", "-s", "com.latiao.desktop", "-a", "tavily_api_key", "-w", key, "-U"],
            capture_output=True, timeout=10,
        )
        # Also update config.json for backward compatibility
        cfg = {}
        if CONFIG_FILE.exists():
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        cfg["tavily_api_key"] = key
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        masked = key[:7] + "••••" + key[-4:] if len(key) > 11 else "••••"
        return {"status": "ok", "has_key": True, "masked": masked}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.delete("/v1/settings/tavily-key")
async def delete_tavily_key():
    """Remove Tavily API key from keychain and config."""
    try:
        subprocess.run(
            ["security", "delete-generic-password", "-s", "com.latiao.desktop", "-a", "tavily_api_key"],
            capture_output=True, timeout=5,
        )
    except Exception:
        pass
    try:
        if CONFIG_FILE.exists():
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            cfg.pop("tavily_api_key", None)
            CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        return {"status": "ok", "has_key": False}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/v1/memory/stats")
async def memory_stats():
    """Get self-learning statistics."""
    try:
        if not MEMORY_DB.exists():
            return {"status": "ok", "stats": {}}
        conn = _get_db()
        learnings_count = conn.execute("SELECT COUNT(*) FROM learnings").fetchone()[0]
        prefs_count = conn.execute("SELECT COUNT(*) FROM preferences").fetchone()[0]
        reflections_count = conn.execute("SELECT COUNT(*) FROM reflections").fetchone()[0]
        tool_calls_count = conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
        avg_confidence = conn.execute("SELECT AVG(confidence) FROM learnings").fetchone()[0] or 0
        return {"status": "ok", "stats": {
            "learnings": learnings_count,
            "preferences": prefs_count,
            "reflections": reflections_count,
            "tool_calls": tool_calls_count,
            "avg_learning_confidence": round(avg_confidence, 3),
        }}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/v1/memory/session-progress")
async def get_session_progress(session_id: str = Query(default="")):
    """Get agent state transition history for stagnation analysis."""
    if session_id and session_id in _session_states:
        s = _session_states[session_id]
        return {"status": "ok", "session_id": session_id, "phase": s["phase"],
                "round": s["round"], "stalled_rounds": s["stalled_rounds"],
                "last_action": s["last_action"], "history": s["history"]}
    sessions = {}
    for sid, s in list(_session_states.items())[-10:]:
        sessions[sid] = {"phase": s["phase"], "round": s["round"],
                         "stalled_rounds": s["stalled_rounds"], "last_action": s["last_action"]}
    return {"status": "ok", "sessions": sessions}


@app.get("/health")
async def health():
    return {"status": "ok", "mode": "stateless"}


@app.get("/v1/logs")
async def get_logs(limit: int = Query(default=100, ge=1, le=500)):
    """Return recent application log entries."""
    logs = list(_log_buffer)
    return {"status": "ok", "logs": logs[-limit:]}


@app.get("/v1/heartbeat")
async def heartbeat():
    """Unified polling endpoint: returns downloads, LLM status, and learnings in one call."""
    return {
        "status": "ok",
        "downloads": list(local_llm._engine._downloads.values()),
        "local_llm": local_llm.get_status(),
        "learnings": _get_recent_learnings(10),
    }


@app.post("/v1/confirm_tool")
async def confirm_tool(request: Request):
    """Frontend sends tool confirmation decision."""
    body = await request.json()
    call_id = body.get("call_id", "")
    approved = body.get("approved", False)

    async with _pending_lock:
        entry = _pending_confirmations.get(call_id)
        if entry:
            entry["approved"] = approved
            entry["event"].set()
            return {"status": "ok", "call_id": call_id, "approved": approved}
    return {"status": "not_found", "message": f"No pending confirmation for call_id: {call_id}"}


# ═══════════════════════════════════════════════════════
#  Cron Job Scheduler
# ═══════════════════════════════════════════════════════

def _load_cron() -> list[dict]:
    """Load cron jobs from disk."""
    try:
        if CRON_FILE.exists():
            return json.loads(CRON_FILE.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("Failed to load cron jobs", exc_info=True)
    return []


def _save_cron(jobs: list[dict]):
    """Save cron jobs to disk."""
    PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
    CRON_FILE.write_text(json.dumps(jobs, indent=2, ensure_ascii=False), encoding="utf-8")


_cron_jobs: list[dict] = []
_cron_lock = threading.Lock()  # protects concurrent read/write to _cron_jobs
_cron_last_run: dict[str, str] = {}  # job_id → last run timestamp


def _cron_field_matches(field: str, value: int, dow_value: int = -1) -> bool:
    """Check if a single cron field matches the current value. Supports *, */N, N, N,M,O."""
    if field == "*":
        return True
    # Handle comma-separated: "9,17"
    if "," in field:
        return any(_cron_field_matches(f.strip(), value, dow_value) for f in field.split(","))
    # Handle step: "*/15"
    if field.startswith("*/"):
        interval = int(field[2:])
        return value % interval == 0
    # Handle range: "9-17"
    if "-" in field:
        lo, hi = field.split("-", 1)
        return int(lo) <= value <= int(hi)
    # Single value
    if field.isdigit():
        return value == int(field)
    return False


def _cron_matches(cron_expr: str, now: datetime) -> bool:
    """Standard 5-field cron expression matcher. Minute Hour DayOfMonth Month DayOfWeek."""
    try:
        parts = cron_expr.strip().split()
        if len(parts) != 5:
            return False
        minute, hour, dom, month, dow = parts
        if not _cron_field_matches(minute, now.minute):
            return False
        if not _cron_field_matches(hour, now.hour):
            return False
        if not _cron_field_matches(dom, now.day):
            return False
        if not _cron_field_matches(month, now.month):
            return False
        # Day-of-week: cron uses 0-7 (0=Sunday, 7=Sunday), Python uses 0=Monday
        py_wday = (now.weekday() + 1) % 7  # Convert to cron DOW (0=Sun)
        return _cron_field_matches(dow, py_wday, now.weekday())
    except Exception:
        return False


def _tick_cron():
    """Check all enabled cron jobs, return list of due jobs."""
    now = datetime.now()
    due = []
    with _cron_lock:
        for job in _cron_jobs:
            if not job.get("enabled", True):
                continue
            job_id = job["id"]
            last = _cron_last_run.get(job_id, "")
            now_str = now.strftime("%Y-%m-%d %H:%M")
            if last == now_str:
                continue  # Already ran this minute
            if _cron_matches(job["schedule"], now):
                _cron_last_run[job_id] = now_str
                due.append(job)
    return due


# ── Cron API endpoints ──

@app.get("/v1/cron")
async def get_cron_jobs():
    """List all cron jobs."""
    with _cron_lock:
        jobs = list(_cron_jobs)
    return {"status": "ok", "jobs": jobs}


@app.post("/v1/cron")
async def create_cron_job(request: Request):
    """Create a new cron job."""
    global _cron_jobs
    body = await request.json()
    job = {
        "id": str(uuid.uuid4()),
        "schedule": body.get("schedule", "0 9 * * *"),
        "task": body.get("task", "新建任务"),
        "action": body.get("action", "notify"),  # notify | execute
        "enabled": body.get("enabled", True),
        "created_at": datetime.now().isoformat(),
    }
    with _cron_lock:
        _cron_jobs.append(job)
        _save_cron(_cron_jobs)
    return {"status": "ok", "job": job}


@app.put("/v1/cron/{job_id}")
async def update_cron_job(job_id: str, request: Request):
    """Update a cron job."""
    global _cron_jobs
    body = await request.json()
    with _cron_lock:
        for job in _cron_jobs:
            if job["id"] == job_id:
                if "schedule" in body:
                    job["schedule"] = body["schedule"]
                if "task" in body:
                    job["task"] = body["task"]
                if "enabled" in body:
                    job["enabled"] = body["enabled"]
                if "action" in body:
                    job["action"] = body["action"]
                _save_cron(_cron_jobs)
                return {"status": "ok", "job": job}
    return {"status": "error", "message": "Job not found"}


@app.delete("/v1/cron/{job_id}")
async def delete_cron_job(job_id: str):
    """Delete a cron job."""
    global _cron_jobs
    with _cron_lock:
        _cron_jobs = [j for j in _cron_jobs if j["id"] != job_id]
        _save_cron(_cron_jobs)
    return {"status": "ok"}


@app.post("/v1/cron/{job_id}/toggle")
async def toggle_cron_job(job_id: str):
    """Toggle a cron job enabled/disabled."""
    global _cron_jobs
    with _cron_lock:
        for job in _cron_jobs:
            if job["id"] == job_id:
                job["enabled"] = not job.get("enabled", True)
                _save_cron(_cron_jobs)
                return {"status": "ok", "job": job}
    return {"status": "error", "message": "Job not found"}


@app.get("/v1/cron/due")
async def get_due_jobs():
    """Check and return currently due cron jobs."""
    due = _tick_cron()
    with _cron_lock:
        total = len(_cron_jobs)
    return {"status": "ok", "due": due, "total_jobs": total}


# ── Local LLM Engine endpoints ──

@app.get("/v1/local-llm/setup")
async def local_llm_setup():
    """Check system environment and report missing dependencies."""
    return local_llm.check_setup()


@app.get("/v1/local-llm/detect")
async def local_llm_detect():
    """Auto-detect system environment and recommend config."""
    return local_llm.detect_system()


@app.get("/v1/local-llm/search")
async def local_llm_search(q: str = Query(default=""), library: str = Query(default=""), limit: int = Query(default=20, le=30)):
    """Search HuggingFace for models. Empty q returns trending models."""
    results = local_llm.search_huggingface(q, limit, library) if q else local_llm.search_huggingface("gguf", limit, library)
    return {"status": "ok", "results": results, "query": q}


@app.post("/v1/local-llm/fix")
async def local_llm_fix(request: Request):
    """Execute a fix for an environment issue."""
    body = await request.json()
    fix_type = body.get("fix_type", "")
    fix_pkg = body.get("fix_pkg", "")
    return local_llm.run_fix(fix_type, fix_pkg)


@app.post("/v1/local-llm/download")
async def local_llm_download(request: Request):
    """Download a model from HuggingFace."""
    body = await request.json()
    model_id = body.get("model_id", "")
    if not model_id:
        return {"status": "error", "message": "model_id required"}
    return local_llm.download_model(model_id)


@app.get("/v1/local-llm/downloads")
async def local_llm_downloads():
    """Get all download states."""
    return local_llm.get_all_downloads()


@app.post("/v1/local-llm/download/pause")
async def local_llm_pause(request: Request):
    body = await request.json()
    return local_llm.pause_download(body.get("model_id", ""))


@app.post("/v1/local-llm/download/resume")
async def local_llm_resume(request: Request):
    body = await request.json()
    return local_llm.resume_download(body.get("model_id", ""))


@app.post("/v1/local-llm/download/cancel")
async def local_llm_cancel(request: Request):
    body = await request.json()
    return local_llm.cancel_download(body.get("model_id", ""))


@app.post("/v1/local-llm/download/clear")
async def local_llm_clear(request: Request):
    body = await request.json()
    return local_llm.clear_downloads(body.get("status", ""))


@app.post("/v1/local-llm/open-path")
async def local_llm_open_path(request: Request):
    """Open a path in Finder/Explorer."""
    body = await request.json()
    path = body.get("path", "")
    if not path:
        # No path specified — open the Models directory so user can browse local files
        return local_llm.open_path(str(Path.home() / "Models"))
    return local_llm.open_path(path)


@app.get("/v1/local-llm/status")
async def local_llm_status():
    """Get local LLM engine status."""
    return local_llm.get_status()


@app.get("/v1/local-llm/models")
async def local_llm_models():
    """List downloaded local models."""
    return {"status": "ok", "models": local_llm.list_local_models()}


@app.get("/v1/local-llm/model-detail")
async def local_llm_model_detail(model_id: str = Query(..., min_length=1)):
    """Fetch HuggingFace model detail: metadata, files, README."""
    return local_llm.get_model_detail(model_id)


@app.get("/v1/local-llm/recommended")
async def local_llm_recommended():
    """List recommended models with download status."""
    return {"status": "ok", "models": local_llm.get_recommended_models(), "backend": local_llm.get_backend()}


@app.get("/v1/local-llm/estimate-context")
async def local_llm_estimate_context(model_path: str = Query(default="")):
    """Estimate max context based on available memory and model size."""
    return local_llm.estimate_max_context(model_path)


@app.post("/v1/local-llm/context-limit")
async def local_llm_set_context(request: Request):
    """Set context limit (applies to next model start)."""
    body = await request.json()
    limit = body.get("limit", 8192)
    return local_llm.set_context_limit(int(limit))


@app.get("/v1/local-llm/context-limit")
async def local_llm_get_context():
    """Get current context limit."""
    return {"status": "ok", "context_limit": local_llm._engine.model_token_limit}


@app.post("/v1/local-llm/start")
async def local_llm_start(request: Request):
    """Start a local model."""
    body = await request.json()
    model_id = body.get("model_id", "")
    port = body.get("port", 1235)
    if not model_id:
        return {"status": "error", "message": "model_id required"}
    result = local_llm.start_model(model_id, port)
    return result


@app.post("/v1/local-llm/stop")
async def local_llm_stop():
    """Stop the running local model."""
    return local_llm.stop_model()


@app.post("/v1/local-llm/delete-model")
async def local_llm_delete_model(request: Request):
    """Delete a local model file from ~/Models/ or download cache."""
    body = await request.json()
    model_id = body.get("model_id", "")
    if not model_id:
        return {"status": "error", "message": "model_id required"}
    return local_llm.delete_model_file(model_id)

# ── Seed default cron jobs ──

def _seed_default_cron():
    """Create default cron jobs if cron.json is empty."""
    global _cron_jobs
    _cron_jobs = _load_cron()
    if not _cron_jobs:
        _cron_jobs = [
            {"id": str(uuid.uuid4()), "schedule": "0 9 * * *", "task": "📋 每日摘要 (记录到记忆库)", "action": "notify", "enabled": True, "created_at": datetime.now().isoformat()},
            {"id": str(uuid.uuid4()), "schedule": "*/30 * * * *", "task": "🔍 健康检查 (记录到记忆库)", "action": "notify", "enabled": True, "created_at": datetime.now().isoformat()},
            {"id": str(uuid.uuid4()), "schedule": "0 18 * * 5", "task": "📊 每周汇总 (记录到记忆库)", "action": "notify", "enabled": False, "created_at": datetime.now().isoformat()},
        ]
        _save_cron(_cron_jobs)


async def _execute_cron_job(job: dict):
    """Execute a due cron job: run the task through the agent loop with tools enabled."""
    task = job.get("task", "")
    action = job.get("action", "notify")
    logger.info("Cron job triggered: %s — %s", task, action)

    # Build messages with identity, env, and tools enabled
    home = str(Path.home())
    cwd = os.getcwd()
    now = datetime.now().strftime("%Y-%m-%d (%A) %H:%M:%S")
    agent_cfg = _get_agent_config("latiao")

    messages = [{"role": "system", "content": (
        f"{agent_cfg['identity']}\n\n"
        f"Runtime environment:\n"
        f"- Current time: {now}\n"
        f"- User home directory: {home}\n"
        f"- Current working directory: {cwd}\n"
        f"- OS: macOS (Darwin)\n"
        f"- Shell: zsh\n\n"
        f"你正在执行一个定时任务。使用可用的工具来完成这个任务。"
        f"执行完毕后输出总结。"
    )}]
    messages.append({"role": "user", "content": f"定时任务: {task}"})

    # Use non-streaming agent loop to execute the task
    protocol, api_url, headers, _ = _resolve_api_target(None)
    if not api_url:
        logger.warning("Cron job: no API target available")
        return
    model = SUBAGENT_MODEL
    agent_tools = _get_agent_tools("latiao", TOOLS)
    active_tools = _filter_tools(task, agent_tools)
    if len(active_tools) > 7:
        essential = {"read_file", "write_file", "list_dir"}
        priority = [t for t in active_tools if t.get("function", {}).get("name") in essential]
        others = [t for t in active_tools if t.get("function", {}).get("name") not in essential]
        active_tools = priority + others[:max(0, 5 - len(priority))]

    current_msgs = [dict(m) for m in messages]
    full_content = ""
    tool_count = 0
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120)) as client:
            for _ in range(10):  # max 10 iterations for cron
                resp = await client.post(api_url, json={
                    "model": model, "messages": current_msgs,
                    "tools": active_tools, "tool_choice": "auto",
                    "max_tokens": 2048, "stream": False,
                }, headers=headers)
                resp_data = resp.json()
                choices = resp_data.get("choices", [])
                if not choices:
                    break
                msg = choices[0].get("message", {})
                content = msg.get("content", "") or ""
                tc_data = msg.get("tool_calls", [])

                if not tc_data and content and _NATIVE_TOOL_RE.search(content):
                    native_tcs = _parse_native_tool_calls(content)
                    if native_tcs:
                        content = _strip_native_tool_calls(content)
                        tc_data = native_tcs

                if tc_data:
                    tool_count += 1
                    current_msgs.append({"role": "assistant", "content": content or None, "tool_calls": tc_data})
                    for tc in tc_data:
                        tool_name = tc.get("function", {}).get("name", "")
                        tool_args_str = tc.get("function", {}).get("arguments", "{}")
                        try:
                            tool_args = json.loads(tool_args_str) if isinstance(tool_args_str, str) else tool_args_str
                        except json.JSONDecodeError:
                            tool_args = {}
                        perm = _resolve_permission(tool_name, tool_args)
                        if perm in ("confirm", "danger"):
                            result = f"⛔ Cron 任务不支持需要确认的操作: {tool_name}"
                        else:
                            result = await execute_tool(tool_name, tool_args)
                        if len(result) > 3000:
                            result = result[:3000] + "\n...(截断)"
                        current_msgs.append({"role": "tool", "tool_call_id": tc.get("id", "cron"), "content": result})
                    continue

                if content:
                    full_content += content
                elif full_content:
                    # Already have content from earlier iterations, stop
                    break
                else:
                    # Empty response from model — retry once
                    logger.warning("Cron job: empty response, retrying")
                    current_msgs.append({
                        "role": "system",
                        "content": "⚠️ 你上一轮的回复是空的。请直接回复总结或使用工具完成任务。",
                    })
                    if len(current_msgs) > 15:
                        # Prevent infinite loop — give up after too many messages
                        full_content = "(Cron 任务未生成有效回复)"
                        break
                    continue
        ai_content = full_content or "(无输出)"
    except Exception as e:
        ai_content = f"[Cron 任务执行失败: {e}]"
        logger.warning("Cron LLM call failed: %s", e)

    # Record to memory DB with AI result
    try:
        conn = _get_db()
        async with _async_db_lock:
            conn.execute(
                "INSERT INTO memory (session_id, type, topic, content, meta) VALUES (?, ?, ?, ?, ?)",
                ("cron", "cron_job", task,
                 f"Cron: {task}\\n执行时间: {datetime.now().isoformat()}\\n\\nAI 分析结果:\\n{ai_content}",
                 json.dumps({"action": action, "schedule": job.get("schedule"), "ai_result": ai_content[:200]})),
            )
            conn.commit()
    except Exception:
        logger.warning("Failed to record cron job to memory DB", exc_info=True)

    logger.info("Cron job completed: %s", task[:50])


async def _cron_loop():
    """Background task: tick cron every 60 seconds."""
    while True:
        try:
            await asyncio.sleep(60)
            due = _tick_cron()
            for job in due:
                await _execute_cron_job(job)
        except Exception:
            logger.warning("Cron loop error", exc_info=True)



@app.post("/v1/identity/open/{agent_id}")
async def api_open_identity(agent_id: str, section: str = ""):
    """Open the agent identity file (or section file) with the system default editor."""
    agents_dir = Path(__file__).resolve().parent / "agents"
    if section:
        agent_file = (agents_dir / f"{agent_id}_{section}.txt").resolve()
        if not agent_file.exists():
            agent_file.write_text(f"# {agent_id} - {section}\n\n（此部分内容待补充）\n")
    else:
        agent_file = (agents_dir / f"{agent_id}.txt").resolve()
    # Path traversal protection
    if not str(agent_file).startswith(str(agents_dir.resolve()) + "/"):
        return {"status": "error", "message": "Invalid agent_id"}
    if not agent_file.exists():
        return {"status": "error", "message": f"Not found: {agent_id}" + (f"_{section}" if section else "")}
    try:
        import subprocess
        subprocess.Popen(["open", str(agent_file)])
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
