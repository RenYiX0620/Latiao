#!/usr/bin/env python3
"""一次性回填：把历史反思里"真踩到的坑"提升为 learnings（2026-09-23，审查⑧续）。

背景：⑧ 之前，反思只拼进当轮工具结果、跨会话零复用（真库 969 条，mx_query 同一句
237 次，learnings 里一条都没有）。⑧ 上线后**新发生**的失败会自动变成知识，但历史
那批还躺在 reflections 表里。本脚本按**与实时路径完全相同**的判定与写入逻辑回填。

用法：
    python scripts/backfill_reflections.py            # 干跑：只打印计划，不写库
    python scripts/backfill_reflections.py --apply    # 真写（自动备份 memory.db）

判定（与实时路径一致，不做第二套标准）：
- 用 _reflection_kind 按结果重新分类——历史行的 was_useful 是硬编码 True，
  不能直接信（"输出较大"这类提示当时也被标成失败）
- 只回填 error / permission / missing / empty 四类
- topic 带错误特征 → 同一个坑重复出现只累加置信度（topic upsert），不刷表
"""
import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真写（默认只干跑）")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少条反思（0=全部）")
    args = ap.parse_args()

    import config
    import db
    import memory

    db_path = Path(config.PROGRESS_DIR) / "memory.db"
    if not db_path.exists():
        print(f"找不到记忆库: {db_path}")
        return 1
    if args.apply:
        backup = db_path.with_name(f"memory.db.bak-{datetime.now():%m%d-%H%M%S}")
        shutil.copy2(db_path, backup)
        print(f"已备份: {backup.name}")

    db._init_db()
    conn = memory._get_db()
    rows = conn.execute(
        "SELECT session_id, tool_name, tool_args, tool_result_summary, reflection "
        "FROM reflections ORDER BY created_at").fetchall()
    if args.limit:
        rows = rows[:args.limit]

    plan: dict[str, dict] = {}
    skipped_kind = 0
    for session_id, tool, args_json, summary, refl in rows:
        kind = memory._reflection_kind(tool, summary or "")
        if kind not in memory.REFLECTION_PITFALL_KINDS:
            skipped_kind += 1
            continue
        topic, sig = memory.reflection_learning_key(tool, summary or "")
        entry = plan.setdefault(topic, {"n": 0, "tool": tool, "args": args_json, "summary": summary,
                                        "refl": refl, "session": session_id})
        entry["n"] += 1

    print(f"反思 {len(rows)} 条 → 真坑 {len(rows) - skipped_kind} 条"
          f"（跳过非失败类 {skipped_kind} 条）→ 不同 topic {len(plan)} 个")
    print("前 10 条：")
    for topic, e in sorted(plan.items(), key=lambda kv: -kv[1]["n"])[:10]:
        print(f"  {e['n']:>4}×  {topic[:58]}")

    if not args.apply:
        print("\n（干跑结束；加 --apply 才会写库）")
        return 0

    written = 0
    for topic, e in plan.items():
        try:
            tool_args = json.loads(e["args"] or "{}")
        except ValueError:
            tool_args = {}
        if memory.promote_reflection_to_learning(e["session"], e["tool"], tool_args,
                                                 e["summary"], e["refl"]):
            written += 1
    print(f"\n回填完成：写入 {written} 条 learning（topic 去重后）")
    shown = memory._get_db().execute(
        "SELECT topic, substr(content,1,60), round(confidence,2) FROM learnings "
        "WHERE source_type='reflection' ORDER BY created_at DESC LIMIT 5").fetchall()
    for t, c, cf in shown:
        print(f"  {cf}  {t[:40]} | {c}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
