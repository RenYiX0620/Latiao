#!/usr/bin/env python3
"""承诺碎片回归集：真实语料重放（2026-10-08 固化）。

背景：闸门是逐个堵变体的，"修 A 坏 B"靠绿测试看不见——当天实测：末句版
`_looks_like_plan_only` 被同文件残留的旧定义遮蔽（Python 后定义胜出），新实现是
死代码而 1305 项全绿；真实语料重放当场抓到 4 条碎片漏网 + emoji 尾巴盲区。
本脚本把那次手工重放固化成命令：把真实会话里的 assistant 短正文逐条喂给判定
函数，任何"已报过/已修过"的碎片形态漏网 = 退出码 1。

三个来源（按顺序取，找到即用）：
  1. --file <path>          指定 JSONL/JSON 文件（每行 {content} 或纯文本行）
  2. --db                   本机会话库 ~/.local-ai-os/memory.db 的 session_messages
  3. 内置基线               今天（2026-10-08）的四次现场原句 + 误伤对照

用法：
  python3 scripts/replay_fragments.py            # 内置基线（CI/任意机器可跑）
  python3 scripts/replay_fragments.py --db       # 叠加本机真实会话
  python3 scripts/replay_fragments.py --file x   # 叠加指定语料

判定口径（与闸门一致，阈值见 agent/loop.py）：
  碎片  = _looks_like_plan_only(text, has_tools=True) 为真
  可疑  = _delivery_fragment_suspect(text) 命中（tail-promise / promise-phrase /
          intent-short）——不拦截，仅报告；连续命中说明判定面比拦截面宽，
          属正常（哨兵本来就该更敏感）
退出码：内置基线里任何**已知碎片**漏网 → 1（回归）；其余 0。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sidecar"))

# ── 内置基线：真实现场原句（改判定函数前先跑这个，漏任何一条 = 回归）──
# (正文, 是否碎片)。来源：sidecar.log + 会话库逐条核对（2026-10-07/08 四次用户报告）。
BASELINE: list[tuple[str, bool]] = [
    # 10-07 现场：工具失败后承诺（两次用户报告的"说了三遍"族）
    ("好的主人，欧娜这就帮你把美股、港股的数据都拉出来，再结合最新消息给你分析明天A股的走势～💋", True),
    ("好的主人，欧娜这就帮你看看这三只票的情况～💋", True),
    ("哎呀主人别急嘛～刚才 **ak_finance 的东财接口抽风了**（连接被拒绝），欧娜没及时切到备用工具，所以卡住了 😘 现在马上用 **mx_query** 帮你查这三只票的情况！💋", True),
    # 10-08 现场 1/2：前半句带数据、末句承诺（emoji 尾巴）
    ("**今天大盘高开低走，上证微涨0.3%，但科创50跌了1.7%，整体偏震荡。** 💋 让我拉一下实时数据给你看～", True),
    ("**今天跌这么狠，核心原因是\"高开低走+获利盘兑现+中东局势发酵\"三重打击。** 💋 让我赶紧搜一下今天的最新情况～", True),
    # 10-08 现场 3：动词表外的新动词（结构兜底的起因）
    ("让我再深挖一下那份\"研报引发的暴跌\"的具体内容～", True),
    # 10-08 现场 4：闸门催过一次后的更具体承诺（升级档的起因）
    ("让我再搜一下几个头部平台（Temu/Shein/TikTok Shop）的最新动态～", True),
    # 10-10 现场 5：括号旁白收尾 + 动作动词（"我马上改过来～（踮起脚凑近你…）"）
    ("3 岁小孩哪来的 9 头身，应该是 4 到 5 头身才对，像年画里走出来的福娃娃娃。"
     " 我马上改过来～（踮起脚凑近你，呆毛扫过你的手臂）", True),
    ("我马上改过来～（踮起脚凑近你）", True),
    # 误伤对照：这些**必须放行**（合法短答/对用户的请求/带数据的汇报）
    ("今日大盘收跌，上证跌0.89%。", False),
    ("我查了，明天休市，不用等开盘。", False),
    ("有结果让我知道一下。", False),
    ("让我总结一下今天的情况：上证收跌0.89%，主力净流出214亿。", False),
    ("让我先看看数据。上证收跌0.89%，主力净流出214亿。", False),
]


def _iter_db_bodies(max_rows: int = 500):
    db = Path.home() / ".local-ai-os" / "memory.db"
    if not db.exists():
        return
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return
    try:
        rows = conn.execute(
            "SELECT data FROM session_messages WHERE role='assistant' "
            "ORDER BY rowid DESC LIMIT ?", (max_rows,)).fetchall()
    except sqlite3.Error:
        return
    for (raw,) in rows:
        try:
            body = (json.loads(raw) or {}).get("content") or ""
        except (json.JSONDecodeError, TypeError):
            continue
        if body:
            yield body


def _iter_file_bodies(path: Path):
    text = path.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            body = (json.loads(line) or {}).get("content") or ""
        except json.JSONDecodeError:
            body = line
        if body:
            yield body


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", action="store_true", help="叠加本机会话库的真实正文")
    ap.add_argument("--file", type=Path, help="叠加指定语料文件（JSONL 或纯文本）")
    ap.add_argument("--max", type=int, default=500, help="--db 取最近 N 条（默认 500）")
    args = ap.parse_args()

    from agent.loop import _delivery_fragment_suspect, _looks_like_plan_only

    # ── 第一关：内置基线（已知碎片必须全部命中；已知误伤必须全部放行）──
    regressions: list[str] = []
    print("== 内置基线（10-07/08 四次现场 + 五条误伤对照）==")
    for text, is_frag in BASELINE:
        gate = _looks_like_plan_only(text, True)
        sent = _delivery_fragment_suspect(text) or "-"
        ok = (gate == is_frag)
        mark = "✔" if ok else "✘ 回归"
        print(f"  {mark} 闸门={str(gate):<5} 哨兵={sent:<14} {text[:46]}")
        if not ok:
            regressions.append(text)
    if regressions:
        print(f"\n❌ 已知碎片漏网/误伤对照被拦 {len(regressions)} 条 —— 判定函数回归，先修再发版")
        return 1

    # ── 第二关：外部语料（只报可疑，不计入退出码——真实库里合法短答本就该放行）──
    bodies: list[str] = []
    if args.db:
        bodies += list(_iter_db_bodies(args.max))
    if args.file:
        bodies += list(_iter_file_bodies(args.file))
    if bodies:
        print(f"\n== 外部语料 {len(bodies)} 条（哨兵命中仅报告，不拦截）==")
        hits = 0
        for body in bodies:
            if len(body) > 260:
                continue
            sent = _delivery_fragment_suspect(body)
            gate = _looks_like_plan_only(body, True)
            if sent or gate:
                hits += 1
                print(f"  [{'闸门拦' if gate else '哨兵记'} {sent or '-'}] "
                      f"{re.sub(chr(92) + 's+', ' ', body)[:60]}")
        print(f"  共 {hits} 条命中（其中闸门应拦的会在下一轮被催促/收口）")

    print("\n✅ 基线全过：已知碎片全部命中，误伤对照全部放行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
