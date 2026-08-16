"""
Local AI OS - Python Sidecar
Stateless: frontend manages all state, sends complete messages array per request.

main.py is the facade entry point. Heavy logic has been split into modules
(verbatim moves from the original single-file main.py):
  - agent_loop.py     agent loops, tool-call parsing, tool dispatch state, chat building
  - cron.py           cron scheduler (state + matching + execution)
  - tool_executor.py  fallback tool implementations + permission resolver
  - api_routes.py     FastAPI route handlers (imported at the bottom to register routes)

main.py retains: AUTH_TOKEN auth, lifespan, the FastAPI app instance, skills
matching, custom-permission state, shared constants, and facade re-exports so
`main.<symbol>` references (memory.py lazy imports, tests) keep working.
"""
from __future__ import annotations

import sys

if __name__ == "__main__":
    # 以脚本方式运行时本模块名为 __main__，而子模块（api_routes/memory 等）
    # 通过 `import main` / `from main import ...` 引用本模块——若不做别名，
    # main.py 会被第二次完整执行（双实例：路由注册到错误的 app、状态分裂）。
    # 将 __main__ 注册为 `main`，保证同一进程内只有一个实例。
    sys.modules["main"] = sys.modules["__main__"]

if len(sys.argv) > 1 and sys.argv[1] == "--mx-query":
    sys.argv = [sys.argv[0]] + sys.argv[2:]
    from skills.mx_data.mx_data import MXData
    query = " ".join(sys.argv[1:])
    try:
        mx = MXData()
        result = mx.query(query)
        print(mx.format_terminal(result, *mx.parse_result(result)))
    except Exception as e:
        print(f"妙想金融查询不可用: {e}", file=sys.stderr)
        sys.exit(1)
    sys.exit(0)

import os

# bundled Python 缺系统 CA 证书链 → HTTPS 握手失败（whisper 模型下载、
# huggingface_hub 等走 requests/httpx 的请求）。用 certifi 的 CA 兜底，
# 必须在任何 HTTP 库初始化前设置。
try:
    import certifi as _certifi
    os.environ.setdefault("SSL_CERT_FILE", _certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", _certifi.where())
except ImportError:
    pass
# 部署时 app 被重建可能导致运行中进程的 CWD 失效 → 启动即修复
try:
    os.getcwd()
except OSError:
    os.chdir(os.path.expanduser("~"))
if sys.platform == "win32" and getattr(sys, 'frozen', False):
    if sys.stdout is None:
        log_dir = os.path.join(os.environ.get("TEMP", "."))
        sys.stdout = open(os.path.join(log_dir, "latiao-sidecar.log"), "a")
        sys.stderr = sys.stdout


import asyncio
import json
import logging
import os
import platform
import sys
from collections import deque

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
from logging.handlers import RotatingFileHandler
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# ── 拆分模块（门面导入）──────────────────────────────
# 以下 import 即触发子模块的模块级初始化（AGENT_PROFILES、_merge_agents、
# load_plugins 等），与原 main.py 中的执行顺序一致。
from agent_loop import (  # noqa: F401 — 门面 re-export：保持 main.xxx 引用可用
    AGENT_PROFILES,
    AGENTS_DIR,
    AGENTS_FILE,
    CONFIG_FILE,
    INTENT_PATTERNS,
    PROGRESS_FILE,
    TOOL_CATEGORIES,
    TOOL_DISPATCH,
    TOOL_HOOKS,
    TOOL_PERMISSIONS,
    TOOLS,
    _agent_loop_stream,
    _append_loop_log,
    _auto_verify,
    _await_tool_confirmation,
    _background_tasks,
    _build_chat_messages,
    _build_local_tools_prompt,
    _cap_tools,
    _check_pre_hooks,
    _check_skill_permission,
    _check_stagnation,
    _deduplicate_response,
    _detect_task_intent,
    _detect_user_language,
    _enhance_auto_verify,
    _extract_last_user_text,
    _filter_tools,
    _gemma_args_to_json,
    _get_agent_config,
    _get_agent_tools,
    _get_best_cloud_config,
    _get_localized_text,
    _handle_tool_execution,
    _has_cloud_models,
    _inject_image,
    _is_local_llm_url,
    _last_cloud_config,
    _load_custom_agents,
    _local_agent_loop_stream,
    _local_llm_serialized,
    _local_llm_stream,
    _local_llm_stream_lock,
    _merge_agents,
    _parse_kv_args,
    _parse_native_tool_calls,
    _parse_prompt_tool_calls,
    _pending_confirmations,
    _pending_lock,
    _record_progress,
    _record_tool_call_db,
    _resolve_api_target,
    _resolve_max_tokens,
    _safe_cwd,
    _salvage_tool_args,
    _save_custom_agents,
    _semgrep_scan,
    _session_states,
    _spawn,
    _strip_native_tool_calls,
    _track_progress,
    execute_tool,
)
from config import PROGRESS_DIR
from cron import _cron_loop, _seed_default_cron
from db import _init_db
from identity import _create_default_identity
from tool_executor import _resolve_permission  # noqa: F401 — 门面 re-export

logger = logging.getLogger("latiao-sidecar")

# Load .env file manually
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    for _line in env_path.read_text(encoding="utf-8").splitlines():
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
# 阻止日志向 root logger 传播——否则 uvicorn/fastapi 给 root 挂的 handler 会
# 让每条日志重复写一次（sidecar.log 里每行出现两次的根因）
logger.propagate = False

# ── File handler: 落盘到 ~/.local-ai-os/sidecar.log，带 5MB×3 轮转 ──
# 打包成 app 后 sidecar 无终端附着，stdout/stderr 丢失；
# 落盘后任务中断可 `tail ~/.local-ai-os/sidecar.log` 查看 iteration 走势与异常堆栈。
try:
    PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
    _file_handler = RotatingFileHandler(
        PROGRESS_DIR / "sidecar.log",
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=3,
        encoding="utf-8",
    )
    _file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    ))
    _file_handler.setLevel(logging.INFO)
    logger.addHandler(_file_handler)
except Exception:
    pass  # 日志目录不可写不应阻断启动

# ═══════════════════════════════════════════════════════
#  Local Auth: token generated by the Rust host → env var
# ═══════════════════════════════════════════════════════
# Rust 端每次启动生成随机 token，通过 LATIAO_AUTH_TOKEN 环境变量注入本进程；
# 前端经 Tauri command get_auth_token 取得后，在请求头 X-Latiao-Token
# （或 Authorization: Bearer）携带。未设置该变量时保持无鉴权（开发/手动启动）。
AUTH_TOKEN = os.environ.get("LATIAO_AUTH_TOKEN", "") or ""
if AUTH_TOKEN:
    logger.info("本地认证已启用（LATIAO_AUTH_TOKEN 已设置）")
else:
    logger.warning(
        "LATIAO_AUTH_TOKEN 未设置 — sidecar 以无鉴权模式运行"
        "（仅供开发/手动启动；由应用启动时 Rust 端会注入 token）"
    )


class _UnauthorizedError(Exception):
    """401 sentinel raised by _check_auth — 由全局 handler 渲染为统一 JSON。"""


def _check_auth(request: Request) -> None:
    """本地 token 认证依赖（应用级，覆盖全部路由）。

    - 校验 X-Latiao-Token，兼容 Authorization: Bearer <token>
    - LATIAO_AUTH_TOKEN 未设置 → 直接放行（无鉴权模式）
    - /health 豁免：前端启动探测用，且不返回敏感信息
    """
    if not AUTH_TOKEN:
        return
    if request.url.path == "/health":
        return
    token = request.headers.get("x-latiao-token", "") or ""
    if not token:
        auth = request.headers.get("authorization", "") or ""
        if auth.startswith("Bearer "):
            token = auth[7:]
    if token != AUTH_TOKEN:
        raise _UnauthorizedError()

# Log key lifecycle events
logger.info("Sidecar 启动")

# huggingface — 国内网络可用 hf-mirror.com 镜像
# os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")


# ═══════════════════════════════════════════════════════
#  Smart Skill System: Auto-match & load skills on demand
# ═══════════════════════════════════════════════════════

SKILL_INDEX: dict[str, dict] = {}
PROJECT_ROOT = Path(__file__).parent  # sidecar/
SKILLS_DIR = PROJECT_ROOT / "skills"

# TF-IDF cache for learnings (avoid rebuilding every search)

# ╔══════════════════════════════════════════════════════╗
# ║  SECTION 1: Skills & App Lifecycle                    ║
# ║  _load_skill_index, _match_skill, lifespan            ║
# ╚══════════════════════════════════════════════════════╝

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
            async with _local_llm_serialized(api_url):
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
    from cron import _load_cron_state, run_cron_catchup
    _load_cron_state()  # 恢复跨重启状态（去重表 + seeded 标记），必须先于播种
    _seed_default_cron()
    _load_skill_index()  # Load skill index at startup
    # Write PID file so the Rust process manager can find us (after _init_db creates dir)
    SIDECAR_PID = PROGRESS_DIR / "sidecar.pid"
    SIDECAR_PID.write_text(str(os.getpid()))
    cron_task = asyncio.create_task(_cron_loop())
    catchup_task = asyncio.create_task(run_cron_catchup())  # 补跑关闭期间错过的任务
    logger.info("Sidecar 启动 — cron loop started")
    yield
    SIDECAR_PID.unlink(missing_ok=True)
    cron_task.cancel()
    catchup_task.cancel()
    logger.info("Sidecar 关闭")

app = FastAPI(
    title="Local AI OS Sidecar",
    lifespan=lifespan,
    # 应用级依赖：所有 /v1/* 端点都要求本地 token（/health 在 _check_auth 内豁免）
    dependencies=[Depends(_check_auth)],
)


@app.exception_handler(_UnauthorizedError)
async def _unauthorized_error_handler(request: Request, exc: _UnauthorizedError) -> JSONResponse:
    return JSONResponse(status_code=401, content={"status": "error", "message": "unauthorized"})

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:1420",
        "http://tauri.localhost",
        "https://tauri.localhost",
        "tauri://localhost",
        "http://127.0.0.1:1420",
        "http://127.0.0.1:8765",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type", "Authorization", "X-Latiao-Token"],
)

LM_STUDIO_URL = os.environ.get("LATIAO_LM_STUDIO_URL", "http://localhost:1234/v1/chat/completions")
SUBAGENT_MODEL = os.environ.get("LATIAO_SUBAGENT_MODEL", "gpt-4o-mini")
TAVILY_API_URL = os.environ.get("TAVILY_API_URL", "https://api.tavily.com/search")
MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50 MB
IS_MACOS = platform.system() == "Darwin"
IS_WINDOWS = platform.system() == "Windows"

# ═══════════════════════════════════════════════════════
#  Harness: 工具权限分级 + 状态持久化
# ═══════════════════════════════════════════════════════

# PROGRESS_DIR is imported from config
PERMISSIONS_CONFIG = PROGRESS_DIR / "permissions.json"

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


# ── 路由注册：api_routes 在文件末尾导入，确保 `from main import app` 拿到完整 app ──
import api_routes  # noqa: E402,F401  (注册全部 FastAPI 路由)

if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("LATIAO_PORT", "8765")))
