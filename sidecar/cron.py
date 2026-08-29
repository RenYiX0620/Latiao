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


async def _execute_cron_job(job: dict):
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
    # 优先使用云端模型（支持原生 function calling）；仅在无云端时退回本地模型
    cloud = _get_best_cloud_config()
    protocol, api_url, headers, is_local = await _resolve_api_target(cloud)
    if not api_url:
        logger.warning("Cron job skipped: no API target（云端未配置且本地模型未运行）: %s", task[:50])
        _record_cron_result(job, "skipped", "跳过：无可用模型（云端未配置且本地模型未运行）")
        return
    model = (cloud or {}).get("model") or SUBAGENT_MODEL
    agent_tools = _get_agent_tools("latiao", TOOLS)
    active_tools = _filter_tools(task, agent_tools)
    if len(active_tools) > 5:
        active_tools = _cap_tools(active_tools, 5)

    current_msgs = [dict(m) for m in messages]
    full_content = ""
    tool_count = 0
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120)) as client:
            for _ in range(10):  # max 10 iterations for cron
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

                if is_local and not tc_data and content:
                    # 本地模式：从文本解析 prompt-based 工具调用（P1-9）
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
                    if len(current_msgs) > 8:
                        # Prevent infinite loop — give up after too many messages
                        # (10 轮硬上限制下 >15 永远不可达，收到 8 条即放弃)
                        full_content = "(Cron 任务未生成有效回复)"
                        break
                    continue
        ai_content = full_content or "(无输出)"
        _record_cron_result(job, "success", ai_content)
    except Exception as e:
        ai_content = f"[Cron 任务执行失败: {e}]"
        logger.warning("Cron LLM call failed: %s", e)
        _record_cron_result(job, "error", str(e))

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
    """带超时与异常保护的 cron 任务执行包装（后台任务异常不外抛）。"""
    with _cron_lock:
        _running_jobs.add(job["id"])
    try:
        await asyncio.wait_for(_execute_cron_job(job), timeout=600)
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
