"""Memory System — self-learning, TF-IDF, preferences, and skill generation."""
import logging
import asyncio
import json
import math
import os
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from config import LM_STUDIO_URL, SUBAGENT_MODEL, SKILLS_DIR
from db import _get_db
from db import _get_db

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



def _tokenize_zh(text: str) -> list[str]:
    """Simple Chinese tokenizer: bigram characters + whole words.
    E.g. '项目结构' → ['项目', '目结', '结构', '项目结构']"""
    # Extract Chinese chars and alphanumeric tokens
    import unicodedata
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


def _quick_reflect(tool_name: str, result: str) -> str:
    """Quick heuristic reflection on tool execution result.
    Returns a reflection note or empty string."""
    result_lower = result.lower()
    # Error detection — covers both English and Chinese tool error messages
    is_error = (
        "error" in result_lower or "错误" in result
        or "failed" in result_lower or "失败" in result
        or "traceback" in result_lower
        or "denied" in result_lower
    )
    if is_error:
        return f"工具 {tool_name} 执行出错，可能需要重试或调整参数"
    if "permission denied" in result_lower:
        return "权限不足，建议检查文件/目录权限"
    if "not found" in result_lower or "不存在" in result:
        return "目标不存在，可能需要先确认路径或创建前置资源"
    if len(result.strip()) < 5:
        return "工具返回为空，可能参数不正确或目标无内容"
    if len(result) > 5000:
        return f"输出较大({len(result)}字符)，后续可能需要聚焦关键部分"
    return ""  # Everything looks fine, no reflection needed


# Lazy import helpers to avoid circular dependency with main.py

def _get_db():
    """Get database connection from db module (no circular dependency)."""
    from db import _get_db as _get_db_inner
    return _get_db_inner()

def _load_skills():
    """Load skills via main module (lazy import)."""
    import main
    return main._load_skills()


# ── Tokenization (already in memory.py from previous extraction) ──


# ═══════════════════════════════════════════════════════
#  Lightweight Embedding Search (no external deps)
#  Uses character n-gram TF-IDF for Chinese semantic similarity.
#  For < 1000 learnings this is fast enough — ~5ms per query.
# ═══════════════════════════════════════════════════════

def _build_tfidf_index():
    """Build an in-memory TF-IDF index from all learnings. Uses cache to avoid rebuilding every call."""
    global _TFIDF_CACHE, _TFIDF_CACHE_DIRTY
    if not _TFIDF_CACHE_DIRTY and _TFIDF_CACHE is not None:
        return _TFIDF_CACHE
    try:
        conn = _get_db()
        rows = conn.execute(
            "SELECT id, topic, content, confidence, hit_count, source_type FROM learnings"
        ).fetchall()
    except Exception:
        return [], {}, {}
    
    if not rows:
        return [], {}, {}
    
    # Build vocabulary and document vectors
    docs = []
    doc_info = []
    all_tokens = set()
    
    for row in rows:
        text = f"{row[1]} {row[2]}"
        tokens = _tokenize_zh(text)
        if not tokens:
            continue
        # Count term frequencies
        tf = {}
        for t in tokens:
            tf[t] = tf.get(t, 0) + 1
        docs.append(tf)
        doc_info.append({
            "id": row[0], "topic": row[1], "content": row[2],
            "confidence": row[3], "hit_count": row[4], "source_type": row[5],
        })
        all_tokens.update(tf.keys())
    
    # Compute IDF
    import math
    N = len(docs)
    idf = {}
    for token in all_tokens:
        df = sum(1 for d in docs if token in d)
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
    _TFIDF_CACHE_DIRTY = False
    return doc_info, doc_vectors, idf


def _tfidf_search(query: str, limit: int = 5) -> list[dict]:
    """Search learnings using TF-IDF cosine similarity."""
    doc_info, doc_vectors, idf = _build_tfidf_index()
    if not doc_vectors:
        return []
    
    # Build query vector
    query_tokens = _tokenize_zh(query)
    if not query_tokens:
        return []
    
    import math
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
    scores = []
    for i, dv in enumerate(doc_vectors):
        # Cosine similarity
        dot = 0
        for token, w in q_vec.items():
            if token in dv:
                dot += w * dv[token]
        # Boost by confidence and hit_count
        boost = doc_info[i]["confidence"] * (1.0 + doc_info[i]["hit_count"] * 0.05)
        scores.append((dot * boost, i))
    
    scores.sort(reverse=True)
    
    results = []
    for score, idx in scores[:limit]:
        if score < 0.01:  # Skip near-zero matches
            continue
        results.append(doc_info[idx])
    
    return results


# ── Original functions ──

def _retrieve_relevant_learnings(query: str, limit: int = MAX_LEARNINGS_INJECT) -> list[dict]:
    """Search past learnings using TF-IDF semantic similarity.
    Falls back to FTS5/LIKE if TF-IDF returns nothing."""
    # Priority 1: TF-IDF semantic search (handles Chinese well)
    results = _tfidf_search(query, limit)
    
    if results:
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

        # Bump hit_count for retrieved learnings (reinforcement)
        if results:
            ids = [r["id"] for r in results]
            with _db_write_lock:
                conn.executemany(
                    "UPDATE learnings SET hit_count = hit_count + 1, updated_at = ? WHERE id = ?",
                    [(datetime.now().isoformat(), lid) for lid in ids],
                )
                conn.commit()

        return results
    except Exception:
        return []


def _store_learning(session_id: str, topic: str, content: str, confidence: float = 0.5, source_type: str = "extracted"):
    """Store a new learning. If a similar topic already exists, update confidence."""
    global _TFIDF_CACHE_DIRTY
    _TFIDF_CACHE_DIRTY = True
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
        return [{"key": r[0], "value": r[1], "confidence": r[2]} for r in rows]
    except Exception:
        return []


def _get_high_confidence_preferences() -> list[dict]:
    """Get high-confidence preferences (>= 0.7) for unconditional system prompt injection."""
    try:
        conn = _get_db()
        rows = conn.execute(
            "SELECT key, value, confidence FROM preferences WHERE confidence >= 0.7 ORDER BY confidence DESC"
        ).fetchall()
        return [{"key": r[0], "value": r[1], "confidence": r[2]} for r in rows]
    except Exception:
        return []


def _record_reflection(session_id: str, tool_name: str, tool_args: dict, tool_result_summary: str, reflection: str, was_useful: bool):
    """Store a post-tool-call reflection."""
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
    except Exception:
        logger.warning("Failed to store reflection in memory DB", exc_info=True)


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
        protocol, api_url, headers, is_local = main._resolve_api_target(cloud_config)
        if not api_url:
            return
        # Prefer cloud model for refinement (SUBAGENT_MODEL may be a local 12B)
        refine_model = SUBAGENT_MODEL
        if cloud_config and cloud_config.get("endpoint"):
            refine_model = cloud_config.get("model", SUBAGENT_MODEL)
        async with httpx.AsyncClient(timeout=httpx.Timeout(15)) as client:
            r = await client.post(api_url, json={
                "model": refine_model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 80,
                "temperature": 0.3,
                "stream": False,
            }, headers=headers)
            if r.status_code == 200:
                data = r.json()
                summary = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                if summary and len(summary) > 5:
                    # Dedup: skip if nearly identical to existing learnings
                    if not _is_duplicate_learning(summary):
                        _record_reflection(session_id, tool_name, args, result[:200], summary, True)
                        logger.info("Learning refined: %s", summary[:100])
    except Exception:
        pass  # Fire-and-forget — never block the agent loop


def _is_duplicate_learning(summary: str, threshold: float = 0.7) -> bool:
    """Check if a learning summary is nearly identical to an existing one.
    Uses simple token overlap for speed (full embedding check would be overkill for <50 chars)."""
    try:
        conn = _get_db()
        rows = conn.execute(
            "SELECT content FROM learnings ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
        summary_tokens = set(summary.lower().split())
        if not summary_tokens:
            return False
        for (existing,) in rows:
            existing_tokens = set(existing.lower().split())
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
        
        # Write to skills directory
        skill_key = re.sub(r'[^a-z0-9-]', '', skill_name.lower().replace(" ", "-"))[:40]
        filepath = SKILLS_DIR / f"{skill_key}.md"
        if not filepath.exists():
            SKILLS_DIR.mkdir(parents=True, exist_ok=True)
            filepath.write_text(skill_content, encoding="utf-8")
            logger.info("Auto-generated skill: %s (%d learnings)", skill_key, len(rows))
            # Reload skills
            global _loaded_skills
            _loaded_skills = _load_skills()
    except Exception:
        logger.warning("Auto-skill generation failed for %s", tool_name, exc_info=True)
    except Exception:
        return False  # On error, allow the learning


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


def _build_learning_context(user_query: str) -> str:
    """Build a context string from relevant learnings + preferences to inject into system prompt."""
    parts = []

    # Get relevant learnings
    learnings = _retrieve_relevant_learnings(user_query)
    if learnings:
        lines = ["## 你从过去的交互中学到了:"]
        for item in learnings:
            confidence_bar = "█" * int(item["confidence"] * 5) + "░" * (5 - int(item["confidence"] * 5))
            lines.append(f"- [{item['topic']}] {item['content']} (置信度: {confidence_bar})")
        parts.append("\n".join(lines))

    # Get learned preferences
    prefs = _retrieve_preferences()
    if prefs:
        lines = ["## 用户偏好 (从历史交互中推断):"]
        for p in prefs:
            lines.append(f"- {p['key']}: {p['value']}")
        parts.append("\n".join(lines))

    return "\n\n".join(parts) if parts else ""


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
    (r"(?:我[更喜欢想要偏好希望中意]|倾向于|比较喜欢|习惯|我不[想要喜欢]|我更[希望喜欢想要倾向]|能不能[不要别]|最好[是不要别]|不喜欢)[^\n]{5,100}", "preference", 0.7),
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


def _extract_learnings_heuristic(user_text: str, session_id: str) -> int:
    """Simple pattern-based knowledge extraction from user messages.
    Falls back to this when LLM-based extraction is unavailable.
    Returns number of learnings extracted."""
    count = 0
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
                pref_key = re.sub(r'[^a-z0-9\u4e00-\u9fff]', '', matched_text[:30].lower())
                _store_preference(pref_key, matched_text, confidence)
            count += 1
    return count



async def _summarize_learning(raw_content: str) -> str:
    """Use LLM to compress a raw learning into a concise semantic summary (1-2 sentences)."""
    try:
        prompt = (
            "将以下知识片段压缩为一到两句中文摘要，只保留可操作的结论，去掉冗余细节。\n\n"
            f"原文: {raw_content[:500]}\n\n摘要:"
        )
        async with httpx.AsyncClient(timeout=httpx.Timeout(30)) as client:
            r = await client.post(
                LM_STUDIO_URL,
                json={
                    "model": SUBAGENT_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 120,
                    "temperature": 0.3,
                    "stream": False,
                },
                headers={"Content-Type": "application/json"},
            )
            if r.status_code == 200:
                data = r.json()
                summary = data["choices"][0]["message"]["content"].strip()
                return summary if summary else raw_content[:200]
            return raw_content[:200]
    except Exception:
        return raw_content[:200]  # Fall back to truncation
