"""长文档压缩（RAG-lite 片段筛选）：超预算的内联文件正文按"与问题相关性"保留。

事故背景（09-12）：128KB PDF 提取 13.7 万字符被整段内联进消息 → 超出本地
引擎上下文 → HTTP 400（"模型服务返回错误"）。

策略：不改变"文件内容随消息内联"的既有链路，只在上游做一次按相关性的
片段选举——把与用户问题最相关的片段留在原位（保持原文数字/名称不被转述），
其余折叠为占位标记。模型因此始终拿到"装得下且相关"的部分，并在末尾明确
知道还有多少内容被省略，可要求分段追问。

设计约束：
- 只依赖标准库（sidecar 热更不带第三方依赖）；
- 纯函数（无 IO），便于单测与复用；
- 无标记 → 原样返回（对普通消息零影响）。
"""
from __future__ import annotations

import re

# 与前端 App.tsx 的拼接格式保持一致：`📎 文件「name」内容如下：\n\n```\n{body}\n````
_FILE_BLOCK_RE = re.compile(
    r"(?P<head>📎\s*文件「(?P<name>[^」]{0,200})」内容如下：\s*\n+```[^\n]*\n)"
    r"(?P<body>.*?)"
    r"(?P<tail>\n```)",
    re.DOTALL,
)

# 单条消息里文件正文的默认预算（字符）。本地 27B 实测 2.4 万字符 ≈ 8-10K token，
# 既能让模型"读得动"又不至于把 prefill 拖到几分钟。
DEFAULT_BUDGET_CHARS = 24_000
MIN_CHUNK_CHARS = 1_200          # 片段下限；更小则合并到相邻段
_TARGET_CHUNK_CHARS = 4_000      # 期望片段大小（按段落边界靠近该值切分）
_MAX_MARKERS = 40                # 省略标记最多标注多少段（防碎片刷屏）

_ASCII_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{1,}")
_NUM_RE = re.compile(r"\d[\d,.]*\s*(?:亿元|万元|元|%|个|家|人|天|年|月|日|号|项|条)?")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _query_terms(question: str) -> dict[str, float]:
    """问题词项与权重：中文 2-gram、英文词、数字（数字/大写专名权重更高）。"""
    q = question or ""
    terms: dict[str, float] = {}
    # 中文双字组（覆盖"业主/资质/工期/金额"这类关键词）
    cjk = _CJK_RE.findall(q)
    for i in range(len(cjk) - 1):
        terms["".join(cjk[i:i + 2])] = 1.0
    # 中文单字（弱权重，兜底）
    for ch in cjk:
        terms[ch] = terms.get(ch, 0.0) + 0.15
    for w in _ASCII_WORD_RE.findall(q):
        terms[w.lower()] = 1.6
    for n in _NUM_RE.findall(q):
        terms[n.strip()] = 2.2
    return terms


def _split_chunks(body: str) -> list[str]:
    """按行切分并合并到 ~_TARGET_CHUNK_CHARS；单行超长时按句末再切。

    注意不能只按空行切：PDF 提取文本常无空行（整篇一"段"），
    会退化成头尾截断而丢掉中段关键信息（09-12 实测）。"""
    units: list[str] = []
    for ln in body.split("\n"):
        if len(ln) <= _TARGET_CHUNK_CHARS:
            units.append(ln)
            continue
        buf = ""
        for part in re.split(r"(?<=[。；;!?！？])", ln):
            buf += part
            if len(buf) >= _TARGET_CHUNK_CHARS:
                units.append(buf)
                buf = ""
        if buf:
            units.append(buf)
    chunks: list[str] = []
    buf = ""
    for u in units:
        buf = f"{buf}\n{u}" if buf else u
        if len(buf) >= _TARGET_CHUNK_CHARS:
            chunks.append(buf)
            buf = ""
    if buf:
        chunks.append(buf)
    if len(chunks) > 1 and len(chunks[-1]) < MIN_CHUNK_CHARS:
        chunks[-2] = chunks[-2] + "\n" + chunks[-1]
        chunks.pop()
    return chunks


def _score(chunk: str, terms: dict[str, float]) -> float:
    if not terms:
        # 无问题时退化为"取开头"（文档开场通常是概述/主体信息）
        return 0.0
    low = chunk.lower()
    score = 0.0
    for t, w in terms.items():
        c = low.count(t)
        if c:
            score += w * (1.0 + min(c, 5) * 0.3)
    # 含数字/金额/单位"的片段对招标、财报类问题更关键，轻微加权
    if _NUM_RE.search(chunk):
        score *= 1.15
    return score


def condense_inlined_files(text: str,
                           budget_chars: int = DEFAULT_BUDGET_CHARS,
                           question: str | None = None) -> tuple[str, list[dict]]:
    """把超预算的内联文件正文替换为"最相关片段集 + 省略标记"。

    :return: (可能被改写的新文本, 每个文件的统计 [{name, original, kept, chunks_total, chunks_kept, condensed}])
    """
    if not text or "📎" not in text:
        return text, []
    stats: list[dict] = []

    def _sub(m: re.Match) -> str:
        name = m.group("name")
        body = m.group("body")
        orig = len(body)
        if orig <= budget_chars:
            stats.append({"name": name, "original": orig, "kept": orig,
                          "chunks_total": 1, "chunks_kept": 1, "condensed": False})
            return m.group(0)
        chunks = _split_chunks(body)
        if len(chunks) <= 1:
            # 切不开（超长单段，如无换行的表格转储）→ 头尾保留
            head = int(budget_chars * 0.7)
            tail = budget_chars - head
            trimmed = (body[:head]
                       + f"\n\n…（原文 {orig} 字符，中间 {orig - budget_chars} 字符已省略；"
                         f"如需该部分请单独提问）…\n\n"
                       + body[-tail:])
            stats.append({"name": name, "original": orig, "kept": len(trimmed),
                          "chunks_total": 1, "chunks_kept": 1, "condensed": True})
            return f"{m.group('head')}{trimmed}{m.group('tail')}"

        terms = _query_terms(question or "")
        scores = [_score(c, terms) for c in chunks]
        order = sorted(range(len(chunks)), key=lambda i: scores[i], reverse=True)
        keep: list[int] = []
        used = 0
        for i in order:
            if used + len(chunks[i]) + 40 > budget_chars:
                continue
            keep.append(i)
            used += len(chunks[i]) + 40
        if not keep:
            keep = [order[0]]
            used = len(chunks[order[0]])

        kept_sorted = sorted(keep)
        assembled: list[str] = []
        for idx, ci in enumerate(kept_sorted):
            assembled.append(f"【片段 {idx + 1}/{len(kept_sorted)}（原文第 {ci + 1} 段）】\n{chunks[ci]}")
        omitted = len(chunks) - len(kept_sorted)
        note = (f"\n\n⚠️ 原文共 {orig} 字符 / {len(chunks)} 段，"
                f"已按问题相关性保留 {len(kept_sorted)} 段（{used} 字符），"
                f"省略 {omitted} 段。若需省略部分，请明确指出要查看的内容（如“第 80 页的资格要求”）。")
        new_body = "\n\n".join(assembled) + note
        stats.append({"name": name, "original": orig, "kept": len(new_body),
                      "chunks_total": len(chunks), "chunks_kept": len(kept_sorted),
                      "condensed": True})
        return f"{m.group('head')}{new_body}{m.group('tail')}"

    out = _FILE_BLOCK_RE.sub(_sub, text)
    return out, stats


def condense_messages(messages: list, budget_chars: int = DEFAULT_BUDGET_CHARS) -> tuple[list, list[dict]]:
    """对消息列表里的每条 user 消息做内联文件压缩（原地不改，返回新列表）。"""
    stats: list[dict] = []
    out: list = []
    for m in messages:
        if m.get("role") != "user":
            out.append(m)
            continue
        content = m.get("content")
        if not isinstance(content, str) or "📎" not in content:
            out.append(m)
            continue
        head, _, tail = content.partition("📎")
        new_content, st = condense_inlined_files(content, budget_chars=budget_chars,
                                                 question=head.strip())
        if st:
            stats.extend(st)
        if new_content != content:
            m = {**m, "content": new_content}
        out.append(m)
    return out, stats
