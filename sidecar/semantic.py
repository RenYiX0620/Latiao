"""语义检索（⑥）：向量缓存 + 增量编码 + 与词频检索的混合排序。

审查⑥：「检索是字符级词频匹配，没有语义……用户问 A、库存的是 B，词面不重叠就检索
不到。团队为此叠了三层垃圾正则过滤 + 阈值调优，全是在治'错误记忆被注入'的症状」。
实测（真库 415 条 learnings，9 个真实查询）：
    词频 TF-IDF 命中@5 = 3/9 → 加语义后 6/9（见 scripts/eval_semantic_recall.py）

**判据是两道独立的门槛，不是把阈值调松**：
    cos_sem >= 0.50（Qwen3 标定：真命中最低 0.453 / 无关最高 0.481）
    或 cos_tfidf >= 0.28（原有门槛，不动）
排序用"各自门槛的相对置信度"：(分数 − 门槛) / (1 − 门槛)——两个门槛都是标定过的，
所以这个刻度的零点就是"刚好够格"，不需要凭空造刻度；主信号取两路中较大者，两路一致
时给小加成。任一环节不可用 → 返回 None，调用方走原来的词频路径（fail-open，检索永不
因为嵌入服务挂了而失效）。
"""
import logging
import struct
import threading

import embedding_service as emb
from db import _db_write_lock, _get_db

logger = logging.getLogger("latiao-sidecar")

MIN_COS = 0.50          # 语义门槛（标定值见模块文档）
BATCH = 32
VECTOR_CACHE_MAX = 20000   # 内存里最多缓存多少条向量（1.6MB/415 条 → 上万条也才几十 MB）

_lock = threading.Lock()
_vectors: dict[str, tuple[list[float], str]] = {}   # id -> (向量, 模型名)
_loaded = False


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


def _cos(a: list[float], b: list[float]) -> float:
    s = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        s += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return s / ((na ** 0.5) * (nb ** 0.5))


def load_vectors(force: bool = False) -> int:
    """把库里的向量读进内存（一次即可；retrieval 走内存里的缓存）。"""
    global _loaded
    with _lock:
        if _loaded and not force:
            return len(_vectors)
        try:
            conn = _get_db()
            rows = conn.execute(
                "SELECT id, embedding, embedding_model FROM learnings "
                "WHERE embedding IS NOT NULL LIMIT ?", (VECTOR_CACHE_MAX,)).fetchall()
        except Exception:
            logger.warning("读取语义向量失败", exc_info=True)
            return len(_vectors)
        _vectors.clear()
        for rid, blob, mid in rows:
            if blob:
                try:
                    _vectors[rid] = (_unpack(blob), mid or "")
                except Exception:
                    continue
        _loaded = True
    return len(_vectors)


def missing_count() -> int:
    """还差多少条没有（当前模型下的）向量。"""
    try:
        conn = _get_db()
        total = conn.execute("SELECT COUNT(*) FROM learnings").fetchone()[0]
    except Exception:
        return 0
    have = sum(1 for _v, mid in _vectors.values() if mid == emb.MODEL_ID)
    return max(0, total - have)


def ensure_vectors(allow_cold_start: bool = True, max_batches: int = 40) -> int:
    """给缺向量的学习补向量（批量编码 + 落库 + 更新内存缓存）。返回本次编码条数。"""
    if not emb.available():
        return 0
    load_vectors()
    done = 0
    for _ in range(max_batches):
        try:
            conn = _get_db()
            rows = conn.execute(
                "SELECT id, topic, content, embedding_model FROM learnings "
                "WHERE embedding IS NULL OR embedding_model IS NULL OR embedding_model != ? "
                "LIMIT ?", (emb.MODEL_ID, BATCH)).fetchall()
        except Exception:
            logger.warning("读取待编码学习失败", exc_info=True)
            return done
        if not rows:
            break
        texts = [f"{r[1]} {r[2]}" for r in rows]
        vecs = emb.embed(texts, allow_cold_start=allow_cold_start)
        if not vecs or len(vecs) != len(rows):
            break                       # 服务不可用 / 半截结果：下次再来
        try:
            with _db_write_lock:
                for (rid, _t, _c, _m), v in zip(rows, vecs):
                    conn.execute("UPDATE learnings SET embedding = ?, embedding_model = ? WHERE id = ?",
                                 (_pack(v), emb.MODEL_ID, rid))
                conn.commit()
        except Exception:
            logger.warning("写入语义向量失败", exc_info=True)
            break
        with _lock:
            for (rid, _t, _c, _m), v in zip(rows, vecs):
                _vectors[rid] = (v, emb.MODEL_ID)
        done += len(vecs)
    if done:
        logger.info("语义向量已更新: %d 条（模型 %s）", done, emb.MODEL_ID)
    return done


def semantic_scores(query: str) -> dict[str, float] | None:
    """查询的语义分数表 {learning_id: cos}；不可用返回 None（调用方走词频）。"""
    if not emb.available() or not query.strip():
        return None
    load_vectors()
    if not _vectors:
        return None
    qv = emb.embed([query])
    if not qv:
        return None
    q = qv[0]
    return {rid: _cos(q, v) for rid, (v, _mid) in _vectors.items()}


def hybrid_search(query: str, tfidf_results: list[dict], limit: int = 5) -> list[dict] | None:
    """混合排序：两道门槛筛资格 + RRF 定次序。不可用返回 None（调用方保持原行为）。

    tfidf_results 由 memory._tfidf_search 给出（它内部已按 0.28 过滤，且带 `score`）。
    语义召回可能带出词频没返回的条目——那些需要现查库补齐（调用方只要 id 对不上的
    文档内容）。
    """
    scores = semantic_scores(query)
    if scores is None:
        return None
    tf_rank = {r.get("id"): i + 1 for i, r in enumerate(tfidf_results) if r.get("id")}
    eligible = {rid for rid, s0 in scores.items() if s0 >= MIN_COS} | set(tf_rank)
    if not eligible:
        return []
    by_id = {r.get("id"): dict(r) for r in tfidf_results if r.get("id")}
    missing = [rid for rid in eligible if rid not in by_id]
    if missing:
        try:
            conn = _get_db()
            qs = ",".join("?" * len(missing))
            for row in conn.execute(
                    f"SELECT id, topic, content, confidence, hit_count, source_type "
                    f"FROM learnings WHERE id IN ({qs})", missing):
                by_id[row[0]] = {"id": row[0], "topic": row[1], "content": row[2],
                                 "confidence": row[3], "hit_count": row[4], "source_type": row[5]}
        except Exception:
            logger.warning("补齐语义召回条目失败", exc_info=True)
    # 排序用"各自门槛的相对置信度"，不用 RRF（首版用 RRF，实测把强语义命中稀释了：
    # 语料主题高度同质，"够门槛"的条目很多，名次被摊平 → 真机只有 3/8，而纯余弦是
    # 6/9）。rel = (分数 - 门槛) / (1 - 门槛)：两个门槛都标定过，所以这个刻度的零点
    # 就是"刚好够格"，不需要凭空造；主信号取两者较大者，两路一致再给小加成。
    scored = []
    for rid in eligible:
        cos = scores.get(rid, 0.0)
        tf = float(by_id.get(rid, {}).get("score") or 0.0)
        sem_rel = max(0.0, (cos - MIN_COS) / (1.0 - MIN_COS))
        tf_rel = max(0.0, (tf - 0.28) / (1.0 - 0.28))
        combined = max(sem_rel, tf_rel) + 0.15 * min(sem_rel, tf_rel)
        scored.append((combined, rid, cos))
    scored.sort(key=lambda x: (-x[0], -x[2]))
    out = []
    for _combined, rid, cos in scored[:limit]:
        item = by_id.get(rid)
        if not item:
            continue
        item["_semantic"] = round(float(cos), 4)
        if cos >= MIN_COS and (item.get("score") or 0) < 0.28:
            item["score"] = round(float(cos), 4)      # 语义命中的条目用余弦当分数
        out.append(item)
    return out


def warmup() -> int:
    """启动预热（后台线程调用）：起服务 + 补齐向量，让第一轮对话就能用上语义。"""
    if not emb.available():
        return 0
    n = ensure_vectors(allow_cold_start=True)
    logger.info("语义检索预热完成：向量 %d 条，缺 %d 条", len(_vectors), missing_count())
    return n
