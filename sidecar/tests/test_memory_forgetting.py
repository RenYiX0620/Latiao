"""⑦ 遗忘机制（2026-09-23，审查第三梯队）。

审查原文：「全库无 TTL、无行数上限、无定期清理……置信度只涨不降：同 topic 重复写入
min(1.0, existing + confidence×0.3)。垃圾记忆进来的唯一出路是被写入门槛拦住，一旦
进去就是永久 1.0 满分」。

这里守两件**不依赖打分**的事（按质量淘汰要等 ⑥ 有检索信号）：
- reflections 是每次工具调用的过程记录，与 tool_calls 同源 → 有保留期
- learnings 有行数上限，超限淘汰"价值最低"的（置信度 × 命中 × 新近度）；未超限不动任何行
"""
import importlib
from datetime import datetime, timedelta

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
    return db, memory


def _add_learning(conn, topic, conf, hits=0, created_days_ago=0, updated_days_ago=None):
    ts = (datetime.now() - timedelta(days=created_days_ago)).isoformat()
    up = (datetime.now() - timedelta(days=(updated_days_ago if updated_days_ago is not None
                                          else created_days_ago))).isoformat()
    conn.execute(
        "INSERT INTO learnings(id, session_id, topic, content, confidence, source_type, "
        "hit_count, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (topic, "s", topic, f"内容 {topic}", conf, "extracted", hits, ts, up))
    conn.commit()


def _topics(conn):
    return {r[0] for r in conn.execute("SELECT topic FROM learnings")}


# ── reflections 保留期 ───────────────────────────────────────────────

def test_prune_reflections_by_age(mem):
    db, memory = mem
    conn = db._get_db()
    old = (datetime.now() - timedelta(days=200)).isoformat()
    new = datetime.now().isoformat()
    for i, ts in enumerate((old, new)):
        conn.execute("INSERT INTO reflections(id, session_id, tool_name, tool_args, "
                     "tool_result_summary, reflection, was_useful, created_at) "
                     "VALUES(?,?,?,?,?,?,?,?)", (f"r{i}", "s", "read_file", "{}", "sum", "refl", 1, ts))
    conn.commit()
    out = db.prune_reflections(retention_days=180)
    assert out["deleted"] == 1 and out["kept"] == 1
    assert [r[0] for r in conn.execute("SELECT id FROM reflections")] == ["r1"]


def test_prune_reflections_disabled(mem):
    db, _ = mem
    out = db.prune_reflections(retention_days=0)
    assert out["deleted"] == 0 and out["skipped"] == "retention disabled"


def test_reflections_retention_from_config(mem, monkeypatch):
    db, _ = mem
    import config
    config.save_config({"memory": {"reflections_retention_days": 7}})
    monkeypatch.delenv("LATIAO_REFLECTIONS_RETENTION_DAYS", raising=False)
    assert db.reflections_retention_days() == 7
    monkeypatch.setenv("LATIAO_REFLECTIONS_RETENTION_DAYS", "3")
    assert db.reflections_retention_days() == 3


# ── learnings 容量上限与淘汰 ─────────────────────────────────────────

def test_under_cap_deletes_nothing(mem):
    db, _ = mem
    conn = db._get_db()
    for i in range(5):
        _add_learning(conn, f"t{i}", 0.6)
    out = db.prune_learnings(max_rows=10)
    assert out["deleted"] == 0 and out["kept"] == 5
    assert len(_topics(conn)) == 5, "未超限不得动任何一行"


def test_over_cap_evicts_lowest_value(mem):
    db, _ = mem
    conn = db._get_db()
    _add_learning(conn, "高置信常用", 1.0, hits=20, created_days_ago=1)
    _add_learning(conn, "中等", 0.6, hits=2, created_days_ago=10)
    _add_learning(conn, "垃圾", 0.2, hits=0, created_days_ago=400)      # 最低价值
    _add_learning(conn, "次垃圾", 0.3, hits=0, created_days_ago=300)
    out = db.prune_learnings(max_rows=2)
    assert out["deleted"] == 2 and out["kept"] == 2
    assert _topics(conn) == {"高置信常用", "中等"}, "应淘汰价值最低的两条"


def test_eviction_invalidates_search_index(mem):
    """淘汰后必须让检索索引失效，否则被删的知识还能被召回（⑨ 同型问题）。"""
    db, memory = mem
    conn = db._get_db()
    _add_learning(conn, "要淘汰的独特标记词", 0.1, hits=0, created_days_ago=500)
    _add_learning(conn, "保留的独特标记词", 0.9, hits=5, created_days_ago=1)
    assert any("要淘汰的独特标记词" in r["topic"]
               for r in memory._tfidf_search("独特标记词", limit=5))
    db.prune_learnings(max_rows=1)
    hits = [r["topic"] for r in memory._tfidf_search("独特标记词", limit=5)]
    assert not any("要淘汰的独特标记词" in t for t in hits), f"淘汰后仍被召回: {hits}"


def test_old_but_perfect_confidence_can_lose_to_recent(mem):
    """文档化设计：新近度有 180 天半衰期——远古的 1.0 会输给近期的 0.6。"""
    db, _ = mem
    conn = db._get_db()
    _add_learning(conn, "远古满分", 1.0, hits=0, created_days_ago=1000)
    _add_learning(conn, "近期中等", 0.6, hits=0, created_days_ago=1)
    db.prune_learnings(max_rows=1)
    assert _topics(conn) == {"近期中等"}


def test_cap_disabled(mem):
    db, _ = mem
    out = db.prune_learnings(max_rows=0)
    assert out["deleted"] == 0 and out["skipped"] == "cap disabled"


def test_learnings_max_from_config(mem, monkeypatch):
    db, _ = mem
    import config
    config.save_config({"memory": {"learnings_max": 123}})
    monkeypatch.delenv("LATIAO_LEARNINGS_MAX", raising=False)
    assert db.learnings_max() == 123
    monkeypatch.setenv("LATIAO_LEARNINGS_MAX", "50")
    assert db.learnings_max() == 50


# ── 自动技能合成：端点走统一解析器（2026-09-23 清理死配置）────────────

@pytest.mark.asyncio
async def test_skill_synthesis_uses_resolved_endpoint(mem, monkeypatch):
    """此前写死 config.LM_STUDIO_URL（localhost:1234）→ 从未生效；
    现在必须用 agent.routing._resolve_api_target 解析出的端点。

    断言"请求发到了解析出的 URL"而不只是"函数没报错"——写死端点的 bug 恰恰是
    "函数照常返回、只是请求发错地方"，只测返回值是抓不到的。
    """
    db, memory = mem
    conn = db._get_db()
    for i in range(3):
        conn.execute("INSERT INTO learnings(id, session_id, topic, content, confidence, "
                     "source_type, hit_count, created_at, updated_at) VALUES(?,?,?,?,?,?,0,?,?)",
                     (f"r{i}", "s", f"read_file 用法{i}", f"read_file 的经验 {i}", 0.9,
                      "refined", "2026-09-23", "2026-09-23"))
    conn.commit()

    seen_urls: list[str] = []

    class _Resp:
        status_code = 200
        def json(self):
            return {"choices": [{"message": {"content": "技能文档正文" * 8}}]}

    class _Client:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, headers=None, json=None, **k):
            seen_urls.append(url)
            return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    import agent.routing as routing
    async def _fake_resolve(cfg):
        return ("openai", "http://127.0.0.1:1235/v1/chat/completions", {}, True)
    monkeypatch.setattr(routing, "_resolve_api_target", _fake_resolve, raising=False)

    import main
    if not hasattr(main, "_last_cloud_config"):
        pytest.skip("main 无 _last_cloud_config（此环境不适用）")
    memory._skill_gen_tracker["read_file"] = memory._SKILL_GENERATION_THRESHOLD - 1
    await memory._maybe_generate_skill("read_file", {"path": "x"}, "读到了内容")

    if seen_urls:
        assert seen_urls[0] == "http://127.0.0.1:1235/v1/chat/completions", \
            f"请求发到了 {seen_urls[0]}（写死端点的老毛病）"
    else:
        pytest.fail("没有发出请求——合成路径没跑到（阈值/前缀条件要跟实现保持一致）")
