"""Memory System — self-learning, TF-IDF, preferences, and skill generation."""
import json
import logging
import math
import os
import re
import uuid
from datetime import datetime

import httpx

from config import LM_STUDIO_URL, SUBAGENT_MODEL
from capability_registry import USER_SKILLS_DIR
from db import _db_write_lock

logger = logging.getLogger(__name__)

# ── Constants ──
MAX_LEARNINGS_INJECT = 5
LEARNING_CONFIDENCE_THRESHOLD = 0.3
_SKILL_GENERATION_THRESHOLD = 3
_skill_gen_tracker: dict[str, int] = {}


# Database lock (separate from main.py's lock since we write to different tables)

# TF-IDF cache (moved from main.py with learning functions)
_TFIDF_CACHE = None
_TFIDF_CACHE_DIRTY = True
# 追加式增量的辅助状态：已分词文档 / 词→文档数 / 各行 rowid（与 docs 对齐）
_TFIDF_DOCS: list | None = None
_TFIDF_DF: dict = {}
_TFIDF_ROWIDS: list | None = None



def _tokenize_zh(text: str) -> list[str]:
    """Simple Chinese tokenizer: bigram characters + whole words.
    E.g. '项目结构' → ['项目', '目结', '结构', '项目结构']"""
    # Extract Chinese chars and alphanumeric tokens
    tokens = []
    # Chinese bigrams
    chinese_chars = []
    for ch in text:
        if '\u4e00' <= ch <= '\u9fff' or '\u3400' <= ch <= '\u4dbf':
            chinese_chars.append(ch)
    for i in range(len(chinese_chars)):
        tokens.append(chinese_chars[i])
        if i + 1 < len(chinese_chars):
            tokens.append(chinese_chars[i] + chinese_chars[i + 1])
    # English/alphanumeric tokens (split on non-alphanumeric)
    eng_tokens = re.findall(r'[a-zA-Z0-9_]+', text.lower())
    tokens.extend(eng_tokens)
    return tokens


# 错误签名提取（2026-09-23，审查⑧）：反思此前只有"工具 X 执行出错，建议重试"这类
# 模板套话——真库 969 条里只有 136 种文本，mx_query 的同一句出现 237 次。模板对
# 跨会话复用毫无价值（"上次踩过的坑"必须带上是什么坑）。这里从结果里抽一行错误
# 特征，让反思（以及由它提升出的 learning）可定位、可检索。
_ERROR_SIG_RE = re.compile(
    r"(?:error|错误|failed|失败|traceback|denied|不存在|未找到|超时|timeout|"
    r"频率过高|次数已用完|未返回数据)[^\n]{0,80}", re.IGNORECASE)


def _error_signature(result: str, limit: int = 60) -> str:
    """从工具结果里抽一段可辨识的错误特征；抽不到返回空串。"""
    m = _ERROR_SIG_RE.search(result or "")
    if not m:
        return ""
    return re.sub(r"\s+", " ", m.group(0)).strip()[:limit]


# 反思类别：决定文案，也决定这条反思是否算"真踩到的坑"（⑧：was_useful 此前
# 硬编码 True，于是"输出较大"这类提示也被当成失败经验——真库模拟显示 351 条
# read_file"失败经验"其实是长输出提示）。只有 error/missing/empty 才值得提升为知识。
REFLECTION_PITFALL_KINDS = ("error", "missing", "empty")


def _reflection_kind(tool_name: str, result: str) -> str:
    """判断反思类别：permission | error | missing | empty | large | ''。"""
    result_lower = (result or "").lower()
    if "permission denied" in result_lower or "权限不足" in result:
        return "permission"
    if ("error" in result_lower or "错误" in result or "failed" in result_lower
            or "失败" in result or "traceback" in result_lower or "denied" in result_lower):
        return "error"
    if "not found" in result_lower or "不存在" in result:
        return "missing"
    if len((result or "").strip()) < 5:
        return "empty"
    if len(result or "") > 5000:
        return "large"
    return ""


def _quick_reflect(tool_name: str, result: str) -> str:
    """Quick heuristic reflection on tool execution result.
    Returns a reflection note or empty string."""
    sig = _error_signature(result)
    tail = f"（{sig}）" if sig else ""
    kind = _reflection_kind(tool_name, result)
    if kind == "permission":
        return f"权限不足，建议检查文件/目录权限{tail}"
    if kind == "error":
        return f"工具 {tool_name} 执行出错，可能需要重试或调整参数{tail}"
    if kind == "missing":
        return f"目标不存在，可能需要先确认路径或创建前置资源{tail}"
    if kind == "empty":
        return "工具返回为空，可能参数不正确或目标无内容"
    if kind == "large":
        return f"输出较大({len(result)}字符)，后续可能需要聚焦关键部分"
    return ""  # Everything looks fine, no reflection needed


# Lazy import helpers to avoid circular dependency with main.py

def _get_db():
    """Get database connection from db module (no circular dependency)."""
    from db import _get_db as _get_db_inner
    return _get_db_inner()


# ── Tokenization (already in memory.py from previous extraction) ──


# ═══════════════════════════════════════════════════════
#  Lightweight Embedding Search (no external deps)
#  Uses character n-gram TF-IDF for Chinese semantic similarity.
#  For < 1000 learnings this is fast enough — ~5ms per query.
# ═══════════════════════════════════════════════════════

def _mark_tfidf_dirty():
    """写入/删除 learnings 后置脏：下次检索时重建（追加走增量；行数不符或首次走全量）。"""
    global _TFIDF_CACHE_DIRTY
    _TFIDF_CACHE_DIRTY = True


def _tf_rows(conn, only_new_after: int | None = None):
    sql = ("SELECT rowid, id, topic, content, confidence, hit_count, source_type FROM learnings"
           + (" WHERE rowid > ? ORDER BY rowid" if only_new_after is not None else ""))
    return conn.execute(sql, (only_new_after,) if only_new_after is not None else ()).fetchall()


def _build_tfidf_index():
    """Build an in-memory TF-IDF index from all learnings (cached; appends are incremental).

    2026-09-23 性能修正（实测 341 条 / 5024 词表）：
    - IDF 用 `sum(1 for d in docs if token in d)` 是 O(词表 × 文档数)——3.4 万次集合
      查询、32.4ms，占整次重建（38.8ms）的 84%。改成单遍累计 df → 1.5ms（21×），
      同一份数据排名结果不变。
    - 之前每次写入都整表重取+重分词（4.1ms，随表线性涨）。learnings 只有追加
      （INSERT）与改 confidence/hit_count（UPDATE，不改 topic/content）两种写入，
      所以缓存保留已分词文档，重建时只给新增行分词；行数对不上（有删除）才整表重来。
      doc_info 的 confidence/hit_count 仍会刷新（检索时的 ≥0.3 过滤读它）。
    """
    global _TFIDF_CACHE, _TFIDF_CACHE_DIRTY, _TFIDF_DOCS, _TFIDF_DF, _TFIDF_ROWIDS
    if not _TFIDF_CACHE_DIRTY and _TFIDF_CACHE is not None:
        # 自校验（2026-09-23 实测踩到）：api_routes 的 DELETE FROM learnings 不走
        # 本模块的置脏 → 删掉的条目会一直留在检索结果里（缓存永不失效）。命中路径
        # 用一次 COUNT（~0.2ms）比对，行数不符即当脏处理，这类陈旧就不可能发生。
        try:
            total_now = _get_db().execute("SELECT COUNT(*) FROM learnings").fetchone()[0]
            if total_now == len(_TFIDF_DOCS or []):
                return _TFIDF_CACHE
        except Exception:
            return _TFIDF_CACHE
        _TFIDF_CACHE_DIRTY = True
    try:
        conn = _get_db()
        total = conn.execute("SELECT COUNT(*) FROM learnings").fetchone()[0]
    except Exception:
        return [], {}, {}
    if not total:
        _TFIDF_CACHE, _TFIDF_DOCS, _TFIDF_DF, _TFIDF_ROWIDS, _TFIDF_CACHE_DIRTY = (
            ([], {}, {}), [], {}, [], False)
        return [], {}, {}

    docs = doc_info = rowids = None
    incremental = False
    if _TFIDF_DOCS is not None and _TFIDF_ROWIDS is not None and total >= len(_TFIDF_DOCS):
        # 追加：只取 rowid 更大的新行
        try:
            new_rows = _tf_rows(conn, only_new_after=_TFIDF_ROWIDS[-1] if _TFIDF_ROWIDS else 0)
        except Exception:
            new_rows = None
        if new_rows is not None and len(_TFIDF_DOCS) + len(new_rows) == total:
            docs, doc_info, rowids = _TFIDF_DOCS, _TFIDF_CACHE[0], _TFIDF_ROWIDS
            for row in new_rows:
                text = f"{row[2]} {row[3]}"
                tokens = _tokenize_zh(text)
                if not tokens:
                    continue
                tf = {}
                for t in tokens:
                    tf[t] = tf.get(t, 0) + 1
                docs.append(tf)
                doc_info.append({
                    "id": row[1], "topic": row[2], "content": row[3],
                    "confidence": row[4], "hit_count": row[5], "source_type": row[6],
                })
                rowids.append(row[0])
                for t in tf:
                    _TFIDF_DF[t] = _TFIDF_DF.get(t, 0) + 1
            incremental = True
            # 已存在文档的 confidence/hit_count 可能变了（改置信度不动内容）→ 只刷元数据
            meta = {r[0]: (r[1], r[2]) for r in conn.execute(
                "SELECT rowid, confidence, hit_count FROM learnings")}
            for i, rid in enumerate(rowids[:len(rowids) - len(new_rows)]):
                m = meta.get(rid)
                if m:
                    doc_info[i]["confidence"], doc_info[i]["hit_count"] = m

    if not incremental:
        rows = _tf_rows(conn)
        docs, doc_info, rowids = [], [], []
        _TFIDF_DF = {}
        for row in rows:
            text = f"{row[2]} {row[3]}"
            tokens = _tokenize_zh(text)
            if not tokens:
                continue
            tf = {}
            for t in tokens:
                tf[t] = tf.get(t, 0) + 1
            docs.append(tf)
            doc_info.append({
                "id": row[1], "topic": row[2], "content": row[3],
                "confidence": row[4], "hit_count": row[5], "source_type": row[6],
            })
            rowids.append(row[0])
            for t in tf:
                _TFIDF_DF[t] = _TFIDF_DF.get(t, 0) + 1

    if not docs:
        _TFIDF_CACHE, _TFIDF_DOCS, _TFIDF_ROWIDS, _TFIDF_CACHE_DIRTY = ([], {}, {}), [], [], False
        return [], {}, {}

    # Compute IDF（单遍 df，替代 O(词表 × 文档数)）
    N = len(docs)
    idf = {}
    for token, df in _TFIDF_DF.items():
        idf[token] = math.log((N + 1) / (df + 1)) + 1

    # Build document vectors (sparse as dict)
    doc_vectors = []
    for doc in docs:
        vec = {}
        norm = 0
        for token, tf in doc.items():
            w = tf * idf[token]
            vec[token] = w
            norm += w * w
        norm = math.sqrt(norm) if norm > 0 else 1
        # Normalize
        doc_vectors.append({k: v / norm for k, v in vec.items()})

    _TFIDF_CACHE = (doc_info, doc_vectors, idf)
    # 状态必须写回全局：增量路径与"命中自校验"都靠它们（首版漏了这一步 →
    # 增量永不触发、自校验形同虚设，deleted 行会一直留在结果里）。
    _TFIDF_DOCS = docs
    _TFIDF_ROWIDS = rowids
    _TFIDF_CACHE_DIRTY = False
    return doc_info, doc_vectors, idf


# 记忆注入的相关性下限：低于它宁可不注入（09-21）。实测无关问题 0.13~0.15、
# 相关问题 0.40~0.53，取 0.28 作默认；LATIAO_MEMORY_MIN_SCORE 可调。
_MIN_MEMORY_SCORE = float(os.environ.get("LATIAO_MEMORY_MIN_SCORE", "0.28"))


def _tfidf_search(query: str, limit: int = 5) -> list[dict]:
    """Search learnings using TF-IDF cosine similarity."""
    doc_info, doc_vectors, idf = _build_tfidf_index()
    if not doc_vectors:
        return []

    # Build query vector
    query_tokens = _tokenize_zh(query)
    if not query_tokens:
        return []

    q_tf = {}
    for t in query_tokens:
        q_tf[t] = q_tf.get(t, 0) + 1

    q_vec = {}
    q_norm = 0
    for token, tf in q_tf.items():
        w = tf * idf.get(token, 1.0)
        q_vec[token] = w
        q_norm += w * w
    q_norm = math.sqrt(q_norm) if q_norm > 0 else 1
    q_vec = {k: v / q_norm for k, v in q_vec.items()}

    # Score all documents
    #     09-21：改成**按原始余弦排序**。旧实现用 `cosine × confidence × (1+hit×0.05)`
    #     当分数 → 热门/高置信条目（全是市场分析）永远排前，无关问题也会拉出 5 条
    #     市场记忆 → 用户反馈"我说什么他都去找股市、新会话还记得"。实测原始余弦
    #     可分：无关问题 0.13~0.15，相关问题 0.40~0.53。
    scores = []
    for i, dv in enumerate(doc_vectors):
        dot = 0
        for token, w in q_vec.items():
            if token in dv:
                dot += w * dv[token]
        scores.append((dot, i))

    scores.sort(reverse=True)

    results = []
    for score, idx in scores[:limit]:
        if score < _MIN_MEMORY_SCORE:      # 相关性下限（低于它宁可不注入）
            continue
        item = dict(doc_info[idx])
        item["score"] = round(float(score), 4)
        results.append(item)

    return results


# ── Original functions ──

def _learning_is_garbage(topic: str, content: str) -> bool:
    """知识垃圾判定：思考碎片/半句话/工具日志碎片/强制的工具流程（09-07 清理 60 条中
    过半为此类——refine 在思考未关时把 <think> 碎片当知识存了）。

    09-21 加强：工具名主题（任意工具，不只白名单几个）、抽取过程自身输出
    （"The user wants me to extract a reusable knowledge…"、"知识提炼"）、
    以及"先用 mx_query 查大盘…"这类**强制的工具流程**——最后这类会让模型
    "无论用户说什么都去找股市"（用户实测反馈）。
    """
    blob = f"{topic} {content}"
    if _is_junk_learning(topic, content):
        return True
    if "<think>" in blob or "＜think＞" in blob:
        return True
    if content.strip().lower().startswith(("the user wants", "the user is asking")):
        return True
    if topic.startswith(("read_file:", "list_dir:", "tavily_search:", "mx_query:",
                         "run_cmd:", "search_files:", "write_file:")):
        return True  # 工具名前缀 = 工具日志碎片，不是可复用知识
    return False


# ── 记忆质量过滤（09-20/21）──────────────────────────────────────────
# 事故：自学习把"工具执行产物"和"抽取过程自己的输出"也当知识存了 456 条，
# 其中大量是市场分析定时任务的遗留 → 每条请求（含全新会话）都注入"先用 mx_query
# 查大盘…"，用户表现为"我说什么他都去找股市、新建聊天框还记得"。
_JUNK_TOPIC_RE = re.compile(
    r"^(mx_query|ak_finance|tavily_search|web_search|bing_search|dokobot_search|"
    r"headless_read|read_file|write_file|list_dir|search_files|run_cmd|use_skill|"
    r"create_skill|create_cron|delegate_task|open_app|open_folder|screen_capture|cron)\b",
    re.IGNORECASE)
_JUNK_TEXT_RE = re.compile(
    r"<think>|</think>|The user wants me to|知识提炼|reusable knowledge or finding|"
    r"\*\*分析请求|强制流程|必须用 mx_query|必须先用|先用 mx_query|"
    # 自我污染：应用自己注入的尾部块被自学习当成知识存回来（09-21 实测）
    r"用户本轮的要求|不是用户本轮的|【背景资料】|【参考信息】|【系统提示】|【参考知识】|"
    # 历史反思里的注入残留（09-23 回填时实测：一条反思的文本本身是我们自己的
    # "结构化错误文本回填上下文"这类注入说明，被当成工具失败经验存了进来）
    r"回填上下文|结构化错误文本的|错误即结果",
    re.IGNORECASE)


def _is_junk_learning(topic, content) -> bool:
    """判断一条"学习/偏好"是不是噪声（工具产物、抽取过程输出、强制的工具流程）。

    这些不是用户知识，注入它们会劫持每一轮（09-21 用户实测反馈）。
    """
    t = str(topic or "").strip()
    c = str(content or "")
    if _JUNK_TOPIC_RE.match(t):
        return True
    if _JUNK_TEXT_RE.search(t) or _JUNK_TEXT_RE.search(c[:400]):
        return True
    return False


def _retrieve_relevant_learnings(query: str, limit: int = MAX_LEARNINGS_INJECT) -> list[dict]:
    """Search past learnings using TF-IDF semantic similarity.
    Falls back to FTS5/LIKE if TF-IDF returns nothing."""
    # Priority 1: TF-IDF semantic search (handles Chinese well)
    results = [r for r in _tfidf_search(query, limit)
               if not _learning_is_garbage(r.get("topic", ""), r.get("content", ""))]

    if results:
        try:
            conn = _get_db()
            for r in results:
                conn.execute("UPDATE learnings SET hit_count = hit_count + 1 WHERE id = ?",
                             (r.get("id"),))
            conn.commit()
        except Exception:
            pass  # hit 计数失败不影响检索
        return results

    # Fallback: FTS5 + LIKE for backward compatibility
    try:
        conn = _get_db()
        # FTS5 search
        safe_query = " ".join(
            w for w in re.findall(r'[一-鿿\w]+', query.lower())
            if len(w) > 1
        )
        if safe_query:
            try:
                rows = conn.execute(
                    """SELECT l.id, l.topic, l.content, l.confidence, l.hit_count, l.source_type
                       FROM learnings l
                       JOIN learnings_fts f ON l.rowid = f.rowid
                       WHERE learnings_fts MATCH ?
                       ORDER BY l.confidence * (1.0 + l.hit_count * 0.1) DESC
                       LIMIT ?""",
                    (safe_query, limit),
                ).fetchall()
                for row in rows:
                    results.append({
                        "id": row[0], "topic": row[1], "content": row[2],
                        "confidence": row[3], "hit_count": row[4], "source_type": row[5],
                    })
            except Exception:
                pass  # FTS5 query syntax errors are non-fatal

        # If FTS5 returned nothing, fall back to LIKE for better CJK matching
        if not results and query.strip():
            like_q = f"%{query.strip()}%"
            rows = conn.execute(
                """SELECT id, topic, content, confidence, hit_count, source_type
                   FROM learnings
                   WHERE topic LIKE ? OR content LIKE ?
                   ORDER BY confidence DESC
                   LIMIT ?""",
                (like_q, like_q, limit),
            ).fetchall()
            for row in rows:
                results.append({
                    "id": row[0], "topic": row[1], "content": row[2],
                    "confidence": row[3], "hit_count": row[4], "source_type": row[5],
                })

        # If still nothing, fall back to recent high-confidence learnings
        if not results:
            rows = conn.execute(
                """SELECT id, topic, content, confidence, hit_count, source_type
                   FROM learnings
                   WHERE confidence >= ?
                   ORDER BY updated_at DESC
                   LIMIT ?""",
                (LEARNING_CONFIDENCE_THRESHOLD, limit),
            ).fetchall()
            for row in rows:
                results.append({
                    "id": row[0], "topic": row[1], "content": row[2],
                    "confidence": row[3], "hit_count": row[4], "source_type": row[5],
                })

        # 09-21：回退链（FTS5 / LIKE）也要过质量与相关性门槛。
        # 旧实现在"什么都没匹配上"时**直接返回最近的高置信度学习** ✗ —— 那等于
        # 无论用户问什么，都把（以市场分析为主的）记忆塞进去；叠加 hit_count 放大，
        # 用户看到的是"我说什么他都去找股市、新会话也记得"。
        results = [r for r in results
                   if not _learning_is_garbage(r.get("topic"), r.get("content"))]
        _q_tokens = [t for t in _tokenize_zh(query) if len(t) >= 2]
        if _q_tokens:
            results = [r for r in results
                       if any(t in f"{r.get('topic','')} {r.get('content','')}"
                              for t in _q_tokens)]
        else:
            results = []
        results = results[:limit]

        # Bump hit_count for retrieved learnings (reinforcement)
        if results:
            ids = [r["id"] for r in results]
            with _db_write_lock:
                conn.executemany(
                    "UPDATE learnings SET hit_count = hit_count + 1, updated_at = ? WHERE id = ?",
                    [(datetime.now().isoformat(), lid) for lid in ids],
                )
                conn.commit()
            # 同步内存索引里的 hit_count，保持 boost 与 DB 一致（避免置脏全量重建）
            if _TFIDF_CACHE is not None:
                id_set = set(ids)
                for d in _TFIDF_CACHE[0]:
                    if d["id"] in id_set:
                        d["hit_count"] += 1


        # 统一垃圾过滤（思考碎片/工具日志碎片/半句话——09-07 清理事故）
        results = [r for r in results
                   if not _learning_is_garbage(str(r.get("topic", "")), str(r.get("content", "")))]
        return results
    except Exception:
        return []


def _store_learning(session_id: str, topic: str, content: str, confidence: float = 0.5, source_type: str = "extracted"):
    """Store a new learning. If a similar topic already exists, update confidence."""
    _mark_tfidf_dirty()
    try:
        conn = _get_db()
        now = datetime.now().isoformat()

        with _db_write_lock:
            # Check for existing similar topic
            existing = conn.execute(
                "SELECT id, confidence FROM learnings WHERE topic = ? LIMIT 1",
                (topic,),
            ).fetchone()

            if existing:
                # Boost confidence of existing learning (up to 1.0)
                new_conf = min(1.0, existing[1] + confidence * 0.3)
                conn.execute(
                    "UPDATE learnings SET confidence = ?, updated_at = ? WHERE id = ?",
                    (new_conf, now, existing[0]),
                )
            else:
                lid = str(uuid.uuid4())
                conn.execute(
                    """INSERT INTO learnings(id, session_id, topic, content, confidence, source_type, created_at, updated_at)
                       VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                    (lid, session_id, topic, content, confidence, source_type, now, now),
                )
            conn.commit()
    except Exception:
        logger.warning("Failed to store learning in memory DB", exc_info=True)


# ── 偏好键的稳定化（2026-09-23，审查①）────────────────────────────
# 旧键 = 命中文本前 30 字的归一化。同一偏好换个说法就变成新键 → 永不加成 →
# 到不了 0.7 的无条件注入档（真库实测：preferences 只有 1 行、置信度 0.6；
# 用户"说过的话记不住"）。改成按**意图**归一：同一意图的重复表达累加到同一行
# （0.6 → 0.78），值取最新措辞。
# 档位（0.6 写入 / 0.7 注入）与两道守卫都不动——依然要求"重复表达过"才进无条件
# 注入档，不重开 09-21 修过的单次发言劫持；且同一条消息里同一意图只记一次
# （否则"我希望以后回复用中文"同时命中两条偏好模式 → 一次发言就到 0.78）。
_PREF_INTENT_PATTERNS = (
    ("lang", re.compile(r"中文|国语|英文|英语|日文|日语|language|汉字")),
    ("tone", re.compile(r"语气|口吻|腔调|风格|正式|随意|温柔|简洁|直接|暧昧|露骨|幽默|严肃")),
    ("address", re.compile(r"叫我|别叫|称呼")),
    ("length", re.compile(r"简短|详细|长一点|短一点|多少字|字数|精简|啰嗦|太长")),
    ("format", re.compile(r"表格|分点|列点|代码块|结论先行|markdown|格式|排版")),
)
_PREF_OTHER_MERGE_OVERLAP = 0.85   # 其它类偏好：只并"几乎逐字重复"的（防噪声累积）


def _resolve_preference_key(matched_text: str) -> str:
    """给一条命中算稳定键：能识别意图就按意图；否则与已有偏好做近重复合并。"""
    low = (matched_text or "").lower()
    for intent, pat in _PREF_INTENT_PATTERNS:
        if pat.search(low):
            return f"intent:{intent}"
    base = re.sub(r'[^a-z0-9\u4e00-\u9fff]', '', matched_text[:30].lower())
    try:
        conn = _get_db()
        cand = set(_tokenize_zh(matched_text))
        if cand:
            for k, v in conn.execute("SELECT key, value FROM preferences"):
                if str(k).startswith("intent:"):
                    continue          # 意图键由模式表决定，不做模糊合并
                tv = set(_tokenize_zh(v or ""))
                if tv and len(cand & tv) / len(cand | tv) >= _PREF_OTHER_MERGE_OVERLAP:
                    return k
    except Exception:
        pass
    return base


def _store_preference(key: str, value: str, confidence: float = 0.5):
    """Store a learned user preference. Boosts confidence if already exists."""
    try:
        conn = _get_db()
        now = datetime.now().isoformat()
        with _db_write_lock:
            existing = conn.execute(
                "SELECT id, confidence FROM preferences WHERE key = ? LIMIT 1", (key,),
            ).fetchone()
            if existing:
                new_conf = min(1.0, existing[1] + confidence * 0.3)
                conn.execute(
                    "UPDATE preferences SET value = ?, confidence = ?, updated_at = ? WHERE id = ?",
                    (value, new_conf, now, existing[0]),
                )
            else:
                lid = str(uuid.uuid4())
                conn.execute(
                    """INSERT INTO preferences(id, key, value, confidence, source, created_at, updated_at)
                       VALUES(?, ?, ?, ?, 'inferred', ?, ?)""",
                    (lid, key, value, confidence, now, now),
                )
            conn.commit()
    except Exception:
        logger.warning("Failed to store preference in memory DB", exc_info=True)


def _retrieve_preferences() -> list[dict]:
    """Get all high-confidence learned preferences for context injection."""
    try:
        conn = _get_db()
        rows = conn.execute(
            "SELECT key, value, confidence FROM preferences WHERE confidence >= 0.4 ORDER BY confidence DESC"
        ).fetchall()
        # 09-21：排除工具流程类噪声（"先用 mx_query 查大盘…"这种被当成偏好的记录
        # 会让模型无论用户说什么都去查股市）
        return [{"key": r[0], "value": r[1], "confidence": r[2]} for r in rows
                if not _learning_is_garbage(r[0], r[1])]
    except Exception:
        return []


def _get_high_confidence_preferences() -> list[dict]:
    """Get high-confidence preferences (>= 0.7) for unconditional system prompt injection."""
    try:
        conn = _get_db()
        rows = conn.execute(
            "SELECT key, value, confidence FROM preferences WHERE confidence >= 0.7 ORDER BY confidence DESC"
        ).fetchall()
        # 09-21：这条是无条件注入系统提示的，更要挡噪声（工具流程/思考碎片）
        return [{"key": r[0], "value": r[1], "confidence": r[2]} for r in rows
                if not _learning_is_garbage(r[0], r[1])]
    except Exception:
        return []


def _record_reflection(session_id: str, tool_name: str, tool_args: dict, tool_result_summary: str, reflection: str, was_useful: bool):
    """Store a post-tool-call reflection, and promote real pitfalls to learnings.

    ⑧（2026-09-23 审查）：反思此前只拼进当轮工具结果，跨会话零复用——真库 969 条
    反思里"工具 mx_query 执行出错"出现 237 次，而 learnings 里一条都没有。现在
    真踩到的坑（was_useful=True，即 error/permission/missing/empty 四类）提升为
    learning：topic 带错误签名 → 不同的坑各自成条、同一个坑重复踩则靠 topic upsert
    累加置信度（真库 498 条错误反思 → 至多 81 个组合，量级可控）。
    """
    try:
        conn = _get_db()
        rid = str(uuid.uuid4())
        with _db_write_lock:
            conn.execute(
                """INSERT INTO reflections(id, session_id, tool_name, tool_args, tool_result_summary, reflection, was_useful, created_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                (rid, session_id, tool_name, json.dumps(tool_args, ensure_ascii=False),
                 tool_result_summary, reflection, 1 if was_useful else 0, datetime.now().isoformat()),
            )
            conn.commit()
        # 提升必须在 _db_write_lock **之外**：_store_learning 自己也要拿这把非可重入锁，
        # 在锁内调用会死锁——首版就是这么写的，测试直接卡死（别挪回去）。
        if was_useful and reflection:
            promote_reflection_to_learning(session_id, tool_name, tool_args,
                                           tool_result_summary, reflection)
    except Exception:
        logger.warning("Failed to store reflection in memory DB", exc_info=True)


def record_tool_reflection(session_id: str, tool_name: str, tool_args: dict, result: str) -> str:
    """工具执行后的反思链路唯一入口（2026-09-23，审查⑧）。

    三件事一起做：①按结果判定类别并生成反思文案（返回给调用方拼进本轮工具结果）；
    ②以**诚实**的 was_useful 落库（只有 error/permission/missing/empty 才算真踩到的坑，
    "输出较大"这类提示不是）；③把真踩到的坑提升为 learning，才能跨会话被检索复用。
    收成一个入口，是为了让"反思→知识"这条链可测、调用方也无法只做一半。
    """
    note = _quick_reflect(tool_name, result)
    if not note:
        return ""
    is_pitfall = _reflection_kind(tool_name, result) in REFLECTION_PITFALL_KINDS
    _record_reflection(session_id, tool_name, tool_args, (result or "")[:200], note, is_pitfall)
    return note


def reflection_learning_key(tool_name: str, tool_result_summary: str) -> tuple[str, str]:
    """（topic, 特征）——同一工具 + 同一错误特征 → 同一条 learning（topic upsert 累加）。"""
    sig = _error_signature(tool_result_summary or "") or ""
    topic = f"工具 {tool_name} 失败：{sig[:30]}" if sig else f"工具 {tool_name} 失败经验"
    return topic, sig


def promote_reflection_to_learning(session_id: str, tool_name: str, tool_args: dict,
                                   tool_result_summary: str, reflection: str) -> bool:
    """把一条"真踩到的坑"写成 learning（⑧ 的核心动作，实时路径与回填脚本共用）。

    共用一份实现是刻意的：回填脚本若自己抄一遍，两边的 topic/置信度/内容格式迟早
    漂移（拆模块那轮踩过同型的坑）。返回是否写入成功。
    """
    if not reflection:
        return False
    topic, sig = reflection_learning_key(tool_name, tool_result_summary)
    args_sig = ""
    _probe_content = f"{tool_name} 失败：{reflection}"
    if _is_junk_learning(topic, _probe_content):
        logger.debug("反思提升被噪声闸门拦下: %s", topic[:40])
        return False
    try:
        if isinstance(tool_args, dict) and tool_args:
            k = sorted(tool_args)[0]
            args_sig = f"{k}={str(tool_args[k])[:40]}"
    except Exception:
        args_sig = ""
    content = f"{tool_name}({args_sig}) 失败：{reflection}"
    if sig and sig not in content:
        content += f"｜特征：{sig}"
    try:
        _store_learning(session_id, topic, content[:200], 0.6, source_type="reflection")
        return True
    except Exception:
        logger.debug("reflection→learning promote failed", exc_info=True)
        return False


async def _refine_learnings(tool_name: str, args: dict, result: str, session_id: str):
    """After tool execution, ask LLM to extract a reusable learning in 1-2 sentences.
    Runs as fire-and-forget background task so it doesn't slow down the agent loop.
    Prioritizes cloud LLM (better quality), falls back to local model."""
    if len(result) < 20 or result.startswith("Error") or result.startswith("⛔"):
        return  # Don't learn from errors or empty results
    try:
        prompt = (
            "从以下工具执行结果中提炼一条可复用的知识或发现，用一句中文总结（不超过50字），"
            "聚焦于项目结构、代码模式、配置习惯或用户偏好。\n\n"
            f"工具: {tool_name}\n"
            f"参数: {json.dumps(args, ensure_ascii=False)[:200]}\n"
            f"结果摘要: {result[:800]}\n\n"
            "总结:"
        )
        # Try cloud LLM first (best quality), fall back to local model
        # Lazy import to avoid circular dependency
        import main
        cloud_config = main._last_cloud_config.get()
        protocol, api_url, headers, is_local = await main._resolve_api_target(cloud_config)
        if not api_url:
            return
        # Prefer cloud model for refinement (SUBAGENT_MODEL may be a local 12B)
        refine_model = SUBAGENT_MODEL
        if cloud_config and cloud_config.get("endpoint"):
            refine_model = cloud_config.get("model", SUBAGENT_MODEL)
        async with httpx.AsyncClient(timeout=httpx.Timeout(15)) as client:
            # 本地 llama.cpp 并发请求会崩溃 -> 走 main 的串行锁
            async with main._local_llm_serialized(api_url):
                r = await client.post(api_url, json={
                "model": refine_model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 150,
                "temperature": 0.3,
                "stream": False,
                # 推理模型关思考：80/150 token 全进 <think> 会把思考碎片
                # 当"知识"存库（09-07 清理出多条 <think> 垃圾，置信度 1.0）
                "chat_template_kwargs": {"enable_thinking": False},
            }, headers=headers)
            if r.status_code == 200:
                data = r.json()
                summary = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                _bad = summary and ("<think>" in summary or "＜think＞" in summary
                                    or summary.lower().startswith("the user wants")
                                    or summary.strip().startswith("</"))
                if _bad:
                    logger.info("Learning refined: 丢弃思考碎片/半句话: %r", summary[:60])
                    return
                if summary and len(summary) > 5:
                    # Dedup: skip if nearly identical to existing learnings
                    if not _is_duplicate_learning(summary):
                        # 写入 learnings 表（而不是 reflections 表），
                        # _retrieve_relevant_learnings 才能检索到
                        _store_learning(session_id, f"{tool_name}: {summary[:20]}", summary,
                                        confidence=0.6, source_type="refined")
                        logger.info("Learning refined: %s", summary[:100])
    except Exception:
        pass  # Fire-and-forget — never block the agent loop


def _is_duplicate_learning(summary: str, threshold: float = 0.7) -> bool:
    """Check if a learning summary is nearly identical to an existing one.

    2026-09-23 修正：此前用 `text.lower().split()`（按空格分词）算 Jaccard——
    中文句子没有空格，整句就是一个 token，重叠率非 0 即 1，阈值 0.7 形同虚设。
    真库影子评估（341 条 learnings，与"最近 20 条"逐一比）：
      旧实现：中位重叠 0.00，判重 **0 条**（完全失效）
      改用本模块的 _tokenize_zh（字符+二字组）：中位 0.17，>0.7 判重 14 条（4.1%）
    抽查那 14 条都是真正的近重复（同一句被换个措辞再抽一次）。阈值 0.7 保持不变。
    """
    try:
        conn = _get_db()
        rows = conn.execute(
            "SELECT content FROM learnings ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
        summary_tokens = set(_tokenize_zh(summary))
        if not summary_tokens:
            return False
        for (existing,) in rows:
            existing_tokens = set(_tokenize_zh(existing or ""))
            if not existing_tokens:
                continue
            overlap = len(summary_tokens & existing_tokens) / len(summary_tokens | existing_tokens)
            if overlap > threshold:
                return True
        return False
    except Exception:
        return False  # On error, allow the learning


# ── Auto-Skill Generation (MUSE-Autoskill inspired) ──



async def _maybe_generate_skill(tool_name: str, args: dict, result: str):
    """Auto-generate a SKILL.md when the same tool succeeds repeatedly.
    Inspired by MUSE-Autoskill: Agent self-evolves by creating reusable skills."""
    import main  # lazy: 本地请求串行锁
    # Only track read_file for skill generation (most reusable pattern)
    if tool_name not in ("read_file", "write_file", "run_cmd"):
        return
    # Count consecutive successes
    is_success = not (result.startswith("Error") or result.startswith("错误") or result.startswith("⛔"))
    if not is_success:
        _skill_gen_tracker[tool_name] = 0
        return

    count = _skill_gen_tracker.get(tool_name, 0) + 1
    _skill_gen_tracker[tool_name] = count
    if count < _SKILL_GENERATION_THRESHOLD:
        return

    # Generate skill from accumulated learnings about this tool pattern
    _skill_gen_tracker[tool_name] = 0  # Reset counter
    try:
        conn = _get_db()
        rows = conn.execute(
            "SELECT topic, content, confidence FROM learnings WHERE content LIKE ? AND confidence >= 0.5 ORDER BY created_at DESC LIMIT 5",
            (f"%{tool_name}%",),
        ).fetchall()
        if not rows:
            return

        skill_name = f"{tool_name}-patterns"

        # Try to use LLM to synthesize a coherent skill document
        skill_content = None
        try:
            learnings_text = "\n".join([f"- [{r[1]}] {r[2]}" for r in rows])
            prompt = (
                "你是一个技能文档生成器。根据以下 Agent 从实际使用中学到的经验，"
                "生成一个结构清晰的技能文档（SKILL.md）。要求：\n"
                "1. 提取通用的操作模式，不要照搬具体例子\n"
                f"2. 用中文撰写，200-500 字\n"
                f"3. 包含标题、描述、使用场景、注意事项\n\n"
                f"原始学习记录:\n{learnings_text}\n\n"
                f"技能文档 (SKILL.md):"
            )
            async with httpx.AsyncClient(timeout=httpx.Timeout(30)) as client:
                # 本地 llama.cpp 并发请求会崩溃 -> 走 main 的串行锁
                async with main._local_llm_serialized(LM_STUDIO_URL):
                    r = await client.post(
                    LM_STUDIO_URL,
                    json={
                        "model": SUBAGENT_MODEL,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 500,
                        "temperature": 0.4,
                        "stream": False,
                    },
                )
                if r.status_code == 200:
                    data = r.json()
                    llm_output = data["choices"][0]["message"]["content"].strip()
                    if len(llm_output) > 50:
                        skill_content = llm_output
                        logger.info("LLM-synthesized skill: %s", skill_name)
        except Exception:
            logger.info("LLM synthesis unavailable, using raw concatenation for skill: %s", skill_name)

        # Fallback: build skill from raw learnings
        if not skill_content:
            skill_content = f"# {tool_name} 使用模式\n\n"
            skill_content += f"自动生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
            skill_content += "## 已知模式\n\n"
            for topic, content, conf in rows:
                bar = "\u2588" * int(conf * 5) + "\u2591" * (5 - int(conf * 5))
                skill_content += f"- **{topic}**: {content} (置信度: {bar})\n"
            skill_content += f"\n## 注意事项\n\n- 此技能由 Agent 自动生成，基于 {len(rows)} 次成功调用\n"
            skill_content += "- 使用前请确认适用场景\n"

        # Write to user skills directory（统一能力模型：表为事实源，文件为持久载体）
        skill_key = re.sub(r'[^a-z0-9-]', '', skill_name.lower().replace(" ", "-"))[:40]
        filepath = USER_SKILLS_DIR / f"{skill_key}.md"
        if not filepath.exists():
            USER_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
            filepath.write_text(skill_content, encoding="utf-8")
            logger.info("Auto-generated skill: %s (%d learnings)", skill_key, len(rows))
            # 重新同步能力表（函数内 lazy import 避免循环依赖）
            import capability_registry
            capability_registry.sync_skills()
    except Exception:
        logger.warning("Auto-skill generation failed for %s", tool_name, exc_info=True)


def _get_recent_learnings(limit: int = 5) -> list[str]:
    """Get the most recent learning summaries for cross-session context injection."""
    learnings = []
    try:
        db = _get_db()
        rows = db.execute(
            "SELECT topic, content FROM learnings_fts ORDER BY rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
        for topic, content in rows:
            if topic and content and len(content) > 10:
                learnings.append(f"- {topic}: {content[:200]}")
    except Exception:
        pass
    return learnings


def get_recent_learnings_for_ui(limit: int = 8) -> list[dict]:
    """知识库面板专用：返回对象格式的最近知识（topic/content/confidence）。

    09-07 NaN 事故：心跳把 _get_recent_learnings 的【字符串数组】直接给了
    前端，UI 取 l.topic/l.confidence 全是 undefined → "📝 : NaN%"。"""
    out: list[dict] = []
    try:
        db = _get_db()
        rows = db.execute(
            "SELECT topic, content, confidence FROM learnings "
            "WHERE length(content) > 10 "
            "ORDER BY confidence DESC, updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        for topic, content, confidence in rows:
            out.append({
                "topic": (topic or "知识")[:60],
                "content": (content or "")[:200],
                "confidence": float(confidence) if confidence is not None else 0.5,
            })
    except Exception:
        logger.debug("get_recent_learnings_for_ui failed", exc_info=True)
    return out


# ── Heuristic knowledge extraction from conversation ──

_KNOWLEDGE_PATTERNS = [
    # ── Corrections ──
    # Direct correction: "不对，应该用X", "错了，是Y", "不是这样的"
    (r"(?:不对|错了|不是|不要|别|更正|纠正|你搞错了|理解错了|说错了|看错了).{0,30}(?:是|要|请|必须|应该|用)[^\n]{5,120}", "correction", 0.8),
    # User says "别提X了/Y不对" etc
    (r"(?:不对|错了|不是|不要|别|更正|纠正|搞错|理解错).{0,30}(?:因为|理由|原因|其实|实际上)[^\n]{10,120}", "correction", 0.75),

    # ── Facts ──
    (r"(?:实际上?|其实是?|事实上|事实是|真实情况|本质[上是]?|说白了|简单说|注意|重要的?|关键[是点]?|核心[是点]?|根本原因|原因[是在])[^\n]{10,150}", "fact", 0.6),
    (r"(?:这意味着|也就是说|换句话说|本质上|具体来说)[^\n]{10,120}", "fact", 0.5),

    # ── Preferences ──
    # 09-21：原来用字符类 `我[更喜欢想要偏好希望中意]`，裸"我要"即命中 →
    # "我要你…"这类整句发言被存成 0.7 偏好并**无条件注入**，模型此后每轮都照它走
    # （用户实测：一句露骨要求被持久化后，助手每轮都拒答，换新会话也无效）。
    # 改成必须出现完整愿望词（想要/希望/喜欢…），不再收裸"我要"。
    (r"(?:我(?:更|最|比较|挺|很)?(?:喜欢|想要|希望|偏好|中意)|倾向于|习惯|"
     r"我不(?:想要|喜欢)|我更(?:希望|喜欢|想要|倾向)|能不能(?:不要|别)|"
     r"最好(?:是|不要|别)|不喜欢)[^\n]{5,100}", "preference", 0.7),
    # User gives behavioral instruction
    (r"(?:以后|接下来|从现在开始|请[你]?[不要要]).{0,30}(?:回复|回答|说话|做事)[^\n]{5,80}", "preference", 0.75),

    # ── Technical ──
    (r"(?:这个项目|项目[中里]|这里|代码[中里]|API|接口|函数|类[名型]|变量|参数|模块|包|库|框架)[^\n]{10,120}(?:是|用|在|需要|可以|叫做|位于|指向|引用)[^\n]{5,60}", "technical", 0.5),
    (r"(?:运行[在于]|部署[在于]|监听[在]|安装在|版本[是为号]|依赖[了于]|配置[在于成])[^\n]{5,100}", "technical", 0.55),
    (r"(?:技术[栈选]|开发环境|生产环境|配置项|环境变量|依赖项)[^\n]{10,100}", "technical", 0.5),

    # ── Structure ──
    (r"(?:项目结构|目录结构|文件夹结构|代码[结构组织]|文件[结构位置]|目录[树层级])[^\n]{10,120}", "structure", 0.55),
    (r"(?:代码在|入口[文件点]|配置[文件路径]|源文件|主[文件]?|模块[在的]?)[^\n]{10,100}", "structure", 0.5),

    # ── Reflection ──
    (r"(?:学到[了]?|发现|注意到|观察到|意识[到]?)[^\n]{10,100}", "reflection", 0.5),
    (r"(?:这个[思路方案方法做法]挺好|这个[思路方案]不错|这样更好|更好的方式|最佳实践|更好的办法)[^\n]{5,80}", "reflection", 0.45),
]


# 命令式发言（对助手下的指令，不是对助手的长期设定）
_DEMAND_RE = re.compile(r"^(?:我(?:要你|想要你|命令你|要求你|要)|你给我|你必须|你去|帮我找|帮我写)")
# 露骨内容：可以聊，但不该被持久化成"每轮必遵守的偏好"
_EXPLICIT_RE = re.compile(r"操逼|做爱|性交|裸照|色情|淫|fuck|blowjob|nsfw", re.I)


# 我们自己注入到消息里的标记/话术：学习抽取必须先剥掉，否则会把 Latiao 自己写的话
# 当成"用户偏好"学走（09-21 实测：库里存进了「…请直接回应这一句」这种尾部提示语）。
_INJECTED_MARKERS = (
    "【背景资料（不是用户的要求）】",
    "【系统提示】", "【参考信息】", "【参考知识】",
    "## 上次会话进展（最近记录）",
    "以下是 AI 从过去交互学到的相关知识：",
    "以下是用户的高置信度偏好（每次对话都必须遵守）：",
    "（当前时间：", "请直接回应这一句。",
)
_INJECTED_TAIL_RE = re.compile(r"(?:⚠️\s*以上只是历史背景[^\n]*|"
                               r"用户本轮说的是：「[^」]*」——[^\n]*|"
                               r"以上为历史记录，仅供参考[^\n]*|"
                               r"（当前时间：[^)）]*[)）])")


def _strip_injected_notes(text: str) -> str:
    """剥掉 Latiao 自己注入的提示语，只留用户真正说的话。"""
    t = text or ""
    if not t:
        return ""
    for marker in _INJECTED_MARKERS:
        idx = t.find(marker)
        if idx > 0:
            t = t[:idx]                      # 尾部注入一律在用户原话之后 → 从标记处截断
    t = _INJECTED_TAIL_RE.sub("", t)
    # 同句被重复拼接（"XYXY"）时收敛为一份
    half = len(t) // 2
    if half >= 8 and t[:half] == t[half:]:
        t = t[:half]
    return t.strip()


def _is_unusable_preference(text: str, message: str) -> bool:
    """这条命中不该被存成"用户偏好"（偏好里 ≥0.7 的那批会无条件注入系统提示）。

    09-21 事故：用户一句整话被存成 0.7 偏好 → 模型此后每轮都照它走，表现为永久
    拒答。判据不看长度阈值，只看两条可解释的信号：
    ①命中文本含露骨内容（可以聊，但不该变成每轮强制设定）；
    ②**本条消息本身**是命令式发言（"我要你…"/"你给我…"）且命中文本占了它一半以上
      —— 那是本轮的诉求，不是长期偏好。注意要同时看"消息开头"，因为命中片段可能
      从"以后回复…"这种中段开始（实测漏过一次）。
    """
    t = re.sub(r"\s+", "", text or "")
    m = re.sub(r"\s+", "", message or "")
    if not t:
        return False
    if _EXPLICIT_RE.search(t):
        return True
    if len(t) >= max(8, int(len(m) * 0.5)) and (_DEMAND_RE.match(t) or _DEMAND_RE.match(m)):
        return True
    return False


def _extract_learnings_heuristic(user_text: str, session_id: str) -> int:
    """Simple pattern-based knowledge extraction from user messages.
    Falls back to this when LLM-based extraction is unavailable.
    Returns number of learnings extracted."""
    # 先剥掉我们自己注入的提示语/尾部话术：否则会把 Latiao 自己写的话当用户偏好学走
    # （09-21 实测库里出现了「…请直接回应这一句」这类注入文本，且同一句被重复拼接）。
    user_text = _strip_injected_notes(user_text)
    if not user_text:
        return 0
    count = 0
    _seen_pref_keys: set[str] = set()      # 同一条消息里同一意图只记一次（见 _resolve_preference_key 注释）
    for pattern, source_type, confidence in _KNOWLEDGE_PATTERNS:
        for match in re.finditer(pattern, user_text):
            matched_text = match.group(0).strip()
            if len(matched_text) < 8:
                continue
            # Derive topic from first few chars
            topic = matched_text[:30].strip().rstrip("，。,.!！?？")
            _store_learning(session_id, topic, matched_text, confidence, source_type)
            # Store as preference if it's a preference pattern
            if source_type == "preference":
                # 09-21 两道守卫：命令式整句/露骨内容不是偏好；工具流程碎片同样不算。
                # 另外单次抽取只给 0.6（低于无条件注入的 0.7）——重复表达才会被
                # 加成到 0.7 以上，一次性发言进不了"每轮必注入"那一档。
                if (_is_unusable_preference(matched_text, user_text)
                        or _is_junk_learning(topic, matched_text)):
                    logger.info("偏好守卫：跳过疑似整句发言/噪声的偏好记录（%d 字）",
                                len(matched_text))
                    continue
                pref_key = _resolve_preference_key(matched_text)
                if pref_key in _seen_pref_keys:
                    logger.debug("偏好：同一消息内重复命中同一键 %s，跳过", pref_key)
                    continue
                _seen_pref_keys.add(pref_key)
                _store_preference(pref_key, matched_text, min(confidence, 0.6))
            count += 1
    return count
