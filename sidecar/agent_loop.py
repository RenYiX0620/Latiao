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
import platform
import re
import tempfile
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
    _delegate_task,
    _resolve_permission,
)
from tool_system import load_plugins

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


def _safe_cwd() -> str:
    """获取当前工作目录。部署时 app 被 rm -rf 重建,运行中 sidecar 的 CWD
    指向已删除目录,os.getcwd() 会抛 FileNotFoundError → 回退 home。"""
    try:
        return os.getcwd()
    except OSError:
        return str(Path.home())


# llama_cpp.server 是单模型实例，多个流式生成请求并发时会崩溃
# （连接被 peer 关闭 → 上层表现为"空响应/任务执行一半停止"）。
# 主对话 agent 循环与 cron 任务并发调用本地模型是实际触发场景——
# 所有打到本地端口的模型请求必须串行执行。
_local_llm_stream_lock = asyncio.Lock()
# 引擎疑似损坏的时间戳：流被取消（停止按钮/新消息）或空响应后置位，
# 下一次本地请求在锁内先验证引擎健康，避免向残留线程竞争损坏的引擎发请求。
_llm_suspect_since: float | None = None


def _is_local_llm_url(api_url: str | None) -> bool:
    return bool(api_url) and ("127.0.0.1" in api_url or "localhost" in api_url)


async def _verify_llm_health(api_url: str) -> bool:
    """锁内健康验证：发一个最小生成请求，确认引擎能正常产出文本。

    注意：mlx_lm/llama server 串行处理请求——长生成期间健康请求排队超时
    是"忙"不是"死"。之前单次超时就 SIGKILL 引擎（误杀运行中的 35B 并触发
    26GB 重载→内存翻倍），现在只有端口确实死亡（连接被拒）才判死。"""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(25)) as c:
            resp = await c.post(api_url, json={
                "model": "health-check", "stream": False, "max_tokens": 4,
                "messages": [{"role": "user", "content": "hi"}],
            })
            resp.raise_for_status()
            data = resp.json()
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
            if not content.strip():
                logger.warning("引擎返回空响应（%s），标记存疑", api_url)
                return False  # 空响应=引擎异常（端口活但产出坏了）
            return True
    except httpx.ConnectError:
        # 端口确实死亡（进程退出）→ 判死，由调用方处置
        logger.warning("本地模型引擎连接被拒（%s），判定引擎死亡", api_url)
        return False
    except Exception:
        # 超时/排队/临时错误：引擎活着但忙——不误杀
        logger.info("健康探测超时但连接可达（%s），引擎忙，视为存活", api_url)
        return True


@asynccontextmanager
async def _local_llm_serialized(api_url: str | None):
    """非流式本地请求串行化：本地 llama.cpp 时持锁，云端不设限。"""
    _local = _is_local_llm_url(api_url)
    if _local:
        await _local_llm_stream_lock.acquire()
    try:
        yield
    finally:
        if _local:
            _local_llm_stream_lock.release()


@asynccontextmanager
async def _local_llm_stream(client, api_url: str, body: dict, headers: dict):
    """流式请求本地/云端模型。本地 llama.cpp 时持锁直到流读完，
    防止并发流式生成导致 server 崩溃（连接被 peer 关闭）。"""
    async with _local_llm_serialized(api_url):
        global _llm_suspect_since
        if _is_local_llm_url(api_url):
            local_llm._engine.mark_engine_busy()
        if _is_local_llm_url(api_url) and _llm_suspect_since is not None:
            ok = await _verify_llm_health(api_url)
            if not ok:
                # 端口确实死亡或引擎产出异常——先杀残留，自动重载由 get_api_url 触发
                try:
                    from urllib.parse import urlparse
                    engine = local_llm._engine
                    port = urlparse(api_url).port or engine.server_port
                    engine._kill_port(port)
                    if port == engine.server_port:
                        engine.server_status = "stopped"
                except Exception:
                    pass
                raise httpx.ConnectError(
                    "本地模型引擎状态异常，已自动停止。请重新加载本地模型。"
                )
            _llm_suspect_since = None
        # 引擎短暂闪断（404/503，如 mlx_lm 高负载重启窗口）自动重试，
        # 避免整轮任务因一次瞬时不可用被判死。
        # 生成器语义：yield 之后消费者持有 r 直到读完，无法重试；
        # 只对"连接建立即失败"（还没 yield 过）的情况重试。
        last_err: Exception | None = None
        # 引擎闪断/自动重载期间 404/503：等待恢复（35B 重载窗口 3-5 分钟），
        # 每 5s 重试一次、最长 5 分钟——用户消息自然排队到引擎就绪，不再秒败
        try:
            for _attempt in range(72):
                try:
                    async with client.stream("POST", api_url, json=body, headers=headers) as r:
                        r.raise_for_status()  # httpx 不自动抛 4xx/5xx，必须显式检查
                        try:
                            yield r
                        except asyncio.CancelledError:
                            if _is_local_llm_url(api_url):
                                # 流被取消（用户点停止/发了新消息）-> 引擎生成线程可能残留
                                # 并继续跑 llama.cpp，下次请求前必须验证健康
                                _llm_suspect_since = _llm_suspect_since or time.monotonic()
                            raise
                        return
                except httpx.HTTPStatusError as e:
                    if e.response.status_code in (404, 503) and _attempt < 71:
                        last_err = e
                        await asyncio.sleep(5)  # 5s × 72 = 最大约 6 分钟等待
                        continue
                    raise
                except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
                    # 连接被拒：若没有自动重载在进行，等待毫无意义——快速失败
                    # （此前固定等 6 分钟，用户面对"没反应"的黑盒）
                    if isinstance(e, httpx.ConnectError) and _is_local_llm_url(api_url):
                        if not local_llm._engine._auto_reloading and _attempt >= 2:
                            raise httpx.ConnectError(
                                "本地模型引擎未运行（端口无监听）。请到模型页加载模型。"
                            ) from e
                    # 重载窗口期端口无进程（连接被拒）或读超时/连接重置：
                    # 等待重试（自动重载完成即恢复）
                    if _attempt < 71:
                        last_err = e
                        await asyncio.sleep(5)
                        continue
                    raise
            if last_err is not None:
                raise last_err
        finally:
            if _is_local_llm_url(api_url):
                local_llm._engine.mark_engine_idle()

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
        "description": "Create a scheduled task with cron expression. Schedule is standard 5-field cron, task is Chinese description.",
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

# 工具列表顺序 = 模型看到的"优先级"：搜索类模型明显倾向选靠前的工具。
# 插件按文件名排序加载（bing_search.py < tavily_search.py），导致 tavily 永远排在
# bing 之后，且 _cap_tools 按原序截断时 tavily_search 总被先切掉——模型根本没机会
# 看到 tavily。按语义优先级只重排一次，保证 tavily 排在搜索组最前、截断时优先保留。
_TOOL_PRIORITY = (
    "read_file", "write_file", "list_dir", "search_files",
    "tavily_search", "web_search", "bing_search",
    "mx_query", "ak_finance",
    "open_app", "open_folder", "run_cmd",
    "delegate_task", "create_cron",
)
_TOOL_RANK = {_name: _rank for _rank, _name in enumerate(_TOOL_PRIORITY)}
TOOLS.sort(key=lambda _t: _TOOL_RANK.get(_t.get("function", {}).get("name", ""), len(_TOOL_RANK)))


async def execute_tool(tool_name: str, arguments: dict) -> str:
    """Execute a tool with feedback verification. Supports both sync and async tool functions."""
    fn = TOOL_DISPATCH.get(tool_name)
    if not fn:
        return f"Error: Unknown tool '{tool_name}'"
    try:
        if asyncio.iscoroutinefunction(fn):
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





def _deduplicate_response(text: str) -> str:
    """Remove repeated identity introductions. Keeps only the first complete one."""
    if not text:
        return text
    # Pattern: text starts with "我是辣条...", then finds "我是辣条" again
    import re
    for prefix in ["我是辣条", "我是 LaTiao", "我是LaTiao", "我是Latiao", "我叫辣条", "我是拉条"]:
        pattern = re.escape(prefix)
        m = re.search(f'^({pattern}.*?){pattern}', text, re.DOTALL)
        if m:
            return m.group(1).strip()
        m = re.search(f'^(你好[，！、\\s]*{pattern}.*?)(?:你好[，！、\\s]*)?{pattern}', text, re.DOTALL)
        if m:
            return m.group(1).strip()
    return text


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
#  Dynamic Tool Filtering: intent-based tool selection
# ═══════════════════════════════════════════════════════

TOOL_CATEGORIES = {
    "file_read": ["read_file", "list_dir", "search_files"],
    "file_write": ["write_file"],
    "command": ["run_cmd"],
    "app": ["open_app", "open_folder"],
    "web": ["tavily_search", "web_search", "bing_search"],
    "financial": ["mx_query", "ak_finance"],
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
    (re.compile(r"上网|联网|搜索网络|搜一下|搜一搜|查一下|查询|查一查|了解一下|最新的|最新消息|新闻|热搜|汇率|天气|资料|search|web|online|latest|news|weather|trending", re.IGNORECASE),
     ["file_read", "web"]),
    # 信息询问型问题（“X 是什么/有哪些/对比/评测”）：给出搜索工具，模型按需调用
    (re.compile(r"是什么|什么是|有哪些|有什么|为什么|如何|怎么|怎么样|怎么回事|介绍一下|介绍下|原理|机制|评测|测评|对比|区别|哪款|哪家|哪个|性价比|值不值得", re.IGNORECASE),
     ["file_read", "web"]),
]


# 权限模式五档（自主权从低到高）：
#   read_only  只读 —— 只能查询，改不了任何东西
#   confirm    变更前确认 —— 高风险操作每次确认（= 默认行为）
#   auto_edit  自动编辑 —— 文件类工具免确认（write_file/open_folder）
#   plan       计划模式 —— 动工前先出方案确认 + 高危确认
#   full       完全访问 —— 仅高危确认，其余自动
READ_ONLY_TOOLS = {"read_file", "list_dir", "search_files", "tavily_search", "web_search", "bing_search"}
AUTO_EDIT_TOOLS = {"write_file", "open_folder"}
ACCESS_LEVELS = {"read_only", "confirm", "auto_edit", "plan", "full"}
# 旧版本 workspace 档位迁移到 auto_edit（语义对应）
_LEGACY_ACCESS_MAP = {"workspace": "auto_edit"}


def _normalize_access(mode: str) -> str:
    return _LEGACY_ACCESS_MAP.get(mode, mode) if mode in _LEGACY_ACCESS_MAP or mode in ACCESS_LEVELS else "full"


def _filter_tools_by_access(tools: list[dict], access: str) -> list[dict]:
    """按权限模式过滤工具列表（读时过滤 + 执行时拦截双保险）。"""
    access = _normalize_access(access)
    if access != "read_only":
        return tools
    out = [t for t in tools if t.get("function", {}).get("name", "") in READ_ONLY_TOOLS]
    return out or [t for t in tools if t.get("function", {}).get("name") in READ_ONLY_TOOLS]


def _check_access(tool_name: str, access: str) -> str | None:
    """执行时权限拦截：返回拒绝原因或 None（放行）。read_only 档强制只读，其余档不拦截。"""
    access = _normalize_access(access)
    if access == "read_only" and tool_name not in READ_ONLY_TOOLS:
        return f"⛔ 当前为只读模式，工具 {tool_name} 不可用。请切换到自动编辑/计划模式/完全访问后重试。"
    return None


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
    # Only add web/financial tools when relevant (not unconditionally)
    if "financial" not in allowed_categories and "web" not in allowed_categories:
        allowed_tools.add("tavily_search")
        allowed_tools.add("mx_query")
        allowed_tools.add("bing_search")
        allowed_tools.add("ak_finance")
    filtered = [t for t in all_tools if t.get("function", {}).get("name") in allowed_tools]
    return filtered if filtered else all_tools



def _cap_tools(tools: list[dict], cap: int = 8) -> list[dict]:
    """Cap tool count, keeping essential tools (read_file, write_file, list_dir) first.
    先去重（DeepSeek 等 API 要求工具名唯一，重复名字直接 400）。"""
    seen: set[str] = set()
    uniq: list[dict] = []
    for t in tools:
        n = t.get("function", {}).get("name")
        if n and n not in seen:
            seen.add(n)
            uniq.append(t)
    essential = {"read_file", "write_file", "list_dir"}
    priority = [t for t in uniq if t.get("function", {}).get("name") in essential]
    others = [t for t in uniq if t.get("function", {}).get("name") not in essential]
    return priority + others[:max(0, cap - len(priority))]



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

_PLAN_KEYWORDS = (
    "分析", "报告", "调研", "研究", "构建", "搭建", "部署", "修复", "排查",
    "优化", "重构", "设计", "开发", "写一个", "写一份", "写一篇", "总结",
    "对比", "评估", "方案", "规划",
    "analyze", "report", "research", "build", "deploy", "fix", "refactor",
    "design", "develop", "compare", "evaluate", "plan",
)


def _should_plan(user_text: str, is_local: bool) -> bool:
    """复杂任务触发规划模式：任务关键词 + 消息够长。本地模型不触发（避免额外等待）。"""
    if is_local or not user_text or len(user_text.strip()) < 30:
        return False
    t = user_text.lower()
    return any(k in t for k in _PLAN_KEYWORDS)


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


def _inject_thinking_disabled(body: dict, model: str, level: str = "high") -> dict:
    """思考强度三档（对应前端 🧠 选择器）：off / high(默认) / max。
    - off: 显式关闭思考（DeepSeek 设 thinking disabled；各 API 兼容）
    - high: 思考开启（默认，reasoning_content 回传已处理）
    - max: 思考开启 + 更大 max_tokens（长推理任务不截断）"""
    m = (model or "").lower()
    if level == "off":
        body["thinking"] = {"type": "disabled"}
    elif level == "max":
        # 长推理预算：高于常规 reasoning 预算（12288）约 1.5 倍
        body.setdefault("max_tokens", 12288)
        if isinstance(body.get("max_tokens"), int) and body["max_tokens"] < 18432:
            body["max_tokens"] = 18432
        # OpenAI 兼容推理模型支持 reasoning_effort（DeepSeek 不认则该字段忽略）
        if "deepseek" not in m and "o1" not in m and "o3" not in m:
            body["reasoning_effort"] = "high"
    return body


def _sanitize_tool_messages(msgs: list[dict]) -> list[dict]:
    """DeepSeek 等 API 严格校验：assistant 消息带 tool_calls 时，后续必须有
    对应的 tool 结果消息（tool_call_id 一一对应），否则返回 400。
    历史消息可能因工具中断/前端保存丢失 tool 结果 → 自动补空结果消息，避免 400。
    补丁必须紧跟缺失点插入（任何非 tool 消息出现前），保证顺序合法。"""
    out: list[dict] = []
    pending_ids: set[str] = set()
    for msg in msgs:
        role = msg.get("role")
        if role == "assistant" and msg.get("tool_calls"):
            if pending_ids:  # 上一条 assistant 的 tool_calls 未响应，先补空
                for tid in pending_ids:
                    out.append({"role": "tool", "tool_call_id": tid, "content": "[工具结果缺失，已自动补空]"})
            pending_ids = {tc.get("id") for tc in msg["tool_calls"] if tc.get("id")}
            out.append(msg)
            continue
        if role == "tool":
            tid = msg.get("tool_call_id")
            if tid in pending_ids:
                pending_ids.discard(tid)
            out.append(msg)
            continue
        if pending_ids:  # 遇到非 tool 消息时，未响应的 tool_call 先补空
            for tid in pending_ids:
                out.append({"role": "tool", "tool_call_id": tid, "content": "[工具结果缺失，已自动补空]"})
            pending_ids = set()
        out.append(msg)
    if pending_ids:  # 消息末尾仍有未响应的 tool_call
        for tid in pending_ids:
            out.append({"role": "tool", "tool_call_id": tid, "content": "[工具结果缺失，已自动补空]"})
    return out


def _resolve_max_tokens(model: str) -> int:
    """Pick max_tokens by model family.

    Reasoning models (DeepSeek-R1, Qwen3-QwQ, OpenAI o-series, *-think/*-reason)
    emit long <think> blocks that can exhaust a 4096 cap and truncate the
    trailing tool_call JSON. Give them a larger budget so the JSON survives;
    non-reasoning models get a smaller, cheaper budget.
    """
    m = (model or "").lower()
    if any(k in m for k in ("r1", "o1", "o3", "o4", "reason", "qwq", "qwen3", "think", "muse", "glimmer", "deepseek")):
        return 12288
    return 6144


# Session state tracking: session_id → {phase, round, stalled_rounds, last_action}
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
    # SKILL_INDEX 由 main.py 门面持有 → 函数内 lazy import 避免循环依赖
    from main import SKILL_INDEX
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
        logger.warning(f"Pre-tool hook failed for {tool_name}", exc_info=True)  # don't block execution
    return False, [], ""


async def _handle_tool_execution(tc: dict, current_msgs: list, session_id: str,
                                  agent_id: str, access_mode: str = "full") -> tuple[bool, list[dict]]:
    """Execute a single tool call within the agent loop. Returns (verify_failed, events)."""
    call_id = tc.get("id") or str(uuid.uuid4())
    func = tc.get("function", {})
    tool_name = func.get("name", "unknown")
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
    if _resolve_permission(tool_name, args) == "confirm" and not _auto_edit_bypass:
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
        tool_content = (
            tool_content[:MAX_TOOL_RESULT]
            + f"\n\n... (工具结果过长,已截断。完整结果 {len(result)} 字符已记录,"
            + "如需查看特定部分请用 read_file 分段读取对应文件。)"
        )
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

# ╔══════════════════════════════════════════════════════╗
# ║  SECTION 6: Parsing & Formatting                     ║
# ║  _parse_native_tool_calls, _parse_prompt_tool_calls  ║
# ╚══════════════════════════════════════════════════════╝

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


# ╔══════════════════════════════════════════════════════╗
# ║  SECTION 7: Cloud Agent Loop                         ║
# ║  OpenAI-compatible function calling                   ║
# ╚══════════════════════════════════════════════════════╝

def _append_loop_log(line: str):
    """追加一行到 latiao-loop.log；超过 5MB 时截断保留尾部一半，避免无界增长。"""
    try:
        log_path = os.path.join(tempfile.gettempdir(), "latiao-loop.log")
        if os.path.exists(log_path) and os.path.getsize(log_path) > 5 * 1024 * 1024:
            with open(log_path, "rb") as f:
                f.seek(-(5 * 1024 * 1024 // 2), os.SEEK_END)
                tail = f.read()
            with open(log_path, "wb") as f:
                f.write(tail)
        with open(log_path, "a") as lf:
            lf.write(line)
    except Exception:
        pass  # 调试日志写失败不影响主流程


async def _agent_loop_stream(messages: list, model: str, api_url: str, headers: dict, session_id: str = "", agent_id: str = "latiao", reflection_mode: str = "off", access_mode: str = "full", thinking_level: str = "high"):
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
    text_only_streak = 0   # 与 local loop 对齐，消除空响应分支 (text_only_streak += 1) 的 NameError 崩溃
    text_output_delivered = False  # nudge 重试期间抑制已交付文本的重复流式输出
    # 总时长看门狗：单次请求硬上限 15 分钟。此前模型服务器偶发 hold 连接
    # 滴灌字节可绕过单次 read timeout（180s×N），用户面对 18 分钟无响应。
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
        active_tools = _cap_tools(active_tools, 5)

    async with httpx.AsyncClient(timeout=httpx.Timeout(120)) as client:
        # ── 规划模式：复杂任务先生成执行计划（显示给用户，按计划执行） ──
        if _should_plan(last_user_text, _is_local_llm_url(api_url)):
            _plan = await _generate_plan(last_user_text, model, api_url, headers, client)
            if _plan:
                yield {"event": "agent_plan", "content": _plan}
                current_msgs.insert(0, {"role": "system",
                    "content": "以下是已确定的执行计划，请严格按计划逐步执行（可调用工具）：\n" + _plan})

        while iteration < 50:  # hard cap at 50, dynamic exit via stagnation
            iteration += 1
            if time.monotonic() > loop_deadline:
                logger.error("[AGENT] 总时长超 900s，中止任务")
                yield {"content": "\n\n⚠️ 任务总时长超过 15 分钟上限，已中止。模型服务可能异常（如响应停滞）。可重试或检查网络。"}
                _track_progress(session_id, "stalled", "total_duration_limit")
                return
            # Re-evaluate tool set every 3 iterations for multi-step tasks
            if iteration > 1 and iteration % 3 == 0:
                # 恢复全量工具，但仍须套用权限过滤（read_only 等模式不可绕过）
                # （本函数为云端循环，无 is_local 变量；本地循环独立实现）
                active_tools = _cap_tools(_filter_tools_by_access(agent_tools, access_mode), 5)
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
            body = {
                "model": model, "messages": _sanitize_tool_messages(current_msgs),
                "tools": active_tools, "tool_choice": "auto",
                "max_tokens": _resolve_max_tokens(model), "stream": True,
                "temperature": 0.5,
                "frequency_penalty": 0.6,
                "stop": ["<|im_end|>", "<|endoftext|>", "<end_of_turn>", "<eos>"],
            }
            _inject_thinking_disabled(body, model, thinking_level)

            streamed_text = ""
            reasoning_text = ""  # 累积 reasoning_content——DeepSeek 推理模型要求传回
            tool_call_bufs: dict[int, dict] = {}

            async with client.stream("POST", api_url, json=body, headers=headers) as r:
                if r.status_code != 200:
                    try:
                        err_body = (await r.aread()).decode("utf-8", errors="replace")[:800]
                    except Exception:
                        err_body = "<read failed>"
                    logger.error("Agent stream HTTP %d body: %s", r.status_code, err_body)
                r.raise_for_status()  # httpx 不自动抛 4xx/5xx，必须显式检查
                # 流式停顿检测：连续 180s 无数据视为僵死（大模型可能缓慢滴灌，120s 超时永不触发）
                aiter = r.aiter_lines()
                while True:
                    try:
                        line = await asyncio.wait_for(anext(aiter), timeout=180)
                    except asyncio.TimeoutError:
                        raise TimeoutError(f"模型输出停滞超 180 秒（模型可能过大或未加载完）：{model[:60]}")
                    except StopAsyncIteration:
                        break
                    if line and line.startswith("data: "):
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            break
                        try:
                            event = json.loads(data_str)
                            choices = event.get("choices") or []
                            if not choices:
                                continue  # usage-only chunk（仅 token 统计，无 delta）
                            delta = choices[0].get("delta", {})

                            content = delta.get("content", "")
                            reasoning = delta.get("reasoning", "")
                            if content:
                                streamed_text += content
                                # Anti-repetition: skip tokens once the first complete intro is detected
                                if len(_deduplicate_response(streamed_text)) < len(streamed_text):
                                    continue
                                # nudge 重试期间不再向用户重复输出已交付的文本
                                if text_output_delivered:
                                    continue
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
                                reasoning_text += reasoning
                                streamed_text += reasoning
                                if len(_deduplicate_response(streamed_text)) < len(streamed_text):
                                    continue
                                if text_output_delivered:
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
                _append_loop_log(f"Iteration {iteration}: found {len(tool_calls)} tool(s): {[tc.get('function',{}).get('name') for tc in tool_calls]}\n")
                _track_progress(session_id, "tool_calling", f"{len(tool_calls)} tool(s)")
                logger.info(f"[LOCAL-AGENT] Iteration {iteration}: {len(tool_calls)} tool(s) called, msgs_in_context={len(current_msgs)}")

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
                    verify_failed, events = await _handle_tool_execution(
                        tc, current_msgs, session_id, agent_id, access_mode)
                    logger.info(f"[LOCAL-AGENT] Iteration {iteration}: tool={tc.get('function',{}).get('name','')} executed, result_len={len(current_msgs[-1].get('content','')) if current_msgs else 0}")
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
            if has_recent_tool_result and text_only_streak < 1 and streamed_text.strip():
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
                    _track_progress(session_id, "completed", f"text_response ({len(streamed_text)} chars)")
                    return
                # 任务型请求但模型只回文字不调工具 → nudge 促其行动（不再向用户重复流式输出）
                current_msgs.append({
                    "role": "system",
                    "content": (
                        "不要写执行计划，直接行动。需要用什么工具就立即调用。"
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
                current_msgs.append({"role": "system", "content": nudge_text})
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
    for idx, m in enumerate(_PROMPT_TOOL_FENCE_RE.finditer(search_text)):
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
        for idx, m in enumerate(_PROMPT_TOOL_RE.finditer(search_text)):
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
        for idx, m in enumerate(_NL_TOOL_RE.finditer(search_text)):
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
                args = {"cmd": raw_query}
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
        for idx, m in enumerate(_BASH_BLOCK_RE.finditer(search_text)):
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
                args = {"directory": find_match.group(1), "pattern": find_match.group(2).strip()}
            else:
                tool_name = "run_cmd"
                args = {"cmd": cmd_text}
            if tool_name:
                tool_calls.append({
                    "id": f"local_bash_{tool_name}_{idx}",
                    "type": "function",
                    "function": {"name": tool_name, "arguments": json.dumps(args, ensure_ascii=False)},
                })
                used_ranges.append((m.start(), m.end()))

    # Clean text by removing parsed tool call regions
    # 关键：used_ranges 是在 search_text（去 think 块+strip）上计算的，
    # 必须对同一坐标系清洗——此前回放到原始 text 上导致偏移错位：
    # 工具栅栏残留在历史里、think 块被拦腰截断，模型下一轮重复调用。
    clean = search_text
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
                                    session_id: str = "", agent_id: str = "latiao", reflection_mode: str = "off", access_mode: str = "full", thinking_level: str = "high"):
    """Local model agent loop: inject tools as prompt, parse tool calls from text."""
    global _llm_suspect_since  # 引擎存疑标记（本函数内多处置位/清除）
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
    recent_tool_calls: set[str] = set()
    stagnation = 0
    max_stagnation = 3  # cap empty-response/dead-end retries to avoid hammering the model server
    text_only_streak = 0
    has_called_tool = False
    text_output_delivered = False  # nudge 重试期间抑制已交付文本的重复流式输出
    # Build tool prompt
    last_user_text = _extract_last_user_text(current_msgs)
    agent_tools = _get_agent_tools(agent_id, TOOLS)
    active_tools = _filter_tools(last_user_text, agent_tools) if last_user_text else agent_tools
    active_tools = _filter_tools_by_access(active_tools, access_mode)
    if len(active_tools) > 8:
        active_tools = _cap_tools(active_tools, 8)
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
            "\n\n⚠️⚠️⚠️ 你现在处于任务执行中途！\n"
            f"会话中已有 {tool_result_count} 条工具执行结果，但任务尚未完成。\n"
            "你必须继续使用工具完成用户的原始请求，不能只回复文字说'好的'或'正在处理'。\n"
            "直接调用工具，不要废话。"
        )

    # Inject tools into the first user message context
    for m in current_msgs:
        if m.get("role") == "user":
            # Insert tools prompt as a system message right before the last user message
            break

    async with httpx.AsyncClient(timeout=httpx.Timeout(120)) as client:
        while iteration < max_iterations:
            iteration += 1
            _append_loop_log(f"Iteration {iteration}: current_msgs={len(current_msgs)}, roles={[m.get('role') for m in current_msgs[-5:]]}\n")

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
            for m in loop_msgs:
                if m.get("role") == "system":
                    m["content"] = m["content"] + "\n\n" + current_prompt
                    break
            else:
                loop_msgs.insert(0, {"role": "system", "content": current_prompt})

            body = {
                "model": model, "messages": loop_msgs,
                "max_tokens": _resolve_max_tokens(model), "stream": True,
                "temperature": 0.5,
                "frequency_penalty": 0.6,
                "stop": ["<|im_end|>", "<|endoftext|>", "<end_of_turn>", "<eos>"],
            }

            streamed_text = ""
            _raw_delta_count = 0  # 诊断: 统计收到的 delta 数(空响应时判断是模型真空还是解析丢了)
            logger.info(f"[LOCAL-AGENT] Iteration {iteration}: calling LLM, msgs={len(loop_msgs)}, first_user_content_len={len(loop_msgs[-1].get('content','')) if loop_msgs else 0}")
            # 本地 llama.cpp 并发流式请求会崩溃 → _local_llm_stream 内部串行化
            # 流式停顿检测：模型过大/未加载完时可能极慢滴灌（120s 超时永不触发），
            # 连续 180s 无任何数据视为僵死，中止不再无限挂起
            async with _local_llm_stream(client, api_url, body, headers) as r:
                aiter = r.aiter_lines()
                while True:
                    try:
                        line = await asyncio.wait_for(anext(aiter), timeout=180)
                    except asyncio.TimeoutError:
                        _llm_suspect_since = _llm_suspect_since or time.monotonic()
                        raise TimeoutError(
                            f"本地模型输出停滞超 180 秒（模型可能过大或未加载完）：{model[:60]}"
                        )
                    except StopAsyncIteration:
                        break
                    if line and line.startswith("data: "):
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            break
                        try:
                            event = json.loads(data_str)
                            choices = event.get("choices") or []
                            if not choices:
                                continue  # usage-only chunk（仅 token 统计，无 delta）
                            delta = choices[0].get("delta", {})
                            _raw_delta_count += 1
                            content = delta.get("content", "")
                            # LM Studio/方舟等返回 reasoning_content,OpenAI o 系列返回 reasoning
                            reasoning = delta.get("reasoning") or delta.get("reasoning_content") or ""
                            if content:
                                streamed_text += content
                                # Anti-repetition: skip tokens after the first complete intro
                                if len(_deduplicate_response(streamed_text)) < len(streamed_text):
                                    continue
                                # nudge 重试期间不再向用户重复输出已交付的文本
                                if text_output_delivered:
                                    continue
                                yield {"content": content}
                            elif reasoning:
                                streamed_text += reasoning
                                if len(_deduplicate_response(streamed_text)) < len(streamed_text):
                                    continue
                                if text_output_delivered:
                                    continue
                                yield {"reasoning": reasoning, "ts": int(time.time() * 1000)}
                        except (json.JSONDecodeError, KeyError, TypeError, IndexError):
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


            # Whitelist: only execute tools in the active set
            tool_names = {t.get("function", {}).get("name") for t in active_tools}
            tool_calls = [tc for tc in tool_calls if tc.get("function", {}).get("name") in tool_names]
            if tool_calls:
                _track_progress(session_id, "tool_calling", f"{len(tool_calls)} tool(s)")

                # Add assistant message (cleaned text)
                current_msgs.append({
                    "role": "assistant",
                    "content": clean_text or "正在调用工具...",
                })
                has_called_tool = True
                text_output_delivered = False  # 工具被调用=实质推进，后续文本是新的最终回复，恢复流式输出

                any_new = False
                round_failed = False
                for tc in tool_calls:
                    sig = f"{tc.get('function',{}).get('name','')}:{hash(str(tc.get('function',{}).get('arguments',''))) } "
                    if sig not in recent_tool_calls:
                        recent_tool_calls.add(sig)
                        any_new = True
                    verify_failed, events = await _handle_tool_execution(
                        tc, current_msgs, session_id, agent_id, access_mode)
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
                    text_only_streak = 0
                else:
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
            if has_recent_tool_result and text_only_streak < 1 and streamed_text.strip():
                # Model returned text after a tool result but didn't call another tool.
                # 推理模型(Muse/Qwen3.5)经常先输出 <think> 思考 + 规划文字,
                # 下一轮才实际调用工具--这不是停滞,不该 nudge 打断节奏。
                # 只有当模型明确在"闲聊/总结"而非"规划下一步工具调用"时才 nudge。
                _planning_signals = ("tool", "工具", "读取", "查询", "搜索", "执行", "调用",
                                     "read_file", "list_dir", "run_cmd", "write_file",
                                     "search", "tavily", "mx_query", "step", "步骤", "下一步")
                _is_planning = any(sig in streamed_text.lower() for sig in _planning_signals)
                if _is_planning and text_only_streak < 2:
                    # 模型在规划下一步,给它一轮空间自然调工具,不 nudge
                    current_msgs.append({"role": "assistant", "content": streamed_text.strip()})
                    text_only_streak += 1
                    text_output_delivered = True
                    continue
                logger.info(f"[LOCAL-AGENT] Iteration {iteration}: model returned text after tool result, pushing for continuation")
                current_msgs.append({
                    "role": "system",
                    "content": _get_localized_text(_detect_user_language(_extract_last_user_text(current_msgs)), {
                        "zh": "⚠️ 你刚才收到了工具的执行结果，但只回复了文字而没有继续调用工具。\n请检查任务是否完成，未完成请继续调用工具。\n调用工具格式：```tool 工具名\n{\"参数\":\"值\"}\n```",
                        "en": "⚠️ You received tool results but only replied with text.\nCheck if the task is complete, if not continue calling tools.\nFormat: ```tool tool_name\n{\"param\":\"value\"}\n```",
                        "ja": "⚠️ ツール実行結果を受け取りましたが、テキストのみ返信しました。\nタスク完了を確認し、未完了の場合はツールを続けて呼び出してください。\n形式：```tool ツール名\n{\"パラメータ\":\"値\"}\n```",
                    }),
                })
                text_output_delivered = True  # 文本已交付，nudge 重试不再重复输出
                text_only_streak += 1
                continue
            if not has_called_tool and text_only_streak < 3 and streamed_text.strip():
                # Model gave a text response without calling tools.
                # Record the response so the model knows it already replied.
                current_msgs.append({"role": "assistant", "content": streamed_text.strip()})
                # 非任务型消息（闲聊/陈述/提问/长回复）→ 文本已交付给用户，直接结束，不再 nudge 重发
                user_q = _extract_last_user_text(current_msgs).strip().rstrip("?？") if current_msgs else ""
                has_task_kw = any(kw in user_q for kw in ["运行", "执行", "做", "帮我", "写", "创建", "查", "搜", "找", "分析", "修复", "构建", "部署", "安装", "配置", "run", "build", "fix", "create", "search", "analyze", "deploy"])
                if not has_task_kw:
                    _track_progress(session_id, "completed", f"text_response ({len(streamed_text)} chars)")
                    return
                # 任务型请求但模型只回文字不调工具 → nudge 促其行动（不再向用户重复流式输出）
                logger.info(f"[LOCAL-AGENT] Iteration {iteration}: model planning instead of calling tools, nudging (streak={text_only_streak})")
                current_msgs.append({
                    "role": "system",
                    "content": (
                        "不要写执行计划，直接行动。需要用什么工具就立即调用。"
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
                    _llm_suspect_since = _llm_suspect_since or time.monotonic()
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
            if _should_reflect(reflection_mode, streamed_text, _is_local_llm_url(api_url)):
                _tool_outs = [str(m.get("content") or "") for m in current_msgs if m.get("role") == "tool"]
                _revised, _changed = await _reflect_output(streamed_text, model, api_url, headers, reflection_mode, client, _tool_outs)
                if _changed and _revised.strip():
                    streamed_text = _revised
                    yield {"event": "reflection_revised", "content": _revised}

            _track_progress(session_id, "completed", f"text_response ({len(streamed_text)} chars)")
            logger.info(f"[LOCAL-AGENT] Iteration {iteration}: no tools, returning text ({len(streamed_text)} chars)")
            return

        tool_count = sum(1 for m in current_msgs if m.get("role") == "tool")
        yield {"content": f"\n\n⚠️ 已达到硬上限 ({max_iterations} 轮)。本会话共执行了 {tool_count} 次工具调用。如需继续，请发送新消息。"}


# ╔══════════════════════════════════════════════════════╗
# ║  SECTION 9: Chat Building & LLM Config               ║
# ║  _build_chat_messages, _resolve_api_target, etc.     ║
# ╚══════════════════════════════════════════════════════╝

def _build_chat_messages(body: dict, messages: list, matched_skill: str|None = None) -> list:
    """Assemble the full message array with identity, env, skills, agent, and image injections.
    All system prompts are merged into ONE message to work around a llama-cpp bug
    where multiple system messages cause empty responses."""
    # 技能系统/提示词由 main.py 门面持有 → 函数内 lazy import 避免循环依赖
    from main import SKILL_INDEX, _build_skill_prompt
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
        logger.warning("Failed to read cloud models config", exc_info=True)
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
        logger.warning("Failed to read best cloud config", exc_info=True)
    return None
