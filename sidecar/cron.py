"""Cron Scheduler — cron job persistence, matching, and execution.

Split from main.py (Cron Job Scheduler section). Code is a verbatim move from
main.py — only imports were adjusted for the module split. Mutable cron state
(_cron_jobs / _cron_lock / _cron_last_run) lives here; routes in api_routes.py
access it through this module object (cron._cron_jobs) so rebindings stay
visible.
"""
import asyncio
import json
import logging
import threading
import uuid
from datetime import datetime
from pathlib import Path

import httpx

from config import PROGRESS_DIR
from db import _db_write_lock, _get_db

logger = logging.getLogger("latiao-sidecar")

# PROGRESS_DIR is imported from config
CRON_FILE = PROGRESS_DIR / "cron.json"


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
    # 清理 _cron_last_run 中已删除任务的记录，避免字典无限增长
    valid_ids = {j.get("id") for j in jobs}
    for stale_id in [k for k in _cron_last_run if k not in valid_ids]:
        del _cron_last_run[stale_id]


_cron_jobs: list[dict] = []
_cron_lock = threading.Lock()  # protects concurrent read/write to _cron_jobs
_cron_last_run: dict[str, str] = {}  # job_id → last run timestamp
_running_jobs: set[str] = set()  # 正在执行的任务 id（前端显示"执行中"）

# 执行状态持久化：跨重启的去重分钟表 + 最近完成事件（前端 toast 通知用）
CRON_STATE_FILE = PROGRESS_DIR / "cron_state.json"
_cron_state: dict = {"last_run": {}, "events": []}
_MAX_EVENTS = 50          # 事件环上限（心跳只取最近 10 分钟，50 条足够）
_CATCHUP_WINDOW_HOURS = 24  # 补跑回溯窗口：更早的错过视为放弃


def _load_cron_state():
    """启动时恢复跨重启的去重状态（防止重启后同一分钟重复执行）。"""
    try:
        if CRON_STATE_FILE.exists():
            data = json.loads(CRON_STATE_FILE.read_text(encoding="utf-8"))
            _cron_state["last_run"] = data.get("last_run", {})
            _cron_state["events"] = data.get("events", [])
            if data.get("seeded"):
                _cron_state["seeded"] = True
            _cron_last_run.update(_cron_state["last_run"])
    except Exception:
        logger.warning("Failed to load cron state", exc_info=True)


def _save_cron_state():
    try:
        PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
        CRON_STATE_FILE.write_text(
            json.dumps(_cron_state, ensure_ascii=False), encoding="utf-8")
    except Exception:
        logger.warning("Failed to save cron state", exc_info=True)


def _push_cron_event(task: str, status: str, summary: str, action: str, full: str = ""):
    """记录一次任务完成事件（供前端轮询心跳时弹 toast / 新建会话展示）。"""
    _cron_state["events"].append({
        "ts": datetime.now().isoformat(timespec="seconds"),
        "task": task, "status": status,
        "summary": summary[:200], "action": action,
        "full": (full or summary)[:4000],  # 完整结果（新会话展示用）
    })
    if len(_cron_state["events"]) > _MAX_EVENTS:
        _cron_state["events"] = _cron_state["events"][-_MAX_EVENTS:]
    _save_cron_state()


def get_recent_cron_events(minutes: int = 10) -> list[dict]:
    """心跳用：返回最近 N 分钟的完成事件。"""
    cutoff = datetime.now().timestamp() - minutes * 60
    out = []
    for e in _cron_state["events"]:
        try:
            if datetime.fromisoformat(e["ts"]).timestamp() >= cutoff:
                out.append(e)
        except (KeyError, ValueError):
            continue
    return out


_FIELD_RANGES = {"minute": (0, 59), "hour": (0, 23), "dom": (1, 31), "month": (1, 12), "dow": (0, 7)}


def _validate_schedule(expr: str) -> str | None:
    """校验 5 段 cron 表达式，合法返回 None，否则返回中文错误说明。"""
    if not expr or not isinstance(expr, str):
        return "表达式不能为空"
    parts = expr.strip().split()
    if len(parts) != 5:
        return "必须是 5 段格式：分 时 日 月 周（如 0 9 * * *）"
    names = ["分", "时", "日", "月", "周"]
    keys = ["minute", "hour", "dom", "month", "dow"]
    for i, (field, name) in enumerate(zip(parts, names, strict=True)):
        rng = _FIELD_RANGES[keys[i]]
        for token in field.split(","):
            token = token.strip()
            if token == "*":
                continue
            if token.startswith("*/"):
                step = token[2:]
                if not step.isdigit() or not (1 <= int(step) <= rng[1]):
                    return f"第{i+1}段({name})步进值无效: {token}"
                continue
            if "-" in token:
                lo, _, hi = token.partition("-")
                if not (lo.isdigit() and hi.isdigit()):
                    return f"第{i+1}段({name})范围无效: {token}"
                lo_v, hi_v = int(lo), int(hi)
                if not (rng[0] <= lo_v <= rng[1] and rng[0] <= hi_v <= rng[1] and lo_v <= hi_v):
                    return f"第{i+1}段({name})范围超出 {rng[0]}-{rng[1]}: {token}"
                continue
            if not token.isdigit() or not (rng[0] <= int(token) <= rng[1]):
                return f"第{i+1}段({name})取值应在 {rng[0]}-{rng[1]}: {token}"
    return None




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
        logger.warning("Cron match check failed", exc_info=True)
        return False


def _get_due_jobs(now: datetime) -> list[dict]:
    """纯查询：返回当前到期的任务，不写任何状态（不更新 _cron_last_run）。"""
    due = []
    now_str = now.strftime("%Y-%m-%d %H:%M")
    with _cron_lock:
        for job in _cron_jobs:
            if not job.get("enabled", True):
                continue
            job_id = job["id"]
            if _cron_last_run.get(job_id, "") == now_str:
                continue  # Already ran this minute
            if job_id in _running_jobs:
                continue  # 上次执行还在跑（本地模型一轮可 20 分钟），避免重叠执行
            if _cron_matches(job["schedule"], now):
                due.append(job)
    return due


def _mark_cron_run(job_ids: list[str], now: datetime):
    """标记任务已执行（写入 _cron_last_run + 持久化）。仅对确认要执行的任务调用。"""
    now_str = now.strftime("%Y-%m-%d %H:%M")
    with _cron_lock:
        for job_id in job_ids:
            _cron_last_run[job_id] = now_str
            _cron_state["last_run"][job_id] = now_str
    _save_cron_state()


def _create_cron(schedule, task):
    import uuid as _uid
    from datetime import datetime
    err = _validate_schedule(schedule)
    if err:
        return "创建失败，cron 表达式无效: " + err + "。正确格式: 分 时 日 月 周，例如 \"0 9 * * *\" 表示每天 9 点。"
    job = {"id": str(_uid.uuid4()), "schedule": schedule, "task": task, "name": task,
           "action": "execute", "enabled": True, "created_at": datetime.now().isoformat(), "last_run": ""}
    with _cron_lock:
        _cron_jobs.append(job)
        _save_cron(_cron_jobs)
    return "定时任务已创建: " + task + " (" + schedule + ")"


# ── Seed default cron jobs ──

def _seed_default_cron():
    """首次启动时播种默认任务（seeded 标记持久化）。

    只在从未初始化过（无 seeded 标记且 cron.json 为空）时播种一次；
    用户删除全部任务后 cron.json 为空，但 seeded 标记已存在——
    重启不会再恢复默认任务。
    """
    global _cron_jobs
    _cron_jobs = _load_cron()
    if _cron_state.get("seeded"):
        return
    if not _cron_jobs:
        _cron_jobs = [
            {"id": str(uuid.uuid4()), "schedule": "0 9 * * *", "task": "📋 每日摘要 (记录到记忆库)", "action": "notify", "enabled": True, "created_at": datetime.now().isoformat()},
            {"id": str(uuid.uuid4()), "schedule": "*/30 * * * *", "task": "🔍 健康检查 (记录到记忆库)", "action": "notify", "enabled": True, "created_at": datetime.now().isoformat()},
            {"id": str(uuid.uuid4()), "schedule": "0 18 * * 5", "task": "📊 每周汇总 (记录到记忆库)", "action": "notify", "enabled": False, "created_at": datetime.now().isoformat()},
        ]
        _save_cron(_cron_jobs)
    _cron_state["seeded"] = True
    _save_cron_state()


async def _execute_cron_job(job: dict, force_local: bool = False):
    """Execute a due cron job: run the task through the agent loop with tools enabled."""
    # 依赖 agent_loop 的循环/工具符号 → 函数内 lazy import 避免循环依赖
    # SUBAGENT_MODEL 常量由 main.py 门面持有
    from agent_loop import (
        _NATIVE_TOOL_RE,
        TOOLS,
        _build_local_tools_prompt,
        _cap_tools,
        _deduplicate_response,
        _filter_tools,
        _get_agent_config,
        _get_agent_tools,
        _get_best_cloud_config,
        _local_llm_serialized,
        _merge_system_messages,
        _parse_native_tool_calls,
        _parse_prompt_tool_calls,
        _resolve_api_target,
        _safe_cwd,
        _strip_native_tool_calls,
        execute_tool,
    )
    from main import SUBAGENT_MODEL
    from tool_executor import _resolve_permission
    task = job.get("task", "")
    action = job.get("action", "notify")
    logger.info("Cron job triggered: %s — %s", task, action)

    # Build messages with identity, env, and tools enabled
    home = str(Path.home())
    cwd = _safe_cwd()
    now = datetime.now().strftime("%Y-%m-%d (%A) %H:%M:%S")
    agent_cfg = _get_agent_config("latiao")

    messages = [{"role": "system", "content": (
        f"## 系统规则\n{agent_cfg['identity']}\n\n"
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
    # 模型选择：本地引擎在跑（用户主动加载了模型）→ 本地优先（免费、
    # 不占云端配额）；本地没跑 → 云端（GLM-5.2 等）。
    # force_local：云端 429 限流时强制本地重跑
    import local_llm as _llm
    _local_ready = False
    if not force_local:
        try:
            _mid = getattr(_llm._engine, "current_model_id", "")
            _local_ready = bool(_mid) and _llm._engine.is_running()
        except Exception:
            _local_ready = False
    cloud = None if (force_local or _local_ready) else _get_best_cloud_config()
    protocol, api_url, headers, is_local = await _resolve_api_target(cloud)
    if is_local and not _local_ready:
        # 引擎没跑但走了本地分支（cloud 为 None）：触发自动重载，
        # 请求在 _local_llm_serialized 里排队等引擎就绪
        _llm.get_api_url()
    if not api_url:
        logger.warning("Cron job skipped: no API target（云端未配置且本地模型未运行）: %s", task[:50])
        _record_cron_result(job, "skipped", "跳过：无可用模型（云端未配置且本地模型未运行）")
        return
    # 模型名解析（审计 A4）：本地时用引擎真实加载的模型 id——此前
    # 落回 SUBAGENT_MODEL（gpt-4o-mini）发给本地 mlx 引擎必 404，
    # 每条本地定时任务都走 error 路径
    if is_local:
        import local_llm
        model = (getattr(local_llm._engine, "current_model_id", "")
                 or getattr(local_llm._engine, "current_model_name", "")
                 or SUBAGENT_MODEL)
    else:
        model = (cloud or {}).get("model") or SUBAGENT_MODEL
    agent_tools = _get_agent_tools("latiao", TOOLS)
    active_tools = _filter_tools(task, agent_tools, scheduling_shortcut=False)
    # 定时任务禁止 delegate_task：cron 主循环与派生的 explore 子任务会争抢
    # 同一个本地引擎（_local_llm_serialized 串行），主任务 10 轮迭代被
    # 子任务拖到 10 分钟最终 LLM 调用失败（09-01 11:20 事故）。cron 应当
    # 自己用 mx_query 等工具完成任务，不派生后台子智能体。
    active_tools = [t for t in active_tools
                    if t.get("function", {}).get("name") != "delegate_task"]
    if len(active_tools) > 5:
        active_tools = _cap_tools(active_tools, 5, keep_first=("mx_query", "ak_finance"))

    current_msgs = [dict(m) for m in messages]
    full_content = ""
    tool_count = 0
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300)) as client:
            for _cron_iter in range(10):  # max 10 iterations for cron
                # 逐轮诊断日志（此前 cron 循环零日志，失败时完全看不到
                # 模型每轮返回了什么——09-01 "(无输出)" 排查黑洞）
                logger.info("[CRON] iter=%d msgs=%d tools=%d model=%s",
                            _cron_iter + 1, len(current_msgs), len(active_tools),
                            (model or "")[:50])
                # 本地模式：prompt-based 工具（此前本地 cron 无任何工具，P1-9）
                # 且发送前统一合并 system——重试轮追加的第二个 system 会让
                # mlx 404（与主循环同款修复）
                _cron_msgs = _merge_system_messages(current_msgs)
                if is_local:
                    _cron_msgs = _cron_msgs + [{
                        "role": "system",
                        "content": _build_local_tools_prompt(active_tools),
                    }]
                    _cron_msgs = _merge_system_messages(_cron_msgs)
                req_body = {
                    "model": model, "messages": _cron_msgs,
                    "max_tokens": 2048, "stream": False,
                    "temperature": 0.5,
                    "frequency_penalty": 0.6,
                "stop": ["<|im_end|>", "<|endoftext|>", "<end_of_turn>", "<eos>"],
                }
                if not is_local:
                    # 本地模型不支持原生 function calling，只对云端发送 tools
                    req_body["tools"] = active_tools
                    req_body["tool_choice"] = "auto"
                async with _local_llm_serialized(api_url):
                    resp = await client.post(api_url, json=req_body, headers=headers)
                resp.raise_for_status()  # httpx 不自动抛 4xx/5xx，必须显式检查
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

                if not tc_data and content:
                    # 本地模式：从文本解析 prompt-based 工具调用（P1-9）。
                    # 云端也解析：GLM-5.2 等火山 coding 端点不解析 function
                    # calling，模型在 content 里输出 Hermes XML/栅栏格式工具
                    # 调用——不解析则原样交付、工具不执行（09-01 16:37 事故）。
                    _clean, tc_data = _parse_prompt_tool_calls(content)
                    if tc_data:
                        content = _clean

                if tc_data:
                    tool_count += 1
                    current_msgs.append({"role": "assistant", "content": _deduplicate_response(content) if content else None, "tool_calls": tc_data})
                    for tc in tc_data:
                        tool_name = tc.get("function", {}).get("name", "")
                        tool_args_str = tc.get("function", {}).get("arguments", "{}")
                        try:
                            tool_args = json.loads(tool_args_str) if isinstance(tool_args_str, str) else tool_args_str
                        except json.JSONDecodeError:
                            tool_args = {}
                        perm = _resolve_permission(tool_name, tool_args)
                        if perm in ("confirm", "danger", "deny", "blocked"):
                            result = f"⛔ Cron 任务不支持需要确认或被权限规则阻止的操作: {tool_name}（级别 {perm}）"
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
                    if len(current_msgs) > 8:
                        # Prevent infinite loop — give up after too many messages
                        # (10 轮硬上限制下 >15 永远不可达，收到 8 条即放弃)
                        full_content = "(Cron 任务未生成有效回复)"
                        break
                    continue
            # 满轮退出（10 轮跑满还没产出文字总结——模型一直在调工具，
            # 09-01 14:53 事故：GLM-5.2 十轮全工具调用，msgs=33 无文字，
            # full_content 空 → "(无输出)"）。此时强制追加一轮"收尾提问"，
            # 把已收集的工具结果提炼成总结；仍失败则把工具结果直接
            # 作为内容兜底，绝不给用户一个空会话。
            if not full_content.strip() or full_content.strip() in ("(无输出)", "(Cron 任务未生成有效回复)"):
                if tool_count > 0:
                    try:
                        current_msgs.append({
                            "role": "system",
                            "content": "轮次已用完，禁止再调用任何工具。请立即输出最终完整报告：直接给出任务要求的所有部分（如大盘走势、板块资金流向明细、操作建议），基于已收集的工具结果，写完整的分析文字。",
                        })
                        _cron_msgs = _merge_system_messages(current_msgs)
                        # 收尾轮不注入任何工具提示（云端原生 tools 也不发）——
                        # 带着工具清单模型会继续"过渡句+下轮调用"而不是写报告
                        # （15:03 事故：收尾轮仍只回 90 字过渡句）。纯文字模式
                        # 逼模型只能输出总结。
                        _final_body = {
                            "model": model, "messages": _cron_msgs,
                            "max_tokens": 2048, "stream": False,
                            "temperature": 0.5, "frequency_penalty": 0.6,
                            "stop": ["<|im_end|>", "```", "<end_of_turn>", "<eos>"],
                        }
                        async with httpx.AsyncClient(timeout=httpx.Timeout(300)) as _fc:
                            async with _local_llm_serialized(api_url):
                                _fr = await _fc.post(api_url, json=_final_body, headers=headers)
                        if _fr.status_code == 200:
                            _fm = (_fr.json().get("choices") or [{}])[0].get("message", {})
                            if (_fm.get("content") or "").strip():
                                full_content = _fm["content"]
                                logger.info("[CRON] 收尾轮成功产出总结 %d 字", len(full_content))
                    except Exception:
                        logger.warning("[CRON] 收尾轮失败", exc_info=True)
                if not (full_content or "").strip() or full_content.strip() in ("(无输出)", "(Cron 任务未生成有效回复)"):
                    # 终极兜底：把工具结果本身作为输出（有数据总比空会话强）
                    _tool_outs = [str(m.get("content") or "") for m in current_msgs if m.get("role") == "tool"]
                    if _tool_outs:
                        full_content = "（任务执行了 %d 次工具调用，以下为收集到的数据）\n\n" % tool_count + "\n\n---\n\n".join(_tool_outs[-6:])
                    else:
                        full_content = "(无输出)"
        ai_content = full_content or "(无输出)"
        _record_cron_result(job, "success", ai_content)
    except Exception as e:
        # 云端 API 429 限流 → 回退本地引擎完整重跑一次（本地引擎不受
        # 云端配额限制；重跑也带 force_local 防再回云端）
        if not force_local and "429" in str(e):
            logger.warning("云端 API 429 限流，回退本地模型重跑定时任务")
            return await _execute_cron_job(job, force_local=True)
        # 异常 str 可能为空（如空消息的 TimeoutError/CancelledError）——
        # 前端 ⏰ 会话会显示"(无输出)"，用户完全不知道发生了什么。
        # 兜底为类型名 + 明确失败原因。
        _err = str(e).strip() or type(e).__name__
        ai_content = f"[Cron 任务执行失败: {_err}]"
        logger.warning("Cron LLM call failed: %s (%s)", _err, type(e).__name__)
        _record_cron_result(job, "error", ai_content)

    # Record to memory DB with AI result
    try:
        conn = _get_db()
        with _db_write_lock:  # 快速 sqlite 操作，持锁时间短，用同步锁即可
            conn.execute(
                "INSERT INTO memory (session_id, type, topic, content, meta) VALUES (?, ?, ?, ?, ?)",
                ("cron", "cron_job", task,
                 f"Cron: {task}\n执行时间: {datetime.now().isoformat()}\n\nAI 分析结果:\n{ai_content}",
                 json.dumps({"action": action, "schedule": job.get("schedule"), "ai_result": ai_content[:200]})),
            )
            conn.commit()
    except Exception:
        logger.warning("Failed to record cron job to memory DB", exc_info=True)

    logger.info("Cron job completed: %s", task[:50])



def _record_cron_result(job: dict, status: str, summary: str):
    """任务结束时更新 job 的执行状态与历史，并推送完成事件。"""
    now_iso = datetime.now().isoformat(timespec="seconds")
    with _cron_lock:
        job["last_run"] = now_iso
        job["last_status"] = status
        job["last_result"] = summary[:200]
        history = job.setdefault("history", [])
        history.append({"ts": now_iso, "status": status, "summary": summary[:200]})
        if len(history) > 20:
            del history[:-20]
        _save_cron(_cron_jobs)
    _push_cron_event(job.get("task", ""), status, summary, job.get("action", "notify"), summary)


def _find_missed_jobs(now: datetime) -> list[dict]:
    """找出窗口期内本应执行却没执行的任务（App 关闭/睡眠期间错过）。

    逐分钟回扫最近 24h（1440 次/job，开销可忽略）；任务从不曾运行时
    以 created_at 为回扫起点。每个任务最多补跑一次。
    """
    missed = []
    window_start = now.timestamp() - _CATCHUP_WINDOW_HOURS * 3600
    with _cron_lock:
        # 保留原始引用：执行结束时要更新 _cron_jobs 里的真实对象（last_status 等）
        jobs_snapshot = [j for j in _cron_jobs if j.get("enabled", True)]
    for job in jobs_snapshot:
        try:
            last_iso = job.get("last_run", "") or ""
            if last_iso:
                last_dt = datetime.fromisoformat(last_iso)
                scan_from = max(last_dt.timestamp(), window_start)
            else:
                created = datetime.fromisoformat(job.get("created_at", "") or datetime.now().isoformat())
                scan_from = max(created.timestamp(), window_start)
            t = scan_from
            # 对齐到下一分钟
            t = (int(t // 60) + 1) * 60
            while t < now.timestamp():
                dt = datetime.fromtimestamp(t)
                if _cron_matches(job["schedule"], dt):
                    missed.append(job)
                    break
                t += 60
        except (ValueError, KeyError):
            continue
    return missed


async def run_cron_catchup():
    """启动时补跑错过的任务（App 关闭期间到期的）。"""
    from agent_loop import _spawn
    now = datetime.now()
    try:
        missed = _find_missed_jobs(now)
    except Exception:
        logger.warning("Cron catch-up scan failed", exc_info=True)
        return
    if not missed:
        return
    for job in missed:
        logger.info("Cron catch-up: %s (错过窗口内的一次执行)", job.get("task", "")[:50])
        _mark_cron_run([job["id"]], now)
        _spawn(_run_cron_job_guarded(job))


async def _run_cron_job_guarded(job: dict):
    """带超时与异常保护的 cron 任务执行包装（后台任务异常不外抛）。

    超时预算：本地小模型（9B GGUF）执行带工具的金融任务，首轮思考即可
    达 3-6 分钟，600s 只够 1-2 轮迭代 → 任务必超时失败（09-01 10:40
    事故：首轮 LLM 6 分钟 + 迭代 4 超时）。给 1200s 总预算，配合
    _execute_cron_job 内迭代上限，够跑完整任务。
    """
    with _cron_lock:
        _running_jobs.add(job["id"])
    try:
        await asyncio.wait_for(_execute_cron_job(job), timeout=1200)
    except Exception:
        logger.warning("Cron job failed/timed out: %s", job.get("task", "")[:50], exc_info=True)
    finally:
        with _cron_lock:
            _running_jobs.discard(job["id"])


async def _cron_loop():
    """Background task: tick cron every 60 seconds."""
    # 依赖 agent_loop 的后台任务工具 → 函数内 lazy import 避免循环依赖
    from agent_loop import _spawn
    while True:
        try:
            await asyncio.sleep(60)
            now = datetime.now()
            due = _get_due_jobs(now)
            if due:
                # 仅对确认执行的任务标记 last_run；并发执行避免单任务阻塞调度 tick
                _mark_cron_run([j["id"] for j in due], now)
                for job in due:
                    _spawn(_run_cron_job_guarded(job))
        except Exception:
            logger.warning("Cron loop error", exc_info=True)
