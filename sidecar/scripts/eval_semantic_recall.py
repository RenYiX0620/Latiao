#!/usr/bin/env python3
"""⑥ 语义检索的量化评估：TF-IDF（现状） vs 本地嵌入模型（候选）。

审查⑥的论断：「检索是字符级词频匹配……用户问"帮我看看持仓"，库存的是"查询证券
持仓明细的方法"——词面不重叠就检索不到」。这个脚本在**真库**上验证这句话，并给出
候选嵌入模型的召回提升。只读：不写任何记忆数据。

用法：
    python scripts/eval_semantic_recall.py --port 8899            # 服务已起
    python scripts/eval_semantic_recall.py --port 8899 --label bge-small-zh
"""
import argparse
import json
import math
import sqlite3
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# (用户可能这么说, 期望被召回的内容里应出现的片段) —— 全部取自真库里真实存在的知识
# (用户可能这么说, 期望被召回的内容里应出现的片段) —— 片段**必须真实存在于库里**，
# 脚本会先校验；不存在的用例直接跳过并说明（审查给的例子"帮我看看我的持仓"在真库里
# 没有对应知识，属于假设场景，不能拿它评判召回好坏）。
CASES = [
    ("为什么有些股票查不到数据", "仅支持 A股/港股/基金/板块/指数"),
    ("金融数据工具报错说缺模块", "ModuleNotFoundError"),
    ("板块资金流向怎么看", "主力净流入资金"),
    ("电子布相关上市公司", "电子布"),
    ("上次那个风电项目的数据", "风电指标"),
    ("怎么把一套流程固定下来让它照着做", "量化策略"),
    ("长电科技现在多少钱", "长电科技"),
    ("股票行情的实时价格从哪来", "实时价"),
    ("我踩过哪些金融数据源的坑", "失败经验"),
    # 干扰项：不该召回任何金融/工程知识（用户的历史痛点：无关问题也塞记忆）
    ("帮我把这段 Python 代码重构一下", None),
    ("今天天气怎么样", None),
    ("给我讲个笑话", None),
]

MIN_SCORE = 0.28      # 与 memory._MIN_MEMORY_SCORE 一致的注入门槛


def embed(port: int, texts: list[str]) -> list[list[float]]:
    body = json.dumps({"input": texts}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/embeddings", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.load(r)
    return [e["embedding"] for e in data["data"]]


def cos(a, b) -> float:
    s = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return s / (na * nb)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--label", default="embedding")
    args = ap.parse_args()

    import config
    import memory

    db = sqlite3.connect(f"file:{Path(config.PROGRESS_DIR) / 'memory.db'}?mode=ro", uri=True)
    rows = db.execute("SELECT id, topic, content FROM learnings").fetchall()
    docs = [(r[0], f"{r[1]} {r[2]}") for r in rows]
    print(f"语料: {len(docs)} 条 learnings")

    doc_vecs = embed(args.port, [d[1] for d in docs])
    print(f"已用 {args.label} 编码语料（维度 {len(doc_vecs[0])}）\n")

    valid = []
    for query, want in CASES:
        if want and not any(want in d[1] for d in docs):
            print(f"  跳过（库里没有该知识）：{query} → {want}")
            continue
        valid.append((query, want))

    print(f"\n{'查询':26s} {'TF-IDF':>16s} {'嵌入':>16s}")
    tf_hit5 = em_hit5 = 0
    em_neg_max = 0.0
    em_pos_min = 1.0
    for query, want in valid:
        tf_res = memory._tfidf_search(query, limit=len(docs))
        tf_rank = next((i + 1 for i, r in enumerate(tf_res)
                        if want and want in f"{r.get('topic','')} {r.get('content','')}"), None)
        qv = embed(args.port, [query])[0]
        scored = sorted(((cos(qv, dv), i) for i, dv in enumerate(doc_vecs)), reverse=True)
        em_rank = next((i + 1 for i, (s0, idx) in enumerate(scored)
                        if want and want in docs[idx][1]), None)
        if want:
            tf_hit5 += 1 if (tf_rank and tf_rank <= 5) else 0
            em_hit5 += 1 if (em_rank and em_rank <= 5) else 0
            if em_rank:
                em_pos_min = min(em_pos_min, scored[em_rank - 1][0])
        else:
            em_neg_max = max(em_neg_max, scored[0][0])
        print(f"{query:26s} {('命中@' + str(tf_rank)) if tf_rank else '未命中':>16s} "
              f"{('命中@' + str(em_rank) + f' ({scored[em_rank-1][0]:.2f})') if em_rank else '未命中':>16s}")
        for j, (sc, idx) in enumerate(scored[:3]):
            mark = "✓" if (want and want in docs[idx][1]) else " "
            print(f"     {mark} #{j+1} {sc:.3f}  {docs[idx][1][:52]}")

    n = sum(1 for _q, w in valid if w)
    print(f"\n结果：正例 {n} 个 → TF-IDF 命中@5 {tf_hit5}/{n}，{args.label} 命中@5 {em_hit5}/{n}")
    print(f"分离度：真命中最低分 {em_pos_min:.3f} vs 干扰项最高分 {em_neg_max:.3f}")
    print(f"干扰项：嵌入最高分 {em_neg_max:.3f}"
          f"{'（低于注入门槛 → 不误注入 ✓）' if em_neg_max < MIN_SCORE else '（⚠ 高于门槛，可能误注入）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
