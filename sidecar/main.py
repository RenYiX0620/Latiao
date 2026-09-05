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
import secrets

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
    - LATIAO_AUTH_TOKEN 未设置 → 拒绝全部非豁免端点（审计 H2：此前
      无鉴权放行 = 手动启动 sidecar 时删模型/装扩展等端点全部裸奔；
      应用启动路径由 Rust 注入 token，不受影响；开发请显式设置该变量）
    - /health 豁免：前端启动探测用，且不返回敏感信息
    - /v1/update-latest.json、/v1/update-file 豁免：Tauri updater 插件的
      请求不携带自定义 token（本地回环 + 安装包经 minisign 签名验证，安全）
    """
    if not AUTH_TOKEN:
        raise _UnauthorizedError("sidecar 鉴权未初始化：请通过应用启动（Rust 注入 LATIAO_AUTH_TOKEN），或设置该环境变量后手动启动")
    if request.url.path in ("/health", "/v1/update-latest.json", "/v1/update-file"):
        return
    token = request.headers.get("x-latiao-token", "") or ""
    if not token:
        auth = request.headers.get("authorization", "") or ""
        if auth.startswith("Bearer "):
            token = auth[7:]
    # 常数时间比较（审计 P2-24）：普通不等比较可被计时侧信道探测 token
    if not secrets.compare_digest(token, AUTH_TOKEN):
        raise _UnauthorizedError()

# Log key lifecycle events
logger.info("Sidecar 启动")

# 诊断设施：进程无响应时 kill -USR1 <pid> 可 dump 全线程堆栈到日志文件
try:
    import faulthandler as _fh
    import signal as _signal
    _fh.register(_signal.SIGUSR1, file=open(str(PROGRESS_DIR / "stack_dump.txt"), "w"))
    logger.info("faulthandler SIGUSR1 诊断已注册")
    # （临时定时 dump 已移除——诊断完成；SIGUSR1 按需 dump 保留）
except Exception:
    pass

# huggingface — 国内网络可用 hf-mirror.com 镜像
# os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")


PROJECT_ROOT = Path(__file__).parent  # sidecar/


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan: startup + shutdown hooks."""
    _load_permissions()
    _create_default_identity()
    _init_db()
    from cron import _load_cron_state, run_cron_catchup
    _load_cron_state()  # 恢复跨重启状态（去重表 + seeded 标记），必须先于播种
    _seed_default_cron()
    # 统一能力模型：工具+技能入能力表（含一次性旧数据迁移），幂等
    try:
        import capability_registry
        capability_registry.initialize(TOOLS, TOOL_PERMISSIONS, TOOL_DISPATCH)
    except Exception:
        logger.warning("capability initialize failed", exc_info=True)
    # MCP 扩展工具启动即注册（审计 B7）：此前只有打开扩展页才加载，
    # 声明了 mcpServers 的扩展在正常使用中模型根本看不到其工具
    try:
        from agent_loop import ensure_mcp_loaded
        ensure_mcp_loaded()
    except Exception:
        logger.warning("MCP 启动注册失败", exc_info=True)
    # Write PID file so the Rust process manager can find us (after _init_db creates dir)
    SIDECAR_PID = PROGRESS_DIR / "sidecar.pid"
    SIDECAR_PID.write_text(str(os.getpid()))
    try:
        from extension_manager import warm_market_cache
        warm_market_cache()
    except Exception:
        logger.warning("市场预热启动失败", exc_info=True)
    # GitHub 生态抓取：启动后台线程（首启全量；24h 内增量跳过），不阻塞启动
    try:
        import threading
        from discovery import run_discovery
        def _discovery_worker():
            try:
                run_discovery()
                logger.info("GitHub 抓取预热完成")
            except Exception:
                logger.warning("GitHub 抓取预热失败", exc_info=True)
        threading.Thread(target=_discovery_worker, daemon=True).start()
    except Exception:
        logger.warning("GitHub 抓取预热启动失败", exc_info=True)
    # 孤儿看门狗：Tauri 宿主被强杀/崩溃时（无 SIGTERM），sidecar 会被
    # reparent 给 launchd 继续活着，35B 引擎随之常驻内存（"退了还占内存"）。
    # 轮询 ppid 检测宿主消失，自杀前先停掉模型引擎。
    import time as _time
    _watchdog_ppid = os.getppid()

    def _orphan_watchdog():
        import threading as _th

        def _watch():
            while True:
                _time.sleep(3)
                try:
                    if os.getppid() != _watchdog_ppid:
                        logger.warning("检测到宿主进程已退出，sidecar 自杀清理")
                        try:
                            from local_llm import shutdown_engine
                            shutdown_engine()
                        except Exception:
                            logger.warning("退出清理时停止引擎失败", exc_info=True)
                        os._exit(0)
                except Exception:
                    break
        _th.Thread(target=_watch, daemon=True).start()

    _orphan_watchdog()
    cron_task = asyncio.create_task(_cron_loop())
    catchup_task = asyncio.create_task(run_cron_catchup())  # 补跑关闭期间错过的任务
    logger.info("Sidecar 启动 — cron loop started")
    yield
    # 正常退出（应用关窗/托盘退出，Rust 端发 SIGTERM）：停掉本地模型引擎，
    # 释放显存/内存。sidecar 单独重启走 detach+SIGKILL，不会经过这里。
    try:
        from local_llm import shutdown_engine
        shutdown_engine()
    except Exception:
        logger.warning("Sidecar 关闭 — 停止引擎失败", exc_info=True)
    SIDECAR_PID.unlink(missing_ok=True)
    cron_task.cancel()
    catchup_task.cancel()
    logger.info("Sidecar 关闭")

# 生产桌面应用：docs/openapi 是 Starlette 原生路由，不走应用级鉴权依赖，
# 本机任意进程可读完整 API schema → 直接关闭
app = FastAPI(
    title="Local AI OS Sidecar",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
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

async def _hot_reload_extensions() -> dict:
    """扩展安装/启停后的热重载：重建技能索引 + 原地重载插件注册表 +
    重新注册 MCP 工具——刚装完的扩展立即可用，无需重启（此前只写磁盘
    配置，插件/MCP 工具要重启才生效）。"""
    import agent_loop
    # fallback 定义实际在 tool_executor（此前误 import tool_system，从未触发过
    # 直至生态安装路径真正调用才暴露）
    from tool_executor import (
        _FALLBACK_DISPATCH,
        _FALLBACK_PERMISSIONS,
        _FALLBACK_TOOLS,
    )
    from tool_system import load_plugins
    # 1) 能力表同步：技能三来源 + 工具注册表（prune 清理已卸载扩展的能力行）
    import capability_registry
    capability_registry.sync_skills()
    capability_registry.sync_tools(agent_loop.TOOLS, agent_loop.TOOL_PERMISSIONS,
                                   agent_loop.TOOL_DISPATCH, prune=True)
    # 2) 插件注册表原地重载。绝不能 rebind agent_loop.TOOLS——各模块
    #    （main/api_routes/cron 等）持有 from agent_loop import TOOLS 的
    #    旧引用，rebind 会让它们全部失效；原地 clear+extend 保持同一对象。
    new_tools, new_dispatch, new_perms, new_hooks = load_plugins(
        _FALLBACK_TOOLS, _FALLBACK_DISPATCH, _FALLBACK_PERMISSIONS)
    agent_loop.TOOLS.clear()
    agent_loop.TOOLS.extend(new_tools)
    agent_loop.TOOL_DISPATCH.clear()
    agent_loop.TOOL_DISPATCH.update(new_dispatch)
    agent_loop.TOOL_PERMISSIONS.clear()
    agent_loop.TOOL_PERMISSIONS.update(new_perms)
    agent_loop.TOOL_HOOKS.clear()
    agent_loop.TOOL_HOOKS.update(new_hooks)
    # 3) MCP 远程工具重新注册
    await agent_loop._load_mcp_tools()
    from capability_registry import skill_catalog
    n_skills = len(skill_catalog())
    logger.info("扩展热重载完成: %d 工具 / %d 技能",
                len(agent_loop.TOOLS), n_skills)
    return {"status": "ok", "tools": len(agent_loop.TOOLS), "skills": n_skills}


# ── 路由注册：api_routes 在文件末尾导入，确保 `from main import app` 拿到完整 app ──
import api_routes  # noqa: E402,F401  (注册全部 FastAPI 路由)
import files_browse  # noqa: E402,F401  (应用内目录浏览：模型目录选择器)

if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()

    # ── 防双实例：已有 sidecar 监听 8765 时直接退出 ──
    # 两个 sidecar 同时管理引擎 = 双份模型加载（内存 95% 尖峰）+ 状态互踩。
    # 应用正常重启由 Rust 端先 kill 旧进程，这里拦的是绕过应用的手动启动。
    import socket, time as _time
    _port = int(os.environ.get("LATIAO_PORT", "8765"))
    _dup = False
    for _try in range(3):
        _probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _probe.settimeout(1.0)
        _dup = _probe.connect_ex(("127.0.0.1", _port)) == 0
        _probe.close()
        if not _dup:
            break
        # 端口被占：可能是刚被 kill 的旧实例尚未完全退出，等 2s 重试
        if _try < 2:
            _time.sleep(2)
    if _dup:
        _other = ""
        try:
            import urllib.request
            with urllib.request.urlopen(f"http://127.0.0.1:{_port}/health", timeout=2) as _r:
                _other = _r.read().decode("utf-8", errors="replace")[:120]
        except Exception:
            pass
        print("⛔ 另一个辣条 sidecar 已在运行（端口被占用），本次启动取消。", flush=True)
        print(f"   现存实例响应: {_other}", flush=True)
        raise SystemExit(1)

    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("LATIAO_PORT", "8765")))
