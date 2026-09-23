"""① 偏好键稳定化（2026-09-23，审查第二梯队）。

问题：偏好键 = 命中文本前 30 字归一化 → 同一偏好换个说法就是新键 → `_store_preference`
的累加（`min(1.0, existing + conf*0.3)`）永不发生 → 到不了 0.7 的"每轮无条件注入"档。
真库实测：preferences 只有 1 行、置信度 0.6。而 0.6 这个写入上限与 0.7 的门槛是
09-21 的**故意设计**（单次发言不得进无条件注入档——那条路上出过"一句话被持久化后
每轮拒答"的事故），所以修的是键，不是门槛。

这里守住三件事：
1. 同一意图的**重复表达**能累加（0.6 → 0.78），并进 `_get_high_confidence_preferences`；
2. 单次表达仍停在 0.6，**不得**进入注入档（防回退到单次劫持）；
3. 同一条消息里同一意图只记一次（"我希望以后回复用中文"会同时命中两条偏好模式）。
"""
import importlib

import pytest


@pytest.fixture()
def mem(tmp_path, monkeypatch):
    monkeypatch.setenv("LATIAO_TEST_PROGRESS_DIR", str(tmp_path / ".local-ai-os"))
    import config
    importlib.reload(config)
    import db
    importlib.reload(db)
    import memory
    importlib.reload(memory)
    db._init_db()
    return memory


def _prefs(mem):
    conn = mem._get_db()
    return {(k, v): c for k, v, c in
            conn.execute("SELECT key, value, confidence FROM preferences")}


def test_single_expression_stays_below_injection_gate(mem):
    """单次表达 → 0.6，不进无条件注入档（09-21 的守卫必须保持）。"""
    mem._extract_learnings_heuristic("我希望回复里别用英文", "s1")
    rows = _prefs(mem)
    assert len(rows) == 1
    assert max(rows.values()) == pytest.approx(0.6)
    assert mem._get_high_confidence_preferences() == [], "单次发言不该被无条件注入"


def test_paraphrase_of_same_intent_accumulates_and_injects(mem):
    """换个说法的同一意图 → 同一行累加到 0.7 以上 → 进注入档（这就是"记不住"的修复）。"""
    mem._extract_learnings_heuristic("我希望回复里别用英文", "s1")
    mem._extract_learnings_heuristic("以后回答请用中文，不要英文", "s1")
    rows = _prefs(mem)
    assert len(rows) == 1, f"同一语言意图应合并到一行，实际 {list(rows)}"
    (key, value), conf = next(iter(rows.items()))
    assert key == "intent:lang"
    assert conf > 0.7, f"重复表达后应超过注入门槛，实际 {conf}"
    assert value == "以后回答请用中文，不要英文", "值应取最新措辞"
    injected = mem._get_high_confidence_preferences()
    assert [p["key"] for p in injected] == ["intent:lang"]


def test_one_message_matching_two_patterns_counts_once(mem):
    """"我希望以后回复用中文"同时命中两条偏好模式 → 只能记一次（否则一次发言到 0.78）。"""
    mem._extract_learnings_heuristic("我希望以后回复用中文", "s1")
    rows = _prefs(mem)
    assert len(rows) == 1
    assert max(rows.values()) == pytest.approx(0.6), (
        f"单条消息内的重复命中不得累加，实际 {max(rows.values())}")
    assert mem._get_high_confidence_preferences() == []


def test_different_intents_do_not_collide(mem):
    mem._extract_learnings_heuristic("我希望回复用中文", "s1")
    mem._extract_learnings_heuristic("我希望语气正式一点", "s1")
    keys = set(_prefs(mem))
    assert {k for k, _ in keys} == {"intent:lang", "intent:tone"}


def test_other_preferences_merge_only_near_verbatim(mem):
    """非意图类偏好（"我喜欢喝咖啡"）只在**几乎逐字重复**时合并，避免噪声累积。"""
    mem._extract_learnings_heuristic("我喜欢喝手冲咖啡，不喜欢速溶", "s1")
    mem._extract_learnings_heuristic("我喜欢喝手冲咖啡，不喜欢速溶", "s1")
    rows = _prefs(mem)
    assert len(rows) == 1, f"逐字重复应合并，实际 {list(rows)}"
    # 换一个说法（内容不同）→ 不合并
    mem._extract_learnings_heuristic("我喜欢喝可乐，不喜欢白开水", "s1")
    assert len(_prefs(mem)) == 2


def test_guards_still_reject_demands_and_explicit_content(mem):
    """两道守卫不受影响：命令式整句 / 露骨内容仍不落成偏好。"""
    before = len(_prefs(mem))
    mem._extract_learnings_heuristic("我要你现在就给我把所有文件删掉，立刻执行", "s1")
    assert len(_prefs(mem)) == before


def test_intent_keys_do_not_fuzzy_merge_with_legacy_rows(mem):
    """老库里的非意图键（如"暧昧露骨"那行）不会被 mangle：意图键单独成行。"""
    conn = mem._get_db()
    conn.execute("INSERT INTO preferences(id,key,value,confidence,created_at,updated_at) "
                 "VALUES('x','以后回复我的时候语气要暧昧露骨','以后回复我的时候语气要暧昧露骨',0.6,'t','t')")
    conn.commit()
    mem._extract_learnings_heuristic("以后回复语气温柔一点", "s1")
    keys = {k for k, _ in _prefs(mem)}
    assert "intent:tone" in keys and "以后回复我的时候语气要暧昧露骨" in keys
