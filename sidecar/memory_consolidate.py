"""记忆固化（Memory Consolidation）—— learnings/preferences 的审计、归档与结构化导出。

2026-10-04 立项（源自 GPT-6-Sol / Claude Code 源码挖掘的对照结论 + 真实数据盘点）：

    盘点事实（~/.local-ai-os/memory.db，2026-10-04）：
    - learnings 637 条：refined 319（均 hit 1.7）、reflection 91（2.2）、technical 99、
      fact 52、structure 49（16.8）、correction 15（96.6）、preference 11（87.9）；
    - 其中 88 条为工具观察噪音（refined + "read_file: ..." 前缀）——源头是
      _refine_learnings 的提示词鼓励"聚焦于项目结构"；
    - 高 hit 条目里混着聊天碎片（"我想买一手…" [preference] hit 203）——误分类 + 高
      token 重叠 → 频繁注入污染上下文；
    - preferences 表只有 1 条；memory_injections 374 次注入仅 1 次反馈（机制空转）。

    设计原则（对照两家 agent 的记忆治理）：
    - 只归档不删除（Trash 文化）；archived=1 的条目不参与检索与注入，可恢复；
    - 用 hit_count 做价值信号（无需新造反馈机制，数据已在说话）；
    - 审计模式只读、零副作用；apply 模式显式执行并输出统计。

用法：
    python3 memory_consolidate.py --audit          # 只读审计，输出人读报告
    python3 memory_consolidate.py --audit --json   # 机器可读
    python3 memory_consolidate.py --apply          # 执行归档（写 archived=1）
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

MEMORY_DB = Path.home() / ".local-ai-os" / "memory.db"

# ── 分类判据 ─────────────────────────────────────────────────────────

# 工具观察前缀：'read_file: …' / 'list_dir: …' 等形式（refined 提炼产物的特征）
_TOOL_PREFIX_RE = re.compile(
    r"^\s*(read_file|list_dir|search_files|write_file|run_command|grep|glob"
    r"|web_search|fetch_url|view_image|shell_command)\s*[:：]"
)

# 聊天碎片特征（用户说的话被误存为 preference/correction）：
# 第一人称任务/口语表达，且不含"以后/每次/不要/总是/请用"这类偏好指令词。
_FIRST_PERSON_RE = re.compile(
    r"(^|[，。！？\s])(我(要|想|觉得|认为|准备|打算|正在)|帮我|给我|咱们|一下我)"
)
_PREF_DIRECTIVE_RE = re.compile(
    r"(以后|每次|今后|从现在起|不要再|别再|不要|总是|永远|请(用|按|以)|必须|一律"
    r"|回复(时|的时)?|叫我|别叫|语气|口吻|格式(用|按))"
)

# 偏好的"意图键"形态（memory.py 的 intent:xxx）——正常偏好条目
_INTENT_KEY_RE = re.compile(r"^intent:")

# 引用/对话内容判据（10-04 第二轮盘点新增）：correction/preference 的语义是
# "用户对助手的长期设定或纠正"，应含对助手的指向（你/助手/回复/回答/下次/以后…）；
# 不含指向且较长的是文章、合同、问答等**引用内容**——它们 token 多，在 TF-IDF 里
# 与任意查询都容易重叠，是 hit 数百次的最大注入污染源（真库实测："不是DeepSeek
# 替你赚钱…"hit 386、"不是教你成为量化大牛…"hit 373）。
_ASSISTANT_TARGET_RE = re.compile(
    r"(你|您|助手|回复|回答|下次|以后|每次|记住|别用|不要用|请用)")
_QUOTED_MIN_LEN = 40

# 低价值 refined：低命中 + 低置信
_LOW_HIT = 2          # hit_count <= 该值视为低命中
_LOW_CONF = 0.70      # 且置信度 < 该值

# 近重复：归一化前缀的 token 相似度
_DUP_PREFIX_LEN = 40

ARCHIVE_REASON_TOOL_OBS = "tool_observation"     # 工具观察噪音
ARCHIVE_REASON_CHAT_FRAG = "chat_fragment"       # 聊天碎片误分类
ARCHIVE_REASON_LOW_VALUE = "low_value_refined"   # 低命中低置信的提炼
ARCHIVE_REASON_QUOTED = "quoted_content"         # 引用/对话内容（非对助手指令）


def _ro_conn() -> sqlite3.Connection:
    if not MEMORY_DB.exists():
        print(f"记忆库不存在: {MEMORY_DB}", file=sys.stderr)
        sys.exit(1)
    return sqlite3.connect(f"file:{MEMORY_DB}?mode=ro", uri=True)


def _rw_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(MEMORY_DB))
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _norm(s: str, n: int = _DUP_PREFIX_LEN) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", (s or "")[:n].lower())


def _ensure_archived_column(conn: sqlite3.Connection) -> bool:
    """幂等补 archived 列（学 embedding 列的先例）。返回是否新加。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(learnings)")}
    if "archived" in cols:
        return False
    try:
        conn.execute("ALTER TABLE learnings ADD COLUMN archived INTEGER DEFAULT 0")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_learnings_archived "
                     "ON learnings(archived, confidence)")
        conn.commit()
        return True
    except Exception as e:
        print(f"加 archived 列失败: {e}", file=sys.stderr)
        return False


# ── 审计 ─────────────────────────────────────────────────────────────

def audit() -> dict:
    """只读审计：分类统计 + 问题清单（含建议动作）。"""
    conn = _ro_conn()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(learnings)")}
    has_archived = "archived" in cols
    sel = ("id, topic, content, confidence, source_type, hit_count, created_at"
           + (", archived" if has_archived else ""))
    rows = conn.execute(f"SELECT {sel} FROM learnings").fetchall()

    total = len(rows)
    by_source: Counter = Counter()
    hit_by_source: dict[str, list[int]] = defaultdict(list)
    problems: dict[str, list[dict]] = {
        ARCHIVE_REASON_TOOL_OBS: [],
        ARCHIVE_REASON_CHAT_FRAG: [],
        ARCHIVE_REASON_LOW_VALUE: [],
        ARCHIVE_REASON_QUOTED: [],
    }
    dup_groups: dict[str, list[dict]] = defaultdict(list)

    for r in rows:
        rid, topic, content, conf, stype, hit, created = r[:7]
        archived = r[7] if has_archived else 0
        if archived:
            continue
        topic = topic or ""
        conf = float(conf or 0)
        hit = int(hit or 0)
        by_source[stype] += 1
        hit_by_source[stype].append(hit)

        item = {"id": rid, "topic": topic[:80], "confidence": round(conf, 2),
                "source_type": stype, "hit_count": hit, "created_at": (created or "")[:10]}

        # ① 工具观察噪音
        if _TOOL_PREFIX_RE.match(topic):
            item["reason"] = ARCHIVE_REASON_TOOL_OBS
            problems[ARCHIVE_REASON_TOOL_OBS].append(item)
            continue

        # ② 聊天碎片（preference / correction 里像"用户说的话"的）
        if stype in ("preference", "correction") and not _INTENT_KEY_RE.match(topic):
            if _FIRST_PERSON_RE.search(topic) and not _PREF_DIRECTIVE_RE.search(topic):
                item["reason"] = ARCHIVE_REASON_CHAT_FRAG
                problems[ARCHIVE_REASON_CHAT_FRAG].append(item)
            # ②b 引用/对话内容：长文本且无对助手的指向（文章/合同/问答引用）
            # 注意：topic 存库时被截断到 30 字（matched_text[:30]），判长度必须用 content。
            elif (len(content or "") >= _QUOTED_MIN_LEN
                  and not _ASSISTANT_TARGET_RE.search(f"{topic} {content or ''}")):
                item["reason"] = ARCHIVE_REASON_QUOTED
                problems[ARCHIVE_REASON_QUOTED].append(item)

        # ③ 低命中低置信的 refined（只归档工具观察之外的）
        if stype == "refined" and hit <= _LOW_HIT and conf < _LOW_CONF:
            if item.get("reason") != ARCHIVE_REASON_TOOL_OBS:
                item["reason"] = ARCHIVE_REASON_LOW_VALUE
                problems[ARCHIVE_REASON_LOW_VALUE].append(item)

        # ④ 近重复（按归一化前缀分组）
        key = _norm(topic)
        if key:
            dup_groups[key].append(item)

    dups = [{"key": k, "count": len(v), "items": v}
            for k, v in dup_groups.items() if len(v) > 1]

    # 偏好表
    prefs = [{"key": (r[0] or "")[:60], "value": (r[1] or "")[:60],
              "confidence": round(float(r[2] or 0), 2)}
             for r in conn.execute("SELECT key, value, confidence FROM preferences")]
    inj = conn.execute(
        "SELECT COUNT(*), SUM(used=1), SUM(used=0), SUM(used IS NULL) FROM memory_injections"
    ).fetchone()
    conn.close()

    # 汇总
    count_noise = sum(len(v) for k, v in problems.items() if k != ARCHIVE_REASON_CHAT_FRAG)
    count_chat = len(problems[ARCHIVE_REASON_CHAT_FRAG])
    return {
        "db": str(MEMORY_DB),
        "total_active": total,
        "by_source": {k: {"count": v,
                          "avg_hit": round(sum(hit_by_source[k]) / max(1, len(hit_by_source[k])), 1)}
                      for k, v in by_source.most_common()},
        "problems": {k: {"count": len(v), "samples": v[:8]} for k, v in problems.items()},
        "near_duplicates": dups[:10],
        "preferences": prefs,
        "injections": {"total": inj[0], "used": inj[1] or 0,
                       "unused": inj[2] or 0, "no_feedback": inj[3] or 0},
        "summary": {
            "archive_candidates": count_noise,
            "review_candidates": count_chat,
            "dup_groups": len(dups),
        },
    }


def format_report(r: dict) -> str:
    L: list[str] = []
    a = L.append
    a(f"════ 记忆固化审计 ════  {r['db']}")
    a(f"活跃 learnings: {r['total_active']} 条\n")
    a("── 来源 × 价值（avg_hit 越高 = 越被反复需要）──")
    for k, v in r["by_source"].items():
        a(f"  {k:12s} n={v['count']:3d}  avg_hit={v['avg_hit']}")
    a("")
    p = r["problems"]
    a(f"── 问题①：工具观察噪音（{p[ARCHIVE_REASON_TOOL_OBS]['count']} 条，建议归档）──")
    a("   （源头：_refine_learnings 提示词鼓励'聚焦项目结构'→ 记录'读了什么'而非'下次怎么做'）")
    for s in p[ARCHIVE_REASON_TOOL_OBS]["samples"][:5]:
        a(f"   · conf={s['confidence']} hit={s['hit_count']}  {s['topic'][:58]}")
    a("")
    a(f"── 问题②：聊天碎片误分类（{p[ARCHIVE_REASON_CHAT_FRAG]['count']} 条，**需人工复核**不自动归档）──")
    a("   （用户说的话被存成 preference/correction → 高 token 重叠 → 频繁注入污染上下文）")
    for s in p[ARCHIVE_REASON_CHAT_FRAG]["samples"][:5]:
        a(f"   · [{s['source_type']}] conf={s['confidence']} hit={s['hit_count']}  {s['topic'][:52]}")
    a("")
    a(f"── 问题③：低价值 refined（{p[ARCHIVE_REASON_LOW_VALUE]['count']} 条，"
      f"hit≤{_LOW_HIT} 且 conf<{_LOW_CONF}，建议归档）──")
    for s in p[ARCHIVE_REASON_LOW_VALUE]["samples"][:4]:
        a(f"   · conf={s['confidence']} hit={s['hit_count']}  {s['topic'][:58]}")
    a("")
    a(f"── 问题④：引用/对话内容（{p[ARCHIVE_REASON_QUOTED]['count']} 条，**高 hit 长文本是最大污染源**）──")
    a("   （文章/合同/问答引用被存成 correction/preference：无对助手的指向且较长）")
    for s in p[ARCHIVE_REASON_QUOTED]["samples"][:5]:
        a(f"   · [{s['source_type']}] conf={s['confidence']} hit={s['hit_count']}  {s['topic'][:52]}")
    a("")
    a(f"── 近重复：{r['summary']['dup_groups']} 组 ──")
    for g in r["near_duplicates"][:4]:
        a(f"   x{g['count']}: {g['key'][:56]}")
    a("")
    a("── 偏好表 ──")
    for pv in r["preferences"]:
        a(f"   · \"{pv['key']}\" conf={pv['confidence']}")
    inj = r["injections"]
    a(f"\n── 注入反馈：{inj['total']} 次注入 / 有反馈 {inj['used'] + inj['unused']} / "
      f"无反馈 {inj['no_feedback']} ──")
    a(f"\n汇总：可自动归档 {r['summary']['archive_candidates']} 条；"
      f"待人工复核 {r['summary']['review_candidates']} 条；重复组 {r['summary']['dup_groups']} 个")
    return "\n".join(L)


# ── 执行归档 ──────────────────────────────────────────────────────────

def apply(reasons: tuple[str, ...] = (ARCHIVE_REASON_TOOL_OBS, ARCHIVE_REASON_LOW_VALUE)) -> dict:
    """按审计结果归档（archived=1，不删除）。默认只归档工具观察 + 低价值 refined；
    聊天碎片需人工复核，不在默认范围内。"""
    r = audit()
    ids: list[str] = []
    for reason in reasons:
        ids += [s["id"] for s in r["problems"][reason]["samples"]]  # samples 只是前 8 条
    # 重新全量取（samples 截断过）
    conn_full = _ro_conn()
    rows = conn_full.execute(
        "SELECT id, topic, content, confidence, source_type, hit_count FROM learnings"
    ).fetchall()
    conn_full.close()

    ids = []
    for rid, topic, content, conf, stype, hit in rows:
        topic = topic or ""
        conf = float(conf or 0)
        hit = int(hit or 0)
        if ARCHIVE_REASON_TOOL_OBS in reasons and _TOOL_PREFIX_RE.match(topic):
            ids.append(rid)
        elif (ARCHIVE_REASON_LOW_VALUE in reasons and stype == "refined"
              and hit <= _LOW_HIT and conf < _LOW_CONF
              and not _TOOL_PREFIX_RE.match(topic)):
            ids.append(rid)
        elif (ARCHIVE_REASON_CHAT_FRAG in reasons and stype in ("preference", "correction")
              and not _INTENT_KEY_RE.match(topic)
              and _FIRST_PERSON_RE.search(topic)
              and not _PREF_DIRECTIVE_RE.search(topic)):
            ids.append(rid)
        elif (ARCHIVE_REASON_QUOTED in reasons and stype in ("preference", "correction")
              and not _INTENT_KEY_RE.match(topic)
              and len(content or "") >= _QUOTED_MIN_LEN
              and not _ASSISTANT_TARGET_RE.search(f"{topic} {content or ''}")):
            ids.append(rid)

    if not ids:
        return {"archived": 0, "reasons": list(reasons)}

    conn = _rw_conn()
    _ensure_archived_column(conn)
    now = datetime.now().isoformat()
    for rid in ids:
        conn.execute("UPDATE learnings SET archived=1, updated_at=? WHERE id=?", (now, rid))
    conn.commit()
    conn.close()
    return {"archived": len(ids), "reasons": list(reasons), "at": now}


def archive_ids(ids: list[str]) -> dict:
    """显式归档指定 id —— 人工复核后的手动清理入口（规则覆盖不到的单条，如
    其它 source_type 里的聊天碎片）。同样只归档不删除。"""
    if not ids:
        return {"archived": 0}
    conn = _rw_conn()
    _ensure_archived_column(conn)
    now = datetime.now().isoformat()
    n = 0
    for rid in ids:
        cur = conn.execute(
            "UPDATE learnings SET archived=1, updated_at=? "
            "WHERE id=? AND COALESCE(archived,0)=0", (now, rid))
        n += cur.rowcount
    conn.commit()
    conn.close()
    return {"archived": n, "ids": ids}


# ── 结构化手册（Task Group schema 的务实简化版）─────────────────────

HANDBOOK_PATH = Path.home() / ".local-ai-os" / "memory" / "handbook.md"

# 分区按价值排序（对照 Codex"用户偏好是主载荷"、CC"失败护盾"的结论）
_SECTIONS = [
    ("preference", "用户偏好", "影响每轮行为；重复表达过的才升进无条件注入档"),
    ("correction", "用户纠正", "下次怎么做不同"),
    ("failure", "失败与修复", "症状 → 原因 → 处理"),
    ("technical", "技术知识", ""),
    ("structure", "项目结构 / 地图", ""),
    ("fact", "事实", ""),
    ("reflection", "反思", ""),
    ("refined", "其他提炼", "被检索命中过的工具提炼"),
]


def _rel_time(iso_str: str) -> str:
    """相对时间（'3 天前'）——模型与人都更擅长相对时间，ISO 时间戳不触发新鲜度判断。"""
    try:
        dt = datetime.fromisoformat((iso_str or "")[:19])
        days = (datetime.now() - dt).days
        if days <= 0:
            return "今天"
        if days == 1:
            return "昨天"
        return f"{days} 天前"
    except Exception:
        return ""


def build_handbook(top_per_section: int = 25) -> dict:
    """把活跃记忆组织成结构化手册文件（只读生成，不改库内任何数据）。

    产物：~/.local-ai-os/memory/handbook.md —— 供用户阅读 / 前端展示 / 人工复核。
    （注入路径仍是按需检索，不做改动；手册是"记忆全景"，不是新的注入源。）
    """
    conn = _ro_conn()
    rows = conn.execute(
        "SELECT id, topic, content, confidence, source_type, hit_count, created_at "
        "FROM learnings WHERE COALESCE(archived, 0) = 0").fetchall()
    prefs = conn.execute(
        "SELECT key, value, confidence FROM preferences WHERE confidence >= 0.4 "
        "ORDER BY confidence DESC").fetchall()
    conn.close()

    buckets: dict[str, list] = {k: [] for k, _, _ in _SECTIONS}
    for rid, topic, content, conf, stype, hit, created in rows:
        stype = stype or "refined"
        if stype == "preference" and (topic or "").startswith("intent:"):
            continue  # 意图类偏好已并入 preferences 表展示
        key = stype if stype in buckets else "refined"
        buckets[key].append({
            "topic": (topic or "").strip(), "content": (content or "").strip(),
            "confidence": round(float(conf or 0), 2), "hit": int(hit or 0),
            "when": _rel_time(created),
        })

    L: list[str] = []
    a = L.append
    a("# 记忆手册（自动生成）\n")
    a(f"> 生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')} ｜ "
      f"活跃 {len(rows)} 条 ｜ 偏好 {len(prefs)} 条\n")
    a("> 这是「记忆全景」，供阅读与复核；助手实际使用的是按需检索，不是本文件。\n")

    if prefs:
        a("\n## 已生效偏好（confidence ≥ 0.4，其中 ≥ 0.7 的每轮无条件注入）\n")
        for k, v, c in prefs:
            mark = "🔒" if float(c or 0) >= 0.7 else "·"
            a(f"- {mark} `{float(c or 0):.2f}` {v or k}")
        a("")

    for key, title, note in _SECTIONS:
        items = buckets[key]
        if not items:
            continue
        items.sort(key=lambda x: (-x["confidence"], -x["hit"]))
        a(f"\n## {title}（{len(items)} 条{'，以下 ' + str(min(len(items), top_per_section)) + ' 条' if len(items) > top_per_section else ''}）")
        if note:
            a(f"> {note}")
        for it in items[:top_per_section]:
            tail = f"  ·{it['when']}" if it["when"] else ""
            hit = f" hit{it['hit']}" if it["hit"] else ""
            a(f"- [{it['confidence']:g}{hit}]{tail} {it['topic'][:70]}")
            if it["content"] and it["content"][:70] != it["topic"][:70]:
                a(f"  {it['content'][:110]}")

    HANDBOOK_PATH.parent.mkdir(parents=True, exist_ok=True)
    HANDBOOK_PATH.write_text("\n".join(L) + "\n", encoding="utf-8")
    return {"path": str(HANDBOOK_PATH), "active": len(rows), "prefs": len(prefs),
            "sections": {k: len(v) for k, v in buckets.items() if v}}


def main() -> None:
    ap = argparse.ArgumentParser(description="记忆固化：审计与归档")
    ap.add_argument("--audit", action="store_true", help="只读审计（默认）")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    ap.add_argument("--apply", action="store_true", help="执行归档（archived=1）")
    ap.add_argument("--include-review", action="store_true",
                    help="归档时一并处理聊天碎片（默认不含，需人工复核后手动传）")
    ap.add_argument("--archive-id", action="append", default=[],
                    help="显式归档指定 learning id（可多次传递）")
    ap.add_argument("--build-handbook", action="store_true",
                    help="生成结构化记忆手册（~/.local-ai-os/memory/handbook.md）")
    args = ap.parse_args()

    if args.build_handbook:
        out = build_handbook()
        print(json.dumps(out, ensure_ascii=False, indent=2) if args.json
              else f"手册已生成：{out['path']}（活跃 {out['active']} 条 / 偏好 {out['prefs']} 条）")
        return

    if args.archive_id:
        out = archive_ids(args.archive_id)
        print(json.dumps(out, ensure_ascii=False, indent=2) if args.json
              else f"已归档 {out['archived']} 条：{out['ids']}")
        return

    if args.apply:
        reasons = [ARCHIVE_REASON_TOOL_OBS, ARCHIVE_REASON_LOW_VALUE]
        if args.include_review:
            reasons.append(ARCHIVE_REASON_CHAT_FRAG)
            reasons.append(ARCHIVE_REASON_QUOTED)
        out = apply(tuple(reasons))
        print(json.dumps(out, ensure_ascii=False, indent=2) if args.json
              else f"已归档 {out['archived']} 条（reasons={out['reasons']}）")
        return

    r = audit()
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        print(format_report(r))


if __name__ == "__main__":
    main()
