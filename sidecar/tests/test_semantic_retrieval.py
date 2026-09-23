"""⑥ 语义检索（2026-09-23，审查第三梯队）。

审查原文：「检索是字符级词频匹配，没有语义……词面不重叠就检索不到；团队为此叠了
三层垃圾正则过滤 + 阈值调优，全是在治'错误记忆被注入'的症状」。

真机量表的结论（scripts/eval_semantic_recall.py，真库 415 条 / 9 个查询）：
    TF-IDF 命中@5 = 3/9 → Qwen3-Embedding-0.6B 混合后 6/9
    bge-small-zh 虽小但分离度失败（无关查询 0.660 > 真命中最低 0.468）→ 弃用

这里用**假嵌入器**测机制（不依赖真模型、可确定性断言）：
- 语义召回的条目要能进注入（前提是过 0.50 标定门槛）
- 没过的、且词频也没给的 —— 绝不能进（这条就是"别重演闲聊也塞记忆"）
- 嵌入服务不可用 → 完全回退词频，行为与今天一致（fail-open）
"""
import importlib
import math

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("LATIAO_TEST_PROGRESS_DIR", str(tmp_path / ".local-ai-os"))
    import config
    importlib.reload(config)
    import db
    importlib.reload(db)
    import semantic
    importlib.reload(semantic)
    import embedding_service as emb
    importlib.reload(emb)
    import memory
    importlib.reload(memory)
    db._init_db()
    return db, memory, semantic, emb


# 受控向量表：显式给出"这些文本彼此相似/不相似"，把余弦攥在手里。
# 为什么不用字符桶那种"自然"映射：中文共享单字（的/一/事…）会让无关文本的余弦
# 也超过门槛，测出来的是玩具映射的怪癖，不是被测机制（首版就这么误报过一次）。
_VEC_TABLE: dict[str, list[float]] = {
    "查询证券持仓明细的方法": [1.0, 0.02, 0.0, 0.0],
    "持仓怎么查": [0.99, 0.05, 0.0, 0.0],          # 与上面近义（余弦 ≈ 0.999）
    "完全不相干的另一件事": [0.0, 1.0, 0.0, 0.0],   # 与上面正交（余弦 0）
    "长电科技实时价": [0.0, 0.0, 1.0, 0.0],
    "长电科技(600584)实时价71.66": [0.0, 0.02, 0.99, 0.0],
    "新内容": [0.0, 0.0, 0.0, 1.0],
    "内容": [0.0, 0.0, 0.0, 1.0],
}


def _fake_vec(text: str, dim: int = 4) -> list[float]:
    """查表给向量；表里没有的走字符桶兜底（仍确定性）。"""
    key = text.strip()
    for k, v in _VEC_TABLE.items():
        if k in key:
            return v
    v = [0.0] * dim
    for ch in key:
        v[ord(ch) % dim] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _install_fake_embedder(monkeypatch, emb, semantic, *, available=True):
    calls: list[list[str]] = []

    def fake_embed(texts, timeout=60, allow_cold_start=False):
        calls.append(list(texts))
        return [_fake_vec(t) for t in texts]

    monkeypatch.setattr(emb, "embed", fake_embed, raising=False)
    monkeypatch.setattr(emb, "available", lambda: available, raising=False)
    monkeypatch.setattr(semantic.emb, "embed", fake_embed, raising=False)
    monkeypatch.setattr(semantic.emb, "available", lambda: available, raising=False)
    return calls


def _add(conn, topic, content, conf=0.6):
    conn.execute("INSERT INTO learnings(id, session_id, topic, content, confidence, source_type, "
                 "hit_count, created_at, updated_at) VALUES(?,?,?,?,?,?,0,?,?)",
                 (topic, "s", topic, content, conf, "extracted", "2026-09-23", "2026-09-23"))
    conn.commit()
    return topic


# ── 门槛与召回 ──────────────────────────────────────────────────────

def test_semantic_hit_enters_but_only_above_threshold(env, monkeypatch):
    db, memory, semantic, emb = env
    _install_fake_embedder(monkeypatch, emb, semantic)
    conn = db._get_db()
    same = _add(conn, "近义知识", "查询证券持仓明细的方法")
    far = _add(conn, "无关知识", "完全不相干的另一件事")
    semantic.ensure_vectors(allow_cold_start=True)

    # 用户换个说法问同一件事（词面几乎不重叠——这正是审查⑥举的失败模式）
    scores = semantic.semantic_scores("持仓怎么查")
    assert scores[same] >= semantic.MIN_COS, f"同义条目应过门槛，实际 {scores[same]:.3f}"
    assert scores[far] < semantic.MIN_COS, f"无关条目不该过门槛，实际 {scores[far]:.3f}"

    hits = semantic.hybrid_search("持仓怎么查", [], limit=5)
    ids = [h["id"] for h in hits]
    assert same in ids
    assert far not in ids, "没过门槛、词频也没给的条目不该被注入"
    assert hits[0]["_semantic"] >= semantic.MIN_COS


def test_fail_open_when_embedder_unavailable(env, monkeypatch):
    """嵌入服务不可用 → 检索行为与今天完全一致（不能因为语义挂了就检索不到东西）。"""
    db, memory, semantic, emb = env
    _install_fake_embedder(monkeypatch, emb, semantic, available=False)
    conn = db._get_db()
    _add(conn, "长电科技实时价", "长电科技(600584)实时价71.66")
    assert semantic.hybrid_search("长电科技实时价", [], limit=5) is None
    results = memory._retrieve_relevant_learnings("长电科技实时价", limit=5)
    assert [r["topic"] for r in results] == ["长电科技实时价"], "词频路径必须照常работать"


def test_fallback_when_service_errors(env, monkeypatch):
    """检索路径绝不阻塞冷启动：服务没起时本次返回 None（后台去起），不等待。"""
    db, memory, semantic, emb = env
    _install_fake_embedder(monkeypatch, emb, semantic)
    monkeypatch.setattr(semantic.emb, "embed",
                        lambda texts, timeout=60, allow_cold_start=False: None, raising=False)
    assert semantic.semantic_scores("任意查询") is None
    assert semantic.hybrid_search("任意查询", [], limit=3) is None


# ── 向量持久化与增量 ────────────────────────────────────────────────

def test_ensure_vectors_is_incremental(env, monkeypatch):
    db, _memory, semantic, emb = env
    calls = _install_fake_embedder(monkeypatch, emb, semantic)
    conn = db._get_db()
    for i in range(3):
        _add(conn, f"知识{i}", f"内容{i}")
    n1 = semantic.ensure_vectors(allow_cold_start=True)
    assert n1 == 3 and len(calls) == 1
    # 再跑一次：没有缺的就不该再编码（增量的意义）
    calls.clear()
    assert semantic.ensure_vectors(allow_cold_start=True) == 0
    assert calls == []
    # 新增一条 → 只编码它
    _add(conn, "新知识", "新内容")
    calls.clear()
    assert semantic.ensure_vectors(allow_cold_start=True) == 1
    assert len(calls) == 1 and len(calls[0]) == 1


def test_vectors_persist_across_reload(env, monkeypatch):
    db, _memory, semantic, emb = env
    _install_fake_embedder(monkeypatch, emb, semantic)
    conn = db._get_db()
    _add(conn, "持久化知识", "内容")
    semantic.ensure_vectors(allow_cold_start=True)
    stored = conn.execute("SELECT embedding, embedding_model FROM learnings").fetchone()
    assert stored[0] is not None and stored[1] == emb.MODEL_ID, "向量应落库（含模型名）"
    semantic._vectors.clear()
    semantic._loaded = False
    assert semantic.load_vectors(force=True) == 1, "重载后能从库里读回向量"


def test_stale_model_vectors_are_recomputed(env, monkeypatch):
    """换模型后旧向量必须重编码（否则两套模型的向量混着算余弦，结果无意义）。"""
    db, _memory, semantic, emb = env
    calls = _install_fake_embedder(monkeypatch, emb, semantic)
    conn = db._get_db()
    _add(conn, "旧模型知识", "内容")
    semantic.ensure_vectors(allow_cold_start=True)
    conn.execute("UPDATE learnings SET embedding_model = 'some-old-model'")
    conn.commit()
    calls.clear()
    assert semantic.ensure_vectors(allow_cold_start=True) == 1
    assert len(calls) == 1


def test_threshold_is_the_calibrated_value(env):
    _, _, semantic, _ = env
    # 标定依据：Qwen3 真命中最低 0.453 / 无关最高 0.481（scripts/eval_semantic_recall.py）
    assert semantic.MIN_COS == 0.50, "改这个数字要重跑量表并更新注释"


# ── 检索侧垃圾判定的修正（A/B 实测驱动）─────────────────────────────

def test_tool_prefixed_knowledge_is_retrievable(env):
    """工具学到的知识（topic 带工具名前缀、内容有具体数据）必须可检索。

    原规则"topic 以工具名开头即垃圾"把它全判死——真机 A/B：去掉 3/9→4/9 且不变脏。
    """
    db, memory, semantic, emb = env
    conn = db._get_db()
    _add(conn, "ak_finance: 长电科技(600584)", "长电科技(600584)实时价71.66元")
    assert not memory._learning_is_garbage("ak_finance: 长电科技(600584)", "长电科技(600584)实时价71.66元")
    assert memory._learning_is_garbage("x", "先用 mx_query 查大盘再回答"), "事故短语仍要拦"
    assert memory._learning_is_garbage("x", "思考碎片 <think> abc</think>"), "<think> 碎片仍要拦"
    assert memory._learning_is_garbage("t", "The user wants me to extract a reusable knowledge")


# ── ② 查询窗口 ──────────────────────────────────────────────────────

def test_build_query_window_for_referential(env):
    _, _, semantic, _ = env
    # 自身没内容的（剥掉指代词后剩 <4 字）才借上文：否则向量里什么主题都没有
    assert "电子布" in semantic.build_query("继续", "帮我看看电子布板块")
    assert "电子布" in semantic.build_query("还有呢", "帮我看看电子布板块")
    assert "电子布" in semantic.build_query("上次那个", "帮我看看电子布板块")
    # **自带主题的不借**：实测借了反而被上文稀释（"上次那个风电项目的数据" 被
    # 在聊电子布的上文带跑，召回了电子布而不是风电）
    assert semantic.build_query("上次那个风电项目的数据", "帮我看看电子布板块") == "上次那个风电项目的数据"
    # 正常问题原样（保持与既有查询的可比性）
    assert semantic.build_query("为什么有些股票查不到数据", "帮我看看电子布板块") == "为什么有些股票查不到数据"
    assert semantic.build_query("上次那个", "") == "上次那个"


# ── ③ LLM 裁判（只判重叠区）────────────────────────────────────────

class _FakeResp:
    def __init__(self, text, status=200):
        self.status_code = status
        self._text = text
    def json(self):
        return {"choices": [{"message": {"content": self._text}}]}


def _patch_judge(monkeypatch, semantic, reply, calls):
    def fake_post(url, json=None, timeout=None):
        calls.append(json)
        if callable(reply):
            return reply(json)
        return _FakeResp(reply)
    import httpx
    monkeypatch.setattr(httpx, "post", fake_post, raising=False)


def test_judge_parses_yes_no(env, monkeypatch):
    _, _, semantic, _ = env
    calls = []
    _patch_judge(monkeypatch, semantic, "1.相关\n2.不相关\n3.相关", calls)
    cands = [("a", "甲"), ("b", "乙"), ("c", "丙")]
    assert semantic.judge_relevance("问题", cands) == {"a", "c"}
    assert calls, "应真的调了一次模型"
    # 模型出错 → None（调用方按原门槛走）
    _patch_judge(monkeypatch, semantic, "随便乱说没有序号", [])
    assert semantic.judge_relevance("问题", cands) == set()
    monkeypatch.setattr("httpx.post", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")), raising=False)
    assert semantic.judge_relevance("问题", cands) is None


def test_judge_only_called_for_ambiguous_band(env, monkeypatch):
    """延迟护栏：候选都在明确区间（≥0.55 或 <0.40）时，一次模型调用都不该发。"""
    db, memory, semantic, emb = env
    conn = db._get_db()
    strong = _add(conn, "强相关", "查询证券持仓明细的方法")
    _add(conn, "弱相关噪声", "完全无关的另一件事")   # 落在重叠区之外：不该触发裁判
    _VEC_TABLE["问题甲"] = [1.0, 0.02, 0.0, 0.0]          # 与强相关 cos≈1
    _VEC_TABLE["完全无关的另一件事"] = [0.0, 1.0, 0.0, 0.0]
    _install_fake_embedder(monkeypatch, emb, semantic)
    semantic.ensure_vectors(allow_cold_start=True)
    calls = []
    _patch_judge(monkeypatch, semantic, "1.相关", calls)
    hits = semantic.hybrid_search("问题甲", [], limit=5)
    assert [h["id"] for h in hits] == [strong], "只有强相关该进"
    assert calls == [], "没有重叠区候选却调了裁判 = 白加延迟"


def test_judge_rescues_ambiguous_candidate(env, monkeypatch):
    """重叠区的候选由裁判决定进不进——这正是单一阈值分不开的那一带。"""
    db, memory, semantic, emb = env
    conn = db._get_db()
    mid = _add(conn, "中间分候选", "板块资金流向怎么看")
    # 余弦正好 0.50：落在实测重叠带 [0.40, 0.55) 内、又低于门槛 0.50 之上的判定——
    # 把门槛抬到 0.55 来模拟"分数落在带内但不达门槛"（带是固定的，不随门槛变）
    _VEC_TABLE["问题乙"] = [1.0, 0.0, 0.0, 0.0]
    _VEC_TABLE["板块资金流向怎么看"] = [0.5, 0.866, 0.0, 0.0]
    _install_fake_embedder(monkeypatch, emb, semantic)
    semantic.ensure_vectors(allow_cold_start=True)
    monkeypatch.setattr(semantic, "MIN_COS", 0.55)
    monkeypatch.setenv("LATIAO_SEMANTIC_JUDGE", "1")   # 裁判默认关，本例要开
    calls = []
    _patch_judge(monkeypatch, semantic, "1.相关", calls)
    hits = semantic.hybrid_search("问题乙", [], limit=5)
    assert [h["id"] for h in hits] == [mid], "裁判说相关就该进"
    assert len(calls) == 1
    calls.clear()
    _patch_judge(monkeypatch, semantic, "1.不相关", calls)
    assert semantic.hybrid_search("问题乙", [], limit=5) == [], "裁判说不相关就不该进"


# ── ④ 注入日志与标签回流 ────────────────────────────────────────────

def test_injection_log_and_label_roundtrip(env):
    db, memory, semantic, emb = env
    conn = db._get_db()
    memory._log_injection("sess-1", "查询持仓", [{"id": "x", "topic": "t", "score": 0.9}])
    row = conn.execute("SELECT session_id, query, injected, used FROM memory_injections").fetchone()
    assert row[0] == "sess-1" and "查询持仓" in row[1] and '"x"' in row[2] and row[3] is None
    assert memory.mark_injection_used("sess-1", True) == 1
    assert conn.execute("SELECT used FROM memory_injections").fetchone()[0] == 1
    assert memory.mark_injection_used("sess-1", False) == 1
    assert conn.execute("SELECT used FROM memory_injections").fetchone()[0] == 0
    assert memory.mark_injection_used("", True) == 0, "没有会话 id 就不标（不做无根据的关联）"
    assert db.prune_injections(90)["deleted"] == 0


def test_judge_default_off_and_configurable(env, monkeypatch):
    """裁判默认关（实测负收益），config/env 可开——这条防止有人"顺手打开"。"""
    _, _, semantic, _ = env
    monkeypatch.delenv("LATIAO_SEMANTIC_JUDGE", raising=False)
    assert semantic.judge_enabled() is False
    monkeypatch.setenv("LATIAO_SEMANTIC_JUDGE", "1")
    assert semantic.judge_enabled() is True
    monkeypatch.setenv("LATIAO_SEMANTIC_JUDGE", "0")
    assert semantic.judge_enabled() is False
    monkeypatch.delenv("LATIAO_SEMANTIC_JUDGE", raising=False)
    import config
    config.save_config({"memory": {"semantic_judge": True}})
    assert semantic.judge_enabled() is True
