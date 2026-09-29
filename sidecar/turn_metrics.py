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
                    "SELECT session_id, model, is_local, started_at, duration_ms, input_tokens, "
                    "gen_tokens, retries, steps, ttft_ms, refine_calls, refine_tokens, budget, "
                    "by_source, ended_reason FROM turn_metrics WHERE session_id = ? "
                    "ORDER BY started_at DESC LIMIT ?", (session_id, int(limit))).fetchall()
            else:
                rows = conn.execute(
                    "SELECT session_id, model, is_local, started_at, duration_ms, input_tokens, "
                    "gen_tokens, retries, steps, ttft_ms, refine_calls, refine_tokens, budget, "
                    "by_source, ended_reason FROM turn_metrics "
                    "ORDER BY started_at DESC LIMIT ?", (int(limit),)).fetchall()
    except sqlite3.Error:
        logger.warning("turn_metrics 读取失败", exc_info=True)
        return []
    keys = ("session_id", "model", "is_local", "started_at", "duration_ms", "input_tokens",
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
