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
            # model 必须用真实加载的 id：假名会让 mlx_lm.server 去 Hub 按名
            # 解析 → 镜像 SSL 校验失败 → 健康检查对健康引擎也报死。
            _model_ref = getattr(local_llm._engine, "current_model_id", "") or ""
            resp = await c.post(api_url, json={
                "model": _model_ref, "stream": False, "max_tokens": 4,
                "messages": [{"role": "user", "content": "hi"}],
            })
            resp.raise_for_status()
            data = resp.json()
            _msg = (data.get("choices") or [{}])[0].get("message", {}) or {}
            content = (_msg.get("content") or "").strip()
            reasoning = (_msg.get("reasoning") or _msg.get("reasoning_content") or "").strip()
            # ⚠️ 推理模型（Ornith/Qwen3.8）max_tokens=4 时 token 全进 <think>，
            # content 空但 reasoning 有字——按 content 判死会把健康引擎杀了
            # 重载（与 local_llm.verify_engine_health 同样已修的坑，审计 P1）
            if not content and not reasoning:
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
    防止并发流式生成导致 server 崩溃（连接被 peer 关闭）。

    引擎死亡时的恢复语义（修复"任务执行一半停止"）：
    - 有可恢复资源（模型记录在、非用户主动停止、是我们自己管理的引擎）：
      触发防重入自动重载，本函数的 5s×72 等待循环覆盖 35B 重载窗口，
      请求自然排队恢复--不再因 kill 与 reload 置位之间的竞态窗口秒死；
    - 无可恢复资源（用户手动停止 / 从未加载模型 / 外部引擎 / 重载已失败）：
      快速失败并给出明确的下一步指引。
    """
    async with _local_llm_serialized(api_url):
        global _llm_suspect_since
        engine = local_llm._engine
        _local = _is_local_llm_url(api_url)
        from urllib.parse import urlparse
        try:
            _own_engine = (urlparse(api_url).port or engine.server_port) == engine.server_port
        except Exception:
            _own_engine = True

        if _local:
            engine.mark_engine_busy()
            engine.mark_stream_enter()
        # 本流内是否已请求过重载（幂等守卫：防重载失败结束后被反复拉起）。
        # 注意不能做"本流只请求一次重载"的单发守卫：引擎可能反复挂起/404，
        # 每个循环都需要重新杀+重载（15:41 事故：单发守卫让最后一轮杀完
        # 引擎却不重载，端口空置任务永等）。防重复由 _request_reload 内部的
        # _auto_reloading 同步标志保障。
        def _request_recovery_reload() -> bool:
            if (_own_engine and engine.current_model_id
                    and not getattr(engine, "_explicit_stop", False)
                    and not engine._auto_reloading
                    and engine.server_status not in ("starting", "error")):
                return engine._request_reload(engine.current_model_id)
            return False

        if _local and _llm_suspect_since is not None:
            try:
                ok = await _verify_llm_health(api_url)
            finally:
                # 健康检查抛错也必须配对 enter/exit——否则 _active_local_streams
                # 永久 +1 → 引擎"永久忙"→ 健康检查永远跳过 → 死引擎永不被发现
                # （审计 P1）
                if _local:
                    engine.mark_stream_exit()
                    engine.mark_engine_idle()
            _llm_suspect_since = None
            if not ok:
                # 端口确实死亡或引擎产出异常--先杀残留，再触发自动重载；
                # 下面的等待-重试循环会等到引擎就绪（此前直接秒死）
                try:
                    port = urlparse(api_url).port or engine.server_port
                    engine._kill_port(port)
                    if port == engine.server_port:
                        engine.server_status = "stopped"
                except Exception:
                    pass
                if not _request_recovery_reload() and not engine._auto_reloading:
                    # 重载无法进行：区分具体原因给出准确指引（重载进行中则
                    # 落入下方等待-重试循环排队，不再秒死）
                    if getattr(engine, "_explicit_stop", False):
                        raise httpx.ConnectError(
                            "本地模型已被手动停止，任务已中断。请到模型页重新加载模型后重发消息。")
                    if engine.server_status == "error":
                        raise httpx.ConnectError(
                            f"本地模型自动重载失败（{(engine.status_message or '未知错误')[:120]}）。"
                            "请到模型页检查模型。")
                    raise httpx.ConnectError(
                        "本地模型引擎状态异常，已自动停止。请到模型页重新加载模型。"
                    )
        # 引擎短暂闪断（404/503，如 mlx_lm 高负载重启窗口）自动重试，
        # 避免整轮任务因一次瞬时不可用被判死。
        # 生成器语义：yield 之后消费者持有 r 直到读完。流中途断裂（athrow 进来
        # 的异常）后生成器不能再次 yield（asynccontextmanager 协议会破坏）--
        # 生成器内只对"连接建立即失败"（还没 yield 过）的情况重试；
        # 流中途断裂抛出明确异常，由 agent 循环的零交付重试接管。
        last_err: Exception | None = None
        _yielded = False
        # 等待恢复的总时长上限：72 次尝试本意覆盖 ~6 分钟重载窗口（每次失败
        # 连接被拒是秒级的），但引擎"挂起"（端口活、不吐响应头）时单次尝试
        # 要耗满读超时 120s——无时间上限理论上可静默拖 2.5 小时。
        _wait_deadline = time.monotonic() + 600
        # 引擎挂起判定计数：连续 2 次读超时（端口活但不吐数据）
        _hung_strikes = 0
        try:
            for _attempt in range(72):
                if time.monotonic() >= _wait_deadline:
                    raise httpx.ConnectError(
                        "等待本地模型恢复超时（10 分钟）。请到模型页检查引擎状态后重发消息。"
                    )
                try:
                    async with client.stream("POST", api_url, json=body, headers=headers) as r:
                        r.raise_for_status()  # httpx 不自动抛 4xx/5xx，必须显式检查
                        _yielded = True
                        try:
                            yield r
                        except asyncio.CancelledError:
                            if _local:
                                # 流被取消（用户点停止/发了新消息）-> 引擎生成线程可能残留
                                # 并继续跑 llama.cpp，下次请求前必须验证健康
                                _llm_suspect_since = _llm_suspect_since or time.monotonic()
                            raise
                        return
                except httpx.HTTPStatusError as e:
                    if _yielded:
                        raise  # 流中途断裂：生成器内不能重试（见函数注释）
                    _status = e.response.status_code
                    if _status in (404, 503) and _attempt < 71:
                        last_err = e
                        # 本地引擎连续 404 = 引擎状态损坏（挂起的 404 变体，
                        # 15:25 事故：模型明明加载着，迭代 2 却连续 6 分钟 404）。
                        # 健康引擎对正确路径绝不会 404——连续 2 次后杀+重载，
                        # 而不是空转 71×5s 后报"模型未就绪"。503 保持纯等待语义。
                        # ⚠️ 但 404 也可能是"模型名不匹配"：mlx_lm.server 对
                        # 未加载的 model 名（如 UI 里选中的 cloud 名 gpt-4o-mini）
                        # 也回 404，但引擎是健康的——此时绝不能杀（21:06 事故：
                        # 用户模型选了 gpt-4o-mini，本地循环发它 → 404 → 误杀
                        # 26GB 引擎重载，事件循环卡死 2 分钟）。
                        _req_model = str(body.get("model") or "")
                        _loaded = str(getattr(engine, "current_model_id", "") or "") + "|" + str(getattr(engine, "current_model_name", "") or "")
                        _model_mismatch = bool(_req_model) and _req_model not in _loaded and _req_model not in ("health-check",)
                        if (_status == 404 and _local and _own_engine
                                and _attempt >= 1 and not engine._auto_reloading
                                and engine.server_status != "starting"
                                and time.monotonic() - getattr(engine, "_engine_started_at", 0.0) > 120
                                and not _model_mismatch):
                            # 注意 starting 保护：手动加载期间 chat 接口 404 是
                            # 常态（模型未就绪），绝不能杀正在加载的引擎（P1-7）。
                            # 120s 宽限期是第二道防线：即便 "模型加载完成" 被
                            # 误报（状态已置 running 但权重还在载入，20:05 事故的
                            # 假完成），引擎启动后 2 分钟内也只等待不杀——
                            # 35B 权重加载可长达数分钟，被误杀只会再拉一轮重载。
                            logger.warning("本地引擎连续 404，判定状态损坏，强制重载")
                            try:
                                engine._kill_port(engine.server_port)
                                engine.server_status = "stopped"
                            except Exception:
                                pass
                            _request_recovery_reload()
                        await asyncio.sleep(5)  # 5s × 72 = 最大约 6 分钟等待
                        continue
                    raise
                except httpx.TransportError as e:
                    # TransportError = ConnectError/ReadError/WriteError/
                    # RemoteProtocolError/各超时的共同基类。引擎被杀可能表现为
                    # 其中任何一种（实测 kill -9 在预填充期抛 ReadError）。
                    if _yielded:
                        # 流中途断裂（引擎被杀/系统压力/网络断）：生成器内不能重发，
                        # 抛出明确语义的异常，由 agent 循环的零交付重试接管
                        raise httpx.RemoteProtocolError(
                            "本地模型流中断（引擎死亡或连接断开）。"
                            "若本轮尚无输出，任务将自动等待引擎恢复并重试。"
                        ) from e
                    if _local:
                        # 挂起检测：读超时/读错误 = 端口活着但不吐数据；连接超时
                        # 同样可能（引擎 accept 积压已满）。连续 2 次且没有其他
                        # 活跃流（=没有别的请求在生成长文本）→ 判定挂起，杀掉重载。
                        # 有其他活跃流时可能是排队等长生成，不动（A3 忙保护）。
                        if (isinstance(e, (httpx.ReadError, httpx.TimeoutException))
                                and engine._active_local_streams <= 1):
                            _hung_strikes += 1
                            if (_hung_strikes >= 2 and not engine._auto_reloading
                                    and engine.current_model_id
                                    and not getattr(engine, "_explicit_stop", False)
                                    and engine.server_status != "error"):
                                logger.warning(
                                    "引擎端口存活但连续读超时且无其他活跃流，判定挂起，强制重载")
                                try:
                                    engine._kill_port(engine.server_port)
                                    engine.server_status = "stopped"
                                except Exception:
                                    pass
                                _request_recovery_reload()
                        # 判断有无恢复资源，决定快速失败还是排队等重载。
                        # 此前只要 _auto_reloading 未置位就秒死，而引擎几秒后
                        # 就能自动恢复（kill 与 reload 置位之间的竞态窗口）。
                        if not _own_engine:
                            raise httpx.ConnectError(
                                "外部模型引擎（LM Studio/Ollama）未运行。请启动外部引擎后重试。"
                            ) from e
                        if getattr(engine, "_explicit_stop", False):
                            raise httpx.ConnectError(
                                "本地模型已被手动停止，任务已中断。请到模型页重新加载模型后重发消息。"
                            ) from e
                        _load_in_progress = engine.server_status == "starting"
                        if not engine.current_model_id and not _load_in_progress:
                            raise httpx.ConnectError(
                                "本地模型引擎未运行（端口无监听）。请到模型页加载模型。"
                            ) from e
                        # 有恢复资源：确保重载已触发（幂等，防重入）。
                        # 重载请求被拒且状态是 error = 上一次重载已失败收场，
                        # 快速失败并给出原因，不再无限等待。
                        if not engine._auto_reloading:
                            _started = _request_recovery_reload()
                            if not _started and engine.server_status == "error":
                                raise httpx.ConnectError(
                                    f"本地模型自动重载失败（{(engine.status_message or '未知错误')[:120]}）。"
                                    "请到模型页检查模型。"
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
            if _local:
                engine.mark_stream_exit()
                engine.mark_engine_idle()

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
    """Filter tools based on agent's allowed tools. 'all' means all tools.

    同时过滤 Tools 页被禁用的工具（capabilities.enabled=0）——此前禁用
    开关只写库、agent 管线从不读，禁用 run_cmd 后模型照常执行
    （审计 A1 安全项）。"""
    cfg = _get_agent_config(agent_id)
    allowed = cfg.get("tools", "all")
    if allowed != "all":
        all_tools = [t for t in all_tools if t.get("function", {}).get("name") in allowed]
    # 工具启用/禁用开关（惰性容错：capabilities 表未初始化时不过滤）
    try:
        from capability_registry import list_capabilities
        disabled = {c.get("name") for c in list_capabilities("tool") if not c.get("enabled")}
        if disabled:
            all_tools = [t for t in all_tools
                         if t.get("function", {}).get("name") not in disabled]
    except Exception:
        pass
    return all_tools


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


def _dispatch_use_skill(args: dict) -> str:
    """use_skill 分发：从 registry 取技能全文。未启用/不存在时返回可用目录。"""
    import capability_registry
    name = str(args.get("skill_name") or "").strip()
    if not name:
        return "错误: 缺少 skill_name 参数"
    skill = capability_registry.get_skill_content(name)
    if skill is None:
        catalog = capability_registry.skill_catalog()
        names = "、".join(s["name"] for s in catalog) or "(空)"
        return f"技能 {name!r} 不存在或已禁用。当前可用技能: {names}"
    return (
        f"# 技能: {skill['name']}\n"
        f"描述: {skill['description'] or '(无)'}\n"
        f"安全等级: {skill['permission']}\n\n"
        f"{skill['content']}"
    )


TOOL_DISPATCH["use_skill"] = _dispatch_use_skill
TOOL_PERMISSIONS["use_skill"] = "safe"

# 工具列表顺序 = 模型看到的"优先级"：搜索类模型明显倾向选靠前的工具。
# 插件按文件名排序加载（bing_search.py < tavily_search.py），导致 tavily 永远排在
# bing 之后，且 _cap_tools 按原序截断时 tavily_search 总被先切掉——模型根本没机会
# 看到 tavily。按语义优先级只重排一次，保证 tavily 排在搜索组最前、截断时优先保留。
_TOOL_PRIORITY = (
    "read_file", "write_file", "list_dir", "search_files",
    "tavily_search", "dokobot_read", "headless_read", "dokobot_search", "web_search", "bing_search",
    "mx_query", "ak_finance",
    "screen_capture", "control_list_processes", "control_process_log", "control_audit",
    "control_wait", "control_launch", "control_mouse_move", "control_mouse_click",
    "control_keyboard_type", "control_keyboard_press", "control_kill_process",
    "open_app", "open_folder", "run_cmd",
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


# ── MCP 扩展：启用扩展声明 mcpServers → 动态注册远程工具 ──
# 工具命名 mcp_<server>_<tool>；dispatch 异步转发，连接失败返回错误文本（不崩整轮）。
def _mcp_tool_name(server: str, tool: str) -> str:
    from mcp_client import sanitize_tool_name
    return sanitize_tool_name(f"mcp_{server}_{tool}")


async def _load_mcp_tools() -> None:
    """扫描启用扩展的 mcpServers 声明，把远程工具并入 TOOLS/DISPATCH。"""
    try:
        from extension_manager import active_extension_dirs, _read_manifest
        from mcp_client import get_mcp_client
        for ext_dir in active_extension_dirs():
            manifest = _read_manifest(ext_dir) or {}
            servers = manifest.get("mcpServers") or {}
            for srv_name, cfg in servers.items():
                if not isinstance(cfg, dict):
                    continue
                entry = f"{ext_dir.parent.name}/{srv_name}"
                try:
                    client = get_mcp_client(entry, cfg)
                    tools = await client.list_tools()
                except Exception as e:
                    logger.warning("MCP 连接失败 %s: %s", entry, e)
                    continue
                for t in tools:
                    tname = t.get("name", "")
                    if not tname:
                        continue
                    fname = _mcp_tool_name(srv_name, tname)
                    schema = (t.get("inputSchema") or {}).copy()
                    desc = t.get("description") or f"MCP 工具 {srv_name}:{tname}"
                    # 已有同名工具时跳过（内置优先）
                    if any(td.get("function", {}).get("name") == fname for td in TOOLS):
                        continue
                    TOOLS.append({
                        "type": "function",
                        "function": {"name": fname, "description": desc, "parameters": schema},
                    })
                    TOOL_PERMISSIONS[fname] = "safe"
                    TOOL_DISPATCH[fname] = (
                        lambda args, _srv=srv_name, _tool=tname, _entry=entry:
                        _mcp_invoke(_entry, _tool, args)
                    )
                    logger.info("MCP 工具注册: %s (%s)", fname, entry)
    except Exception:
        logger.warning("MCP 扩展扫描失败", exc_info=True)


async def _mcp_invoke(entry: str, tool: str, args: dict) -> str:
    try:
        from mcp_client import _MCP_CLIENTS
        client = _MCP_CLIENTS.get(entry)
        if client is None:
            return "⛔ MCP 连接已失效，请重启应用或重新安装扩展"
        return await client.call_tool(tool, args)
    except Exception as e:
        return f"⛔ MCP 工具调用失败: {e}"


_MCP_LOADED = False


def ensure_mcp_loaded() -> None:
    """进程内一次性 MCP 注册（幂等）。api_routes 每个请求入口调用。"""
    global _MCP_LOADED
    if _MCP_LOADED:
        return
    _MCP_LOADED = True
    try:
        import asyncio as _asyncio
        try:
            _asyncio.get_running_loop()
        except RuntimeError:
            _asyncio.run(_load_mcp_tools())
        else:
            # 已在事件循环内（理论上 api 层 async 调用前不至此）
            _asyncio.create_task(_load_mcp_tools())
    except Exception:
        logger.warning("MCP 工具注册失败", exc_info=True)


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

    # ── 统一能力计数：工具与技能（use_skill）共用 capabilities 表 ──
    try:
        import capability_registry
        capability_registry.bump_usage(tool_name)
        if tool_name == "use_skill":
            capability_registry.bump_usage(str(arguments.get("skill_name") or ""))
    except Exception:
        logger.debug("bump_usage failed for %s", tool_name, exc_info=True)

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
        # 体积轮转（审计 B10）：append-only 已长到 1.1MB 且无上限。
        # 超 1MB 时保留尾部 500KB——read_file 从头截断读最旧 5 万字符，
        # 轮转同时保证最近进度仍在文件里（尾部注入读的也是最新段）
        if PROGRESS_FILE.stat().st_size > 1024 * 1024:
            _rotate_progress_file()
    except Exception:
        logger.warning("Failed to record progress", exc_info=True)


def _rotate_progress_file():
    """把 PROGRESS.md 截到最近 500KB（保留尾部，即最新进度）。

    切点必须对齐 UTF-8 字符边界：按字节直切会在多字节汉字中间断开，
    文件从此不再是合法 UTF-8 → read_file 整个拒绝读取 → 断点续作失效
    （22:46 事故根因："文件编码不是 UTF-8"）。
    """
    try:
        keep = 500 * 1024
        size = PROGRESS_FILE.stat().st_size
        if size <= keep:
            return
        with open(PROGRESS_FILE, "rb") as f:
            f.seek(size - keep)
            tail = f.read()
        # 对齐字符边界：跳过开头的残缺多字节字符（找下一个 UTF-8 合法起始字节）
        offset = 0
        while offset < len(tail):
            b = tail[offset]
            # 合法起始：ASCII(<0x80) 或 2/3/4 字节前缀(0xC0-0xF7)
            if b < 0x80 or (0xC0 <= b <= 0xF7):
                break
            offset += 1
        tail = tail[offset:]
        with open(PROGRESS_FILE, "wb") as f:
            f.write("(早期进度已轮转)\n\n".encode("utf-8") + tail)
    except Exception:
        logger.warning("PROGRESS 轮转失败", exc_info=True)


def _progress_tail(max_chars: int = 600) -> str:
    """读取 PROGRESS.md 尾部（最新进度），用于注入 system prompt。

    600 而非 2000：PROGRESS.md 是 2/3 英文的工具日志，2000 字符的英文注入
    是新会话被带偏成英文回复的最大英文源（09-03 事故）。"""
    try:
        if not PROGRESS_FILE.exists():
            return ""
        size = PROGRESS_FILE.stat().st_size
        read = min(size, max_chars * 4)  # 多读些字节再截字符，中文占 3 字节
        with open(PROGRESS_FILE, "rb") as f:
            f.seek(max(0, size - read))
            tail = f.read().decode("utf-8", errors="replace")
        return tail[-max_chars:]
    except Exception:
        return ""





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


def _detect_text_loop(text: str) -> bool:
    """检测流式输出陷入复读循环（同一片段连续重复）。

    本地小模型长生成时偶发：最后两三句话无限重复直到 max_tokens。
    判定：取尾部窗口，若存在长度 ≥12 字符的片段在尾部连续重复 ≥3 次
    （或尾部 200 字符内同一片段出现 ≥4 次），判为循环。
    性能：只在流式累积每 ~2KB 时调用一次，非逐 token。"""
    if not text or len(text) < 120:
        return False
    tail = text[-600:]
    n = len(tail)
    # 尝试所有可能的循环片段长度（12 ~ 100 字符）
    for plen in range(12, min(100, n // 3) + 1):
        piece = tail[-plen:]
        if piece.strip() != piece or len(piece.strip()) < 12:
            continue
        # 该片段在尾部连续出现次数
        count = 0
        pos = n
        while pos - plen >= 0 and tail[pos - plen:pos] == piece:
            count += 1
            pos -= plen
        if count >= 3:
            return True
    # 兜底：短片段高频重复（如"好的，"×8）。注意 \1 是反向引用——
    # 此前误写成字面 \x01 控制字符，兜底永远不匹配（P1-10）
    import re as _re
    m = _re.search(r"(.{6,40}?)\1{3,}$", tail, _re.DOTALL)
    if m:
        return True
    return False


def _is_meta_wrapup(text: str) -> bool:
    """判断文本是否为“元评论式收尾”而非实质回答。

    思考类模型（Ornith/Qwen3.8）常把完整分析写在 CoT 里，正文只输出
    “上面的分析已经覆盖了…任务完成”之类的收尾话——用户什么都读不到
    （19:38 事故）。此类文本特征是：含任务完成声明短语、且长度不够
    承载真正的分析（<800 字符）。
    """
    if not text or len(text.strip()) >= 800:
        return False
    lowered = text.lower()
    markers = (
        "任务完成", "已完成", "已经完成", "上面的分析", "已经覆盖",
        "已覆盖", "分析已", "已为你", "任务已", "以上是", "以上内容",
        "task complete", "task is done", "i've already", "i have already",
        "the analysis", "already provided", "已经给出", "已给出",
    )
    hits = sum(1 for m in markers if m in lowered)
    return hits >= 2


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
    "scheduling": ["create_cron"],
    "file_read": ["read_file", "list_dir", "search_files"],
    "file_write": ["write_file"],
    "command": ["run_cmd"],
    "app": ["open_app", "open_folder"],
    "web": ["tavily_search", "web_search", "bing_search", "dokobot_read", "headless_read", "dokobot_search"],
    "financial": ["mx_query", "ak_finance"],
    # 五控：进程/鼠标/屏幕/流程
    "control": [
        "screen_capture", "control_list_processes", "control_process_log",
        "control_audit", "control_wait", "control_launch",
        "control_mouse_move", "control_mouse_click",
        "control_keyboard_type", "control_keyboard_press", "control_kill_process",
    ],
}
# 控制类工具在意图匹配不明确时也应保留（避免被 _filter_tools 滤掉）
CONTROL_TOOL_NAMES = set(TOOL_CATEGORIES["control"])

INTENT_PATTERNS = [
    # 定时任务意图：放最前——"每10分钟分析大盘"同时命中 financial，
    # 但用户首要诉求是建定时任务，先给 create_cron（任务内容由 cron 执行时
    # 独立跑 agent 循环处理，不受本次过滤影响）
    (re.compile(r"定时|每\s*\d+\s*(分钟|小时|天|周|秒)|每天|每小时|每周|每分钟|每个小时|cron|计划任务|日程|提醒|周期性", re.IGNORECASE),
     ["scheduling"]),
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
    (re.compile(r"上网|联网|搜索网络|搜一下|搜一搜|查一下|查询|查一查|了解一下|最新的|最新消息|新闻|热搜|汇率|天气|资料|网页|网址|链接|页面|http|url|search|web|online|latest|news|weather|trending", re.IGNORECASE),
     ["file_read", "web"]),
    # 信息询问型问题（“X 是什么/有哪些/对比/评测”）：给出搜索工具，模型按需调用
    (re.compile(r"是什么|什么是|有哪些|有什么|为什么|如何|怎么|怎么样|怎么回事|介绍一下|介绍下|原理|机制|评测|测评|对比|区别|哪款|哪家|哪个|性价比|值不值得", re.IGNORECASE),
     ["file_read", "web"]),
    # 五控：进程/鼠标/屏幕/流程控制意图
    (re.compile(r"进程|pid|杀|终止|启动|后台运行|后台任务|运行中|列表进程|进程列表|cpu|内存占用|tasklist|kill|process|list_process", re.IGNORECASE),
     ["control"]),
    (re.compile(r"截屏|截图|屏幕|界面|点一下|点击|鼠标|移动鼠标|滚动|双击|右键|键入|输入文字|按键|快捷键|keyboard|mouse|click|screenshot|screen_capture", re.IGNORECASE),
     ["control"]),
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


def _filter_tools(user_text: str, all_tools: list[dict], scheduling_shortcut: bool = True) -> list[dict]:
    """Return a filtered tool list based on user intent. Falls back to all tools if uncertain.

    scheduling_shortcut=False：定时任务执行（cron）时禁用"定时意图短路"——
    cron 任务文本自带「定时分析」字样，会被短路成只有 create_cron+read_file，
    金融查询工具全没（09-01 14:00 事故：交易时段无工具可用→空响应放弃）。
    """
    if not user_text or len(user_text) < 3:
        return all_tools
    allowed_categories: set[str] = set()
    for pattern, cats in INTENT_PATTERNS:
        if pattern.search(user_text):
            allowed_categories.update(cats)
    if not allowed_categories:
        return all_tools  # No match = keep all tools
    allowed_tools: set[str] = set()
    # 定时意图优先短路：建任务只需 create_cron + read_file，其余工具（金融/
    # 控制等）对"创建定时任务"是噪音——9B 小模型面对 16 个工具描述会迷失，
    # 反复"我先查清楚"而不调 create_cron（09-01 10:13 事故）
    if scheduling_shortcut and "scheduling" in allowed_categories:
        allowed_tools.update({"create_cron", "read_file"})
        allowed_tools.update({"use_skill", "delegate_task"})
        return [t for t in all_tools if t.get("function", {}).get("name") in allowed_tools] or all_tools
    if "scheduling" in allowed_categories:
        # cron 执行场景：去掉 scheduling 分类本身（含 create_cron），
        # 让金融/文件等真实任务类别决定工具集
        allowed_categories.discard("scheduling")
    for cat in allowed_categories:
        allowed_tools.update(TOOL_CATEGORIES.get(cat, []))
    # Always include read_file as fallback
    allowed_tools.add("read_file")
    # 元工具保底：create_cron/delegate_task/use_skill 不属于任何意图分类，
    # 意图过滤后模型根本看不到它们——"每10分钟分析大盘"被归 financial 后
    # create_cron 被滤掉，模型只能口嗨"我来搭"而无法真正创建定时任务
    # （09-01 事故）。这类跨任务元工具始终保留。
    allowed_tools.update({"create_cron", "delegate_task", "use_skill"})
    # 控制类工具保底：用户意图五花八门（"看看电脑状态"→file_read），
    # 若把控制工具滤掉，模型无法完成进程/鼠标/屏幕操作——有明确控制意图时
    # 保留全部控制工具；无控制意图时仅保留轻量只读控制（list/audit/wait）
    if "control" in allowed_categories:
        allowed_tools.update(CONTROL_TOOL_NAMES)
    else:
        allowed_tools.update({"control_list_processes", "control_audit", "control_wait",
                              "control_process_log", "screen_capture"})
    # 金融意图同时保留 web 工具：美股/港股等境外市场 mx_query 查不到，
    # 需要 tavily 联网搜索——此前 financial 只给 mx_query/ak_finance，
    # 模型想搜行情时工具被白名单过滤、空响应收场（P0-1）
    if "financial" in allowed_categories:
        allowed_tools.update(TOOL_CATEGORIES.get("web", []))
    # Only add web/financial tools when relevant (not unconditionally)
    if "financial" not in allowed_categories and "web" not in allowed_categories:
        allowed_tools.add("tavily_search")
        allowed_tools.add("mx_query")
        allowed_tools.add("bing_search")
        allowed_tools.add("ak_finance")
    filtered = [t for t in all_tools if t.get("function", {}).get("name") in allowed_tools]
    return filtered if filtered else all_tools



def _cap_tools(tools: list[dict], cap: int = 8, keep_first: tuple[str, ...] = ()) -> list[dict]:
    """Cap tool count, keeping essential tools (read_file, write_file, list_dir) first.
    先去重（DeepSeek 等 API 要求工具名唯一，重复名字直接 400）。
    keep_first：额外优先保留的工具名（如 cron 金融任务必须保留 mx_query——
    全局优先级里它排在 read/tavily 之后，cap 5 会被裁掉，任务无金融工具
    可用 → 空响应放弃，09-01 14:00 事故第二层）。"""
    seen: set[str] = set()
    uniq: list[dict] = []
    for t in tools:
        n = t.get("function", {}).get("name")
        if n and n not in seen:
            seen.add(n)
            uniq.append(t)
    essential = {"read_file", "write_file", "list_dir"} | set(keep_first)
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
    if is_local or not user_text:
        return False
    # plan 访问档强制规划（此前档位对规划无任何影响，审计 A2）
    return len(user_text.strip()) >= 30 and any(k in user_text.lower() for k in _PLAN_KEYWORDS)


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


REASONING_MODEL_HINTS = ("reasoner", "r1", "reasoning", "thinking", "o1", "o3", "o4", "gpt-5")
_FORCED_REASONING = ("deepseek-reasoner",)
_ANTHROPIC_HINTS = ("claude", "anthropic")


def _inject_thinking_disabled(body: dict, model: str, level: str = "high") -> dict:
    """思考强度三档（对应前端 🧠 选择器）：off / high(默认) / max。

    按模型族正确设置（此前 off 只写 Anthropic 字段 `thinking`，DeepSeek/OpenAI
    忽略该字段 → 用户设"关闭思考"无效）：
    - anthropic(claude):  thinking = {type: disabled}（官方关闭方式）
    - openai 推理系(o1/o3/o4/gpt-5): reasoning_effort = "none"（官方关闭方式）
    - deepseek-chat 等非推理: 无需字段（本来就不思考）
    - deepseek-reasoner:  API 层面强制思考、不提供开关 —— 不设无效字段，
      返回 body 并带 _thinking_unsupported 标记，前端据此提示
    """
    m = (model or "").lower()
    body["_thinking_level"] = level
    if level == "off":
        if any(h in m for h in _ANTHROPIC_HINTS):
            body["thinking"] = {"type": "disabled"}
        elif "deepseek-reasoner" in m or m in _FORCED_REASONING:
            body["_thinking_unsupported"] = True  # 无法关闭，提示用户
        elif any(h in m for h in ("o1", "o3", "o4", "gpt-5")) or "reasoner" in m or "r1" in m:
            body["reasoning_effort"] = "none"
        # 其他非推理模型：不设字段（默认不思考）
    elif level == "max":
        # 长推理预算：高于常规推理预算（12288）约 1.5 倍
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


def _merge_system_messages(messages: list) -> list:
    """把所有 system 消息合并进开头的那一个（内容用空行连接）。

    mlx_lm.server v0.31 只接受一个 system 消息且必须在最前面，任何位置的
    第二个 system 都直接 404（"System message must be at the beginning"）。
    nudge 轮在消息列表尾部追加 system（16:16 实测 roles=
    [system, user, assistant, system] 全 404）——必须全部合并。
    """
    sys_contents = [str(m.get("content", "")) for m in messages if m.get("role") == "system"]
    if not sys_contents:
        return messages
    merged_system = {"role": "system", "content": "\n\n".join(c for c in sys_contents if c)}
    out = []
    inserted = False
    for m in messages:
        if m.get("role") == "system":
            if not inserted:
                out.append(merged_system)
                inserted = True
            continue
        out.append(m)
    if not inserted:
        out.insert(0, merged_system)
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
    """Wait for user to approve/deny a confirm-level tool. Returns (approved, events).

    超时不再静默当拒绝（P2-14）：保留 pending 状态并给用户明确提示事件，
    任务暂停而不是以 User denied 收场。"""
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
        events.append({
            "content": (
                f"\n\n⚠️ 工具 `{tool_name}` 等待确认超时（2 分钟无人操作），"
                "任务已暂停，未执行该操作。可在界面中重新批准后继续。"
            ),
        })
    finally:
        async with _pending_lock:
            _pending_confirmations.pop(call_id, None)
    return approved, events


async def _start_tool_confirmation(call_id: str, tool_name: str, args: dict) -> dict:
    """启动工具确认：注册 pending，返回 {"event": 待发事件, "event_obj": 等待用}。

    ⚠️ SSE 生成器必须**先 yield 该事件、再等待结果**——事件若攒到确认完成后
    才发出，前端在等待期间收不到 tool_confirm，弹窗永不出现（死锁到超时）。
    此前 _await_tool_confirmation 正是这个结构，导致确认功能从未真正工作。"""
    event = asyncio.Event()
    async with _pending_lock:
        _pending_confirmations[call_id] = {"event": event, "approved": False}
    return {"event": {"event": "tool_confirm", "call_id": call_id, "tool": tool_name, "args": args},
            "event_obj": event}


async def _wait_tool_confirmation(call_id: str, tool_name: str,
                                  event_obj: asyncio.Event, timeout: float = 120) -> tuple[bool, list[dict]]:
    """等待已启动（_start_tool_confirmation）的确认结果。
    返回 (approved, events)——events 只含超时提示等补充事件（初始
    tool_confirm 已由调用方发出）。超时保持暂停，不默认执行。"""
    events = []
    try:
        await asyncio.wait_for(event_obj.wait(), timeout=timeout)
        async with _pending_lock:
            approved = _pending_confirmations.get(call_id, {}).get("approved", False)
    except asyncio.TimeoutError:
        approved = False
        events.append({
            "content": (
                f"\n\n⚠️ 工具 `{tool_name}` 等待确认超时（2 分钟无人操作），"
                "任务已暂停，未执行该操作。可在界面中重新批准后继续。"
            ),
        })
    finally:
        async with _pending_lock:
            _pending_confirmations.pop(call_id, None)
    return approved, events


async def _await_tool_confirmation(call_id: str, tool_name: str, args: dict) -> tuple[bool, list[dict]]:
    """兼容入口：启动 + 等待一次性完成（仅限不经过 SSE 的内部调用）。"""
    started = await _start_tool_confirmation(call_id, tool_name, args)
    approved, events = await _wait_tool_confirmation(call_id, tool_name, started["event_obj"])
    return approved, [started["event"]] + events


def _confirm_bypassed(tool_name: str, access_mode: str) -> bool:
    """confirm 级工具是否免确认（full 档全免；auto_edit 档文件类免确认）。
    供 _handle_tool_execution 与 SSE 调用方（提前发确认事件）共用，避免判定漂移。"""
    _access = _normalize_access(access_mode)
    if _access == "full":
        return True
    if _access == "auto_edit" and tool_name in AUTO_EDIT_TOOLS:
        try:
            from main import _custom_permissions
            _has_rule = any(r.get("tool") == tool_name for r in _custom_permissions)
        except Exception:
            _has_rule = False
        return not _has_rule
    return False


async def _start_plan_confirmation(plan_id: str, plan: str) -> dict:
    """启动计划确认（同工具确认：先发事件再等待）。"""
    event = asyncio.Event()
    async with _pending_lock:
        _pending_confirmations[plan_id] = {"event": event, "approved": False}
    return {"event": {"event": "plan_confirm", "call_id": plan_id, "tool": "执行计划",
                      "args": {"plan": plan[:2000]}},
            "event_obj": event}


async def _wait_plan_confirmation(plan_id: str, event_obj: asyncio.Event,
                                  timeout: float = 300) -> tuple[bool, list[dict]]:
    """等待计划确认结果（给用户 5 分钟阅读计划）。超时保持暂停。"""
    events = []
    try:
        await asyncio.wait_for(event_obj.wait(), timeout=timeout)
        async with _pending_lock:
            approved = _pending_confirmations.get(plan_id, {}).get("approved", False)
    except asyncio.TimeoutError:
        approved = False
        events.append({
            "content": "\n\n⚠️ 计划等待确认超时（5 分钟无人操作），任务已暂停未执行。可重新发起任务。",
        })
    finally:
        async with _pending_lock:
            _pending_confirmations.pop(plan_id, None)
    return approved, events


async def _await_plan_confirmation(plan_id: str, plan: str) -> tuple[bool, list[dict]]:
    """兼容入口：启动 + 等待一次性完成。"""
    started = await _start_plan_confirmation(plan_id, plan)
    approved, events = await _wait_plan_confirmation(plan_id, started["event_obj"])
    return approved, [started["event"]] + events


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


# 时间敏感工具：结果自带日期数据，模型易把"昨晚/今天"等相对时间换算错后
# 被检索结果的旧日期锚定（09-03 两次事故：08:25 老会话、08:57 全新会话，
# 均把"昨晚美股"搜成 9月1日）。
_TIME_SENSITIVE_TOOLS = frozenset({
    "tavily_search", "bing_search", "dokobot_search", "dokobot_read",
    "headless_read", "mx_query", "ak_finance",
})

_WEEK_ZH = "一二三四五六日"


def _stamp_time_sensitive() -> str:
    """生成当前时刻锚行，注入时间敏感工具结果头部（截断后追加，不会被截掉）。"""
    now = datetime.now()
    return (f"⏱ [数据时刻] {now.strftime('%Y-%m-%d')} (周{_WEEK_ZH[now.weekday()]}) "
            f"{now.strftime('%H:%M:%S')} —— 下方结果内日期若与此矛盾，以当前时间为准\n\n")


async def _handle_tool_execution(tc: dict, current_msgs: list, session_id: str,
                                  agent_id: str, access_mode: str = "full",
                                  pre_started: dict | None = None) -> tuple[bool, list[dict]]:
    """Execute a single tool call within the agent loop. Returns (verify_failed, events).

    pre_started: SSE 调用方已通过 _start_tool_confirmation 启动确认并发出
    tool_confirm 事件时传入（含 event_obj）——本函数只等待结果，不再重复发事件。
    确认事件若在等待完成后才发出，前端弹窗永不出现（死锁）。"""
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

    # ── 相同调用防重复（14:26 事故：模型被 nudge 后每轮重跑启动协议、
    # 反复重读 PROGRESS.md 陷入循环；今早还有同 3 条搜索词 35 连搜）──
    # 同会话内相同 (tool, args) 已成功 ≥2 次 → 不再执行，返回引导进入下一步。
    _dup_ok = _count_successful_duplicates(current_msgs, tool_name, args)
    if _dup_ok >= 2 and tool_name not in _REPEAT_ALLOWED_TOOLS:
        result = (
            f"⛔ 相同调用已成功执行 {_dup_ok} 次，不再重复执行：{tool_name}。\n"
            "不要重复同一操作——启动协议若已满足就进入下一步"
            "（例如用 mx_query 查询行情数据），或直接把完整分析写进回复正文"
            "（简体中文，含关键数字与结论）。"
        )
        current_msgs.append({"role": "tool", "tool_call_id": call_id, "content": result})
        return False, [{"event": "tool_end", "call_id": call_id, "tool": tool_name, "result": result, "ts": int(time.time() * 1000)}]

    # ── 权限规则拒绝（deny/danger）──
    # 自定义权限规则返回 danger/deny 时必须拦截，此前落空直接执行——
    # 权限语义严重不一致（实测 list_dir 设 danger 仍读到目录）
    _perm_level = _resolve_permission(tool_name, args)
    if _perm_level in ("deny", "danger", "blocked"):
        result = (
            f"⛔ 权限规则拒绝执行: {tool_name}（级别: {_perm_level}）。"
            "如需执行，请在设置中调整该工具的权限规则后重试。"
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
    # full（完全访问）档：confirm 级工具免确认直接执行——此前 5 档中
    # confirm/plan/full 三档无门控、与默认档完全等价（审计 A2）。
    # danger/deny 规则拦截仍在上方生效，不受此豁免影响。
    _full_bypass = (_access == "full")
    # 事件列表必须先初始化：confirm 分支的 pre_started 路径（当前两个 SSE
    # 循环的唯一调用方式）此前从未绑定 events 就 extend → UnboundLocalError
    # 整个任务崩溃（审计 P0：每次确认弹窗路径必炸）
    events: list = []
    if _perm_level == "confirm" and not _auto_edit_bypass and not _full_bypass:
        if pre_started is not None and pre_started.get("event_obj") is not None:
            # 事件已由 SSE 调用方提前发出（死锁修复），这里只等待结果
            approved, extra = await _wait_tool_confirmation(call_id, tool_name, pre_started["event_obj"])
            events.extend(extra)
        else:
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
        # 保留首 2000 + 尾 800：尾部常含关键结论/错误信息（P2-16）
        tool_content = (
            tool_content[:2000]
            + f"\n\n... (中间已省略。完整结果 {len(result)} 字符已记录,"
            + "如需查看特定部分请用 read_file 分段读取对应文件。)\n\n"
            + tool_content[-800:]
        )
    current_msgs.append({"role": "tool", "tool_call_id": call_id,
                         "content": (_stamp_time_sensitive() + tool_content
                                     if tool_name in _TIME_SENSITIVE_TOOLS else tool_content)})
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
    # 进展感知看门狗：无进展静默期硬上限 15 分钟。此前模型服务器偶发 hold
    # 连接滴灌字节可绕过单次 read timeout（180s×N），用户面对 18 分钟无响应。
    # 纯墙钟一刀切会误杀正常推进的长任务（如多轮深度研究），改为
    # 每轮有实质进展（内容产出/工具执行）就顺延——只有连续 15 分钟
    # 完全无进展才中止。
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
        active_tools = _cap_tools(active_tools, 8)

    async with httpx.AsyncClient(timeout=httpx.Timeout(120)) as client:
        # ── 规划模式：复杂任务先生成执行计划（显示给用户，等确认后执行） ──
        if _should_plan(last_user_text, _is_local_llm_url(api_url)) or _normalize_access(access_mode) == "plan":
            _plan = await _generate_plan(last_user_text, model, api_url, headers, client)
            if _plan:
                yield {"event": "agent_plan", "content": _plan}
                # 计划门控：先发 plan_confirm 事件（前端渲染确认卡），再等待结果。
                # 事件若攒到确认完成后才发，前端在等待期间收不到 → 弹窗死锁。
                plan_id = f"plan_{uuid.uuid4()}"
                plan_started = await _start_plan_confirmation(plan_id, _plan)
                yield plan_started["event"]
                approved, plan_events = await _wait_plan_confirmation(plan_id, plan_started["event_obj"])
                for ev in plan_events:
                    yield ev
                if not approved:
                    _track_progress(session_id, "plan_rejected", "user_denied_plan")
                    yield {"content": "\n\n⏹️ 计划已被拒绝，任务未执行。你可以调整要求后重新发起。"}
                    return
                current_msgs.insert(0, {"role": "system",
                    "content": "以下是已确认（用户批准）的执行计划，请严格按计划逐步执行（可调用工具）：\n" + _plan})
            elif _normalize_access(access_mode) == "plan":
                # 计划模式档下计划生成失败 → 中止而非裸执行（用户明确要求先出计划）；
                # 自动触发的规划（非计划模式档）保留降级直执行的旧行为
                yield {"content": "\n\n⚠️ 计划模式：计划生成失败，任务未执行。请重试或切换到其他模式。"}
                _track_progress(session_id, "plan_generate_failed", "cloud")
                return

        while iteration < 50:  # hard cap at 50, dynamic exit via stagnation
            iteration += 1
            if time.monotonic() > loop_deadline:
                logger.error("[AGENT] 连续 15 分钟无进展，中止任务")
                yield {"content": "\n\n⚠️ 任务连续 15 分钟无进展（未产出内容或执行工具），已中止。模型服务可能异常（如响应停滞）。可重试或检查网络。"}
                _track_progress(session_id, "stalled", "total_duration_limit")
                return
            # Re-evaluate tool set every 3 iterations for multi-step tasks
            if iteration > 1 and iteration % 3 == 0:
                # 恢复全量工具，但仍须套用权限过滤（read_only 等模式不可绕过）
                # （本函数为云端循环，无 is_local 变量；本地循环独立实现）
                active_tools = _cap_tools(_filter_tools_by_access(agent_tools, access_mode), 8)
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
            # cloud_config 指向本地引擎（如本地 MLX 代理）时，多个 system
            # 消息同样会被 mlx 拒绝——发送前统一合并（P2-11）
            _msgs_for_body = _merge_system_messages(_sanitize_tool_messages(current_msgs))
            body = {
                "model": model, "messages": _msgs_for_body,
                "tools": active_tools, "tool_choice": "auto",
                "max_tokens": _resolve_max_tokens(model), "stream": True,
                "temperature": 0.5,
                "frequency_penalty": 0.6,
                "stop": ["<|im_end|>", "<|endoftext|>", "<end_of_turn>", "<eos>"],
            }
            _inject_thinking_disabled(body, model, thinking_level)
            # 私有标记（_thinking_*）仅为内部审计/提示用，绝不能发给 API（未知字段 400）
            body.pop("_thinking_level", None)
            body.pop("_thinking_unsupported", None)

            streamed_text = ""
            reasoning_text = ""  # 累积 reasoning_content——DeepSeek 推理模型要求传回
            tool_call_bufs: dict[int, dict] = {}
            _raw_delta_count = 0  # 复读循环检测节流计数（每 40 个 delta 检查一次）
            _dedup_fired = False  # 去重一次性截断标志（审计 A5）

            # 流式总超时保护：Qwen3.8 等 27B 推理模型在流式推理时可能让
            # mlx_lm.server 挂起（端口活、不吐响应头，httpx read timeout 不触发
            # ——22:27 事故：卡 8 分钟无进展）。响应头等待 + 后续迭代统一受
            # 180s 硬超时约束，超时抛 TimeoutError 由外层零交付重试接管。
            stream_ctx = client.stream("POST", api_url, json=body, headers=headers)
            try:
                async with asyncio.timeout(180):
                    async with stream_ctx as r:
                        if r.status_code != 200:
                            try:
                                err_body = (await r.aread()).decode("utf-8", errors="replace")[:800]
                            except Exception:
                                err_body = "<read failed>"
                            logger.error("Agent stream HTTP %d body: %s", r.status_code, err_body)
                        r.raise_for_status()  # httpx 不自动抛 4xx/5xx，必须显式检查
                        # 流式停顿检测：连续 180s 无数据视为僵死（大模型可能缓慢滴灌，120s 超时永不触发）
                        aiter = r.aiter_lines()
                        _fence_filter = _ThinkFenceFilter()
                        _body_out = False  # 本轮是否产出过正文增量（收尾全量修正用）
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
                                    _raw_delta_count += 1

                                    content = delta.get("content", "")
                                    reasoning = delta.get("reasoning", "")
                                    if content:
                                        streamed_text += content
                                        # 复读循环检测（节流）——放在 dedup 过滤之前，
                                        # 复读被 dedup 过滤时也要能截断
                                        if _raw_delta_count % 40 == 0 and _detect_text_loop(streamed_text):
                                            logger.warning("[AGENT] 检测到输出复读循环，截断生成")
                                            yield {"content": "\n\n⚠️ 检测到输出重复，已自动截断。"}
                                            raise TimeoutError("输出复读循环，已截断")
                                        # 自我介绍去重：只截断一次，之后照常流式输出
                                        # （审计 A5：此前命中后永久吞掉后续真实内容）
                                        if not _dedup_fired:
                                            _ded = _deduplicate_response(streamed_text)
                                            if len(_ded) < len(streamed_text):
                                                _dedup_fired = True
                                                streamed_text = _ded + content
                                        if text_output_delivered:
                                            # 追问续写轮：替换上一条而非追加（重复堆叠修复）
                                            if _raw_delta_count % 40 == 0:
                                                yield {"event": "content_revised", "content": _strip_think_fences(streamed_text)}
                                            continue
                                        # Filter native control tokens so the UI doesn't show
                                        # raw <|tool_call|> / <|channel> / <|channel|> markers
                                        clean = _NATIVE_CONTROL_RE.sub("", content)
                                        # 剥掉 think 围栏标记（```think>/```think<）——流式渲染时
                                        # ReactMarkdown 把它当未闭合代码块 → 后续正文灰框；
                                        # 用缓冲过滤器捕获被 tokenizer 拆分的围栏
                                        clean = _fence_filter.feed(clean)
                                        if clean:
                                            _body_out = True
                                            yield {"content": clean}
                                        if len(streamed_text) < 5:
                                            _track_progress(session_id, "generating", "text_start")
                                    elif reasoning:
                                        # Reasoning model (Qwen3.6, DeepSeek-R1, etc.) — stream thinking as content
                                        # so the UI doesn't appear frozen during the thinking phase
                                        reasoning_text += reasoning
                                        streamed_text += reasoning
                                        if _raw_delta_count % 40 == 0 and _detect_text_loop(streamed_text):
                                            logger.warning("[AGENT] 检测到输出复读循环，截断生成")
                                            yield {"content": "\n\n⚠️ 检测到输出重复，已自动截断。"}
                                            raise TimeoutError("输出复读循环，已截断")
                                        if not _dedup_fired:
                                            _ded = _deduplicate_response(streamed_text)
                                            if len(_ded) < len(streamed_text):
                                                _dedup_fired = True
                                                streamed_text = _ded + reasoning
                                        if text_output_delivered:
                                            if _raw_delta_count % 40 == 0:
                                                yield {"event": "content_revised", "content": _strip_think_fences(streamed_text)}
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
            except TimeoutError:
                # 引擎挂起/超时（22:27 事故：Qwen3.8 端口活但流不出数据，
                # httpx read timeout 不触发）。不能只抛错——引擎还挂着，
                # 下一轮还会超时。这里杀进程+触发重载，让后续请求自愈。
                # ⚠️ 仅本地引擎路径可杀——云端请求超时/复读截断也抛
                # TimeoutError，若误杀本地引擎会让已加载的模型白重载一轮
                # （审计 P1：云端 stall 杀死本地引擎 + 内存尖峰）。
                if _is_local_llm_url(api_url):
                    logger.warning("本地流 180s 超时，判定引擎挂起，杀进程并触发重载")
                    try:
                        import local_llm as _llm_mod
                        _eng = _llm_mod._engine
                        if _eng.current_model_id:
                            _eng._kill_port(_eng.server_port)
                            _eng.server_status = "stopped"
                            _eng._request_reload(_eng.current_model_id)
                    except Exception:
                        logger.warning("超时后引擎重载触发失败", exc_info=True)
                else:
                    logger.warning("云端流超时/复读截断，不影响本地引擎")
                raise TimeoutError(f"流式响应超时（180s 无进展）：{model[:60]}")

            # 收尾修正：本轮产过正文就发一次全量替换（前端支持任意时刻
            # content_revised），把流中可能残留的围栏在收尾统一剥干净（双保险）
            if (text_output_delivered or _body_out) and streamed_text.strip():
                yield {"event": "content_revised", "content": _strip_think_fences(streamed_text)}

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

            if not tool_calls:
                # 纯文本轮计入停滞计数（工具轮/完成轮会复位，P2-12）
                _track_progress(session_id, "text_round", "text_only")
                if streamed_text.strip():
                    loop_deadline = time.monotonic() + 900  # 产出内容=实质进展，顺延看门狗

            if tool_calls:
                loop_deadline = time.monotonic() + 900  # 实质进展：顺延无进展看门狗
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
                    # 先发确认事件再等待（死锁修复）：confirm 级工具的
                    # tool_confirm 必须在执行前到达前端，弹窗才会出现
                    if not tc.get("id"):
                        tc["id"] = str(uuid.uuid4())
                    pre_started = None
                    try:
                        _tname = tc.get("function", {}).get("name", "unknown")
                        _targs = json.loads(tc.get("function", {}).get("arguments", "{}") or "{}")
                        if _resolve_permission(_tname, _targs) == "confirm" \
                                and not _confirm_bypassed(_tname, access_mode) \
                                and not _check_access(_tname, access_mode):
                            pre_started = await _start_tool_confirmation(tc["id"], _tname, _targs)
                            yield pre_started["event"]
                    except Exception:
                        pre_started = None
                    verify_failed, events = await _handle_tool_execution(
                        tc, current_msgs, session_id, agent_id, access_mode, pre_started=pre_started)
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
                if len(streamed_text.strip()) >= 200:
                    # 工具结果后已给出实质性回答——接受为最终答案直接收尾
                    # （与 local 循环同口径，防追问后重答堆叠）
                    # 语言确保：不符则以 content_revised 整体替换上一条（云端仍直播，
                    # 遵循度好，属兜底；16:50 事故同款保护）
                    _deliver = await _ensure_final_language(
                        client, api_url, headers, model,
                        streamed_text.strip(), last_user_text)
                    if _deliver != streamed_text.strip():
                        yield {"event": "content_revised", "content": _deliver}
                    current_msgs.append({"role": "assistant", "content": _deliver})
                    if _should_reflect(reflection_mode, _deliver, _is_local_llm_url(api_url)):
                        _tool_outs = [str(m.get("content") or "") for m in current_msgs if m.get("role") == "tool"]
                        _revised, _changed = await _reflect_output(_deliver, model, api_url, headers, reflection_mode, client, _tool_outs)
                        if _changed and _revised.strip():
                            streamed_text = _revised
                            yield {"event": "reflection_revised", "content": _revised}
                    _track_progress(session_id, "completed", f"text_response ({len(_deliver)} chars)")
                    return
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
                    # 语言兜底：不符则以 content_revised 整体替换（同 2448 路径）
                    _deliver = await _ensure_final_language(
                        client, api_url, headers, model,
                        streamed_text.strip(), user_q)
                    if _deliver != streamed_text.strip():
                        yield {"event": "content_revised", "content": _deliver}
                    _track_progress(session_id, "completed", f"text_response ({len(_deliver)} chars)")
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

# OpenAI 风格 json 栅栏：```json {"name": "...", "arguments": {...}} ```
# Qwen3.8/MoziAI 等 27B 级模型实测输出此格式（工具名在 JSON 内部而非栅栏语言位），
# 此前 Fenced/XML/Bare/Inline 四层全不认 → 模型反复正确输出工具调用却被当纯文本，
# nudge 3 次后放弃 → "任务刚开始就停"（09-02 09:14 事故）。
_PROMPT_JSON_FENCE_RE = re.compile(
    r'```json\s*(\{.*?\})\s*```',
    re.DOTALL,
)

# Hermes 风格 XML：Qwen3 系在 prompt-based 模式下常输出
# <tool_call>name<arg_key>k</arg_key><arg_value>v</arg_value></tool_call>
_TOOLCALL_XML_RE = re.compile(
    r'<tool_call>\s*([\w.]+)\s*((?:<arg_key>[^<]*</arg_key>\s*<arg_value>.*?</arg_value>\s*)*)</tool_call>',
    re.DOTALL,
)
_TOOLCALL_KV_RE = re.compile(r'<arg_key>(\w+)</arg_key>\s*<arg_value>(.*?)</arg_value>', re.DOTALL)

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

# 模型输出的 ```think> / ```think< 围栏（Kimi 风格思维链）：围栏标记会让
# 前端 ReactMarkdown 把它当作未闭合代码块 → 后续正文全部渲染成灰框代码块
_THINK_FENCE_RE = re.compile(r"```think\s*[<>]")


def _strip_think_fences(text: str) -> str:
    return _THINK_FENCE_RE.sub("", text)


class _ThinkFenceFilter:
    """逐 delta 清洗 ```think 围栏，能捕获被 tokenizer 拆分的标记。

    LLM 流式输出常把 ```think> 拆成多个 delta（如 ``` / think / >），
    对单个 delta 做正则永远匹配不到。本过滤器在累积缓存中识别拆分中的
    标记：尾部可能拼成 ```think 前缀时暂存等待下一 delta，拼完整后整体
    剥掉；确认是普通代码块（```json 等）再放行（最多延迟 1-2 个 delta）。
    """

    _MAX_PENDING = 64

    def __init__(self) -> None:
        self._pending = ""

    def feed(self, text: str) -> str:
        """输入一个流式增量，返回可安全发送给前端的内容（可为空串）。"""
        if not text:
            return ""
        buf = _THINK_FENCE_RE.sub("", self._pending + text)
        # 尾部可能是拆分中的 ```think 前缀（``` 本身、反引号、或 ``` 后跟字母）
        # → 暂存等待下一 delta，拼完整后由上面的正则整体剥掉
        if re.search(r"```[a-zA-Z]*$", buf) or buf.endswith("`"):
            self._pending = buf
            return ""
        if len(buf) > self._MAX_PENDING:  # 防极端场景卡死，强制放行
            self._pending = ""
            return buf
        self._pending = ""
        return buf

    def finalize(self) -> str:
        """流结束时取回缓存。think 标记中间态剥掉；纯反引号残片保留
        （可能是普通代码块的闭合围栏，丢了会导致代码块不闭合）。"""
        pending, self._pending = self._pending, ""
        if not pending:
            return ""
        if "```think" in pending:
            return _THINK_FENCE_RE.sub(r"```+", "", pending)
        return pending


# 未执行的"规划意图"词形：模型说了要做但整轮没调用工具（21:42 事故的
# 扩充词表——"让我用/让我先/先看看"等声明式话术不可当实质完成收尾；
# 注意避免误伤完整回答开头（"让我来整理一下关键数据…"是实质回答）
_PENDING_INTENT_PATTERNS = (
    "改用", "我再单独查", "我单独查", "我改", "我要调用",
    "我马上", "我这就", "我先查", "我重新查",
    "让我用", "我来用", "让我读取", "让我先", "让我再",
    "我先做", "先看看",
)


# 规划话术信号：模型把"我打算做什么"当成最终回答发出来（13:58 事故：
# 891 字符英文规划 "Let me plan the subtasks: 1…4" 被收尾闸门当完整答案
# 放行，任务半途而停）。≥2 个信号才算规划——真实回答里带一个
# "下一步/step" 不应被误拦（17:07 重复堆叠事故的教训）。
_PLANNING_SIGNALS = (
    "tool", "工具", "读取", "查询", "搜索", "执行", "调用",
    "read_file", "list_dir", "run_cmd", "write_file",
    "search", "tavily", "mx_query", "step", "步骤", "下一步",
    "let me", "i'll", "i will", "让我", "我先", "接下来",
    "再分析", "稍后", "马上", "look at", "check the",
    "i need to", "let's", "let me plan", "my plan", "subtask",
    "according to the rules", "first, i", "let's get started",
    "get started", "plan the", "then i", "after that",
)


def _looks_like_planning(text: str) -> bool:
    """判断回复文本是否只是"计划/声明"而非实质回答。≥2 个规划信号才成立。"""
    if not text or len(text.strip()) < 10:
        return False
    low = text.lower()
    return sum(1 for sig in _PLANNING_SIGNALS if sig in low) >= 2


def _reply_lang_mismatch(user_text: str, reply_text: str) -> bool:
    """回复语言与用户语言明显不符（中文用户收到英文/英文占优回复）→ True。

    判定：回复中用户语言的字符数，远少于外来语言字母数（英文占优）。
    891 字符英文规划（0 汉字）命中；14:45 重放中 598 字母 vs 42 汉字 的
    混合英文回答也命中；"NVIDIA涨5%"（字母 6，不满足英文量门槛）与
    中文为主的正常回答不误伤。"""
    user_lang = _detect_user_language(user_text)
    if not reply_text:
        return False
    zh = len(re.findall(r'[\u4e00-\u9fff]', reply_text))
    ja_kana = len(re.findall(r'[\u3040-\u309f\u30a0-\u30ff]', reply_text))
    en = len(re.findall(r'[a-zA-Z]', reply_text))
    if user_lang == "zh":
        return en >= 80 and en > zh * 3
    if user_lang == "ja":
        return en >= 80 and en > (zh + ja_kana) * 3
    if user_lang == "en":
        other = zh + ja_kana
        return other >= 80 and other > en * 3
    return False


# 允许重复调用的工具（持续监测类），防重复护栏对它们不生效
_REPEAT_ALLOWED_TOOLS = frozenset({"screen_capture", "control_wait"})


async def _ensure_final_language(client, api_url: str, headers: dict, engine_model: str,
                                 text: str, user_text: str) -> str:
    """交付前语言确保：回复语言与用户消息不符时走翻译轮，返回可交付文本。

    缓冲交付后所有 return 路径统一经过这里——即使收尾闸门被跳过
    （如工具失败分支），英文也不会原样到达用户（16:55 事故）。
    翻译轮失败（引擎瞬时故障）时加中文说明前缀再交付，用户不会看到
    无解释的纯英文。"""
    if text and _reply_lang_mismatch(user_text, text):
        translated = await _force_translate(client, api_url, headers, engine_model, text,
                                            _detect_user_language(user_text))
        if translated == text:
            return (f"⚠️ 本地模型本轮生成了英文回复，自动翻译暂不可用"
                    f"（可回复「继续」让我重新整理）。原文如下：\n\n{text}")
        return translated
    return text


async def _force_translate(client, api_url: str, headers: dict, engine_model: str,
                           text: str, user_lang: str) -> str:
    """一轮强制翻译：把模型回复翻译成用户语言（本地引擎非流式单轮）。

    模型对翻译任务的执行远比"用某语言重新分析"稳定——语言兜底的最后一公里
    （09-03 事故：两轮中文规则+3 次 nudge 后 27B 模型仍输出英文）。失败时
    返回原文（不阻断交付）。"""
    lang_name = {"zh": "简体中文", "en": "English", "ja": "日本語"}.get(user_lang, "简体中文")
    _tmsgs = [
        {"role": "system",
         "content": (f"你是翻译器。把用户提供的文本完整翻译成{lang_name}，"
                     "直接输出译文。不要调用工具，不要输出任何解释、注释或前后缀。")},
        {"role": "user", "content": text[:6000]},
    ]
    _tb = {"model": engine_model, "messages": _tmsgs, "max_tokens": 4096,
           "stream": False, "temperature": 0.2, "stop": ["<|im_end|>", "<eos>"]}
    # 引擎长流刚结束时偶发连接重置（17:17 实测 608ms 内 read 失败）——重试一次
    for _attempt in range(2):
        try:
            async with _local_llm_serialized(api_url):
                _tr = await client.post(api_url, json=_tb, headers=headers)
            if _tr.status_code == 200:
                out = ((_tr.json().get("choices") or [{}])[0]
                       .get("message", {}).get("content", "") or "").strip()
                if out and len(out) >= 40:
                    return _strip_think_fences(out)
            return text
        except Exception:
            if _attempt == 0:
                await asyncio.sleep(2)
                continue
            logger.warning("强制翻译轮失败，返回原文", exc_info=True)
    return text


def _count_successful_duplicates(current_msgs: list, tool_name: str, args: dict) -> int:
    """统计同会话内相同 (tool_name, args) 的已成功执行次数（失败结果不计数，
    保留"失败→重试一次"的合法模式；14:26 事故：模型被 nudge 后反复重读
    PROGRESS.md 每轮重跑启动协议陷入循环）。"""
    try:
        _norm_args = dict(args)
        # 路径归一化：read_file 的 "~" 与绝对路径指向同一文件，
        # 不归一化时模型交替两种写法就能绕过护栏（17:10 重放实测）
        if tool_name == "read_file" and _norm_args.get("path"):
            from pathlib import Path as _P
            _norm_args["path"] = str(_P(_norm_args["path"]).expanduser())
        args_sig = json.dumps(_norm_args, ensure_ascii=False, sort_keys=True)
    except Exception:
        return 0
    call_ids: set[str] = set()
    for m in current_msgs:
        if m.get("role") != "assistant":
            continue
        for tc in (m.get("tool_calls") or []):
            f = tc.get("function", {})
            if f.get("name") != tool_name:
                continue
            try:
                _a = json.loads(f.get("arguments", "{}"))
                if tool_name == "read_file" and isinstance(_a, dict) and _a.get("path"):
                    from pathlib import Path as _P
                    _a["path"] = str(_P(_a["path"]).expanduser())
                same = (json.dumps(_a, ensure_ascii=False, sort_keys=True) == args_sig)
            except Exception:
                same = False
            if same and tc.get("id"):
                call_ids.add(tc["id"])
    ok = 0
    for m in current_msgs:
        if m.get("role") == "tool" and m.get("tool_call_id") in call_ids:
            if not str(m.get("content", "")).startswith(("Error", "⛔", "⚠️")):
                ok += 1
    return ok


def _extract_think_body(text: str) -> str:
    """提取思考段内容：```think>…</think> / ```think>…```think< / <think>…</think>。

    27B Q4 模型常把完整分析写进思考段、正文只留半截声明（16:45/19:38/21:13
    事故）——正文不足时用思考段内容兜底交付。
    """
    m = re.search(
        r"(?:```think\s*[>]|<think>)([\s\S]*?)(?:</think>|```think\s*[<]|```)",
        text,
        flags=re.DOTALL,
    )
    return m.group(1).strip() if m else ""


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

    # Priority 1.2: ```json {"name": ..., "arguments": {...}} ``` —— OpenAI 风格
    # json 栅栏（Qwen3.8/MoziAI 27B 实测输出格式），此前四层全不认 → 工具调用
    # 被当纯文本，任务"刚开始就停"
    if not tool_calls:
        for idx, jm in enumerate(_PROMPT_JSON_FENCE_RE.finditer(search_text)):
            try:
                obj = json.loads(jm.group(1))
            except json.JSONDecodeError:
                continue
            if not (isinstance(obj, dict) and obj.get("name") and "arguments" in obj):
                continue
            tool_calls.append({
                "id": f"local_json_{obj['name']}_{idx}",
                "type": "function",
                "function": {"name": str(obj["name"]),
                             "arguments": json.dumps(obj["arguments"], ensure_ascii=False)},
            })
            used_ranges.append((jm.start(), jm.end()))

    # Priority 1.4: Hermes 风格 XML —— Qwen3 系 prompt-based 模式常输出
    # <tool_call>name<arg_key>k</arg_key><arg_value>v</arg_value></tool_call>，
    # 此前不解析 → 工具调用原样交付用户、无工具执行（09-01 22:5x 事故）
    if not tool_calls:
        for x_idx, xm in enumerate(_TOOLCALL_XML_RE.finditer(search_text)):
            _xname = xm.group(1)
            _xargs = {}
            for kv in _TOOLCALL_KV_RE.finditer(xm.group(2)):
                _xargs[kv.group(1)] = kv.group(2)
            tool_calls.append({
                "id": f"local_xml_{_xname}_{x_idx}",
                "type": "function",
                "function": {"name": _xname, "arguments": json.dumps(_xargs, ensure_ascii=False)},
            })
            used_ranges.append((xm.start(), xm.end()))

    # Priority 1.5: Bare JSON tool call — 推理模型（Qwen3.8 等）常直接输出
    # "我来查...{"query": "..."}" 的裸 JSON（无 ```tool 栅栏），解析器不认 →
    # JSON 残留在文本里 → 模型复读同样内容 → 复读检测误判循环截断（22:06 事故）。
    # 这里识别"独立 JSON 对象且参数命中文档化工具名"的裸调用，并剥离。
    if not tool_calls:
        _bare_json_re = re.compile(r'\{\s*"(?:query|path|cmd|pattern|url|command)"\s*:\s*"[^"]*"\s*[,}]', re.DOTALL)
        for b_idx, m in enumerate(_bare_json_re.finditer(search_text)):
            # 往前看 30 字符是否有"动/查/读/搜"等动作词（避免误判正文 JSON）
            prefix = search_text[max(0, m.start()-40):m.start()]
            if not re.search(r'[动查读搜取，。]\s*$', prefix) and not re.search(r'[请让我先我再来]', prefix):
                continue
            json_obj = m.group(0)
            try:
                args = json.loads(json_obj)
            except Exception:
                continue
            # 根据参数推工具名：query→mx_query/tavily_search；path→read_file；cmd→run_cmd
            key = next((k for k in args if k in ("query", "path", "cmd", "url")), None)
            if not key:
                continue
            tool_name = ("read_file" if key == "path"
                         else "run_cmd" if key == "cmd"
                         else "tavily_search" if key == "url"
                         else "mx_query")
            tool_calls.append({
                "id": f"local_bare_{tool_name}_{b_idx}",
                "type": "function",
                "function": {"name": tool_name, "arguments": json.dumps(args, ensure_ascii=False)},
            })
            used_ranges.append((m.start(), m.end()))
            break  # 一次只处理一个裸调用（避免误吞多对象）

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
    _pending_tool_analysis = False  # 工具结果已产出，但尚未收到实质性文字回答
    _intent_nudges = 0              # “只声明不动手/只道歉”的追问计数
    _brief_answer_nudged = False  # "资料充足却短回答"的追问只触发一次，防死循环
    # Build tool prompt
    last_user_text = _extract_last_user_text(current_msgs)
    # 学习提取与云端循环同口径（审计 B9）：此前只在云端入口调用，
    # 纯本地用户偏好/知识永不入库，"记住你的偏好"完全失效
    if last_user_text:
        _extract_learnings_heuristic(last_user_text, session_id)
    agent_tools = _get_agent_tools(agent_id, TOOLS)
    active_tools = _filter_tools(last_user_text, agent_tools) if last_user_text else agent_tools
    active_tools = _filter_tools_by_access(active_tools, access_mode)
    if len(active_tools) > 8:
        active_tools = _cap_tools(active_tools, 12)
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
            "直接调用工具，不要废话。\n"
            "\n📌 输出纪律（必须遵守）：\n"
            "1. 不要把思考过程用 ```think> 代码块围栏输出，也不要在正文里描述'我将要做什么'。\n"
            "2. 思考完成后，把完整分析结论直接写在正文：关键数据、要点、结论。\n"
            "3. 正文禁止出现'让我用…''我先…''接下来我要…'等待办话术——要么立刻调用工具，要么直接写出完整分析。\n"
            "4. 工具执行后不要再声明步骤，直接写结论。"
        )

    # Inject tools into the first user message context
    for m in current_msgs:
        if m.get("role") == "user":
            # Insert tools prompt as a system message right before the last user message
            break

    async with httpx.AsyncClient(timeout=httpx.Timeout(120)) as client:
        # ── 规划模式（本地模型）：计划模式档强制生成计划并等待用户确认 ──
        # 此前本地循环完全没有计划门控：计划模式档形同虚设，模型直接执行
        if _normalize_access(access_mode) == "plan" and last_user_text:
            _plan = await _generate_plan(last_user_text, model, api_url, headers, client)
            if _plan:
                yield {"event": "agent_plan", "content": _plan}
                # 先发 plan_confirm 事件再等待（与云端循环同口径，避免弹窗死锁）
                plan_id = f"plan_{uuid.uuid4()}"
                plan_started = await _start_plan_confirmation(plan_id, _plan)
                yield plan_started["event"]
                approved, plan_events = await _wait_plan_confirmation(plan_id, plan_started["event_obj"])
                for ev in plan_events:
                    yield ev
                if not approved:
                    _track_progress(session_id, "plan_rejected", "user_denied_plan")
                    yield {"content": "\n\n⏹️ 计划已被拒绝，任务未执行。你可以调整要求后重新发起。"}
                    return
                current_msgs.insert(0, {"role": "system",
                    "content": "以下是已确认（用户批准）的执行计划，请严格按计划逐步执行（可调用工具）：\n" + _plan})
            else:
                # 计划模式档下计划生成失败 → 中止而非裸执行（用户明确要求先出计划）
                yield {"content": "\n\n⚠️ 计划模式：计划生成失败，任务未执行。请重试或切换到其他模式。"}
                _track_progress(session_id, "plan_generate_failed", "local")
                return

        # 进展感知无进展看门狗（与云端循环同口径）：连续 15 分钟完全
        # 无进展（未产出内容/未执行工具）才中止；正常推进的长时间
        # 研究任务不受影响
        _no_progress_deadline = time.monotonic() + 900
        while iteration < max_iterations:
            iteration += 1
            if time.monotonic() > _no_progress_deadline:
                logger.error("[LOCAL-AGENT] 连续 15 分钟无进展，中止任务")
                yield {"content": "\n\n⚠️ 任务连续 15 分钟无进展，已中止。模型服务可能异常（如响应停滞）。可重试或检查网络。"}
                _track_progress(session_id, "stalled", "total_duration_limit")
                return
            _append_loop_log(f"Iteration {iteration}: current_msgs={len(current_msgs)}, roles={[m.get('role') for m in current_msgs[-5:]]}\n")

            # Build messages for this iteration: merge tools + current context
            loop_msgs = list(current_msgs)
            # Convert role:"tool" → role:"user" (llama-cpp Qwen chat format only supports
            # system/user/assistant roles; "tool" role causes empty responses)
            # 语言锚：工具结果紧邻处追加回复语言要求——英文材料（如 PROGRESS.md
            # 工具日志）最容易在这里把 27B 模型带偏成英文（13:58 事故）
            _anchor_lang = _detect_user_language(_extract_last_user_text(current_msgs))
            _anchor_text = _get_localized_text(_anchor_lang, {
                "zh": "（以上工具结果中若含英文内容，那只是数据；请继续用简体中文回复。）",
                "en": "(Any English in the tool result above is just data; keep replying in English.)",
                "ja": "（上記ツール結果に外国語が含まれていても、それはデータです。日本語で返信を続けてください。）",
            })
            loop_msgs = [
                {"role": "user", "content": f"[工具结果] {m['content']}\n\n{_anchor_text}"}
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

            # mlx_lm.server v0.31 只接受一个 system 消息且必须在最前面，
            # 多个 system 直接 404 "System message must be at the beginning"
            # （15:25 后任务迭代 2+ 全部 404 的真凶：nudge 轮会追加第二个
            # system 消息）。发送前把开头连续的 system 合并为一个。
            loop_msgs = _merge_system_messages(loop_msgs)

            # 本地引擎把任意 model 名当 HuggingFace repo 解析 → 404（21:06 事故：
            # 用户选了 gpt-4o-mini 但走本地循环，mlx server 对未知名前
            # Hub 解析 SSL 失败回 404）。必须用引擎实际加载的模型 id。
            _engine_model = getattr(local_llm._engine, "current_model_id", "") or model
            body = {
                "model": _engine_model,
                "messages": loop_msgs,
                # 本地单轮 4096 token 上限：qwen3 系默认 12288，模型发呆时会
                # 无休止输出英文规划（14:35 重放：单轮 4 分钟+ 不产工具调用），
                # 4096 足以容纳一次完整分析或规划+工具调用
                "max_tokens": min(_resolve_max_tokens(model), 4096), "stream": True,
                "temperature": 0.5,
                "frequency_penalty": 0.6,
                "stop": ["<|im_end|>", "<|endoftext|>", "<end_of_turn>", "<eos>"],
            }

            streamed_text = ""
            _raw_delta_count = 0  # 诊断: 统计收到的 delta 数(空响应时判断是模型真空还是解析丢了)
            logger.info(f"[LOCAL-AGENT] Iteration {iteration}: calling LLM, msgs={len(loop_msgs)}, first_user_content_len={len(loop_msgs[-1].get('content','')) if loop_msgs else 0}")
            # 本地 llama.cpp 并发流式请求会崩溃 -> _local_llm_stream 内部串行化
            # 流式停顿检测：模型过大/未加载完时可能极慢滴灌（120s 超时永不触发），
            # 连续 180s 无任何数据视为僵死，中止不再无限挂起
            #
            # 零交付断裂重试：引擎在本轮 LLM 调用中途死亡（健康处置杀进程/
            # 系统内存压力/OOM）时流会中断。若本轮尚未向用户交付任何内容
            # （streamed_text 为空 <=> 未 yield 过任何 content/reasoning），
            # 等待引擎自动重载后重发本轮调用--任务无感继续，不再
            # "执行一半停止"。已有部分输出时重发会重复，直接抛出。
            _stream_break_retries = 0
            _any_output = False  # 本轮是否产出过 content/reasoning（role-only 空块不算，P1-6）
            _dedup_fired = False  # 去重一次性截断标志（审计 A5：命中后不再永久吞输出）
            while True:
                try:
                    async with _local_llm_stream(client, api_url, body, headers) as r:
                        aiter = r.aiter_lines()
                        _fence_filter = _ThinkFenceFilter()
                        _silent_secs = 0  # 连续静默秒数（心跳与停滞判定共用，P0-4）
                        while True:
                            try:
                                # 60s 分片等待：静默超 60s 发一次心跳保活（前端
                                # 看门狗 180s 无事件会掐连接），累计静默达到
                                # 自适应停滞阈值（首 token 90s / 之后 180s，
                                # role-only 空块不算输出，P1-6）才判停滞
                                _stall_timeout = 90 if (not _any_output) else 180
                                line = await asyncio.wait_for(anext(aiter), timeout=60)
                                _silent_secs = 0
                            except asyncio.TimeoutError:
                                _silent_secs += 60
                                if _silent_secs < _stall_timeout:
                                    # 尚未到停滞阈值：发心跳，前端看门狗续命
                                    yield {"event": "heartbeat"}
                                    continue
                                _llm_suspect_since = _llm_suspect_since or time.monotonic()
                                if not _any_output and _is_local_llm_url(api_url):
                                    # 零交付停滞 = 引擎挂起（接受连接但不出字，
                                    # 15:10 事故同款）。杀掉+重载，并抛传输异常
                                    # 让外层零交付重试接管：引擎恢复后本轮调用
                                    # 自动重跑，任务不再以错误中断。
                                    logger.warning(
                                        "[LOCAL-AGENT] Iteration %s: 零交付停滞 %.0fs，"
                                        "判定引擎挂起，强制重载后重试本轮",
                                        iteration, _stall_timeout)
                                    _eng = local_llm._engine
                                    try:
                                        _eng._kill_port(_eng.server_port)
                                        _eng.server_status = "stopped"
                                    except Exception:
                                        pass
                                    if (_eng.current_model_id
                                            and not getattr(_eng, "_explicit_stop", False)
                                            and not _eng._auto_reloading
                                            and _eng.server_status != "error"):
                                        _eng._request_reload(_eng.current_model_id)
                                    raise httpx.ReadTimeout(
                                        "本地模型输出停滞（引擎挂起），已自动重载，任务将自动重试。")
                                raise TimeoutError(
                                    f"本地模型输出停滞超 {_stall_timeout:.0f} 秒（模型可能过大或未加载完）：{model[:60]}"
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
                                        _any_output = True
                                        streamed_text += content
                                        # 复读循环检测（节流：每 40 个 delta 一次）：
                                        # 必须放在 dedup 过滤之前——此前 dedup 命中后
                                        # continue 会跳过本检查，模型复读时全程无输出
                                        # 无截断、引擎 100% CPU 转到 max_tokens
                                        # （16:29 任务"停在尾端没反应"的帮凶之一）
                                        if _raw_delta_count % 40 == 0 and _detect_text_loop(streamed_text):
                                            logger.warning("[LOCAL-AGENT] 检测到输出复读循环，截断生成")
                                            yield {"content": "\n\n⚠️ 检测到输出重复，已自动截断。"}
                                            raise TimeoutError("输出复读循环，已截断")
                                        # 自我介绍去重：只截断一次，保留首段后继续流式
                                        # 输出——此前命中后永久 continue，模型第二次
                                        # "我是辣条"之后的所有真实内容被静默丢弃
                                        # （审计 A5：用户在推理模型里看到回复停在中间）
                                        if not _dedup_fired:
                                            _ded = _deduplicate_response(streamed_text)
                                            if len(_ded) < len(streamed_text):
                                                _dedup_fired = True
                                                streamed_text = _ded + content
                                        # 缓冲交付（16:50 事故）：本地模型内容不再逐字直播——
                                        # 英文会在收尾闸门/翻译轮运行之前就漏给用户。
                                        # 本轮内容全部累积，由各交付 return 路径统一做
                                        # 语言确保后一次性交付；reasoning 通道仍直播。
                                        _fence_filter.feed(content)
                                    elif reasoning:
                                        _any_output = True
                                        streamed_text += reasoning
                                        if _raw_delta_count % 40 == 0 and _detect_text_loop(streamed_text):
                                            logger.warning("[LOCAL-AGENT] 检测到输出复读循环，截断生成")
                                            yield {"content": "\n\n⚠️ 检测到输出重复，已自动截断。"}
                                            raise TimeoutError("输出复读循环，已截断")
                                        if not _dedup_fired:
                                            _ded = _deduplicate_response(streamed_text)
                                            if len(_ded) < len(streamed_text):
                                                _dedup_fired = True
                                                streamed_text = _ded + reasoning
                                        yield {"reasoning": reasoning, "ts": int(time.time() * 1000)}
                                except (json.JSONDecodeError, KeyError, TypeError, IndexError):
                                    pass
                                except Exception:
                                    logger.error("Local agent SSE parse error", exc_info=True)
                                    raise
                    break
                except httpx.TransportError as e:
                    # 与 _local_llm_stream 同口径：任何传输层错误都算流中断。
                    if streamed_text.strip():
                        # 部分输出已交付且引擎死亡：把已交付文本落为 assistant
                        # 消息并注入断点续写提示，重发本轮让模型从断点继续，
                        # 不再让用户拿着半截回答收错误（P0-3，限一次）
                        if _stream_break_retries >= 1:
                            raise
                        logger.warning(
                            f"[LOCAL-AGENT] Iteration {iteration}: 部分输出中断"
                            f"({type(e).__name__})，记录已交付文本后重发本轮续写")
                        current_msgs.append({"role": "assistant", "content": streamed_text.strip()})
                        current_msgs.append({
                            "role": "system",
                            "content": "你刚才的回答被中断了（模型服务异常）。"
                                       "请从断点继续完成回答，不要从头重复。",
                        })
                        body["messages"] = _merge_system_messages(current_msgs)
                        streamed_text = ""
                        _raw_delta_count = 0
                        _any_output = False
                        _dedup_fired = False
                        _stream_break_retries += 1
                        await asyncio.sleep(10)
                        continue
                    if _stream_break_retries >= 1:
                        raise
                    _stream_break_retries += 1
                    logger.warning(
                        f"[LOCAL-AGENT] Iteration {iteration}: LLM 流零交付中断"
                        f"({type(e).__name__})，等待引擎恢复后重发本轮调用 ({_stream_break_retries}/1)")
                    await asyncio.sleep(10)
                    continue

            # 收尾修正已移除（16:50 事故）：此前每轮流结束都发一次 content_revised
            # 全量替换，等于把模型文本在闸门/翻译之前直播给用户。缓冲交付下
            # 文本只由各交付 return 路径一次性 yield。

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
                _no_progress_deadline = time.monotonic() + 900  # 工具执行=实质进展
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
                    # 先发确认事件再等待（死锁修复）：confirm 级工具的
                    # tool_confirm 必须在执行前到达前端，弹窗才会出现
                    if not tc.get("id"):
                        tc["id"] = str(uuid.uuid4())
                    pre_started = None
                    try:
                        _tname = tc.get("function", {}).get("name", "unknown")
                        _targs = json.loads(tc.get("function", {}).get("arguments", "{}") or "{}")
                        if _resolve_permission(_tname, _targs) == "confirm" \
                                and not _confirm_bypassed(_tname, access_mode) \
                                and not _check_access(_tname, access_mode):
                            pre_started = await _start_tool_confirmation(tc["id"], _tname, _targs)
                            yield pre_started["event"]
                    except Exception:
                        pre_started = None
                    verify_failed, events = await _handle_tool_execution(
                        tc, current_msgs, session_id, agent_id, access_mode, pre_started=pre_started)
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
                    if any_new:
                        # 工具产出了新结果：在收到实质性文字回答（≥200 字符）
                        # 之前，不允许用一句话收尾（“让我读取数据再分析”式
                        # 声明/道歉不算完成）。
                        _pending_tool_analysis = True
                        _intent_nudges = 0
                else:
                    stagnation += 1
                if stagnation >= max_stagnation:
                        yield {"content": f"\n\n⚠️ 连续 {stagnation} 轮无新进展，Agent 停止。如需继续请发新消息。"}
                        return
                continue

            # No tool calls — pure text response done
            # 停滞计数：纯文本轮计入 stalled_rounds（工具轮/完成轮会复位，P2-12）
            _track_progress(session_id, "text_round", "text_only")
            if streamed_text.strip():
                _no_progress_deadline = time.monotonic() + 900  # 产出内容=实质进展
            # 工具已产出结果但模型尚未给出实质性回答时，不允许一句话收尾。
            # 此前依赖“最近 3 条消息里有工具结果”这个窗口，nudge 消息一多
            # 工具结果就被挤出窗口，模型连说两轮“让我读取数据再分析”都能
            # 被当作最终答案返回（16:45 大盘事故）。改为用 _pending_tool_analysis
            # 状态贯穿：只有 ≥200 字符的实质回答或新的工具调用才算推进。
            _recent_tool_failed = any(
                m.get("role") == "tool" and ("Error" in str(m.get("content", "")) or "⚠️" in str(m.get("content", "")) or "失败" in str(m.get("content", "")))
                for m in current_msgs[-4:]
            )
            if _pending_tool_analysis and streamed_text.strip() and not _recent_tool_failed:
                # 推理模型(Muse/Qwen3.5/Ornith)常先输出规划文字、下一轮才调工具——
                # 但“只声明不动手”的回复（让我读取/我再查一下…）不能当完成。
                _is_planning = _looks_like_planning(streamed_text)
                # 模型明确说"我改用/我要调用/我再单独查"但整轮没有实际工具调用
                # （21:42 事故：模型说"改用联网搜索"却直接收尾，未调 tavily）——
                # 这类带未执行意图的 ≥200 字符正文不算实质完成，继续 nudge。
                _pending_intent = any(k in streamed_text.lower() for k in _PENDING_INTENT_PATTERNS)
                # 纯外文长回复（如英文规划 891 字符）不算完成（13:58 事故）：
                # 用户说中文就必须中文交付，拦截后走 nudge 用中文重写。
                _lang_mismatch = _reply_lang_mismatch(
                    _extract_last_user_text(current_msgs), streamed_text)
                if (len(streamed_text.strip()) >= 200 and not _is_meta_wrapup(streamed_text)
                        and not _pending_intent and not _is_planning and not _lang_mismatch):
                    # 模型在工具结果后已给出实质性回答（≥200 字符）——接受为
                    # 最终答案直接收尾，不再追问。此前无差别追问导致模型
                    # 从头再答一遍，UI 里重复堆叠（17:07/17:08 两任务的
                    # "已经查完了👆"式重复）。
                    # 例外：元评论式收尾（"上面的分析已覆盖…任务完成"）不算
                    # 实质回答——分析只在模型思考里，正文从未交付（19:38
                    # 事故），继续走追问轮。
                    # 缓冲交付：语言确保后一次性交付（16:50 事故后不再直播）
                    _deliver = await _ensure_final_language(
                        client, api_url, headers, _engine_model,
                        streamed_text.strip(), _extract_last_user_text(current_msgs))
                    _pending_tool_analysis = False
                    current_msgs.append({"role": "assistant", "content": _deliver})
                    yield {"content": "\n\n" + _strip_think_fences(_deliver)}
                    _track_progress(session_id, "completed", f"text_response ({len(_deliver)} chars)")
                    logger.info(f"[LOCAL-AGENT] Iteration {iteration}: no tools, returning text ({len(_deliver)} chars)")
                    return
                if _lang_mismatch and not _is_planning and not _pending_intent:
                    # 实质回答（≥200 字、非规划）但语言不符 → 不追问重分析
                    # （会丢数据），直接翻译轮交付（09-03 事故最后一公里：
                    # 14:45 重放中 598 字母/42 汉字 的混合英文分析即走此路）
                    _deliver = await _force_translate(
                        client, api_url, headers, _engine_model,
                        streamed_text.strip(),
                        _detect_user_language(_extract_last_user_text(current_msgs)))
                    _pending_tool_analysis = False
                    yield {"content": "\n\n" + _strip_think_fences(_deliver)}
                    current_msgs.append({"role": "assistant", "content": _deliver})
                    _track_progress(session_id, "completed", f"translated_response ({len(_deliver)} chars)")
                    logger.info(f"[LOCAL-AGENT] Iteration {iteration}: 语言不符，翻译轮交付 ({len(_deliver)} chars)")
                    return
                # 思考段提取兜底：正文是半截/声明（<200 字符、未执行意图或元评论），
                # 但思考段里有 ≥200 字符的实质分析（27B 模型把分析全写进思考段，
                # 21:13 事故）——直接以思考段内容作为最终回答交付，不再追问。
                # 语言不符（英文思考）不在此交付，走 nudge/翻译兜底（09-03 事故）
                _think_body = _extract_think_body(streamed_text)
                if (len(_think_body) >= 200 and not _is_meta_wrapup(_think_body)
                        and not _reply_lang_mismatch(_extract_last_user_text(current_msgs), _think_body)):
                    _pending_tool_analysis = False
                    current_msgs.append({"role": "assistant", "content": _think_body})
                    yield {"event": "content_revised", "content": _think_body}
                    _track_progress(session_id, "completed", f"think_body ({len(_think_body)} chars)")
                    logger.info(f"[LOCAL-AGENT] Iteration {iteration}: 正文半截，交付思考段内容 ({len(_think_body)} chars)")
                    return
                if _intent_nudges < 3:
                    current_msgs.append({"role": "assistant", "content": streamed_text.strip()})
                    if _is_planning:
                        current_msgs.append({
                            "role": "system",
                            "content": _get_localized_text(_detect_user_language(_extract_last_user_text(current_msgs)), {
                                "zh": "⚠️ 必须用简体中文回复。\n"
                                       "不要只发声明或道歉。你刚才说还要继续——现在就调用工具去执行；如果数据其实已经足够，就把完整分析写进回复正文（含关键数字与结论）。\n"
                                       "⚠️ 重要：如果连续两次查询都只返回「全部A股」或「指数」汇总（没有各板块明细），说明本查询词对「各板块汇总」无解——请立即改用 tavily_search 搜索板块资金流向排名，或用 mx_query 查具体板块（如：'半导体板块资金流向'、'人工智能板块资金流向'），不要重复查相同的汇总词。",
                                "en": "You MUST reply in English.\n"
                                      "Don't just announce or apologize. You said you would continue — call the tool NOW; if the data is sufficient, write the full analysis in your reply body.\n"
                                      "IMPORTANT: If two consecutive queries return only 'all A-shares' or 'index' summary (no sector detail), that query is unsolved — immediately switch to tavily_search for sector flows, or mx_query a specific sector (e.g., 'semiconductor sector capital flow'). Do NOT repeat the same summary query.",
                                "ja": "必ず日本語で返信してください。\n"
                                      "宣言や謝罪だけでなく、続けると言ったなら今すぐツールを呼び出してください。データが十分なら完全な分析を本文に書いてください。\n"
                                      "重要：連続2回「全A株」「指数」のみの要約しか返らない場合は、そのクエリは無解です。直ちに tavily_search でセクター資金流を検索するか、mx_query で具体的セクター（例：「半導体セクター資金流」）を検索してください。同じ要約クエリを繰り返さないでください。",
                            }),
                        })
                        logger.info(f"[LOCAL-AGENT] Iteration {iteration}: 意图声明未行动，nudge 立即调用工具（{_intent_nudges + 1}/3）")
                    else:
                        current_msgs.append({
                            "role": "system",
                            "content": _get_localized_text(_detect_user_language(_extract_last_user_text(current_msgs)), {
                                "zh": "⚠️ 必须用简体中文回复。\n"
                                       "⚠️ 你刚才收到了工具的执行结果，但你的回复里没有给出实质内容"
                                       "（只有收尾话或道歉，真正的分析还留在你的思考里）。\n"
                                       "如果还需要数据，直接调用工具；否则把完整的分析写进回复正文："
                                       "包含从工具结果中得到的关键数据与结论，让用户直接读到。\n"
                                       "调用工具格式：```tool 工具名\n{\"参数\":\"值\"}\n```",
                                "en": "You MUST reply in English.\n"
                                      "⚠️ You received tool results but your reply contained no real content"
                                      " (only meta-commentary or apologies).\n"
                                      "If you still need data, call a tool directly; otherwise write"
                                      " the FULL analysis into your reply body with key data and"
                                      " conclusions from the tool results.\n"
                                      "Tool format: ```tool tool_name\n{\"param\":\"value\"}\n```",
                                "ja": "必ず日本語で返信してください。\n"
                                      "⚠️ ツール実行結果を受け取りましたが、返信に実質的な内容がありません"
                                      "（メタコメントや謝罪のみ）。\n"
                                      "データがまだ必要なら直接ツールを呼び出し、そうでなければ主要データと"
                                      "結論を含む完全な分析を本文に書いてください。\n"
                                      "形式：```tool ツール名\n{\"パラメータ\":\"値\"}\n```",
                            }),
                        })
                        logger.info(f"[LOCAL-AGENT] Iteration {iteration}: model returned text after tool result, pushing for continuation（{_intent_nudges + 1}/3）")
                    text_output_delivered = True  # 文本已交付，nudge 重试不再重复输出
                    text_only_streak += 1
                    _intent_nudges += 1
                    continue
                # 追问到上限仍只有声明/道歉：做一次"终答提取"兜底——不带工具、
                # 单一指令"写出完整分析"。本地 27B 级模型常被自身思维链卡住：
                # 思考里已有分析但正文只回意图声明（09-02 09:56 事故：mx_query
                # 数据到手，3 轮 nudge 模型仍只回 9 字符声明）。
                final_answer = ""
                _user_lang = _detect_user_language(_extract_last_user_text(current_msgs))
                _lang_name = {"zh": "简体中文", "en": "English", "ja": "日本語"}.get(_user_lang, "简体中文")
                try:
                    _final_msgs = [m for m in current_msgs if m.get("role") != "system"][-8:]
                    _final_msgs = _final_msgs + [{
                        "role": "system",
                        "content": (
                            f"任务收尾。请现在直接写出对用户的最终回答（必须用{_lang_name}）："
                            "把之前工具结果中的关键数据"
                            "与分析结论完整写进正文。不要再声明意图、不要再道歉、不要再调用任何工具。"
                            "直接输出分析内容本身。"
                        ),
                    }]
                    _fb = {
                        # 同主循环：必须用引擎实际模型 id，任意名会被当 HF repo 解析 404
                        "model": _engine_model, "messages": _final_msgs,
                        "max_tokens": 2048, "stream": False,
                        "temperature": 0.4, "frequency_penalty": 0.6,
                        "stop": ["<|im_end|>", "</think>", "<eos>"],
                    }
                    async with _local_llm_serialized(api_url):
                        _fr = await client.post(api_url, json=_fb, headers=headers)
                    if _fr.status_code == 200:
                        final_answer = ((_fr.json().get("choices") or [{}])[0]
                                        .get("message", {}).get("content", "") or "").strip()
                except Exception:
                    logger.warning("终答提取失败，回退原始文本", exc_info=True)
                # 语言兜底：终答仍为外文时，做一轮强制翻译（模型对翻译任务执行
                # 稳定，保证用户永远收到母语回复，09-03 事故最后一公里）
                if final_answer and _reply_lang_mismatch(_extract_last_user_text(current_msgs), final_answer):
                    final_answer = await _force_translate(
                        client, api_url, headers, _engine_model, final_answer, _user_lang)
                # 终答有效（比原声明长 3 倍以上）→ 交付终答；否则维持原收尾
                if len(final_answer) > max(200, len(streamed_text.strip()) * 3):
                    yield {"content": "\n\n" + _strip_think_fences(final_answer)}
                    _track_progress(session_id, "completed", f"final_answer ({len(final_answer)} chars)")
                    return
                # 原文本语言兜底：外文 → 强制翻译一轮再交付；缓冲交付下必须
                # 无条件显式 yield（此前依赖流式直播，16:50 事故后已移除）
                _deliver = await _ensure_final_language(
                    client, api_url, headers, _engine_model,
                    streamed_text.strip(), _extract_last_user_text(current_msgs))
                yield {"content": "\n\n" + _strip_think_fences(_deliver)}
                current_msgs.append({"role": "assistant", "content": _deliver})
                _track_progress(session_id, "completed", f"text_response ({len(_deliver)} chars)")
                logger.warning(f"[LOCAL-AGENT] Iteration {iteration}: {_intent_nudges} 轮追问仍无实质回答，收尾返回")
                # 必须 return：之前这里只打日志不返回，落回"短回答追问"分支
                # 再白送一轮（20:48 事故：收尾后又进"追问充分回答一轮"，迭代 6 重复跑）
                return
            if not has_called_tool and text_only_streak < 3 and streamed_text.strip():
                # Model gave a text response without calling tools.
                # Record the response so the model knows it already replied.
                current_msgs.append({"role": "assistant", "content": streamed_text.strip()})
                # 非任务型消息（闲聊/陈述/提问/长回复）→ 文本已交付给用户，直接结束，不再 nudge 重发
                user_q = _extract_last_user_text(current_msgs).strip().rstrip("?？") if current_msgs else ""
                has_task_kw = any(kw in user_q for kw in ["运行", "执行", "做", "帮我", "写", "创建", "查", "搜", "找", "分析", "修复", "构建", "部署", "安装", "配置", "run", "build", "fix", "create", "search", "analyze", "deploy"])
                if not has_task_kw:
                    # 缓冲交付：语言确保 + 显式 yield（闲聊回复也可能英文）
                    _deliver = await _ensure_final_language(
                        client, api_url, headers, _engine_model,
                        streamed_text.strip(), user_q)
                    yield {"content": "\n\n" + _strip_think_fences(_deliver)}
                    _track_progress(session_id, "completed", f"text_response ({len(_deliver)} chars)")
                    return
                # 任务型请求但模型只回文字不调工具 → nudge 促其行动（不再向用户重复流式输出）
                logger.info(f"[LOCAL-AGENT] Iteration {iteration}: model planning instead of calling tools, nudging (streak={text_only_streak})")
                _stag = _check_stagnation(session_id)
                if _stag:
                    current_msgs.append({"role": "system", "content": _stag})
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

            # 短回答 + 有工具失败：模型很可能因工具报错而放弃（如 400 参数错误）。
            # 追加一轮提示让它绕过失败的工具重试/换工具，而不是 112 字符草草收场。
            if (len(streamed_text.strip()) < 200 and not has_called_tool
                    and any(m.get("role") == "tool" and ("Error" in str(m.get("content", "")) or "⚠️" in str(m.get("content", "")) or "失败" in str(m.get("content", "")))
                            for m in current_msgs[-4:])):
                current_msgs.append({
                    "role": "system",
                    "content": "上一个工具调用失败了。请换一个工具或调整参数重试，"
                               "不要因一次失败就直接给简短结论；若全部工具不可用，再如实告知。",
                })
                text_output_delivered = True  # 追问轮替换上一条，不堆叠
                logger.info(f"[LOCAL-AGENT] Iteration {iteration}: 短回答+工具失败，继续追问一轮")
                continue
            # 短回答 + 工具成功且有实质资料（如查大盘指数拿到 45 行预览，
            # 最终却只回 84 字符"草草收场"）：提示模型基于已有资料给出
            # 充分、带数据的回答。阈值 600：低于它的大多是"几点了"这类
            # 小工具查询，逼长回答反而奇怪。只追加一次，防止死循环。
            _tool_out_total = sum(len(str(m.get("content") or "")) for m in current_msgs if m.get("role") == "tool")
            if (len(streamed_text.strip()) < 200 and has_called_tool
                    and _tool_out_total > 600 and not _brief_answer_nudged):
                _brief_answer_nudged = True
                current_msgs.append({
                    "role": "system",
                    "content": "你已通过工具获得了实质数据（见上方工具结果），"
                               "但刚才的回答太简短。请基于这些数据给出充分的回答："
                               "包含关键数字与必要的展开说明，让用户不需要再追问。"
                               "若任务确已完成且无需展开，再如实收尾。",
                })
                text_output_delivered = True  # 追问轮替换上一条，不堆叠
                logger.info(f"[LOCAL-AGENT] Iteration {iteration}: 短回答+有实质资料({_tool_out_total} chars)，追问充分回答一轮")
                continue

            # 无检查兜底路径（门控被 _recent_tool_failed 跳过时落在这里，
            # 16:55 事故英文 425 字符即从此交付）——缓冲交付下必须
            # 语言确保 + 显式 yield，英文不再原样到达用户
            _deliver = await _ensure_final_language(
                client, api_url, headers, _engine_model,
                streamed_text.strip(), _extract_last_user_text(current_msgs))
            yield {"content": "\n\n" + _strip_think_fences(_deliver)}
            _track_progress(session_id, "completed", f"text_response ({len(_deliver)} chars)")
            logger.info(f"[LOCAL-AGENT] Iteration {iteration}: no tools, returning text ({len(_deliver)} chars)")
            return

        tool_count = sum(1 for m in current_msgs if m.get("role") == "tool")
        yield {"content": f"\n\n⚠️ 已达到硬上限 ({max_iterations} 轮)。本会话共执行了 {tool_count} 次工具调用。如需继续，请发送新消息。"}


# ╔══════════════════════════════════════════════════════╗
# ║  SECTION 9: Chat Building & LLM Config               ║
# ║  _build_chat_messages, _resolve_api_target, etc.     ║
# ╚══════════════════════════════════════════════════════╝

def _build_chat_messages(body: dict, messages: list) -> list:
    """Assemble the full message array with identity, env, skills, agent, and image injections.
    All system prompts are merged into ONE message to work around a llama-cpp bug
    where multiple system messages cause empty responses."""
    # 技能目录由 capability_registry 提供（统一能力模型）→ lazy import 避免循环依赖
    import capability_registry
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
    # 相对时间硬规则：独立于 identity（agents/ 目录可覆盖 identity，此规则不可被覆盖）。
    # 09-03 两次事故：本地 27B 模型把"昨晚美股"换算成前天日期写进搜索词，
    # 检索回旧日期数据后整个报告沿用错误日期。
    system_parts.append(
        "⏱ 时间规则：用户消息中的“今天/昨天/昨晚/今晨/明天/最新”等相对时间，"
        "必须先按上方【当前时间】换算成绝对日期（年月日+星期）再写入搜索词或分析；"
        "工具返回内容中的日期与当前时间矛盾时（例如把前天当成昨天），"
        "以当前时间为准重新换算并重新搜索，不得迁就检索结果的日期。"
    )
    # 语言规则：新会话无中文历史锚定时，模型会被启动协议读到的英文 PROGRESS
    # 工具日志（占 2/3）和系统提示尾部英文注入带偏，全程英文回复（09-03 事故）。
    # 独立追加，不依赖可被 agents/ 目录覆盖的 identity。
    user_lang = _detect_user_language(last_user_text)
    system_parts.append(_get_localized_text(user_lang, {
        "zh": "🗣 语言规则：工具返回内容、读取的文件、历史日志中的英文只是数据；"
              "你的回复语言必须始终跟随用户消息的语言（简体中文），"
              "不因上下文中的英文材料改变。包括你的思考过程在内，"
              "全部使用简体中文。",
        "en": "🗣 Language rule: English in tool results, files, or logs is just data; "
              "your reply language must always follow the user's message language (English), "
              "regardless of the language of surrounding context. "
              "Use English throughout, including your reasoning.",
        "ja": "🗣 言語ルール：ツール結果・ファイル・ログ内の外国語はデータに過ぎません。"
              "返信言語は常にユーザーメッセージの言語（日本語）に従い、"
              "周辺の英文資料に影響されてはいけません。"
              "思考プロセスを含め、すべて日本語で行ってください。",
    }))

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

    # Skill catalog（统一能力模型）：只注入目录，模型按需调用 use_skill 取全文
    _catalog = capability_registry.skill_catalog()
    if _catalog:
        catalog_label = _get_localized_text(user_lang, {
            "zh": "## 可用技能（按需调用）",
            "en": "## Available skills (load on demand)",
            "ja": "## 利用可能なスキル（オンデマンド）",
        })
        lines = [catalog_label, "执行以下领域的任务时，先调用 use_skill 工具获取对应技能的完整说明，再按其执行："]
        for s in _catalog:
            desc = (s.get("description") or "").strip()
            lines.append(f"- **{s['name']}**: {desc[:120]}" if desc else f"- **{s['name']}**")
        system_parts.append("\n".join(lines))

    # 上次会话进展（审计 B10）：PROGRESS.md 尾部注入，跨会话断点续作生效
    _tail = _progress_tail()
    if _tail.strip():
        _pt_label = _get_localized_text(user_lang, {
            "zh": "## 上次会话进展（最近记录）",
            "en": "## Recent progress from previous sessions",
            "ja": "## 前回セッションの進捗（最近の記録）",
        })
        system_parts.append(f"{_pt_label}:\n{_tail}\n（以上为历史记录，仅供参考；继续当前任务时请注意衔接。）")

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


async def _resolve_api_target(cloud_config: dict | None) -> tuple[str, str, dict, bool]:
    """Resolve API URL, protocol, headers, and whether it's a local LLM (no cloud config).
    Cloud models are detected by having an endpoint (key is optional for local proxies).

    async：get_api_url 内含同步健康探测（最长 20s + 空闲复验 3s sleep），
    必须放线程池执行，否则阻塞事件循环（P2-13）。"""
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
        from starlette.concurrency import run_in_threadpool
        protocol = "openai"
        local_api = await run_in_threadpool(local_llm.get_api_url)
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
    # 剥离 URL/网址再计数：链接里的字母远多于中文消息的汉字数，
    # 不剥离会把"中文+链接"误判为 en，触发强制英文回复规则（09-03 事故）
    text = re.sub(r"https?://\S+|www\.\S+", "", text)
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
