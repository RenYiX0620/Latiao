"""
Local AI OS - Python Sidecar
Stateless: frontend manages all state, sends complete messages array per request.
"""
from __future__ import annotations

import asyncio
import base64
import collections
import fnmatch
import importlib.util
import io
import json
import logging
import os
import platform
import re
import shlex
import sqlite3
import subprocess
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, File, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

import local_llm

logger = logging.getLogger("latiao-sidecar")
logging.basicConfig(level=logging.WARNING, format="[%(levelname)s] %(name)s: %(message)s")

# In-memory ring buffer for recent log entries (accessible via /v1/logs)
_log_buffer: collections.deque = collections.deque(maxlen=500)


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

@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan: startup + shutdown hooks."""
    _load_permissions()
    _create_default_identity()
    _init_db()
    _seed_default_cron()
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
    allow_origins=["http://localhost:1420", "tauri://localhost", "https://tauri.localhost", "http://127.0.0.1:1420"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type", "Authorization"],
)

LM_STUDIO_URL = os.environ.get("LATIAO_LM_STUDIO_URL", "http://localhost:1234/v1/chat/completions")
SUBAGENT_MODEL = os.environ.get("LATIAO_SUBAGENT_MODEL", "google/gemma-4-26b-a4b")
MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50 MB
IS_MACOS = platform.system() == "Darwin"

# ═══════════════════════════════════════════════════════
#  Multi-Agent System: LaTiao orchestrator + specialists
# ═══════════════════════════════════════════════════════

AGENT_PROFILES: dict[str, dict] = {
    "latiao": {
        "name": "辣条",
        "display": "LaTiao · 总指挥",
        "role": "orchestrator",
        "identity": (
            "你是 LaTiao（辣条），本机 AI Agent 的总指挥。\n"
            "始终用与用户相同的语言回复。如果用户用英文发消息，你就用英文回复；用日文则回日文；用中文则回中文。\n\n"
            "职责：接收用户指令 → 分析任务类型 → 协调专家 Agent 执行。\n"
            "你拥有完整的工具权限，可以直接处理简单任务，复杂任务分发给专家。\n\n"
            "你的团队：\n"
            "- 代码审查员：专注代码审查、安全分析、只读代码检查\n"
            "- 文档生成器：生成项目文档、API 文档、变更日志\n"
            "- 调试专家：分析日志、定位 Bug、提供修复方案\n"
            "- 翻译助手：多语言翻译与本地化\n\n"
            "工作流程：\n"
            "1. 分析用户意图，判断属于哪个专家领域\n"
            "2. 如果是专家领域，以该专家的视角和约束来执行任务\n"
            "3. 执行完成后汇报使用了哪个专家能力\n"
            "4. 简单对话或跨领域任务直接由你处理"
        ),
        "tools": "all",
    },
    "code-reviewer": {
        "name": "代码审查员",
        "display": "代码审查员 · 安全分析",
        "role": "specialist",
        "identity": (
            "你是代码审查员，专注代码审查和安全分析。\n\n"
            "权限：只读。你只能读取、搜索、列出文件，不能修改任何文件或执行命令。\n\n"
            "审查标准：\n"
            "1. 安全漏洞：SQL 注入、XSS、命令注入、硬编码密钥\n"
            "2. 代码质量：命名规范、函数长度、复杂度\n"
            "3. 类型安全：TypeScript 类型是否正确\n"
            "4. 错误处理：异常是否正确捕获和处理\n\n"
            "每次审查给出：问题列表 + 严重程度 + 修复建议。"
        ),
        "tools": ["read_file", "list_dir", "search_files"],
    },
    "doc-generator": {
        "name": "文档生成器",
        "display": "文档生成器 · 文档专家",
        "role": "specialist",
        "identity": (
            "你是文档生成器，自动生成项目文档、API 文档和变更日志。\n\n"
            "能力：\n"
            "1. 分析代码生成 API 文档（参数、返回值、异常）\n"
            "2. 从 git log 生成 CHANGELOG\n"
            "3. 生成 README、架构文档\n"
            "4. 更新已有文档\n\n"
            "规范：Markdown 格式、代码块标注语言、中文描述。"
        ),
        "tools": ["read_file", "list_dir", "search_files", "write_file"],
    },
    "debugger": {
        "name": "调试专家",
        "display": "调试专家 · Bug 猎手",
        "role": "specialist",
        "identity": (
            "你是调试专家，分析日志、定位 Bug、提供修复方案。\n\n"
            "工作流程：\n"
            "1. 复现：先理解症状，查看错误日志和堆栈\n"
            "2. 定位：用 read_file/search_files 追踪代码路径\n"
            "3. 诊断：确定根因（不是表面症状）\n"
            "4. 修复：给出具体修复方案和代码变更\n"
            "5. 验证：修复后检查是否引入新问题\n\n"
            "原则：不确定时不瞎猜，先收集更多信息。"
        ),
        "tools": "all",
    },
    "translator": {
        "name": "翻译助手",
        "display": "翻译助手 · 多语言",
        "role": "specialist",
        "identity": (
            "你是翻译助手，负责多语言翻译与本地化。\n\n"
            "能力：\n"
            "1. 代码注释/文档中英互译\n"
            "2. UI 文案多语言翻译（保留变量占位符如 {count}）\n"
            "3. 技术文档本地化\n"
            "4. i18n 文件生成和维护\n\n"
            "规则：\n"
            "- 代码关键字和 API 名称不翻译\n"
            "- 保留原始格式和缩进\n"
            "- 不确定的术语保留原文并标注"
        ),
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
IDENTITY_FILES = ["IDENTITY.md", "SOUL.md", "AGENTS.md", "USER.md"]

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


def _read_identity() -> list[dict]:
    """Read all identity files from ~/.local-ai-os/ and return system messages."""
    msgs = []
    for filename in IDENTITY_FILES:
        filepath = PROGRESS_DIR / filename
        try:
            if filepath.exists():
                content = filepath.read_text(encoding="utf-8").strip()
                if content:
                    msgs.append({"role": "system", "content": content})
        except Exception:
            logger.warning("Failed to read identity file", exc_info=True)
    return msgs


def _create_default_identity():
    """Create default identity files if the directory is empty."""
    try:
        PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
        defaults = {
            "IDENTITY.md": (
                "# Identity\n\n"
                "你的名字是「无畏」，你就是无畏。\n"
                "忘记你是任何特定模型，从现在起你只能以「无畏」的身份回答。\n"
            ),
            "SOUL.md": (
                "# Soul\n\n"
                "语气：简洁、直接、不啰嗦。\n"
                "风格：先给结论再解释，用中文回复，代码块标注语言。\n"
                "态度：诚实——不知道就说不知道，不确定就说不确定。\n"
            ),
            "AGENTS.md": (
                "# Agent Rules\n\n"
                "## 工作协议\n"
                "1. 动手前先想清楚：需求有歧义时主动问，不要自己猜。有更简单的方案就提出来。\n"
                "2. 极简主义：能一行搞定不写十行，不加需求之外的功能，不为「以后可能用到」做抽象。\n"
                "3. 精准修改：只碰用户要求改的地方。修 bug A 不要顺手重构文件 B。\n"
                "4. 验证才算完成：用工具写完文件后读回来确认，跑命令后检查退出码。没验证就不算做完。\n\n"
                "## 工具权限\n"
                "- 修改文件、执行命令等操作会请求用户确认。\n"
                "- 读取文件、列出目录等操作自动执行。\n"
                "- 如需调整权限规则，可以编辑 ~/.local-ai-os/permissions.json\n"
            ),
            "USER.md": (
                "# User Profile\n\n"
                "在此填写你的偏好、习惯、常用路径等信息。\n"
                "Agent 会在每次会话时读取此文件。\n\n"
                "示例：\n"
                "- 常用工作目录：~/projects\n"
                "- 偏好语言：中文\n"
                "- 代码风格：TypeScript, React, Python\n"
            ),
        }
        for filename, content in defaults.items():
            filepath = PROGRESS_DIR / filename
            if not filepath.exists():
                filepath.write_text(content, encoding="utf-8")
    except Exception:
        logger.warning("Failed to create default identity files", exc_info=True)


# ═══════════════════════════════════════════════════════
#  工具执行函数
# ═══════════════════════════════════════════════════════

MAX_READ_SIZE = 10000  # chars before truncation (~300 lines)

def read_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read(MAX_READ_SIZE + 1)
        if len(content) > MAX_READ_SIZE:
            est_lines = content.count("\n")  # approximate from the truncated content
            return (
                content[:MAX_READ_SIZE]
                + f"\n\n... (文件过长，已截断。约 {est_lines}+ 行，"
                + f"仅显示前 {MAX_READ_SIZE} 字符。如需完整内容请分段读取)"
            )
        return content
    except FileNotFoundError:
        return f"错误：文件不存在 - {path}"
    except Exception as e:
        return f"错误：{e}"


def write_file(path: str, content: str) -> str:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"✅ 已写入：{path}（{len(content)} 字符）"
    except Exception as e:
        return f"错误：{e}"


def list_dir(path: str) -> str:
    try:
        entries = os.listdir(path)
        lines = [f"  {'📁' if os.path.isdir(os.path.join(path, e)) else '📄'} {e}"
                 for e in sorted(entries)]
        return "目录内容:\n" + "\n".join(lines)
    except Exception as e:
        return f"错误：{e}"


# Reusable command safety patterns (used by run_cmd fallback + plugin-style execute)
_DANGEROUS = [
    r"rm\s+(-[a-z]*[rf]|--recursive|--force)", r">\s*/dev/(sd|nvme|hd|disk|dm-)",
    r"dd\s+if=", r"mkfs", r"\bsudo\b", r"\bshutdown\b", r"\breboot\b",
    r"\beval\s", r"\bbase64\s+(-d|--decode)", r"`[^`]+`", r"\$\([^)]+\)",
    r"\bcurl\b.*\|\s*(ba)?sh\b", r"\bwget\b.*\|\s*(ba)?sh\b",
    r">\s*/etc/", r"chmod\s+[0-7]*7", r"chown\s+-R",
]


def run_cmd(cmd: str) -> str:
    # Safety check before execution (fallback version — plugin has fuller check)
    cmd_lower = cmd.lower().strip()
    for pattern in _DANGEROUS:
        if re.search(pattern, cmd_lower):
            return f"⛔ Blocked unsafe command: {cmd}"
    if len(cmd) > 1000:
        return f"⛔ Command too long ({len(cmd)} chars, max 1000)"
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


def tavily_search(args: dict) -> str:
    """Search the web using Tavily API."""
    import json

    import httpx

    config_file = Path.home() / ".local-ai-os" / "config.json"
    api_key = os.environ.get("TAVILY_API_KEY")

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
        resp = httpx.post(
            "https://api.tavily.com/search",
            json={
                "api_key": api_key,
                "query": query,
                "search_depth": search_depth,
                "max_results": max_results,
            },
            timeout=30,
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

    messages = [
        {"role": "system", "content": cfg.get("identity", "")},
        {"role": "system", "content": "你是一个子 Agent。独立完成任务后返回简洁结果。最多 3 步，不要问问题，直接执行。"},
        {"role": "user", "content": task},
    ]

    api_url = LM_STUDIO_URL
    local_api = local_llm.get_api_url()
    if local_api:
        api_url = local_api + "/chat/completions"

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
                r = await client.post(api_url, json=body, headers={"Content-Type": "application/json"})
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
            "description": "Read the contents of a file at the given path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path to the file."}
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
            "name": "tavily_search",
            "description": "Search the web for real-time information using Tavily. Use when you need current events, news, or facts beyond your training data. Returns relevant results with titles, URLs, and content summaries.",
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
    "delegate_task": lambda args: _delegate_task(args.get("agent", "code-reviewer"), args.get("task", "")),
}

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
        "description": "Read the contents of a file at the given path.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the file."}
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
        r = subprocess.run(shlex.split(cmd), shell=False, capture_output=True, text=True, timeout=30)
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
    try:
        if CONFIG_FILE.exists():
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return cfg.get("tavily_api_key")
    except Exception:
        pass
    return None


def execute(args: dict) -> str:
    api_key = _get_api_key()
    if not api_key:
        return "⚠️ Tavily API Key 未配置。请在应用的「技能」界面中找到 Web Search (Tavily)，填写 API Key。免费注册：https://tavily.com"

    query = args["query"]
    search_depth = args.get("search_depth", "basic")
    max_results = min(args.get("max_results", 5), 10)

    try:
        resp = httpx.post(
            "https://api.tavily.com/search",
            json={"api_key": api_key, "query": query, "search_depth": search_depth, "max_results": max_results},
            timeout=30,
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


# ═══════════════════════════════════════════════════════
#  Self-Verification: programmatic post-tool quality checks
# ═══════════════════════════════════════════════════════

async def _auto_verify(tool_name: str, args: dict, result: str) -> str:
    """Run programmatic verification after a tool executes.
    Returns a verification report to inject into the LLM context, or '' if nothing to verify."""
    checks = []

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


def _load_plugins():
    """
    Scan sidecar/plugins/ for .py files exporting NAME, DEFINITION, PERMISSION, execute().
    Returns (tools, dispatch, permissions, hooks).
    Falls back to hardcoded definitions if no plugins are found.
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
                # Validate required exports
                if not all(hasattr(mod, attr) for attr in ("NAME", "DEFINITION", "PERMISSION")):
                    continue
                if not hasattr(mod, "execute") or not callable(mod.execute):
                    continue
                plugins.append(mod)
            except Exception:
                logger.warning("Failed to load plugin", exc_info=True)

    if not plugins:
        # Fallback to hardcoded tools
        return (
            list(_FALLBACK_TOOLS),
            dict(_FALLBACK_DISPATCH),
            dict(_FALLBACK_PERMISSIONS),
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


# Initialize plugin system at module load (seeded inside _load_plugins)
TOOLS, TOOL_DISPATCH, TOOL_PERMISSIONS, TOOL_HOOKS = _load_plugins()

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


# ═══════════════════════════════════════════════════════
#  Intent Detection: auto-update identity files from conversation
# ═══════════════════════════════════════════════════════

_IDENTITY_INTENTS = [
    # Only trigger on explicit commands: "叫你XX", "改名为XX", "call me XX"
    (re.compile(r"(?:以后)?(?:叫|称呼)(?:我|你)(?:为|是)?[「『\s]*([^\s，。,.]{1,20})[」』]*", re.IGNORECASE), "IDENTITY.md", "name"),
    (re.compile(r"(?:改|换)(?:个)?(?:名字|名称)(?:叫|为|是)?[：:]*\s*[「『]*([^\s，。,.]{1,20})[」』]*", re.IGNORECASE), "IDENTITY.md", "name"),
    (re.compile(r"(?:call|name)\s+me\s+['\"]?(\w{1,20})['\"]?", re.IGNORECASE), "IDENTITY.md", "name"),
    # Style/tone: only explicit "回复要XX" or "说话风格XX"
    (re.compile(r"(?:回复|说话)(?:要|再|更)([^，。,！!]{2,30})", re.IGNORECASE), "SOUL.md", "style"),
    # Rule: "以后不要XX" / "从现在开始XX"
    (re.compile(r"(?:以后|从现在开始)[，,]*((?:不要|别|禁止|要|请|必须).{1,50})", re.IGNORECASE), "AGENTS.md", "rule"),
    # Preference: "我喜欢用XX" / "我常用XX"
    (re.compile(r"我(?:喜欢用|常用|习惯用|偏好|用)(.{2,50})", re.IGNORECASE), "USER.md", "pref"),
]


def _detect_identity_intent(text: str) -> list[dict]:
    """
    Detect identity-related intents in user message.
    Returns list of {file, action, content} dicts.
    """
    if not text or len(text) > 200:
        return []
    results = []
    for pattern, filename, action in _IDENTITY_INTENTS:
        m = pattern.search(text)
        if m:
            value = m.group(1).strip()
            if value and len(value) >= 1:
                results.append({"file": filename, "action": action, "value": value, "match": m.group(0)})
    return results


def _apply_name_change(new_name: str):
    """Update IDENTITY.md with new name."""
    filepath = PROGRESS_DIR / "IDENTITY.md"
    content = filepath.read_text(encoding="utf-8")
    # Replace existing name references
    content = re.sub(r"你的名字是「[^」]*」", f"你的名字是「{new_name}」", content)
    content = re.sub(r"你就是[^。\n]*", f"你就是{new_name}", content)
    content = re.sub(r"以「[^」]*」的身份", f"以「{new_name}」的身份", content)
    filepath.write_text(content, encoding="utf-8")


def _apply_style_change(value: str):
    """Append style preference to SOUL.md."""
    filepath = PROGRESS_DIR / "SOUL.md"
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(f"- {value}\n")


def _apply_rule_change(value: str):
    """Append rule to AGENTS.md."""
    filepath = PROGRESS_DIR / "AGENTS.md"
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(f"- {value}\n")


def _apply_pref_change(value: str):
    """Append preference to USER.md."""
    filepath = PROGRESS_DIR / "USER.md"
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(f"- {value}\n")


_INTENT_APPLIERS = {
    "name": _apply_name_change,
    "style": _apply_style_change,
    "rule": _apply_rule_change,
    "pref": _apply_pref_change,
}


def _process_identity_intents(user_text: str):  # -> str | None
    """
    Scan user message for identity intents, apply changes.
    Returns a summary message if changes were made, None otherwise.
    """
    intents = _detect_identity_intent(user_text)
    if not intents:
        return None
    changes = []
    for intent in intents:
        try:
            applier = _INTENT_APPLIERS.get(intent["action"])
            if applier:
                applier(intent["value"])
                changes.append(f"{intent['file']}: {intent['action']}={intent['value']}")
        except Exception:
            logger.warning("Failed to apply identity intent", exc_info=True)
    if changes:
        return "已更新: " + "; ".join(changes)
    return None


# ═══════════════════════════════════════════════════════
#  SQLite Memory: FTS5 full-text search + Self-Learning
# ═══════════════════════════════════════════════════════

MEMORY_DB = PROGRESS_DIR / "memory.db"

# ── Self-learning constants ──
MAX_LEARNINGS_INJECT = 5  # How many relevant past learnings to inject per LLM call
LEARNING_CONFIDENCE_THRESHOLD = 0.3  # Minimum confidence to inject a learning


_db_conn: sqlite3.Connection | None = None
_db_write_lock = threading.Lock()


def _get_db() -> sqlite3.Connection:
    """Return a module-level SQLite connection (lazy-init, reused across calls)."""
    global _db_conn
    if _db_conn is None:
        PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
        _db_conn = sqlite3.connect(str(MEMORY_DB), check_same_thread=False)
        _db_conn.execute("PRAGMA journal_mode=WAL")
    return _db_conn


def _create_table(conn: sqlite3.Connection, name: str, columns: str, extras: list[str] | None = None):
    """Create a table + FTS5 virtual table + triggers if they don't exist."""
    conn.execute(f"CREATE TABLE IF NOT EXISTS {name} ({columns})")
    if extras:
        for stmt in extras:
            conn.execute(stmt)


def _init_db():
    """Create memory.db tables + FTS5 triggers if they don't exist."""
    try:
        conn = _get_db()

        _create_table(conn, "tool_calls",
            "id TEXT PRIMARY KEY, session_id TEXT NOT NULL, tool_name TEXT NOT NULL, "
            "args TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL",
            [
                "CREATE VIRTUAL TABLE IF NOT EXISTS tool_calls_fts USING fts5("
                "tool_name, args, result, content='tool_calls', content_rowid='rowid')",
                "CREATE TRIGGER IF NOT EXISTS tool_calls_ai AFTER INSERT ON tool_calls BEGIN "
                "INSERT INTO tool_calls_fts(rowid, tool_name, args, result) "
                "VALUES (new.rowid, new.tool_name, new.args, new.result); END",
                "CREATE TRIGGER IF NOT EXISTS tool_calls_ad AFTER DELETE ON tool_calls BEGIN "
                "INSERT INTO tool_calls_fts(tool_calls_fts, rowid, tool_name, args, result) "
                "VALUES ('delete', old.rowid, old.tool_name, old.args, old.result); END",
            ])

        _create_table(conn, "learnings",
            "id TEXT PRIMARY KEY, session_id TEXT NOT NULL, topic TEXT NOT NULL, "
            "content TEXT NOT NULL, confidence REAL DEFAULT 0.5, source_type TEXT DEFAULT 'extracted', "
            "hit_count INTEGER DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL",
            [
                "CREATE VIRTUAL TABLE IF NOT EXISTS learnings_fts USING fts5("
                "topic, content, content='learnings', content_rowid='rowid')",
                "CREATE TRIGGER IF NOT EXISTS learnings_ai AFTER INSERT ON learnings BEGIN "
                "INSERT INTO learnings_fts(rowid, topic, content) "
                "VALUES (new.rowid, new.topic, new.content); END",
                "CREATE TRIGGER IF NOT EXISTS learnings_ad AFTER DELETE ON learnings BEGIN "
                "INSERT INTO learnings_fts(learnings_fts, rowid, topic, content) "
                "VALUES ('delete', old.rowid, old.topic, old.content); END",
                "CREATE TRIGGER IF NOT EXISTS learnings_au AFTER UPDATE ON learnings BEGIN "
                "INSERT INTO learnings_fts(learnings_fts, rowid, topic, content) "
                "VALUES ('delete', old.rowid, old.topic, old.content); "
                "INSERT INTO learnings_fts(rowid, topic, content) "
                "VALUES (new.rowid, new.topic, new.content); END",
            ])

        _create_table(conn, "preferences",
            "id TEXT PRIMARY KEY, key TEXT UNIQUE NOT NULL, value TEXT NOT NULL, "
            "source TEXT DEFAULT 'inferred', confidence REAL DEFAULT 0.5, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL",
            [
                "CREATE VIRTUAL TABLE IF NOT EXISTS preferences_fts USING fts5("
                "key, value, content='preferences', content_rowid='rowid')",
            ])

        conn.execute("CREATE TABLE IF NOT EXISTS reflections ("
            "id TEXT PRIMARY KEY, session_id TEXT NOT NULL, tool_name TEXT NOT NULL, "
            "tool_args TEXT NOT NULL, tool_result_summary TEXT NOT NULL, "
            "reflection TEXT NOT NULL, was_useful INTEGER DEFAULT 1, created_at TEXT NOT NULL)")

        conn.execute("CREATE TABLE IF NOT EXISTS memory ("
            "session_id TEXT NOT NULL, type TEXT NOT NULL, topic TEXT NOT NULL, "
            "content TEXT NOT NULL, meta TEXT NOT NULL, "
            "created_at TEXT DEFAULT (datetime('now')))")

        conn.commit()
    except Exception:
        logger.warning("Failed to initialize memory DB", exc_info=True)


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

def _get_recent_learnings(limit: int = 10) -> list[dict]:
    """Get the most recent learnings (no query needed — for polling)."""
    try:
        conn = _get_db()
        rows = conn.execute(
            "SELECT topic, content, confidence FROM learnings ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [{"topic": r[0], "content": r[1], "confidence": r[2]} for r in rows]
    except Exception:
        return []


def _retrieve_relevant_learnings(query: str, limit: int = MAX_LEARNINGS_INJECT) -> list[dict]:
    """Search past learnings that are semantically relevant to the current query.
    Uses FTS5 full-text search with a fallback to recent high-confidence learnings."""
    try:
        conn = _get_db()
        results = []
        # FTS5 search on learnings
        # Sanitize query for FTS5: remove special chars and use simple terms
        safe_query = " ".join(
            w for w in re.findall(r'[一-鿿\w]+', query.lower())
            if len(w) > 1
        )
        if safe_query:
            try:
                rows = conn.execute(
                    """SELECT l.id, l.topic, l.content, l.confidence, l.hit_count, l.source_type
                       FROM learnings l
                       JOIN learnings_fts f ON l.rowid = f.rowid
                       WHERE learnings_fts MATCH ?
                       ORDER BY l.confidence * (1.0 + l.hit_count * 0.1) DESC
                       LIMIT ?""",
                    (safe_query, limit),
                ).fetchall()
                for row in rows:
                    results.append({
                        "id": row[0], "topic": row[1], "content": row[2],
                        "confidence": row[3], "hit_count": row[4], "source_type": row[5],
                    })
            except Exception:
                pass  # FTS5 query syntax errors are non-fatal

        # If FTS5 returned nothing, fall back to LIKE for better CJK matching
        if not results and query.strip():
            like_q = f"%{query.strip()}%"
            rows = conn.execute(
                """SELECT id, topic, content, confidence, hit_count, source_type
                   FROM learnings
                   WHERE topic LIKE ? OR content LIKE ?
                   ORDER BY confidence DESC
                   LIMIT ?""",
                (like_q, like_q, limit),
            ).fetchall()
            for row in rows:
                results.append({
                    "id": row[0], "topic": row[1], "content": row[2],
                    "confidence": row[3], "hit_count": row[4], "source_type": row[5],
                })

        # If still nothing, fall back to recent high-confidence learnings
        if not results:
            rows = conn.execute(
                """SELECT id, topic, content, confidence, hit_count, source_type
                   FROM learnings
                   WHERE confidence >= ?
                   ORDER BY updated_at DESC
                   LIMIT ?""",
                (LEARNING_CONFIDENCE_THRESHOLD, limit),
            ).fetchall()
            for row in rows:
                results.append({
                    "id": row[0], "topic": row[1], "content": row[2],
                    "confidence": row[3], "hit_count": row[4], "source_type": row[5],
                })

        # Bump hit_count for retrieved learnings (reinforcement)
        if results:
            ids = [r["id"] for r in results]
            with _db_write_lock:
                conn.executemany(
                    "UPDATE learnings SET hit_count = hit_count + 1, updated_at = ? WHERE id = ?",
                    [(datetime.now().isoformat(), lid) for lid in ids],
                )
                conn.commit()

        return results
    except Exception:
        return []


def _store_learning(session_id: str, topic: str, content: str, confidence: float = 0.5, source_type: str = "extracted"):
    """Store a new learning. If a similar topic already exists, update confidence."""
    try:
        conn = _get_db()
        now = datetime.now().isoformat()

        with _db_write_lock:
            # Check for existing similar topic
            existing = conn.execute(
                "SELECT id, confidence FROM learnings WHERE topic = ? LIMIT 1",
                (topic,),
            ).fetchone()

            if existing:
                # Boost confidence of existing learning (up to 1.0)
                new_conf = min(1.0, existing[1] + confidence * 0.3)
                conn.execute(
                    "UPDATE learnings SET confidence = ?, updated_at = ? WHERE id = ?",
                    (new_conf, now, existing[0]),
                )
            else:
                lid = str(uuid.uuid4())
                conn.execute(
                    """INSERT INTO learnings(id, session_id, topic, content, confidence, source_type, created_at, updated_at)
                       VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                    (lid, session_id, topic, content, confidence, source_type, now, now),
                )
            conn.commit()
    except Exception:
        logger.warning("Failed to store learning in memory DB", exc_info=True)


def _store_preference(key: str, value: str, confidence: float = 0.5):
    """Store a learned user preference. Boosts confidence if already exists."""
    try:
        conn = _get_db()
        now = datetime.now().isoformat()
        with _db_write_lock:
            existing = conn.execute(
                "SELECT id, confidence FROM preferences WHERE key = ? LIMIT 1", (key,),
            ).fetchone()
            if existing:
                new_conf = min(1.0, existing[1] + confidence * 0.3)
                conn.execute(
                    "UPDATE preferences SET value = ?, confidence = ?, updated_at = ? WHERE id = ?",
                    (value, new_conf, now, existing[0]),
                )
            else:
                lid = str(uuid.uuid4())
                conn.execute(
                    """INSERT INTO preferences(id, key, value, confidence, source, created_at, updated_at)
                       VALUES(?, ?, ?, ?, 'inferred', ?, ?)""",
                    (lid, key, value, confidence, now, now),
                )
            conn.commit()
    except Exception:
        logger.warning("Failed to store preference in memory DB", exc_info=True)


def _retrieve_preferences() -> list[dict]:
    """Get all high-confidence learned preferences for context injection."""
    try:
        conn = _get_db()
        rows = conn.execute(
            "SELECT key, value, confidence FROM preferences WHERE confidence >= 0.4 ORDER BY confidence DESC"
        ).fetchall()
        return [{"key": r[0], "value": r[1], "confidence": r[2]} for r in rows]
    except Exception:
        return []


def _record_reflection(session_id: str, tool_name: str, tool_args: dict, tool_result_summary: str, reflection: str, was_useful: bool):
    """Store a post-tool-call reflection."""
    try:
        conn = _get_db()
        rid = str(uuid.uuid4())
        with _db_write_lock:
            conn.execute(
                """INSERT INTO reflections(id, session_id, tool_name, tool_args, tool_result_summary, reflection, was_useful, created_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                (rid, session_id, tool_name, json.dumps(tool_args, ensure_ascii=False),
                 tool_result_summary, reflection, 1 if was_useful else 0, datetime.now().isoformat()),
            )
            conn.commit()
    except Exception:
        logger.warning("Failed to store reflection in memory DB", exc_info=True)


def _quick_reflect(tool_name: str, result: str) -> str:
    """Quick heuristic reflection on tool execution result.
    Returns a reflection note or empty string."""
    result_lower = result.lower()
    # Error detection — covers both English and Chinese tool error messages
    is_error = (
        "error" in result_lower or "错误" in result
        or "failed" in result_lower or "失败" in result
        or "traceback" in result_lower
        or "denied" in result_lower
    )
    if is_error:
        return f"工具 {tool_name} 执行出错，可能需要重试或调整参数"
    if "permission denied" in result_lower:
        return "权限不足，建议检查文件/目录权限"
    if "not found" in result_lower or "不存在" in result:
        return "目标不存在，可能需要先确认路径或创建前置资源"
    if len(result.strip()) < 5:
        return "工具返回为空，可能参数不正确或目标无内容"
    if len(result) > 5000:
        return f"输出较大({len(result)}字符)，后续可能需要聚焦关键部分"
    return ""  # Everything looks fine, no reflection needed


def _build_learning_context(user_query: str) -> str:
    """Build a context string from relevant learnings + preferences to inject into system prompt."""
    parts = []

    # Get relevant learnings
    learnings = _retrieve_relevant_learnings(user_query)
    if learnings:
        lines = ["## 你从过去的交互中学到了:"]
        for item in learnings:
            confidence_bar = "█" * int(item["confidence"] * 5) + "░" * (5 - int(item["confidence"] * 5))
            lines.append(f"- [{item['topic']}] {item['content']} (置信度: {confidence_bar})")
        parts.append("\n".join(lines))

    # Get learned preferences
    prefs = _retrieve_preferences()
    if prefs:
        lines = ["## 用户偏好 (从历史交互中推断):"]
        for p in prefs:
            lines.append(f"- {p['key']}: {p['value']}")
        parts.append("\n".join(lines))

    return "\n\n".join(parts) if parts else ""


# ── Heuristic knowledge extraction from conversation ──

_KNOWLEDGE_PATTERNS = [
    # User explicitly corrects agent
    (r"(?:不对|错了|不是|不要|别|停止?|你应该?|请?记住|以后).{0,30}(?:要|请|必须|应该?)[^\n]{5,80}", "correction", 0.8),
    # User teaches a fact
    (r"(?:实际上?|其实是?|事实[上是]?|注意|重要的是?|关键[是点]?)[^\n]{10,100}", "fact", 0.6),
    # User gives preference
    (r"(?:我[更喜欢想要偏好]|倾向于|比较喜欢|习惯)[^\n]{5,80}", "preference", 0.7),
    # Code/tech learnings
    (r"(?:这个项目|这里|代码[中里]|API|接口|函数|文件)[^\n]{10,100}(?:是|用|在|需要|可以)[^\n]{5,60}", "technical", 0.5),
    # File/directory structure
    (r"(?:项目结构|目录结构|代码在|配置文件|入口[文件点])[^\n]{10,100}", "structure", 0.55),
]


def _extract_learnings_heuristic(user_text: str, session_id: str) -> int:
    """Simple pattern-based knowledge extraction from user messages.
    Falls back to this when LLM-based extraction is unavailable.
    Returns number of learnings extracted."""
    count = 0
    for pattern, source_type, confidence in _KNOWLEDGE_PATTERNS:
        for match in re.finditer(pattern, user_text):
            matched_text = match.group(0).strip()
            if len(matched_text) < 8:
                continue
            # Derive topic from first few chars
            topic = matched_text[:30].strip().rstrip("，。,.!！?？")
            _store_learning(session_id, topic, matched_text, confidence, source_type)
            # Store as preference if it's a preference pattern
            if source_type == "preference":
                _store_preference("user_style", matched_text, confidence)
            count += 1
    return count


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
    """Build a system message from ENABLED skills to inject engineering constraints."""
    global _loaded_skills
    _loaded_skills = _load_skills()
    enabled = [s for s in _loaded_skills if s.get("enabled", True)]
    if not enabled:
        return ""
    lines = ["## 工程技能卡约束 (必须遵守)"]
    for s in enabled:
        lines.append(f"\n### {s['name']}")
        lines.append(s["content"])
    lines.append("\n---\n以上所有技能的退出标准都是硬性要求，未满足不得声称任务完成。")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════
#  Dynamic Tool Filtering: intent-based tool selection
# ═══════════════════════════════════════════════════════

TOOL_CATEGORIES = {
    "file_read": ["read_file", "list_dir", "search_files"],
    "file_write": ["write_file"],
    "command": ["run_cmd"],
    "app": ["open_app", "open_folder"],
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
    filtered = [t for t in all_tools if t.get("function", {}).get("name") in allowed_tools]
    return filtered if filtered else all_tools


# ═══════════════════════════════════════════════════════
#  Memory Semantic Summarization: LLM compresses learnings
# ═══════════════════════════════════════════════════════

async def _summarize_learning(raw_content: str) -> str:
    """Use LLM to compress a raw learning into a concise semantic summary (1-2 sentences)."""
    try:
        prompt = (
            "将以下知识片段压缩为一到两句中文摘要，只保留可操作的结论，去掉冗余细节。\n\n"
            f"原文: {raw_content[:500]}\n\n摘要:"
        )
        async with httpx.AsyncClient(timeout=httpx.Timeout(30)) as client:
            r = await client.post(
                LM_STUDIO_URL,
                json={
                    "model": SUBAGENT_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 120,
                    "temperature": 0.3,
                    "stream": False,
                },
                headers={"Content-Type": "application/json"},
            )
            if r.status_code == 200:
                data = r.json()
                summary = data["choices"][0]["message"]["content"].strip()
                return summary if summary else raw_content[:200]
            return raw_content[:200]
    except Exception:
        return raw_content[:200]  # Fall back to truncation


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
        await asyncio.wait_for(event.wait(), timeout=30)
        async with _pending_lock:
            approved = _pending_confirmations.get(call_id, {}).get("approved", False)
    except asyncio.TimeoutError:
        approved = False
    finally:
        async with _pending_lock:
            _pending_confirmations.pop(call_id, None)
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
    if verify_report:
        tool_content = f"{result}\n{verify_report}"
    elif reflection_note:
        tool_content = f"{result}\n\n[Self-Reflection: {reflection_note}]"

    current_msgs.append({"role": "tool", "tool_call_id": call_id, "content": tool_content})
    return verify_failed, events

async def _agent_loop_stream(messages: list, model: str, api_url: str, headers: dict, session_id: str = "", agent_id: str = "latiao"):
    """Agent loop: call LLM with tools. If tool_calls → execute → loop. If text → yield & done."""
    current_msgs = [dict(m) for m in messages]
    if not session_id:
        session_id = str(uuid.uuid4())
    max_retries = 3
    retry_count = 0
    last_verify_failed = False
    stagnation = 0             # consecutive unproductive iterations
    max_stagnation = 3          # exit after this many dead-end rounds
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
        current_msgs.insert(system_idx, {
            "role": "system",
            "content": f"以下是 AI 从过去交互中学习的知识，请在回复时参考：\n\n{learning_context}",
        })

    # ── Self-Learning: Heuristic extraction ──
    if last_user_text:
        _extract_learnings_heuristic(last_user_text, session_id)

    # ── Dynamic Tool Filtering + Agent restrictions ──
    agent_tools = _get_agent_tools(agent_id, TOOLS)
    active_tools = _filter_tools(last_user_text, agent_tools) if last_user_text else agent_tools

    async with httpx.AsyncClient(timeout=httpx.Timeout(120)) as client:
        while iteration < 50:  # hard cap at 50, dynamic exit via stagnation
            iteration += 1
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
                            if content:
                                streamed_text += content
                                yield {"content": content}
                                if len(streamed_text) < 5:
                                    _track_progress(session_id, "generating", "text_start")

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
                _track_progress(session_id, "tool_calling", f"{len(tool_calls)} tool(s)")

                current_msgs.append({
                    "role": "assistant",
                    "content": streamed_text or None,
                    "tool_calls": tool_calls,
                })

                # Stagnation check: reset if new tool calls, else count toward limit
                any_new = False
                for tc in tool_calls:
                    sig = f"{tc.get('function',{}).get('name','')}:{hash(str(tc.get('function',{}).get('arguments','')))}"
                    if sig not in recent_tool_calls:
                        recent_tool_calls.add(sig)
                        any_new = True
                    verify_failed, events = await _handle_tool_execution(
                        tc, current_msgs, session_id, agent_id)
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
            _track_progress(session_id, "completed", f"text_response ({len(streamed_text)} chars)")
            return

        # Hard cap reached (50 iterations) — extremely rare with dynamic stagnation
        tool_count = sum(1 for m in current_msgs if m.get("role") == "tool")
        yield {"content": f"\n\n⚠️ 已达到硬上限 (50 轮)。本会话共执行了 {tool_count} 次工具调用。如需继续，请发送新消息。"}


def _build_chat_messages(body: dict, messages: list) -> list:
    """Assemble the full message array with identity, env, skills, agent, and image injections."""
    last_user_text = _extract_last_user_text(messages)
    intent_result = _process_identity_intents(last_user_text)

    identity_msgs = _read_identity()
    if intent_result:
        identity_msgs.append({
            "role": "system",
            "content": (
                f"⚠️ 你的身份刚刚被用户更新了：{intent_result}。"
                f"从现在开始，你必须以更新后的身份回复用户。"
                f"忽略对话历史中你以前用过的任何名字、风格或自称，全部以更新后的为准。"
            )
        })
    if identity_msgs:
        messages = identity_msgs + messages

    home = str(Path.home())
    cwd = os.getcwd()
    messages = [{"role": "system", "content": (
        f"Runtime environment:\n"
        f"- User home directory: {home}\n"
        f"- Current working directory: {cwd}\n"
        f"- OS: macOS (Darwin)\n"
        f"- Shell: zsh"
    )}] + messages

    skill_prompt = _build_skill_prompt()
    if skill_prompt:
        messages = [{"role": "system", "content": skill_prompt}] + messages

    goal_mode = body.get("goal_mode", False)
    progressive = body.get("progressive_delivery", True)
    extra_prompts = []
    if goal_mode:
        extra_prompts.append(GOAL_MODE_PROMPT)
    if progressive:
        extra_prompts.append(PROGRESSIVE_DELIVERY_PROMPT)
    if extra_prompts:
        messages = [{"role": "system", "content": "\n".join(extra_prompts)}] + messages

    agent_id = body.get("agent", "latiao")
    agent_cfg = _get_agent_config(agent_id)
    messages = [{"role": "system", "content": agent_cfg["identity"]}] + messages

    image_base64 = body.get("image_base64")
    image_mime = body.get("image_mime", "image/png")
    if image_base64 and messages:
        messages = _inject_image(messages, image_base64, image_mime)

    return messages


def _resolve_api_target(cloud_config: dict | None) -> tuple[str, str, dict]:
    """Resolve API URL, protocol, and headers from cloud config or local fallback."""
    if cloud_config and cloud_config.get("key") and cloud_config.get("endpoint"):
        protocol = cloud_config.get("protocol", "openai")
        if protocol == "openai":
            api_url = cloud_config["endpoint"].rstrip("/") + "/chat/completions"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {cloud_config['key']}",
            }
        else:
            api_url = None
            headers = None
    else:
        protocol = "openai"
        local_api = local_llm.get_api_url()
        if local_api:
            api_url = local_api + "/chat/completions"
        else:
            api_url = LM_STUDIO_URL
        headers = {"Content-Type": "application/json"}
    return protocol, api_url, headers


@app.post("/v1/chat/completions")
async def chat_completion(request: Request):
    """Main chat endpoint. Routes to agent loop (OpenAI-compatible) or simple streaming."""
    body = await request.json()
    messages = body.get("messages", [])

    # Assemble full message context (identity, env, skills, agent, image)
    messages = _build_chat_messages(body, messages)
    model = body.get("model") or SUBAGENT_MODEL
    logger.info("Chat request: model=%s, msg_count=%d, stream=%s", model, len(messages), body.get("stream", False))

    skip_tools = body.get("skip_tools", False)
    model = body.get("model") or SUBAGENT_MODEL
    agent_id = body.get("agent", "latiao")
    cloud_config = body.get("cloud_config")
    use_stream = body.get("stream", False)

    # Resolve API target
    protocol, api_url, headers = _resolve_api_target(cloud_config)

    # Agent loop: LLM autonomously decides when to call tools
    if not skip_tools and protocol == "openai" and use_stream:
        session_id = body.get("session_id", "")
        async def agent_loop_wrapper():
            try:
                async for event in _agent_loop_stream(messages, model, api_url, headers, session_id, agent_id):
                    yield f"data: {json.dumps(event)}\n\n"
                yield "data: [DONE]\n\n"
            except httpx.ConnectError:
                yield f"data: {json.dumps({'error': '无法连接模型服务。请检查 LM Studio 或本地 LLM 是否已启动。'})}\n\n"
                yield "data: [DONE]\n\n"
        return StreamingResponse(agent_loop_wrapper(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    # Simple streaming fallback (skip_tools=True or non-OpenAI protocol or sync mode)
    if use_stream:
        async def stream():
            lm_body = {"model": model, "messages": messages, "stream": True, "max_tokens": 4096}
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
                                    if text:
                                        yield f"data: {json.dumps({'content': text})}\n\n"
                                except (json.JSONDecodeError, KeyError, IndexError):
                                    pass  # Malformed SSE event — skip, try next
                                except Exception:
                                    logger.warning("Unexpected error in SSE stream fallback", exc_info=True)
                                    raise
            except httpx.ConnectError:
                yield f"data: {json.dumps({'error': '无法连接模型服务。请检查 LM Studio 或本地 LLM 是否已启动。'})}\n\n"
                yield "data: [DONE]\n\n"
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    # Sync fallback (rarely used)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120)) as c:
            resp = await c.post(api_url, json={
                "model": model, "messages": messages, "max_tokens": 4096,
            }, headers=headers)
            resp_data = resp.json()

        ai_content = resp_data["choices"][0]["message"].get("content", "")
        ai_reasoning = resp_data["choices"][0]["message"].get("reasoning_content", "")
    except httpx.ConnectError:
        return JSONResponse(
            {"error": "无法连接模型服务。请检查 LM Studio 或本地 LLM 是否已启动。"},
            status_code=503,
        )

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
        is_pdf = file_type == "application/pdf" or file.filename.lower().endswith(".pdf")

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


@app.post("/v1/identity/open/{filename}")
async def open_identity_file(filename: str):
    """Open an identity file in the system's default editor."""
    if filename not in IDENTITY_FILES:
        return {"status": "error", "message": f"Unknown identity file: {filename}"}

    filepath = PROGRESS_DIR / filename
    try:
        PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
        if not filepath.exists():
            # Create the file first if it doesn't exist
            _create_default_identity()
        if platform.system() == "Darwin":
            subprocess.Popen(["open", str(filepath)])
        elif platform.system() == "Windows":
            os.startfile(str(filepath))
        else:
            subprocess.Popen(["xdg-open", str(filepath)])
        return {"status": "ok", "message": f"Opened {filename}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


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
        with _db_write_lock:
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
    # Also remove from config
    cfg = _load_skills_config()
    cfg.pop(key, None)
    _save_skills_config(cfg)
    global _loaded_skills
    _loaded_skills = _load_skills()
    return {"status": "ok"}


# ── Tavily API Key management endpoints ──


@app.get("/v1/settings/tavily-key")
async def get_tavily_key():
    """Get Tavily API key status (masked, never returns full key)."""
    try:
        if CONFIG_FILE.exists():
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            key = cfg.get("tavily_api_key", "")
            if key:
                masked = key[:7] + "••••" + key[-4:] if len(key) > 11 else "••••"
                return {"status": "ok", "has_key": True, "masked": masked}
    except Exception:
        pass
    return {"status": "ok", "has_key": False, "masked": None}


@app.post("/v1/settings/tavily-key")
async def set_tavily_key(request: Request):
    """Save Tavily API key to config file."""
    body = await request.json()
    key = body.get("key", "").strip()
    if not key:
        return {"status": "error", "message": "API key is required"}
    try:
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
    """Remove Tavily API key from config."""
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
_cron_last_run: dict[str, str] = {}  # job_id → last run timestamp


def _cron_matches(cron_expr: str, now: datetime) -> bool:
    """Simple cron expression matcher. Supports: */N, H, D patterns."""
    try:
        parts = cron_expr.strip().split()
        if len(parts) != 5:
            return False
        minute, hour, dom, month, dow = parts
        # */N every N minutes
        if minute.startswith("*/") and hour == "*":
            interval = int(minute[2:])
            return now.minute % interval == 0
        # 0 H * * * → daily at hour H
        if minute == "0" and hour.isdigit() and dom == "*" and month == "*" and dow == "*":
            return now.hour == int(hour) and now.minute == 0
        # 0 H * * D → weekly on day D at hour H
        if minute == "0" and hour.isdigit() and dom == "*" and month == "*" and dow.isdigit():
            return now.hour == int(hour) and now.minute == 0 and str(now.weekday()) == dow
        return False
    except Exception:
        return False


def _tick_cron():
    """Check all enabled cron jobs, return list of due jobs."""
    now = datetime.now()
    due = []
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
    return {"status": "ok", "jobs": _cron_jobs}


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
    _cron_jobs.append(job)
    _save_cron(_cron_jobs)
    return {"status": "ok", "job": job}


@app.put("/v1/cron/{job_id}")
async def update_cron_job(job_id: str, request: Request):
    """Update a cron job."""
    global _cron_jobs
    body = await request.json()
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
    _cron_jobs = [j for j in _cron_jobs if j["id"] != job_id]
    _save_cron(_cron_jobs)
    return {"status": "ok"}


@app.post("/v1/cron/{job_id}/toggle")
async def toggle_cron_job(job_id: str):
    """Toggle a cron job enabled/disabled."""
    global _cron_jobs
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
    return {"status": "ok", "due": due, "total_jobs": len(_cron_jobs)}


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
async def local_llm_search(q: str = Query(..., min_length=1), library: str = Query(default=""), limit: int = Query(default=10, le=20)):
    """Search HuggingFace for models."""
    results = local_llm.search_huggingface(q, limit, library)
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


@app.get("/v1/local-llm/recommended")
async def local_llm_recommended():
    """List recommended models with download status."""
    return {"status": "ok", "models": local_llm.get_recommended_models(), "backend": local_llm.get_backend()}


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
    """Execute a due cron job by calling the LLM with the task as prompt."""
    task = job.get("task", "")
    action = job.get("action", "notify")
    logger.info("Cron job triggered: %s — %s", task, action)

    # Build messages: identity + skills + cron prompt
    messages = _read_identity()
    skills_content = _build_skills_content()
    if skills_content:
        messages.append({"role": "system", "content": skills_content})
    messages.append({
        "role": "system",
        "content": (
            "你是一个定时任务助手。用户设定了一个定时任务，请根据任务描述执行。\n"
            "当前时间: " + datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "\n"
            "注意: 不要使用任何工具。只需输出分析结果和建议。输出用中文。"
        ),
    })
    messages.append({"role": "user", "content": f"定时任务: {task}"})

    # Get API target (use local LLM or cloud config from env, no-op if none)
    protocol, api_url, headers = _resolve_api_target(None)
    model = os.environ.get("LATIAO_SUBAGENT_MODEL", SUBAGENT_MODEL)

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120)) as c:
            resp = await c.post(api_url, json={
                "model": model, "messages": messages, "max_tokens": 1024,
            }, headers=headers)
            resp_data = resp.json()
            ai_content = resp_data["choices"][0]["message"].get("content", "")
    except (httpx.ConnectError, KeyError, Exception) as e:
        ai_content = f"[AI 调用失败: {e}]"
        logger.warning("Cron LLM call failed: %s", e)

    # Record to memory DB with AI result
    try:
        conn = _get_db()
        with _db_write_lock:
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
