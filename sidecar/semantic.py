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
import os
import re
import struct
import threading

import embedding_service as emb
from db import _db_write_lock, _get_db

logger = logging.getLogger("latiao-sidecar")

MIN_COS = 0.50          # 语义门槛（标定值见模块文档）
BATCH = 8        # 请求批量（实测批量越小 Metal 缓冲增长越少）
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


def ensure_vectors(allow_cold_start: bool = True, max_batches: int = 200) -> int:
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
        # 大批量回填后回收服务实例：实测满载时 Metal 缓冲把 RSS 从 850MB 顶到 2.2~2.5GB
        # 且不释放；下次查询会按需重启（且那一刻直接回退词频，不会卡对话）。
        # 只在**积压清空**后回收（首版按"编了 N 条就回收"写，结果在批次上限处
        # 提前回收，还剩 95 条没编码就停了）。
        try:
            remaining = missing_count()
        except Exception:
            remaining = 0
        if done >= 64 and remaining == 0:
            logger.info("批量回填 %d 条完成（无积压）后回收嵌入服务实例以释放内存", done)
            emb.stop()
        elif remaining:
            logger.info("语义向量仍缺 %d 条（本次编码 %d 条），下次继续", remaining, done)
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


def _text_for(rid: str, by_id: dict) -> str:
    """候选文本（裁判要读内容）：优先用已有的，否则查库。"""
    item = by_id.get(rid)
    if item:
        return f"{item.get('topic', '')} {item.get('content', '')}"
    try:
        row = _get_db().execute(
            "SELECT topic, content FROM learnings WHERE id = ?", (rid,)).fetchone()
        return f"{row[0]} {row[1]}" if row else ""
    except Exception:
        return ""


def hybrid_search(query: str, tfidf_results: list[dict], limit: int = 5,
                  prev_user_text: str = "") -> list[dict] | None:
    """混合排序：两道门槛筛资格 + 重叠区 LLM 裁判 + 门槛相对置信度定次序。

    不可用返回 None（调用方保持原行为）。

    tfidf_results 由 memory._tfidf_search 给出（它内部已按 0.28 过滤，且带 `score`）。
    语义召回可能带出词频没返回的条目——那些需要现查库补齐（调用方只要 id 对不上的
    文档内容）。
    """
    scores = semantic_scores(build_query(query, prev_user_text))
    if scores is None:
        return None
    tf_rank = {r.get("id"): i + 1 for i, r in enumerate(tfidf_results) if r.get("id")}
    eligible = {rid for rid, s0 in scores.items() if s0 >= MIN_COS} | set(tf_rank)
    by_id = {r.get("id"): dict(r) for r in tfidf_results if r.get("id")}
    # 重叠区（0.40~0.55）交给 LLM 裁判读内容判断——只有这一段值得花一次模型调用，
    # 毕竟余弦在这个区间里分不开真命中与无关查询（实测 0.453~0.481 重叠）。
    judged_ok: set[str] = set()
    ambiguous = ([(rid, _text_for(rid, by_id)) for rid, s0 in scores.items()
                  if AMBIGUOUS_LOW <= s0 < AMBIGUOUS_HIGH and rid not in eligible]
                 if judge_enabled() else [])
    if ambiguous:
        _j = judge_relevance(query, ambiguous[:8])
        if _j:
            # 条数上限：裁判放行的条目最多补 2 个（它判"同领域也算相关"的倾向比
            # 门槛宽松，实测不加限会把"帮我把 Python 代码重构一下"注入 4 条）。
            judged_ok = set(sorted(_j)[:2])
            eligible |= judged_ok
    # 注意顺序：**先**让裁判处理重叠区，再判断"有没有可选"——首版把
    # `if not eligible: return []` 放在裁判之前，于是"一条都没达门槛"时根本走不到
    # 裁判，而那种情况恰恰是裁判该救的（测试当场抓到）。
    if not eligible:
        return []
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
    # 三档排序（首版把裁判救回的条目和"卡线"条目混在一起按分数排，它们相对门槛的
    # 置信度≈0，永远挤不进 top-5 → 裁判白判。实测：加裁判后命中数没变就是这个原因）：
    #   档 0：cos 已达语义门槛（强语义命中，最可信）
    #   档 1：门槛没到、但**裁判读了内容说相关**（这就是裁判存在的意义）
    #   档 2：仅词频命中（原有路径）
    # 档内按分数排；这样裁判救回的条目能补进空位，又不会顶掉强命中。
    scored = []
    for rid in eligible:
        cos = scores.get(rid, 0.0)
        tf = float(by_id.get(rid, {}).get("score") or 0.0)
        sem_rel = max(0.0, (cos - MIN_COS) / (1.0 - MIN_COS))
        tf_rel = max(0.0, (tf - 0.28) / (1.0 - 0.28))
        combined = max(sem_rel, tf_rel) + 0.15 * min(sem_rel, tf_rel)
        if cos >= MIN_COS:
            tier = 0
        elif rid in judged_ok:
            tier = 1
        else:
            tier = 2
        scored.append((tier, -combined, rid, cos))
    scored.sort(key=lambda x: (x[0], x[1], -x[3]))
    out = []
    for _tier, _neg, rid, cos in scored[:limit]:
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


# ── ② 查询窗口：指代类问题补上上一轮上下文 ────────────────────────────
# 实测发现的问题：只用最后一条用户消息编码时，"上次那个风电项目的数据"这类**指代**
# 查询的向量里没有任何主题信息（"上次那个"占了大半），召回的自然是噪声。
# 只在出现指代词或消息过短时拼接上一轮用户消息——正常运行不改变查询（保持可比性）。
_REFERENTIAL = ("上次", "那个", "这个", "刚才", "之前", "上面", "它", "他们", "继续",
                "还有呢", "再说", "那个东西", "同样")


_REFERENTIAL_STRIP = ("上次", "那个", "这个", "刚才", "之前", "上面", "还有呢", "再说",
                      "同样", "继续", "它", "他们")


def build_query(question: str, prev_user_text: str = "") -> str:
    """给检索用的查询文本：**自身没剩多少信息**时才拼接上一轮用户消息。

    实测教训：一开始只要出现指代词/短于 12 字就拼上文，结果"上次那个**风电项目**的数据"
    被上文（在聊电子布）稀释掉了，反而更差——它自带主题，不需要借上下文。
    所以判据是"剥掉指代词后还剩多少实义内容"：'继续' → 剩 0 → 借用上文；
    '上次那个风电项目的数据' → 剩 7 字 → 原样查询。
    """
    q = (question or "").strip()
    prev = (prev_user_text or "").strip()
    if not prev or not q:
        return q
    stripped = q
    for k in _REFERENTIAL_STRIP:
        stripped = stripped.replace(k, "")
    if len(stripped.strip()) < 4:
        return f"{prev[:200]}\n{q}"
    return q


# ── ③ LLM 裁判：重叠区交给模型读内容判断 ──────────────────────────────
# 为什么需要（实测数据）：真命中最低分 0.453、无关查询最高 0.481——**单一绝对阈值
# 分不开这一带**；边际（真例最低 0.007 vs 干扰最高 0.017）与 z 值（2.1~7.3 vs 2.7~3.7）
# 同样重叠。余弦的数值差已经没有信息了，只有内容层判断能分开。
# 因此**只对落在重叠区的候选**做一次小请求（不是每轮都判，避免给对话加延迟）。
AMBIGUOUS_LOW = 0.40
AMBIGUOUS_HIGH = 0.55      # 覆盖实测重叠区 0.40~0.55


def judge_enabled() -> bool:
    """重叠区是否交给 LLM 裁判。**默认关**（实测它在我的样本上是负收益）。

    实测（真库 415 条 / 9 正例 + 4 干扰，本地 Hermes 35B 当裁判）：
        仅词频 3/9 | 语义 4/9 | 语义+裁判 4/9
      且裁判会把"帮我把 Python 代码重构一下"这类同领域问题判成相关（限 2 条后仍注入
      1~2 条），每次调用 1~2 秒。也就是说：**在我编的样本上它既不提命中，又加延迟与
      误注入**。真要判定这个机制，得靠 ④ 的真实标签（点赞/点踩回流），不能再拿我编的
      13 个查询去拟合。config.json memory.semantic_judge=true 或
      LATIAO_SEMANTIC_JUDGE=1 可打开（打开时会用三档排序，裁判认可的条目补空位）。
    """
    raw = os.environ.get("LATIAO_SEMANTIC_JUDGE")
    if raw is not None:
        return raw.strip().lower() not in ("0", "false", "no", "off")
    try:
        import json as _json
        from config import CONFIG_FILE
        if CONFIG_FILE.exists():
            cfg = _json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            section = cfg.get("memory") if isinstance(cfg, dict) else None
            if isinstance(section, dict) and section.get("semantic_judge") is not None:
                return bool(section.get("semantic_judge"))
    except Exception:
        pass
    return False

_judge_lock = threading.Lock()
_JUDGE_TIMEOUT = 6.0      # 实测关思考后单次 1.5~2.5s（含 8 个候选）


def judge_relevance(question: str, candidates: list[tuple[str, str]]) -> set[str] | None:
    """让本地模型逐条判断候选是否与本轮问题相关。返回被判定相关的 id 集合。

    candidates: [(id, 文本)]。失败/超时/解析不出 → None（调用方按原门槛处理，fail-open）。
    """
    if not candidates:
        return set()
    try:
        import httpx
        from config import SUBAGENT_MODEL
        from local_llm import get_api_url
    except Exception:
        return None
    # 注意：get_api_url() 是**基址**（…:1235/v1），要自己接 /chat/completions——
    # 首版直接 POST 到基址，404、判定全失败（而且失败很快，不像是超时，容易误判）
    lines = [f"{i+1}. {text[:120]}" for i, (_cid, text) in enumerate(candidates)]
    prompt = (
        "下面每条候选是可能被注入到回答里的历史知识。请判断：**要回答用户这个问题，"
        "这条知识是否真的用得上**。注意判据是「回答时用得上」，仅仅是同一个领域、"
        "话题沾边都算不相关。逐行只输出「序号:相关」或「序号:不相关」，不要解释。\n\n"
        f"用户问题：{question[:200]}\n\n候选：\n" + "\n".join(lines)
    )
    try:
        with _judge_lock:
            resp = httpx.post(get_api_url().rstrip("/") + "/chat/completions", json={
                "model": SUBAGENT_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                # 关思考（与薄循环同一开关）：本地模型是思考型的，不关就会把
                # max_tokens 全花在 reasoning_content 上、content 返回空字符串
                # ——实测就是这样"全部判成不相关"，排查了三轮才看到原始回复。
                "chat_template_kwargs": {"enable_thinking": False},
                "max_tokens": 120, "temperature": 0.0, "stream": False,
            }, timeout=httpx.Timeout(_JUDGE_TIMEOUT))
        if resp.status_code != 200:
            return None
        _msg = resp.json()["choices"][0]["message"]
        # reasoning_content 兜底：万一模型还是思考了，答案也可能落在那里
        text = (_msg.get("content") or "").strip() or (_msg.get("reasoning_content") or "")
    except Exception:
        logger.debug("LLM 裁判调用失败（回退门槛判定）", exc_info=True)
        return None
    # 解析「序号:相关 / 不相关」；模型把"不相关"也含"相关"两字，所以先判否定
    keep: set[str] = set()
    for m in re.finditer(r"(\d+)\s*[:：.、]\s*(不相关|相关|否|是)", text):
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(candidates) and m.group(2) in ("相关", "是"):
            keep.add(candidates[idx][0])
    return keep
