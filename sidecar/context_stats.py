"""上下文用量统计：按类别统计"这一轮实际发出去的内容"的 token 用量与缓存命中率。

数据来源（实测结论，2026-09-18）：
  · token 计数：GGUF 用 llama-cpp-python 的 vocab_only 只加载词表（不占显存/内存大头，
    1~2 秒）做精确计数；MLX/带 tokenizer.json 的仓库用 HuggingFace tokenizers 精确计数；
    两者都拿不到时退回字符启发式（estimate_tokens）。
  · 缓存命中率：MLX 引擎（mlx_lm.server）在 usage.prompt_tokens_details.cached_tokens 里给；
    原生 llama.cpp（Windows / macOS 回退）在最后一块的 timings.cache_n / prompt_n 里给；
    云端 OpenAI 兼容端点用 usage.prompt_tokens_details.cached_tokens。
    ⚠️ macOS 上 GGUF 默认走的 python 引擎（llama_cpp.server）既不返回 usage 也没有 timings，
    该路径下缓存命中率显示为"未提供"而不是编一个数。

会话状态放内存（进程重启即清空），仅保留最近一轮的快照 + 最近 N 次缓存样本。
"""
import json
import logging
import re
import threading
import time
from collections import defaultdict

logger = logging.getLogger(__name__)

CACHE_SAMPLES = 20      # 缓存命中率滚动窗口
TEMPLATE_OVERHEAD = 30  # 聊天模板/特殊 token 的近似开销（llama.cpp 实测最小对话差 4 个）

# 类别键（前端按此渲染，顺序即展示顺序）
CATEGORIES = ("messages", "system_tools", "skills", "system_prompt", "mcp_tools", "other")

_sessions: dict[str, dict] = {}
_lock = threading.Lock()
_counters: dict[str, object] = {}   # model_path -> 计数函数
_counter_order: list[str] = []      # 简单 LRU（最多 2 个词表常驻）
_COUNTER_CACHE_MAX = 2


# ── token 计数 ─────────────────────────────────────────

def estimate_tokens(text: str) -> int:
    """字符启发式（仅在拿不到 tokenizer 时兜底，例如云端模型）。

    系数按 Qwen 系分词器实测校准（2026-09-18，样本见 tests/test_context_stats.py）：
    中文/英文/提示词类样本误差 ≤10%；代码与 JSON 密集文本会偏高或偏低 20~30%。
    仅在拿不到 tokenizer 与引擎真实值时使用（本地模型走精确计数，云端走真实 prompt_tokens）。
    """
    if not text:
        return 0
    cjk = len(re.findall(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", text))
    letters_digits = len(re.findall(r"[A-Za-z0-9]", text))
    spaces = text.count(" ") + text.count("\n") + text.count("\t")
    punct = max(0, len(text) - cjk - letters_digits - spaces)
    return max(1, int(round(cjk * 0.67 + letters_digits / 4.6 + punct / 2.8 + spaces * 0.1)))


def _gguf_counter(model_path: str):
    import llama_cpp  # 与引擎同款依赖；vocab_only 只读词表
    llm = llama_cpp.Llama(model_path=model_path, vocab_only=True, verbose=False)

    def _count(text: str) -> int:
        return len(llm.tokenize(text.encode("utf-8"), add_bos=False, special=False))

    return _count


def _hf_counter(model_dir: str):
    from tokenizers import Tokenizer
    tk = Tokenizer.from_file(f"{model_dir.rstrip('/')}/tokenizer.json")

    def _count(text: str) -> int:
        return len(tk.encode(text, add_special_tokens=False).ids)

    return _count


def _get_counter(model_path: str):
    """按模型路径取精确计数器；不可用返回 None（调用方退回估算）。"""
    if not model_path:
        return None
    with _lock:
        if model_path in _counters:
            if model_path in _counter_order:
                _counter_order.remove(model_path)
            _counter_order.append(model_path)
            return _counters[model_path]
    counter = None
    try:
        if model_path.endswith(".gguf"):
            counter = _gguf_counter(model_path)
        else:
            import os
            if os.path.isfile(f"{model_path.rstrip('/')}/tokenizer.json"):
                counter = _hf_counter(model_path)
    except Exception:
        logger.info("精确计数不可用，退回估算: %s", model_path, exc_info=True)
        counter = None
    if counter is not None:
        with _lock:
            _counters[model_path] = counter
            _counter_order.append(model_path)
            while len(_counter_order) > _COUNTER_CACHE_MAX:
                evicted = _counter_order.pop(0)
                _counters.pop(evicted, None)
    return counter


def count_tokens(text: str, model_path: str = "") -> tuple[int, str]:
    """返回 (tokens, "exact" | "estimated")。"""
    if not text:
        return 0, "exact"
    counter = _get_counter(model_path)
    if counter is not None:
        try:
            return int(counter(text)), "exact"
        except Exception:
            logger.debug("精确计数失败，退回估算", exc_info=True)
    return estimate_tokens(text), "estimated"


# ── 会话状态 ───────────────────────────────────────────

def _session(session_id: str) -> dict:
    return _sessions.setdefault(session_id, {
        "system_parts": [],      # [(category, text)] 来自 _build_chat_messages
        "snapshot": None,        # 最近一轮的统计快照
        "cache_samples": [],     # 最近 N 次缓存命中率
        "real_prompt_tokens": 0, # 引擎返回的真实输入 token（校准用）
        "model_path": "",
    })


def record_system_parts(session_id: str, parts: list) -> None:
    """记录系统提示词各段及其类别（由 _build_chat_messages 在组装时调用）。"""
    if not session_id or not parts:
        return
    with _lock:
        _session(session_id)["system_parts"] = [(str(c), str(t)) for c, t in parts if t]


def _split_system(system_text: str, parts: list) -> dict:
    """把合并后的系统消息按已记录的段切分；夹缝里的内容归到 other。

    （循环后续会往系统消息里追加知识注入、提醒等，这些不属于任何段，
    以及围栏路径下的工具目录也会落在这里。）
    """
    buckets = defaultdict(list)
    cursor = 0
    for cat, text in parts:
        idx = system_text.find(text, cursor)
        if idx < 0:
            continue
        if idx > cursor:
            buckets["other"].append(system_text[cursor:idx])
        buckets[cat].append(text)
        cursor = idx + len(text)
    if cursor < len(system_text):
        tail = system_text[cursor:]
        # 围栏路径的工具目录（"## 工具"/"可用工具"）算系统工具，其余算其他
        if re.search(r"工具|tools?", tail, re.I):
            buckets["system_tools"].append(tail)
        else:
            buckets["other"].append(tail)
    leading_gap = ""
    if parts:
        first_idx = system_text.find(parts[0][1]) if parts[0][1] else -1
        if first_idx > 0:
            leading_gap = system_text[:first_idx]
    if leading_gap:
        # 原生路径的"输出纪律"提示词（无工具目录）算系统提示词
        if re.search(r"工具|tools?", leading_gap, re.I):
            buckets["system_tools"].append(leading_gap)
        else:
            buckets["system_prompt"].append(leading_gap)
    return buckets


def record_request(session_id: str, messages: list, tools: list,
                   model_path: str = "", limit: int = 0, limit_source: str = "") -> None:
    """记录"本轮实际发出去的内容"的类别快照（每步采样前调用）。"""
    if not session_id:
        return
    bucket_texts: dict[str, list[str]] = defaultdict(list)
    system_text = ""
    for m in messages or []:
        content = m.get("content")
        if not isinstance(content, str) or not content:
            continue
        if m.get("role") == "system":
            system_text += ("\n\n" if system_text else "") + content
        else:
            bucket_texts["messages"].append(content)
    if system_text:
        with _lock:
            parts = list(_session(session_id)["system_parts"])
        for cat, texts in _split_system(system_text, parts).items():
            bucket_texts[cat].extend(texts)
    for t in tools or []:
        name = ((t or {}).get("function") or {}).get("name", "")
        try:
            text = json.dumps(t, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            continue
        bucket_texts["mcp_tools" if name.startswith("mcp__") else "system_tools"].append(text)

    counts: dict[str, int] = {}
    sources: set[str] = set()
    for cat in CATEGORIES:
        total = 0
        for text in bucket_texts.get(cat, []):
            n, src = count_tokens(text, model_path)
            total += n
            sources.add(src)
        counts[cat] = total

    with _lock:
        sess = _session(session_id)
        sess["model_path"] = model_path
        sess["snapshot"] = {
            "counts": counts,
            "estimated_total": sum(counts.values()) + TEMPLATE_OVERHEAD,
            "limit": int(limit or 0),
            "limit_source": limit_source,
            "token_source": "estimated" if "estimated" in sources else "exact",
            "updated_at": time.time(),
        }


def _extract_cache_rate(usage: dict | None, timings: dict | None) -> float | None:
    """只有引擎**明确给出**缓存字段时才返回命中率；字段缺失返回 None（UI 显示"未提供"）。

    否则会把"引擎没报缓存"误显示成 0%——那是在编造一个否定的结论。
    """
    if isinstance(timings, dict) and "prompt_n" in timings and "cache_n" in timings:
        prompt_n = int(timings.get("prompt_n") or 0)
        cache_n = int(timings.get("cache_n") or 0)
        if prompt_n > 0:
            return max(0.0, min(1.0, cache_n / prompt_n))
    if isinstance(usage, dict):
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict) and details.get("cached_tokens") is not None:
            prompt = int(usage.get("prompt_tokens") or 0)
            if prompt > 0:
                cached = int(details.get("cached_tokens") or 0)
                return max(0.0, min(1.0, cached / prompt))
    return None


def record_usage(session_id: str, usage: dict | None = None, timings: dict | None = None) -> None:
    """记录引擎返回的真实用量：输入 token 总数 + 缓存命中率样本。"""
    if not session_id:
        return
    prompt_tokens = 0
    if isinstance(usage, dict):
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
    elif isinstance(timings, dict):
        prompt_tokens = int(timings.get("prompt_n") or 0)
    rate = _extract_cache_rate(usage, timings)
    with _lock:
        sess = _session(session_id)
        if prompt_tokens:
            sess["real_prompt_tokens"] = prompt_tokens
        if rate is not None:
            sess["cache_samples"].append(rate)
            if len(sess["cache_samples"]) > CACHE_SAMPLES:
                del sess["cache_samples"][:-CACHE_SAMPLES]


def stats(session_id: str, limit: int = 0, limit_source: str = "") -> dict:
    """面板数据：容量、总量、六类占比、平均缓存命中率。"""
    with _lock:
        sess = _sessions.get(session_id) or {}
        snap = dict(sess.get("snapshot") or {})
        samples = list(sess.get("cache_samples") or [])
    if not snap:
        return {
            "status": "ok", "available": False, "session_id": session_id,
            "limit": int(limit or 0), "limit_source": limit_source,
            "total": 0, "percent": None, "breakdown": [],
            "cache_hit_rate": (sum(samples) / len(samples)) if samples else None,
            "cache_samples": len(samples), "token_source": "none", "updated_at": None,
        }
    counts = snap.get("counts") or {}
    est_total = int(snap.get("estimated_total") or 0)
    real_total = int(sess.get("real_prompt_tokens") or 0)
    # 有真实输入 token 就用真实值（引擎口径含模板开销），否则用各部分之和 + 模板近似
    total = real_total or est_total
    snap_limit = int(snap.get("limit") or 0) or int(limit or 0)
    snap_src = snap.get("limit_source") or limit_source
    denom = sum(counts.values()) or 1
    breakdown = [
        {"key": cat, "tokens": int(counts.get(cat, 0)),
         "percent": round(counts.get(cat, 0) * 100.0 / denom, 1)}
        for cat in CATEGORIES
    ]
    return {
        "status": "ok",
        "available": True,
        "session_id": session_id,
        "limit": snap_limit,
        "limit_source": snap_src,
        "total": total,
        "estimated_total": est_total,
        "real_prompt_tokens": real_total,
        "percent": round(total * 100.0 / snap_limit, 1) if snap_limit else None,
        "breakdown": breakdown,
        "cache_hit_rate": round(sum(samples) / len(samples), 4) if samples else None,
        "cache_samples": len(samples),
        "token_source": snap.get("token_source", "estimated"),
        "model_path": sess.get("model_path", ""),
        "updated_at": snap.get("updated_at"),
    }


def reset(session_id: str = "") -> None:
    """清掉某会话（或全部）的统计——测试与会话删除时用。"""
    with _lock:
        if session_id:
            _sessions.pop(session_id, None)
        else:
            _sessions.clear()
