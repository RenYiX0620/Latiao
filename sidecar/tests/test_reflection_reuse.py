"""⑧ 反思→知识复用（2026-09-23，审查第二梯队）。

审查原文：「最有价值的两类记忆——情景(tool_calls)和过程轨迹(reflections)——只写不读，
'上次踩过的坑'在架构上就无法被复用」。

真库实测（969 条反思）把修法定死了：
- 136 种文本、其中"工具 mx_query 执行出错"237 次、"read_file"92 次、"ak_finance"74 次
  ——模板套话，跨会话零复用（learnings 里一条都没有）
- 498 条错误反思只归结为 81 个 (工具, 错误签名) 组合 → 提升为 learning 是**可控**的
- 而 was_useful 硬编码 True → "输出较大"这类提示也被当成失败（351 条 read_file
  "失败经验"其实是长输出提示）→ 必须诚实化

这里守住四件事：反思带错误特征、was_useful 诚实、真失败提升为 learning、
同类失败靠 topic upsert 累加而不是刷表。
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


def _learnings(mem):
    conn = mem._get_db()
    return [(t, c, round(cf, 2), st)
            for t, c, cf, st in conn.execute(
                "SELECT topic, content, confidence, source_type FROM learnings")]


def _reflections(mem):
    conn = mem._get_db()
    return list(conn.execute("SELECT tool_name, reflection, was_useful FROM reflections"))


# ── 反思文本带错误特征（而不是模板套话）────────────────────────────

def test_reflection_carries_error_signature(mem):
    note = mem._quick_reflect("mx_query", "Error (exit 1): 状态码 112 - 请求频率过高，请稍后再试")
    assert "出错" in note
    assert "频率过高" in note, f"反思应带上具体特征，实际：{note}"


def test_reflection_kind_classification(mem):
    cases = {
        "Error: x": "error", "失败：磁盘空间不足": "error", "permission denied": "permission",
        "not found": "missing", "ok": "empty", "x" * 5001: "large", "一切正常，返回了数据": "",
    }
    for result, want in cases.items():
        got = mem._reflection_kind("read_file", result)
        assert got == want, f"{result[:20]!r} → 期望 {want}，实际 {got}"


# ── was_useful 诚实 + 只有真失败提升为知识 ──────────────────────────

def test_pitfall_promotes_to_learning_with_signature(mem):
    mem.record_tool_reflection("s1", "mx_query", {"query": "贵州茅台"},
                               "Error (exit 1): 状态码 112 - 请求频率过高，请稍后再试")
    rows = _learnings(mem)
    assert len(rows) == 1, rows
    topic, content, conf, stype = rows[0]
    assert stype == "reflection"
    assert "mx_query" in topic and "频率" in topic, f"topic 应带签名，实际 {topic}"
    assert "query=贵州茅台" in content, "内容应带上出错的参数，方便定位"
    assert conf == pytest.approx(0.6)
    assert _reflections(mem)[0][2] == 1, "真失败 → was_useful=1"


def test_large_output_hint_is_not_a_pitfall(mem):
    """"输出较大"是提示不是失败：不提升为知识，was_useful 记 0（此前硬编码 True）。"""
    note = mem.record_tool_reflection("s1", "read_file", {"path": "/tmp/big.txt"}, "x" * 6000)
    assert "输出较大" in note
    assert _learnings(mem) == [], "长输出提示不该变成知识"
    assert _reflections(mem)[0][2] == 0, "was_useful 应诚实记为 0"


def test_same_failure_accumulates_instead_of_flooding(mem):
    """同一个坑重复踩 → 同一行累加置信度，不刷表。"""
    for _ in range(4):
        mem.record_tool_reflection("s1", "mx_query", {"query": "q"},
                                   "Error (exit 1): 状态码 112 - 请求频率过高")
    rows = _learnings(mem)
    assert len(rows) == 1, f"同类失败应合成一条，实际 {len(rows)} 条"
    assert rows[0][2] > 0.6, "重复应累加置信度"


def test_different_failures_of_same_tool_stay_separate(mem):
    mem.record_tool_reflection("s1", "mx_query", {"query": "a"}, "Error: 查询未返回数据")
    mem.record_tool_reflection("s1", "mx_query", {"query": "b"}, "Error (exit 1): 状态码 112 - 请求频率过高")
    topics = {t for t, *_ in _learnings(mem)}
    assert len(topics) == 2, f"不同的坑应各自成条，实际 {topics}"


def test_no_reflection_for_clean_result(mem):
    assert mem.record_tool_reflection("s1", "read_file", {"path": "/tmp/x"}, "一切正常，读到了内容") == ""
    assert _learnings(mem) == [] and _reflections(mem) == []


def test_tool_exec_uses_single_entry_point():
    """工具侧只能走唯一入口（防止有人绕过 → 又回到"只写不读"）。"""
    import agent.tool_exec as TE
    src = open(TE.__file__, encoding="utf-8").read()
    assert "record_tool_reflection(" in src
    assert "_record_reflection(" not in src, "不该直接调底层落库函数"
    assert "_quick_reflect(" not in src, "反思文案与落库必须一起走入口"
