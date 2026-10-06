"""每轮运行指标落库（2026-09-29，ZCode 对照后的增补）。

**为什么加**：ZCode 把 `duration_ms` / `time_to_first_token_ms` / `retry_count` 落进
`model_usage` 表，历史可查（"上周哪次最慢/最贵"、按天聚合、热力图都从这张表来）；
我们的 TTFT/tps/token 只在 `context_stats` 的内存里，重启即失。差距不是"有没有
TTFT"，是"TTFT 有没有历史可查"。

**写什么**：一行一轮（不是一请求一行）——数值全部取自 `context_stats.turn_cost()`
的同一份计数器，**不新增埋点**；`by_source` 以 JSON 存（主循环 / 子代理 / 知识提炼，
口径会演化，学 `session_messages` 的做法别逐字段建列）。

**何时写**：轮末。正常路径是 SSE 消费层的 `finally`（`api_routes._logged_agent_turn`，
那里已经有结束原因 `completed`/`error`/`aborted`），通道路径在 `channels_bridge` 收尾。
子代理循环**不单独写行**——它的用量通过 `context_stats.credit_parent` 记进父会话的
`subagent` 桶，父会话这一行里就能看到。

**保留期**：默认 180 天（ZCode 用 30 天；我们一行一轮、体量比它小两个数量级，
没必要删那么勤），`LATIAO_TURN_METRICS_DAYS=0` 关闭清理。
"""
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta

from db import _db_write_lock, _get_db

logger = logging.getLogger("latiao-sidecar")

_RETENTION_DAYS_DEFAULT = 180


def retention_days() -> int:
    try:
        return max(0, int(os.environ.get("LATIAO_TURN_METRICS_DAYS",
                                         _RETENTION_DAYS_DEFAULT)))
    except (TypeError, ValueError):
        return _RETENTION_DAYS_DEFAULT


def record_turn(session_id: str, *, model: str = "", is_local: bool = False,
                ended_reason: str = "completed") -> bool:
    """把本轮的指标写进 turn_metrics（幂等：同一轮重复调用覆盖同一行）。

    返回是否写入成功——记账失败绝不能影响回合结束（与 ZCode 的
    `usage.*.write.failed` 只 warn 不抛同一取舍）。
    """
    sid = str(session_id or "").strip()
    if not sid:
        return False
    try:
        import context_stats
        t = context_stats.turn_cost(sid)
    except Exception:
        logger.debug("turn_metrics: 取本轮成本失败", exc_info=True)
        return False
    started_ms = int(t.get("started_ms") or 0)
    if not started_ms:
        return False        # 本轮没有 begin_turn（子代理/直连调用）→ 不记
    now_ms = int(datetime.now().timestamp() * 1000)
    _ttft = t.get("ttft_avg_ms")
    try:
        with _db_write_lock:
            conn = _get_db()
            conn.execute(
                "INSERT OR REPLACE INTO turn_metrics("
                "id, session_id, model, is_local, started_at, ended_at, duration_ms, "
                "input_tokens, gen_tokens, retries, steps, ttft_ms, refine_calls, "
                "refine_tokens, budget, by_source, ended_reason) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"{sid}:{started_ms}", sid, str(model or ""), 1 if is_local else 0,
                    datetime.fromtimestamp(started_ms / 1000).isoformat(),
                    datetime.fromtimestamp(now_ms / 1000).isoformat(),
                    max(0, now_ms - started_ms),
                    int(t.get("input_tokens") or 0), int(t.get("gen_tokens") or 0),
                    int(t.get("retries") or 0), int(t.get("steps") or 0),
                    int(_ttft) if isinstance(_ttft, (int, float)) else None,
                    int(t.get("refine_calls") or 0), int(t.get("refine_tokens") or 0),
                    int(t.get("budget") or 0),
                    json.dumps(t.get("by_source") or {}, ensure_ascii=False),
                    str(ended_reason or ""),
                ))
            conn.commit()
        return True
    except sqlite3.Error:
        logger.warning("turn_metrics 写入失败（不影响回合）", exc_info=True)
        return False


def list_turns(session_id: str = "", limit: int = 50) -> list[dict]:
    """最近的轮次指标（按开始时间倒序）；session_id 为空则跨会话。"""
    try:
        with _db_write_lock:
            conn = _get_db()
            if session_id:
                rows = conn.execute(
                    "SELECT id, session_id, model, is_local, started_at, duration_ms, input_tokens, "
                    "gen_tokens, retries, steps, ttft_ms, refine_calls, refine_tokens, budget, "
                    "by_source, ended_reason FROM turn_metrics WHERE session_id = ? "
                    "ORDER BY started_at DESC LIMIT ?", (session_id, int(limit))).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, session_id, model, is_local, started_at, duration_ms, input_tokens, "
                    "gen_tokens, retries, steps, ttft_ms, refine_calls, refine_tokens, budget, "
                    "by_source, ended_reason FROM turn_metrics "
                    "ORDER BY started_at DESC LIMIT ?", (int(limit),)).fetchall()
    except sqlite3.Error:
        logger.warning("turn_metrics 读取失败", exc_info=True)
        return []
    keys = ("id", "session_id", "model", "is_local", "started_at", "duration_ms", "input_tokens",
            "gen_tokens", "retries", "steps", "ttft_ms", "refine_calls", "refine_tokens",
            "budget", "by_source", "ended_reason")
    out = []
    for r in rows:
        d = dict(zip(keys, r))
        try:
            d["by_source"] = json.loads(d.get("by_source") or "{}")
        except (TypeError, ValueError):
            d["by_source"] = {}
        out.append(d)
    return out


def count_turns(session_id: str = "") -> int:
    """库内总轮数（跨会话或按会话）——给汇总界面的"共 N 轮"一个准确数，
    避免用 list_turns 的行数上限冒充总数。"""
    try:
        with _db_write_lock:
            conn = _get_db()
            if session_id:
                row = conn.execute("SELECT COUNT(*) FROM turn_metrics WHERE session_id = ?",
                                   (session_id,)).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) FROM turn_metrics").fetchone()
            return int(row[0] if row else 0)
    except sqlite3.Error:
        logger.warning("turn_metrics 计数失败", exc_info=True)
        return 0


def dashboard_stats(window_days: int = 182) -> dict:
    """整页"使用统计"仪表盘的聚合（2026-10-04，对齐 ZCode 的使用统计页）。

    - 重活在 SQL：全库合计一行、按天一查询、按天×模型一查询；行量由 180 天
      保留期兜底（窗口超过保留期没有额外意义）。
    - 连续天数从按天日期序列推（当前连续从今天起算，今天没用则从昨天——
      与 ZCode 的口径一致：半夜后看仍是连续的）。
    - 失败返回全零形状，不抛（仪表盘不能把页面打挂）。
    """
    from datetime import date as _date

    empty = {"all_time": {"n": 0, "input": 0, "gen": 0, "longest_turn_ms": 0},
             "per_day": [], "per_day_model": [], "peak_day": None,
             "streak_current": 0, "streak_longest": 0, "window_days": int(window_days)}
    try:
        window_days = max(7, min(365, int(window_days)))
        cutoff = (datetime.now() - timedelta(days=window_days)).isoformat()
        with _db_write_lock:
            conn = _get_db()
            all_row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(input_tokens),0), COALESCE(SUM(gen_tokens),0), "
                "COALESCE(MAX(duration_ms),0) FROM turn_metrics").fetchone()
            per_day = conn.execute(
                "SELECT substr(started_at,1,10) d, COUNT(*), COALESCE(SUM(input_tokens),0), "
                "COALESCE(SUM(gen_tokens),0) FROM turn_metrics WHERE started_at >= ? "
                "GROUP BY d ORDER BY d", (cutoff,)).fetchall()
            per_day_model = conn.execute(
                "SELECT substr(started_at,1,10) d, model, COALESCE(SUM(input_tokens+gen_tokens),0) "
                "FROM turn_metrics WHERE started_at >= ? GROUP BY d, model", (cutoff,)).fetchall()
    except sqlite3.Error:
        logger.warning("turn_metrics 仪表盘聚合失败", exc_info=True)
        return empty
    days = [{"date": r[0], "n": int(r[1]), "input": int(r[2]), "gen": int(r[3])}
            for r in per_day]
    # 模型名归一（2026-10-06）：早期记录存完整文件路径、后期存短名——同一模型
    # 被拆成两条趋势线（实测 occamy/Qwen-MLX 各两色，图例同名重复）。
    # 归一 = 取路径末段 + 去 .gguf 后缀，再按（日期, 归一名）重新聚合。
    def _norm_model_name(m) -> str:
        s = str(m or "").replace("\\", "/").rstrip("/")
        base = s.rsplit("/", 1)[-1]
        if base.lower().endswith(".gguf"):
            base = base[:-5]
        return base or "未知"

    _merged: dict[tuple, int] = {}
    for r in per_day_model:
        key = (r[0], _norm_model_name(r[1]))
        _merged[key] = _merged.get(key, 0) + int(r[2])
    by_day_model = [{"date": d, "model": m, "tokens": t}
                    for (d, m), t in sorted(_merged.items())]

    # 连续天数：最长 = 日期序列里最长的逐日连续段
    dset: set[str] = set()
    longest = cur = 0
    prev: _date | None = None
    for d in sorted(d for x in days if (d := x["date"])):
        try:
            cur_date = _date.fromisoformat(d)
        except ValueError:
            continue
        cur = cur + 1 if (prev is not None and (cur_date - prev).days == 1) else 1
        longest = max(longest, cur)
        prev = cur_date
        dset.add(d)
    # 当前连续：今天起算；今天还没用则从昨天起算（半夜间口径）
    cur_streak = 0
    probe = _date.fromisoformat(datetime.now().date().isoformat())
    if probe.isoformat() not in dset:
        probe -= timedelta(days=1)
    while probe.isoformat() in dset:
        cur_streak += 1
        probe -= timedelta(days=1)

    peak = max(days, key=lambda x: x["input"] + x["gen"]) if days else None
    return {
        "all_time": {"n": int(all_row[0]), "input": int(all_row[1]),
                     "gen": int(all_row[2]), "longest_turn_ms": int(all_row[3])},
        "per_day": days,
        "per_day_model": by_day_model,
        "peak_day": peak,
        "streak_current": cur_streak,
        "streak_longest": max(longest, cur_streak),
        "window_days": window_days,
    }


def prune(days: int | None = None) -> int:
    """删除超过保留期的行，返回删除条数（0 = 关闭或没有可删）。"""
    d = retention_days() if days is None else max(0, int(days))
    if not d:
        return 0
    cutoff = (datetime.now() - timedelta(days=d)).isoformat()
    try:
        with _db_write_lock:
            conn = _get_db()
            cur = conn.execute("DELETE FROM turn_metrics WHERE started_at < ?", (cutoff,))
            conn.commit()
            return int(cur.rowcount or 0)
    except sqlite3.Error:
        logger.warning("turn_metrics 清理失败", exc_info=True)
        return 0


def clear() -> int:
    """清空全部用量统计（用户在统计页点"清空"），返回删除条数。

    只动 turn_metrics——会话历史（session_messages）、记忆、设置都不碰；
    下一轮对话结束后会照常重新开始记录。失败返回 -1（调用方转错误提示）。
    """
    try:
        with _db_write_lock:
            conn = _get_db()
            cur = conn.execute("DELETE FROM turn_metrics")
            conn.commit()
            return int(cur.rowcount or 0)
    except sqlite3.Error:
        logger.warning("turn_metrics 清空失败", exc_info=True)
        return -1
