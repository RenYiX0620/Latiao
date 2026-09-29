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
import os
import re
import threading
import time
import uuid
from datetime import datetime


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


def list_cron_history(limit: int = 20, offset: int = 0, job: str = "") -> dict:
    """读定时任务的历史产出（memory 表 type='cron_job'，2026-09-23 修）。

    为什么补这个读取点：这张表从 5 月底起记了 155 行"每次跑完的完整 AI 产出"
    （如"A股大盘资金走势报告"），但**全库没有一个读取点**——跑完就再也看不到，
    前端只显示每个任务最近一次的 60 字摘要。现在它有三个读者：/v1/cron/history
    端点、cron_history 工具（模型按需查）、前端历史区。

    过滤：job 关键字匹配 topic（任务描述）。返回不含 meta（里面是执行参数，体积大）。
    """
    try:
        conn = _get_db()
    except Exception as e:
        return {"status": "error", "message": str(e), "items": []}
    where = "type = 'cron_job'"
    args: list = []
    if job:
        where += " AND topic LIKE ?"
        args.append(f"%{job}%")
    try:
        total = conn.execute(f"SELECT COUNT(*) FROM memory WHERE {where}", args).fetchone()[0]
        rows = conn.execute(
            f"SELECT rowid, topic, content, created_at FROM memory WHERE {where} "
            f"ORDER BY created_at DESC LIMIT ? OFFSET ?", (*args, int(limit), int(offset))).fetchall()
    except Exception as e:
        logger.warning("读取定时任务历史失败", exc_info=True)
        return {"status": "error", "message": str(e), "items": []}
    items = []
    for rid, topic, content, created_at in rows:
        # content 形如 "Cron: <task>\n执行时间: …\n\nAI 分析结果:\n<body>"，剥出正文
        body = content.split("AI 分析结果:", 1)[1].strip() if "AI 分析结果:" in (content or "") else (content or "")
        items.append({"id": rid, "task": topic or "", "created_at": created_at or "",
                      "result": body, "summary": " ".join(body.split())[:120]})
    return {"status": "ok", "total": total, "items": items}


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


# ── 收口判据（09-23）────────────────────────────────────────────
# 此前拿到正文后只累加不退出，循环会一直问模型直到它回一个空响应（或跑满 10 轮）。
# 真机实测（读配置写摘要的简单任务）：9 次调用 / 88.6s，其中第 4 轮就已写出可交付的
# 160 字结论，后面 5 轮（含 7 次工具调用）全是空转。
# 判据：非空正文 + 本轮**没有工具调用** + 不像过渡句 → 收口。
# 有工具调用的轮次一律不在此列（那是在干活，不是在交付）。
_CRON_FINAL_MIN_CHARS_TOOLED = 120   # 已走过工具轮：120 字即可认为在交付结论
_CRON_FINAL_MIN_CHARS_CHAT = 250     # 一次工具都没用过：更保守（纯聊天/问答型任务）
_CRON_TRANSITION_RE = re.compile(
    r"(我先|让我先|我先来|接下来我|下面我|我将|我正在|先查一下|先去查|稍等|马上开始|正在执行)")


_CRON_MAX_STEPS = int(os.environ.get("LATIAO_CRON_MAX_STEPS", "12") or 12)


def _cron_tool_whitelist(task: str) -> set:
    """定时任务的工具白名单：相关性收窄 + 禁 delegate_task + 最多 5 个。

    - **禁 delegate_task**：cron 主任务与派生的子任务会争抢同一个本地引擎
      （串行闸），主任务被拖到超时（09-01 11:20 事故）——定时任务应当自己用
      mx_query 等工具完成，不派生后台子智能体。
    - 相关收窄 + 最多 5 个：弱模型面对 30 个工具只会乱选（原实现同款约束）。
    """
    from agent_loop import TOOLS, _cap_tools, _filter_tools, _get_agent_tools
    agent_tools = _get_agent_tools("latiao", TOOLS)
    active = _filter_tools(task, agent_tools, scheduling_shortcut=False)
    active = [t for t in active if t.get("function", {}).get("name") != "delegate_task"]
    if len(active) > 5:
        active = _cap_tools(active, 5, keep_first=("mx_query", "ak_finance"))
    names = {t.get("function", {}).get("name") for t in active}
    return {n for n in names if n}


async def _execute_cron_job(job: dict, force_local: bool = False):
    """执行一次定时任务——走**与聊天/通道同一个** ThinAgentLoop。

    2026-09-29 审计（结构性问题）：此前 cron 自带一套 10 轮工具循环，于是聊天侧有的
    任务级验证器 / 预算守卫与近阈提示 / 停滞与同错升级 / 压缩与旧结果回收 / 用量记账，
    **定时任务全都没有**——而"无人值守长跑"恰恰最需要这些闸门；它的开销在面板与
    turn_metrics 里也完全不可见。

    现在只保留 cron 特有的四件事：**模型选择**（本地优先 / 云端 / 429 回退本地）、
    **工具白名单**（`_cron_tool_whitelist`）、**工具档位**（job.access_mode，默认 full =
    与原行为一致；无人值守时建议显式设 read_only）、**结果落库与事件**。执行、记账、
    闸门、交付兜底全部交给唯一循环。
    """
    from agent.context import _normalize_access
    from agent.loop import ThinAgentLoop
    from agent.prompt_build import _build_chat_messages
    from agent_loop import _get_best_cloud_config, _resolve_api_target
    import local_llm as _llm
    from main import SUBAGENT_MODEL

    task = job.get("task", "")
    action = job.get("action", "notify")
    access_mode = _normalize_access(job.get("access_mode") or "full")
    logger.info("Cron job triggered: %s — %s（工具档 %s）", task[:60], action, access_mode)

    # ── 模型选择（保持原语义）──
    # 本地引擎在跑（用户主动加载了模型）→ 本地优先（免费、不占云端配额）；
    # 本地没跑 → 云端（GLM-5.2 等）；force_local：云端 429 限流时强制本地重跑。
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
        # 引擎没跑但走了本地分支（cloud 为 None）：触发自动重载，请求在串行闸里排队等就绪
        _llm.get_api_url()
    if not api_url:
        logger.warning("Cron job skipped: no API target（云端未配置且本地模型未运行）: %s", task[:50])
        _record_cron_result(job, "skipped", "跳过：无可用模型（云端未配置且本地模型未运行）")
        return
    if is_local:
        # 本地用引擎真实加载的模型 id（审计 A4：此前落回 SUBAGENT_MODEL 会必 404）
        model = (getattr(_llm._engine, "current_model_id", "")
                 or getattr(_llm._engine, "current_model_name", "")
                 or SUBAGENT_MODEL)
    else:
        model = (cloud or {}).get("model") or SUBAGENT_MODEL

    session_id = f"cron:{job.get('id') or 'job'}"
    _cron_user = (f"定时任务: {task}\n\n"
                  "请完整执行这个任务，最后输出**完整的报告正文**（含关键数字与结论）；"
                  "不要把计划或执行说明当结论。")
    body = {"agent": job.get("agent") or "latiao", "session_id": session_id,
            # 非交互闸：定时任务不是"用户在回答首启引导"（否则短任务名会被记成称呼）
            "non_interactive": True,
            "messages": [{"role": "user", "content": _cron_user}]}
    _t_start = time.monotonic()
    try:
        msgs = _build_chat_messages(body, body["messages"])
        loop = ThinAgentLoop(
            msgs, model, api_url, headers, session_id=session_id,
            access_mode=access_mode, is_local=is_local,
            tool_whitelist=_cron_tool_whitelist(task))
        # 无人值守：步数收紧（原实现是 10 轮迭代），预算/停滞/同错闸门照常生效
        loop.max_steps = _CRON_MAX_STEPS
        parts: list[str] = []
        async for ev in loop.run():
            if isinstance(ev, dict) and ev.get("content"):
                parts.append(str(ev["content"]))
        ai_content = "".join(parts).strip()
        _steps = getattr(loop, "steps", 0)
    except Exception as e:
        # 引擎/装配层异常（循环内部的错误都已转成内容交付，不会到这儿）
        _err = str(e).strip() or type(e).__name__
        logger.warning("[CRON] 执行异常: %s（%s）", task[:40], _err, exc_info=True)
        _record_cron_result(job, "error", f"[异常] {_err}")
        _cron_write_memory(task, action, job, f"[Cron 任务执行失败: {_err}]")
        return

    # 云端 429 → 本地重跑一次（原语义保留；循环把 HTTP 错误当内容交付，故按标记识别）
    if (not force_local and not is_local
            and re.search(r"HTTP\s*429", ai_content or "")):
        logger.warning("云端 429 限流，回退本地模型重跑定时任务")
        return await _execute_cron_job(job, force_local=True)

    if not ai_content:
        ai_content = "（任务没有产出内容）"
    logger.info("[CRON] 完成：任务=%s 步数=%d 耗时=%.1fs 正文=%d字",
                task[:40], _steps, time.monotonic() - _t_start, len(ai_content))
    _record_cron_result(job, "success", ai_content)
    try:
        # 用量记账（审计①：定时任务的开销此前完全不可见，面板与 turn_metrics 都没有）
        import turn_metrics
        turn_metrics.record_turn(session_id, model=model, is_local=is_local,
                                 ended_reason="cron")
    except Exception:
        logger.debug("cron turn_metrics 落库失败", exc_info=True)
    _cron_write_memory(task, action, job, ai_content)


def _cron_write_memory(task: str, action: str, job: dict, ai_content: str) -> None:
    """把定时任务结果写进记忆库（原实现的行为，抽出来给成功/失败两条路共用）。"""
    try:
        conn = _get_db()
        with _db_write_lock:  # 快速 sqlite 操作，持锁时间短，用同步锁即可
            conn.execute(
                "INSERT INTO memory (session_id, type, topic, content, meta) VALUES (?, ?, ?, ?, ?)",
                ("cron", "cron_job", task,
                 f"Cron: {task}\n执行时间: {datetime.now().isoformat()}\n\nAI 分析结果:\n{ai_content}",
                 json.dumps({"action": action, "schedule": job.get("schedule"),
                             "ai_result": ai_content[:200]})),
            )
            conn.commit()
    except Exception:
        logger.warning("Failed to record cron job to memory DB", exc_info=True)


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


# 定时任务总超时（2026-09-29 审计：原是写死的 1200s，提出成常量便于配置/测试）
_CRON_JOB_TIMEOUT = float(os.environ.get("LATIAO_CRON_TIMEOUT", "1200") or 1200)


async def _run_cron_job_guarded(job: dict):
    """带超时与异常保护的 cron 任务执行包装（后台任务异常不外抛）。

    超时预算：本地小模型（9B GGUF）执行带工具的金融任务，首轮思考即可
    达 3-6 分钟，600s 只够 1-2 轮迭代 → 任务必超时失败（09-01 10:40
    事故：首轮 LLM 6 分钟 + 迭代 4 超时）。给 1200s 总预算，配合
    _execute_cron_job 内迭代上限，够跑完整任务。

    2026-09-29 审计：超时/取消此前**只有 logger.warning**——job 的 last_status /
    历史 / 事件都不更新，用户设的"收盘叫我"超时时完全静默（前端那条摘要还停在上一轮）。
    现在三种中止都经 `_record_cron_result` 留痕（状态一律 error + 明确文案：前端只认
    success/error，其他值会显示成"已跳过"，比静默更误导）。
    """
    with _cron_lock:
        _running_jobs.add(job["id"])
    try:
        await asyncio.wait_for(_execute_cron_job(job), timeout=_CRON_JOB_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("[CRON] 任务超时（%.0fs）被中止: %s",
                       _CRON_JOB_TIMEOUT, str(job.get("task") or "")[:50])
        _record_cron_result(
            job, "error",
            f"[超时] 任务超过 {int(_CRON_JOB_TIMEOUT // 60)} 分钟被中止，本轮无结果"
            "（已记为已执行，不会自动重试）。建议改用云端模型或把任务拆小。")
    except asyncio.CancelledError:
        # 应用退出/重启：也要留痕，否则用户以为任务跑过
        logger.warning("[CRON] 任务被取消（应用退出/重启）: %s", str(job.get("task") or "")[:50])
        _record_cron_result(job, "error", "[已取消] 任务在执行中被中止（应用退出或重启）。")
        raise
    except Exception as e:
        _err = f"{type(e).__name__}: {e}".strip(": ")
        logger.warning("[CRON] 任务异常: %s（%s）", str(job.get("task") or "")[:50], _err)
        _record_cron_result(job, "error", f"[异常] {_err}")
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
