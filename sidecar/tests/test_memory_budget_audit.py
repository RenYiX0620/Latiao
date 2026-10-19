"""记忆预算 / 审计 / 写入审批（2026-10-19，外部审计报告的三项收口）。

- 预算：learnings 行数+字符、preferences 行数+字符；超限按价值批量淘汰（一条事务），
  未超限零写入。此前 preferences **没有任何回收路径**。
- 审计：add/update/delete/prune/approve/reject 都留脱敏摘要（删除也能追溯）。
- 审批：memory.review_writes 打开时新记忆进 memory_pending，批准后才落库。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db as db_mod  # noqa: E402
import memory  # noqa: E402


@pytest.fixture(autouse=True, scope="module")
def _init():
    db_mod._init_db()


def _wipe():
    conn = db_mod._get_db()
    with db_mod._db_write_lock:
        for t in ("learnings", "preferences", "memory_audit", "memory_pending"):
            conn.execute(f"DELETE FROM {t}")
        conn.commit()


# ── 预算 ────────────────────────────────────────────────────────
def test_learning_char_budget_evicts_low_value(monkeypatch):
    _wipe()
    monkeypatch.setenv("LATIAO_LEARNINGS_MAX", "100")            # 行数不设限
    monkeypatch.setenv("LATIAO_LEARNINGS_MAX_CHARS", "100")
    memory._store_learning("s1", "低价话题", "x" * 80, confidence=0.2, source_type="t")
    memory._store_learning("s1", "高价话题", "y" * 80, confidence=0.95, source_type="t")
    conn = db_mod._get_db()
    topics = {r[0] for r in conn.execute("SELECT topic FROM learnings")}
    assert "高价话题" in topics, "高价值条目被误删"
    assert "低价话题" not in topics, "字符预算没生效（写后就该触发）"


def test_preferences_budget_prunes_low_value(monkeypatch):
    _wipe()
    monkeypatch.setenv("LATIAO_PREFERENCES_MAX", "2")
    monkeypatch.setenv("LATIAO_PREFERENCES_MAX_CHARS", "100000")
    memory._store_preference("k-low", "v1", confidence=0.2)
    memory._store_preference("k-hi", "v2", confidence=0.95)
    memory._store_preference("k-mid", "v3", confidence=0.6)
    conn = db_mod._get_db()
    keys = {r[0] for r in conn.execute("SELECT key FROM preferences")}
    assert len(keys) == 2, f"偏好行预算没生效: {keys}"
    assert "k-low" not in keys and "k-hi" in keys


# ── 审计 ────────────────────────────────────────────────────────
def test_audit_records_add_and_update():
    _wipe()
    memory._store_learning("s1", "审计话题", "内容 A", confidence=0.6)
    memory._store_learning("s1", "审计话题", "内容 B", confidence=0.6)   # 同主题 → update
    actions = [r["action"] for r in memory.list_audit(20)]
    assert "add" in actions and "update" in actions


def test_audit_records_delete_via_helper():
    """删除留痕：/v1/memory/forget 走的就是 db.audit_memory 这一条。"""
    _wipe()
    db_mod.audit_memory("learning", "delete", row_id="lid-1", topic="被删话题",
                        preview="被删内容", source="/v1/memory/forget")
    items = memory.list_audit(10)
    assert items and items[0]["action"] == "delete"
    assert items[0]["topic"] == "被删话题"


def test_secret_is_redacted_before_storage_and_audit():
    _wipe()
    raw = "sk-ABCDEFGHIJKLMNOP1234567890"
    memory._store_learning("s1", "密钥泄露测试", f"接口密钥是 {raw}", confidence=0.9)
    conn = db_mod._get_db()
    content = conn.execute("SELECT content FROM learnings").fetchone()[0]
    previews = " ".join(r["preview"] for r in memory.list_audit(10))
    assert raw not in content, "明文密钥进了记忆库"
    assert raw not in previews, "明文密钥进了审计流水"


# ── 写入审批（默认关，打开后先入队）──────────────────────────────
def test_review_mode_queues_then_approve(monkeypatch):
    _wipe()
    monkeypatch.setenv("LATIAO_MEMORY_REVIEW", "1")
    memory._store_learning("s1", "待审话题", "待审内容", confidence=0.8)
    conn = db_mod._get_db()
    assert conn.execute("SELECT COUNT(*) FROM learnings").fetchone()[0] == 0
    pend = memory.list_pending(10)
    assert len(pend) == 1 and pend[0]["topic"] == "待审话题"
    assert memory.approve_pending(pend[0]["id"])["status"] == "ok"
    assert conn.execute("SELECT COUNT(*) FROM learnings").fetchone()[0] == 1
    assert memory.list_pending(10) == []
    assert any(r["action"] == "approve" for r in memory.list_audit(20))


def test_review_mode_reject_discards(monkeypatch):
    _wipe()
    monkeypatch.setenv("LATIAO_MEMORY_REVIEW", "1")
    memory._store_preference("待审键", "待审值", confidence=0.7)
    pend = memory.list_pending(10)
    assert len(pend) == 1
    assert memory.reject_pending(pend[0]["id"])["status"] == "ok"
    conn = db_mod._get_db()
    assert conn.execute("SELECT COUNT(*) FROM preferences").fetchone()[0] == 0
    assert any(r["action"] == "reject" for r in memory.list_audit(20))


def test_updates_are_not_gated_by_review(monkeypatch):
    """审批只管**新条目**：已有条目靠重复出现累加置信度，不能被卡住。

    注意更新语义是"只加置信度、保留原内容"（历史行为），不是覆盖 content。
    """
    _wipe()
    memory._store_learning("s1", "已有话题", "v1", confidence=0.6)
    monkeypatch.setenv("LATIAO_MEMORY_REVIEW", "1")
    memory._store_learning("s1", "已有话题", "v2", confidence=0.6)
    conn = db_mod._get_db()
    rows = conn.execute("SELECT content, confidence FROM learnings").fetchall()
    assert len(rows) == 1 and rows[0][0] == "v1"
    assert rows[0][1] > 0.7, "重复出现应累加置信度（没被审批卡住）"
    assert memory.list_pending(10) == []
