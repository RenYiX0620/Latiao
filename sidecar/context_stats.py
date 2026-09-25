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
import os
import re
import threading
import time
from collections import defaultdict

logger = logging.getLogger("latiao-sidecar")   # 与 App 的 handler 一致（__name__ 不带 handler，日志会静默丢失）

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


def _resolve_gguf_file(model_path: str) -> str | None:
    """把模型路径解析到真正的 .gguf 文件；取不到返回 None。

    LM Studio 的布局是「目录名以 .gguf 结尾、文件在目录里同名」（`X.gguf/X.gguf`）。
    此前只判 `endswith(".gguf")` 就把目录当文件喂给 llama_cpp → 每轮抛一次
    ValueError 再退回估算（实测 10 分钟 561 条 Traceback，而且这些模型的"精确
    计数"永远拿不到）。`local_llm.start_model` 早已会解析目录，这里补上同一步。
    """
    import glob
    p = os.path.expanduser((model_path or "").strip())
    if not p:
        return None
    if os.path.isdir(p):
        cands = sorted(glob.glob(os.path.join(p, "*.gguf")))
        if not cands:
            return None
        same = [c for c in cands if os.path.basename(c).lower() == os.path.basename(p).lower()]
        return (same or cands)[0]
    return p if p.lower().endswith(".gguf") else None


# 加载失败的模型要**记住**：llama_cpp 对某些 GGUF（例如它不认识的量化/架构）永远加载不了，
# 而 count_tokens 是按"每条消息 + 每个系统段 + 每个工具定义"逐个调的 —— 不记失败就会
# 每轮重试几十次、每次写一整条 traceback（实测把日志灌到 4.7MB、单小时 1894 条）。
_FAILED_COUNTERS: dict[str, float] = {}   # model_path -> 首次失败时间
_FAIL_COUNTER_TTL = 1800.0                # 半小时后允许重试：模型可能被换掉/修好


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
        failed_at = _FAILED_COUNTERS.get(model_path)
        if failed_at is not None and (time.monotonic() - failed_at) < _FAIL_COUNTER_TTL:
            return None                      # 已知加载不了，直接退回估算（不再重试、不再刷日志）
    counter = None
    try:
        _gguf = _resolve_gguf_file(model_path)
        if _gguf:
            counter = _gguf_counter(_gguf)
        elif os.path.isfile(f"{model_path.rstrip('/')}/tokenizer.json"):
            counter = _hf_counter(model_path)
    except Exception as e:
        with _lock:
            first = model_path not in _FAILED_COUNTERS
            _FAILED_COUNTERS[model_path] = time.monotonic()
        if first:
            # 只说一次，且不打印整条 traceback（细节留给 DEBUG）
            logger.warning("精确计数不可用（该模型加载不出词表），本会话改用估算: %s —— %s",
                           model_path, str(e)[:120])
            logger.debug("精确计数失败细节", exc_info=True)
        counter = None
    if counter is not None:
        with _lock:
            _counters[model_path] = counter
            _counter_order.append(model_path)
            while len(_counter_order) > _COUNTER_CACHE_MAX:
                evicted = _counter_order.pop(0)
                _counters.pop(evicted, None)
                _FAILED_COUNTERS.pop(evicted, None)
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
        # ── 运行指标（状态栏展示；除缓存外均按"本轮"累计，见 begin_turn）──
        "turns": 0,              # 当前上下文里的用户轮数
        "steps": 0,              # 本轮采样步数
        "llm_seconds": 0.0,      # 本轮 LLM 采样总耗时
        "tool_seconds": 0.0,     # 本轮工具执行总耗时
        "ttft_samples": [],      # 本轮每步的首 token 延迟（秒）
        "gen_tokens": 0,         # 本轮生成的 completion token 数
        "gen_seconds": 0.0,      # 扣除首 token 等待后的生成耗时
    })


def begin_turn(session_id: str) -> None:
    """新一轮用户消息开始：重置"本轮"运行指标（缓存命中率与真实 token 保持滚动）。"""
    if not session_id:
        return
    with _lock:
        sess = _session(session_id)
        sess.update(steps=0, llm_seconds=0.0, tool_seconds=0.0,
                    ttft_samples=[], gen_tokens=0, gen_seconds=0.0)


def record_step(session_id: str, seconds: float, ttft: float | None = None) -> None:
    """记录一次采样步：总耗时 + 首 token 延迟（TTFT）。"""
    if not session_id:
        return
    try:
        sec = float(seconds)
        tt = float(ttft) if ttft is not None else None
    except (TypeError, ValueError):
        return
    with _lock:
        sess = _session(session_id)
        sess["steps"] += 1
        sess["llm_seconds"] += max(0.0, sec)
        if tt is not None and 0 <= tt <= sec:
            sess["ttft_samples"].append(tt)
            sess["gen_seconds"] += max(0.0, sec - tt)


def record_tool_time(session_id: str, seconds: float) -> None:
    """记录工具执行耗时（并发执行时由调用方传入该批次的墙钟耗时）。"""
    if not session_id:
        return
    try:
        sec = float(seconds)
    except (TypeError, ValueError):
        return
    with _lock:
        _session(session_id)["tool_seconds"] += max(0.0, sec)


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

    turns = sum(1 for m in (messages or []) if m.get("role") == "user")
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
        sess["turns"] = turns
        # 提示头指纹：下一轮 0% 时一眼看出是"头部变了"还是"引擎没复用"
        _fp = _head_fp(messages, tools)
        _ht = _head_sys_text(messages)
        _prev_ht = sess.get("head_text")
        if _prev_ht is not None and _prev_ht != _ht:
            logger.info("提示头变化：%s", _head_diff_hint(_prev_ht, _ht))
        sess["head_text"] = _ht
        sess["head_prev"] = sess.get("head_fp")
        sess["head_fp"] = _fp
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
        # llama.cpp 语义（09-19 实测校准）：prompt_n = 本次真正重算的 token，
        # cache_n = 从 KV 缓存复用的 token，两者相加才是完整 prompt。
        # 此前用 cache_n / prompt_n：全命中时（prompt_n=1, cache_n=1153）会算出
        # 1153/1 = 115300%（被截成 100%），部分命中时（883/271=326%）同样失真。
        recomputed = int(timings.get("prompt_n") or 0)
        cached = int(timings.get("cache_n") or 0)
        total = recomputed + cached
        if total > 0:
            return max(0.0, min(1.0, cached / total))
    if isinstance(usage, dict):
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict) and details.get("cached_tokens") is not None:
            prompt = int(usage.get("prompt_tokens") or 0)
            if prompt > 0:
                cached = int(details.get("cached_tokens") or 0)
                return max(0.0, min(1.0, cached / prompt))
    return None


def _head_sys_text(messages: list) -> str:
    """系统消息拼接（头部指纹对应的原文，用于定位"哪一段变了"）。"""
    return "\n".join(str((m or {}).get("content") or "")
                      for m in (messages or []) if (m or {}).get("role") == "system")


def _head_diff_hint(old: str, new: str) -> str:
    """两个系统提示的首个差异点（各取上下文 50/70 字），用于定位头部变化来源。"""
    n = min(len(old), len(new))
    i = 0
    while i < n and old[i] == new[i]:
        i += 1
    return (f"第 {i} 字符处起不同（{len(old)}→{len(new)} 字符）\n"
            f"    旧: …{old[max(0, i - 50):i + 70]}…\n"
            f"    新: …{new[max(0, i - 50):i + 70]}…")


def _head_fp(messages: list, tools: list) -> str:
    """提示头指纹：系统消息 + 工具表（决定前缀缓存能否命中的那部分）。

    缓存读 0% 时只能有两种成因：①提示头变了（我们的问题）②引擎没复用（引擎/模型
    的问题）。没有指纹时两种在日志里长得一模一样——09-19 排查时就被这两条 0% 卡住。
    """
    import hashlib
    h = hashlib.md5()
    for m in messages or []:
        if (m or {}).get("role") == "system":
            h.update(str(m.get("content") or "").encode("utf-8", "replace"))
            h.update(b"\x00")   # 分隔符只跟随系统消息：追加对话消息不改变头部指纹
    for t in tools or []:
        fn = (t or {}).get("function") or {}
        h.update(str(fn.get("name") or "").encode("utf-8", "replace"))
        h.update(str(fn.get("parameters") or "").encode("utf-8", "replace"))
    return h.hexdigest()[:8]


def record_usage(session_id: str, usage: dict | None = None, timings: dict | None = None) -> None:
    """记录引擎返回的真实用量：输入 token 总数 + 缓存命中率样本。"""
    if not session_id:
        return
    prompt_tokens = 0
    completion_tokens = 0
    if isinstance(usage, dict):
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
    elif isinstance(timings, dict):
        prompt_tokens = int(timings.get("prompt_n") or 0)
        completion_tokens = int(timings.get("predicted_n") or 0)
    rate = _extract_cache_rate(usage, timings)
    with _lock:
        sess = _session(session_id)
        if prompt_tokens:
            sess["real_prompt_tokens"] = prompt_tokens
        if completion_tokens:
            sess["gen_tokens"] += completion_tokens
        if rate is not None:
            _fp = sess.get("head_fp") or "-"
            _prev = sess.get("head_prev")
            _note = ("同上" if _prev == _fp else
                     f"与上次不同（上次 {_prev}）" if _prev else "本次会话首次")
            logger.info("缓存命中：本轮 %.0f%%（复用 %d / 共 %d token｜头部 %s %s）",
                        rate * 100,
                        int((timings or {}).get("cache_n") or (usage or {}).get(
                            "prompt_tokens_details", {}).get("cached_tokens") or 0),
                        int((timings or {}).get("cache_n") or 0)
                        + int((timings or {}).get("prompt_n") or 0)
                        or int((usage or {}).get("prompt_tokens") or 0),
                        _fp, _note)
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
            "turns": int(sess.get("turns") or 0), "steps": int(sess.get("steps") or 0),
            "llm_seconds": round(float(sess.get("llm_seconds") or 0.0), 1),
            "tool_seconds": round(float(sess.get("tool_seconds") or 0.0), 1),
            "ttft_avg": (round(sum(sess.get("ttft_samples") or []) / len(sess["ttft_samples"]), 2)
                         if sess.get("ttft_samples") else None),
            "tps": (round(sess["gen_tokens"] / sess["gen_seconds"]) if sess.get("gen_seconds") else None),
        }
    counts = snap.get("counts") or {}
    est_total = int(snap.get("estimated_total") or 0)
    real_total = int(sess.get("real_prompt_tokens") or 0)
    # 有真实输入 token 就用真实值（引擎口径含聊天模板为每个工具包裹的特殊 token）
    total = real_total or est_total
    counts = dict(counts)
    # 归一：把"真实总量 − 各部分之和"的差额补给对应类别，否则面板里各行相加
    # 会小于标题的总量（工具包裹 token 实测每个约 24 个，属系统工具）
    delta = real_total - sum(counts.values()) if real_total else 0
    if delta > 0:
        if counts.get("system_tools", 0) or counts.get("mcp_tools", 0):
            counts["system_tools"] = counts.get("system_tools", 0) + delta
        else:
            counts["other"] = counts.get("other", 0) + delta
    snap_limit = int(snap.get("limit") or 0) or int(limit or 0)
    snap_src = snap.get("limit_source") or limit_source
    denom = (total if real_total else sum(counts.values())) or 1
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
        # 运行指标（状态栏）：本轮步数/耗时/TTFT/生成速度 + 上下文轮数
        "turns": int(sess.get("turns") or 0),
        "steps": int(sess.get("steps") or 0),
        "llm_seconds": round(float(sess.get("llm_seconds") or 0.0), 1),
        "tool_seconds": round(float(sess.get("tool_seconds") or 0.0), 1),
        "ttft_avg": (round(sum(sess.get("ttft_samples") or []) / len(sess["ttft_samples"]), 2)
                     if sess.get("ttft_samples") else None),
        "tps": (round(sess["gen_tokens"] / sess["gen_seconds"]) if sess.get("gen_seconds") else None),
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
