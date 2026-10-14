"""注入自动结算（2026-10-04）：不再依赖用户点赞——下次注入时用上一条注入之后的
assistant 回答做 token 重叠检测，自动标 used。

背景：memory_injections 建了 374 次注入仅 1 次反馈（机制空转）。自动结算是弱信号：
重叠只说明"回答里出现了注入知识的词"，不证明因果；used=0 也只表示"这一轮没用上"。
不据此自动降权——只作为长期门槛调优的统计样本。
"""
import json
from datetime import datetime, timedelta


def _seed(db_module, session_id, injected_items, answer_text=None,
          inj_ago_sec=30, ans_ago_sec=10):
    conn = db_module._get_db()
    t0 = (datetime.now() - timedelta(seconds=inj_ago_sec)).isoformat()
    conn.execute(
        "INSERT INTO memory_injections(session_id, query, injected, created_at) VALUES(?,?,?,?)",
        (session_id, "测试查询", json.dumps(injected_items, ensure_ascii=False), t0))
    if answer_text is not None:
        t1 = (datetime.now() - timedelta(seconds=ans_ago_sec)).isoformat()
        conn.execute(
            "INSERT INTO session_messages(id, session_id, seq, role, data, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (f"m-{session_id}", session_id, 1, "assistant",
             json.dumps({"role": "assistant", "content": answer_text},
                        ensure_ascii=False), t1))
    conn.commit()


def _used(db_module, session_id):
    return db_module._get_db().execute(
        "SELECT used FROM memory_injections WHERE session_id=? ORDER BY created_at",
        (session_id,)).fetchall()


def test_settle_marks_used_when_answer_overlaps():
    import db as db_module
    import memory
    sid = "sess-settle-used"
    _seed(db_module, sid,
          [{"id": "l1", "topic": "主力净额等于主力买入减主力卖出，正值表示净流入"}],
          answer_text="主力净额 = 主力买入 - 主力卖出，今天净流入 3.2 亿，正值说明买盘占优。")
    memory._settle_prev_injection(sid)
    assert _used(db_module, sid)[0][0] == 1


def test_settle_marks_unused_when_answer_unrelated():
    import db as db_module
    import memory
    sid = "sess-settle-unused"
    _seed(db_module, sid,
          [{"id": "l2", "topic": "主力净额等于主力买入减主力卖出，正值表示净流入"}],
          answer_text="今天天气不错，适合出门散步，顺便买杯咖啡。")
    memory._settle_prev_injection(sid)
    assert _used(db_module, sid)[0][0] == 0


def test_settle_waits_when_no_answer_yet():
    """回答还没产生：不误标，保持 NULL 留待下次。"""
    import db as db_module
    import memory
    sid = "sess-settle-wait"
    _seed(db_module, sid, [{"id": "l3", "topic": "某条知识的主题"}], answer_text=None)
    memory._settle_prev_injection(sid)
    assert _used(db_module, sid)[0][0] is None


def test_settle_ignores_answer_before_injection():
    """注入之前的旧回答不能拿来结算（顺序敏感）。"""
    import db as db_module
    import memory
    sid = "sess-settle-order"
    _seed(db_module, sid,
          [{"id": "l4", "topic": "主力净额与净流入"}],
          answer_text="主力净额 与 净流入 都提到了。",
          inj_ago_sec=10, ans_ago_sec=60)          # 回答比注入早 50 秒
    memory._settle_prev_injection(sid)
    assert _used(db_module, sid)[0][0] is None


def test_log_injection_settles_previous_end_to_end():
    """端到端：_log_injection 写入新注入前，先结算上一条。"""
    import db as db_module
    import memory
    sid = "sess-settle-e2e"
    _seed(db_module, sid,
          [{"id": "l5", "topic": "板块资金看主力净额与主力强度两个维度"}],
          answer_text="看板块资金主要看主力净额和主力强度两个维度。")
    memory._log_injection(sid, "新查询", [{"id": "l6", "topic": "新知识"}])
    rows = _used(db_module, sid)
    assert rows[0][0] == 1        # 上一条已结算
    assert rows[1][0] is None     # 新一条未结算
