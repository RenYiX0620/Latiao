"""记忆系统审查修复（2026-09-23，第一梯队 ②③⑤⑨⑫）。

背景：一份外部审查指出 12 个问题，逐条核对后确认其中 9 条完全成立。这里守住
第一梯队那五条的修复，每条都钉上"改之前是什么样"的实测数字，防止回退。
"""
import importlib
import time

import pytest


@pytest.fixture()
def mem(tmp_path, monkeypatch):
    """隔离的记忆库（LATIAO_TEST_PROGRESS_DIR 决定 data dir → memory.db 位置）。"""
    monkeypatch.setenv("LATIAO_TEST_PROGRESS_DIR", str(tmp_path / ".local-ai-os"))
    import config
    importlib.reload(config)
    import db
    importlib.reload(db)
    import memory
    importlib.reload(memory)
    db._init_db()
    return memory


# ── ③ 中文去重（旧实现：真库 341 条判重 0 条）────────────────────────

def test_dedup_detects_chinese_near_duplicate(mem):
    mem._store_learning("s1", "查询持仓的方法", "查询证券持仓明细的方法：调用 mx_query 并解析表格", 0.6)
    # 换了个措辞的同一件事 → 新分词器下重叠 > 0.7 → 判重
    assert mem._is_duplicate_learning("查询证券持仓明细的方法：调用 mx_query 并解析表格。") is True
    # 无关内容不判重
    assert mem._is_duplicate_learning("今天天气不错，适合出门散步和拍照") is False


def test_dedup_still_works_for_english(mem):
    mem._store_learning("s1", "python venv", "create a venv with python3 -m venv .venv", 0.6)
    assert mem._is_duplicate_learning("create a venv with python3 -m venv .venv") is True
    assert mem._is_duplicate_learning("unrelated content about cooking pasta") is False


def test_dedup_old_whitespace_split_was_broken(mem):
    """钉住"旧实现为什么失效"：按空格分词时中文整句一个 token，重叠非 0 即 1。"""
    a = "查询证券持仓明细的方法"
    b = "查询证券持仓明细的另一种方法"
    old_a, old_b = set(a.lower().split()), set(b.lower().split())
    old_overlap = len(old_a & old_b) / len(old_a | old_b)
    new_a, new_b = set(mem._tokenize_zh(a)), set(mem._tokenize_zh(b))
    new_overlap = len(new_a & new_b) / len(new_a | new_b)
    assert old_overlap == 0.0, "旧实现重叠率为 0（完全失效）"
    assert new_overlap > 0.7, f"新实现应判为重（实测 {new_overlap:.2f}）"


# ── ② 知识注入只保留薄循环一处 ──────────────────────────────────────

def test_prompt_build_no_longer_injects_learnings(mem):
    """_build_chat_messages 不再检索/注入 learnings（避免与薄循环双份）。"""
    import agent.prompt_build as PB
    assert not hasattr(PB, "_retrieve_relevant_learnings"), (
        "prompt_build 不应再持有这个符号（注入已搬走；测试也不该把它当补丁点）")
    body = {"messages": [{"role": "user", "content": "帮我看看证券持仓明细怎么查"}]}
    out = PB._build_chat_messages(body, list(body["messages"]))
    joined = "\n".join(str(m.get("content") or "") for m in out)
    assert "以下是 AI 从过去交互学到的相关知识" not in joined


def test_thin_loop_still_injects_learnings():
    """薄循环那处保留（它是现在唯一的注入点）。"""
    import agent.loop as L
    src = open(L.__file__, encoding="utf-8").read()
    assert "_retrieve_relevant_learnings" in src
    assert "【参考知识】" in src


# ── ⑨ TF-IDF：单遍 df + 追加式增量 ──────────────────────────────────

def test_tfidf_incremental_matches_full_rebuild(mem):
    for i in range(5):
        mem._store_learning("s1", f"主题{i}", f"内容 关于持仓查询的方法 {i}", 0.6)
    mem._TFIDF_CACHE_DIRTY = True
    mem._TFIDF_DOCS = None
    di, vecs, idf = mem._build_tfidf_index()
    assert len(di) == 5
    docs_before = mem._TFIDF_DOCS
    mem._store_learning("s1", "主题x", "新增：查询证券持仓明细的方法", 0.6)
    di_inc, vecs_inc, idf_inc = mem._build_tfidf_index()      # 走增量
    # 直接观测：增量是在**同一个** docs 列表上追加；全量会新建列表。
    assert mem._TFIDF_DOCS is docs_before, "没走增量（重新整表分词了）"
    mem._TFIDF_DOCS = None
    mem._TFIDF_CACHE_DIRTY = True
    di_full, vecs_full, idf_full = mem._build_tfidf_index()   # 强制全量
    assert [d["id"] for d in di_inc] == [d["id"] for d in di_full]
    assert len(idf_inc) == len(idf_full)
    assert all(abs(idf_inc[k] - idf_full[k]) < 1e-12 for k in idf_full)
    # 增量索引能检索到新条目（说明它真的进了向量表）
    assert "主题x" in [r["topic"] for r in mem._tfidf_search("查询证券持仓明细", limit=3)]


def test_tfidf_cache_invalidated_on_delete(mem):
    """删行必须让索引失效——此前 api_routes 的 DELETE 不置脏，删掉的仍在结果里。"""
    mem._store_learning("s1", "要删掉的", "这段内容会被删除，关键词：独特标记词", 0.9)
    mem._store_learning("s1", "保留的", "另一段内容，关键词：独特标记词", 0.9)
    assert mem._tfidf_search("独特标记词", limit=5)
    conn = mem._get_db()
    with mem._db_write_lock:
        conn.execute("DELETE FROM learnings WHERE topic = ?", ("要删掉的",))
        conn.commit()
    mem._mark_tfidf_dirty()
    topics = [r["topic"] for r in mem._tfidf_search("独特标记词", limit=5)]
    assert "要删掉的" not in topics and "保留的" in topics


def test_tfidf_cache_self_checks_row_count(mem):
    """即使外部改动没置脏，命中路径的 COUNT 自校验也能发现（本机实测踩到过）。"""
    mem._store_learning("s1", "a", "内容内容内容内容", 0.6)
    mem._build_tfidf_index()
    conn = mem._get_db()
    with mem._db_write_lock:
        conn.execute("DELETE FROM learnings")
        conn.commit()
    # 不置脏、直接检索：自校验发现行数不符 → 当脏处理 → 结果为空
    assert mem._tfidf_search("内容内容", limit=5) == []


def test_tfidf_rebuild_cost_is_small(mem):
    """重建耗时守卫：旧实现算 IDF 是 O(词表 × 文档数)（真库 341 条 32.4ms）。"""
    for i in range(200):
        mem._store_learning("s1", f"t{i}", f"第 {i} 条内容 关于资金流向与板块分析的方法论", 0.6)
    mem._TFIDF_CACHE_DIRTY = True
    mem._TFIDF_DOCS = None
    t0 = time.perf_counter()
    mem._build_tfidf_index()
    cost_ms = (time.perf_counter() - t0) * 1000
    assert cost_ms < 120, f"200 条重建耗时 {cost_ms:.1f}ms（旧实现同规模约 20ms+，慢在此处可见）"


# ── ⑤ preferences_fts 死索引 ────────────────────────────────────────

def test_preferences_fts_not_created_and_legacy_dropped(mem):
    conn = mem._get_db()
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'preferences_fts%'")}
    assert names == set(), f"preferences_fts 不应再存在（死索引）：{names}"


# ── ⑫ PROGRESS 注入节奏 ─────────────────────────────────────────────

def test_progress_injected_first_turn_and_every_n(mem, monkeypatch):
    import agent.prompt_build as PB
    # 用 monkeypatch（自动还原）而不是手工赋值 + importlib.reload：reload 会换掉模块
    # 身份、造出第二份 _build_chat_messages，直接触发 test_module_boundaries 的
    # re-export 守卫（首版就这么挂的——守卫是对的，写法错了）。
    monkeypatch.setattr(PB, "_progress_tail", lambda *a, **k: "- 上次在查半导体板块的资金流向")
    n = PB._PROGRESS_INJECT_EVERY_TURNS
    assert n >= 2

    def injected(user_turns: int) -> bool:
        messages = []
        for i in range(user_turns):
            messages.append({"role": "user", "content": f"第{i}轮的问题内容足够长"})
            messages.append({"role": "assistant", "content": "回答"})
        body = {"messages": messages}
        out = PB._build_chat_messages(body, list(messages))
        return "上次会话进展" in "\n".join(str(m.get("content") or "") for m in out)

    assert injected(1) is True, "首轮应注入"
    assert injected(2) is False, "第 2 轮不该每轮都注入"
    assert injected(n) is False
    assert injected(n + 1) is True, f"第 {n + 1} 轮应再次注入"
