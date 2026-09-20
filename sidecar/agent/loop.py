"""薄循环（对标 Codex turn.rs / dsh ReactLoopAgent）——Scope 容器的第一个消费者。

架构原则：
- 模型驱动终止：step 循环"采样 → 工具执行 → 结果回填"，模型不再调用
  工具即结束。无 nudge 链、无停滞计数、无 200 字闸门。
- 错误即结果：工具失败的结构化错误文本回填上下文，由模型自纠。
- 一切机制皆插件：本循环只消费 scope 的 waterfall（pre_step/request/
  deliver）与工具目录；快车道/弱模型辅助/规划门/压缩全部是插件
  （agent/plugins/builtin.py）。
- steer：新消息入队，step 边界/交付后认领（不再取消）。
- 门控：LATIAO_AGENT_LOOP_V3=1 启用；默认仍走 v1（Stage 4 切换删除）。
"""
import asyncio
import datetime
import json
import os
import re
import time
import uuid
import logging
from pathlib import Path

import httpx

from agent.core import Scope
from agent.transport import (
    _is_local_llm_url,
    _local_llm_stream,
    mark_llm_suspect,
)
from agent.parsing import (
    _parse_delta_line,
    _parse_prompt_tool_calls,
)
from agent.text_quality import (
    _GenerationLoopError,
    _detect_text_loop,
    _strip_repeat_tail,
    _strip_think_fences,
)
from agent.messages import lang_of, msg as _msg
from agent.context import (
    _detect_user_language,
    detect_language_decision,
    _ensure_market_tools,
    _extract_last_user_text,
    _filter_tools_by_access,
    _is_light_query,
    _local_native_tools_ok,
    _maybe_add_inline_file_note,
    _merge_system_messages,
    _normalize_access,
    _resolve_max_tokens,
    _sanitize_tool_messages,
    _slim_history_for_local,
    _strip_transient_reminders,

    _is_custom_engine,
    _custom_engine_max_tokens,)
from agent.gates import _build_local_tools_prompt
from agent.context import _NATIVE_LEAN_PROMPT
from agent.plugins.builtin import setup_all

logger = logging.getLogger("latiao-sidecar")

MAX_STEPS = 40          # 安全网（compaction 插件落地后放宽）

# ── 信息增量饱和（09-19）：让"数据够了没"成为可计算状态 ──────────────
# 背景：模型驱动的终止条件只有"同参重复"和"工具轮上限"。搜索类任务每轮换 query
# （"美股收盘" → "三巫日/半导体" → "存储芯片" → 又回到"收盘 半导体 存储芯片"），
# 精确签名永不重复 → 只能等 12 轮上限，一次提问跑十几分钟。这里改用**信息增量**：
# 比较本轮检索结果相对已收集信息的新增比例，连续走低即判饱和并收口。
_INFO_GAIN_RATIO = float(os.environ.get("LATIAO_INFO_GAIN_RATIO", "0.25") or 0.25)
_SEARCH_BUDGET_CALLS = int(os.environ.get("LATIAO_SEARCH_BUDGET", "3") or 3)
# 指纹口径（09-19 实测调过两轮）：日期归一成单点、中文切二元组、数字归一——
# 否则"9月18日"里的 9/18 会被当成新数字、"道琼斯指数下跌"换个词就算新增，
# 同一批数据的复述会拿到 0.5 的假增量，判据永远不触发。
_INFO_DATE_RE = re.compile(r"(?:\d{4}[-/年])?(\d{1,2})[-/月](\d{1,2})日?")
_INFO_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_INFO_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?\s*%?")
_INFO_LATIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-]{2,}")
_INFO_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_INFO_STOPWORDS = frozenset({
    "the", "and", "for", "with", "that", "this", "from", "was", "were", "are",
    "has", "have", "not", "but", "you", "can", "will", "data", "index",
    "美股", "指数", "涨跌", "收盘", "数据", "结果", "以及", "同时", "表示", "显示",
})


def _search_tool_names() -> set:
    """检索类工具（web + financial 两类）——软饱和后从工具面撤下。"""
    try:
        from agent.context import TOOL_CATEGORIES
        names = set()
        for cat in ("web", "financial"):
            names |= set(TOOL_CATEGORIES.get(cat) or ())
        return names
    except Exception:
        return {"tavily_search", "web_search", "bing_search", "headless_read",
                "mx_query", "ak_finance"}


# 流式标记表：(开标记, 对应闭合, 处置方式)——think→思考通道，其余→丢弃
_MARKUP_MARKERS = (
    ("<think>", "</think>", "think"),
    ("<tool_call>", "</tool_call>", "drop"),
    ("<function_calls>", "</function_calls>", "drop"),
    ("<invoke", "</invoke>", "drop"),
)

_DUP_PROBE_CHARS = int(os.environ.get("LATIAO_DUP_PROBE_CHARS", "60") or 60)


_ROUND_CONTENT_MAX = int(os.environ.get("LATIAO_ROUND_CONTENT_MAX", "1200") or 1200)
# 低档（=限长档）：工具轮单轮输出上限。速度唯一直接杠杆是"少生成 token"——
# 09-19 实测：低档若只关思考，token 总量不变（只是从思考倒进正文），耗时不变。
_LOW_MAX_TOKENS = int(os.environ.get("LATIAO_LOW_MAX_TOKENS", "2048") or 2048)
_SEARCH_TOOL_REPEAT_LIMIT = int(os.environ.get("LATIAO_SEARCH_TOOL_REPEAT", "3") or 3)


def _trim_process_narration(text: str, limit: int = 0, lang: str = "zh") -> str:
    """工具轮的过长正文折叠为摘要（保留开头，其余省略）。

    09-19 实测：关思考后模型把推理倒进正文，单轮 11171 字英文过程文本 → 用户看到
    上万字英文、翻译轮还因过长失败。工具轮正文只是过程说明，折叠不影响结论。
    """
    limit = limit or _ROUND_CONTENT_MAX
    text = text or ""
    if len(text) <= limit:
        return text
    head = text[: int(limit * 0.6)].rstrip()
    return head + "\n\n" + _msg("narration_folded", lang, n=f"{len(text) - len(head):,}")


def _looks_like_replay(prev_text: str, current_prefix: str) -> bool:
    """本轮开头是否在复读上一轮正文（逐字复读实测为完全一致）。

    仅在上一轮正文够长（≥60 字）时判定，避免"好的""收到"这类短句误伤。
    """
    prev = re.sub(r"\s+", "", (prev_text or ""))[:200]
    cur = re.sub(r"\s+", "", (current_prefix or ""))[:200]
    if len(prev) < 60 or len(cur) < 40:
        return False
    import difflib
    # 等长前缀比较：长度不等会把相似度稀释（实测 120 字 vs 52 字只有 0.60 → 漏判）
    n = min(len(prev), len(cur))
    return difflib.SequenceMatcher(None, prev[:n], cur[:n]).ratio() >= 0.9


def _filter_search_tools(tools: list) -> list:
    """撤下检索类工具，保留文件/命令/技能等（纯函数，便于单测）。"""
    names = _search_tool_names()
    return [t for t in (tools or []) if ((t.get("function") or {}).get("name") not in names)]


def _info_points(text: str) -> set:
    """抽取信息点指纹：归一化日期/数字 + 中文二元组 + 英文词（≥3 字，去停用词）。"""
    points = set()
    raw = _INFO_URL_RE.sub(" ", text or "")   # 链接每轮都变，剥掉否则全是"新信息"
    # 日期先整体取出并从文本移除，再单独计入归一化 token——否则 "2026-09-18" 会被
    # 数字规则拆成 #2026/#9/#18，同一批数据复述时凭空多出"新信息"（实测踩过）
    _dates = _INFO_DATE_RE.findall(raw)
    raw = _INFO_DATE_RE.sub(" ", raw)
    for _m1, _m2 in _dates:
        points.add(f"#d:{int(_m1):02d}-{int(_m2):02d}")
    for m in _INFO_NUM_RE.findall(raw):
        try:
            val = float(m.replace(",", "").rstrip("%").strip())
        except ValueError:
            continue
        if val:
            points.add(f"#{val:g}")
    for w in _INFO_LATIN_RE.findall(raw):
        lw = w.lower()
        if lw not in _INFO_STOPWORDS:
            points.add(lw)
    for run in _INFO_CJK_RE.findall(raw):
        if len(run) == 1:
            continue
        for i in range(len(run) - 1):     # 滑动二元组：换措辞仍是同一批指纹
            bg = run[i:i + 2]
            if bg not in _INFO_STOPWORDS:
                points.add(bg)
    return points


def _info_gain(text: str, seen: set) -> tuple:
    """返回 (增量比例, 新增点数)。抽不到信息点时返回 (1.0, 0) —— 不据此判饱和。"""
    points = _info_points(text)
    if not points:
        return 1.0, 0
    new = points - seen
    return len(new) / len(points), len(new)



def _tool_calls_signature(tool_calls: list) -> str:
    """工具调用的规范化签名（名称 + 排序后参数 JSON）——重复同参调用检测的
    结构信号（ZCode buildRepeatedToolCallSignature / DSH repeat-tool-reminder
    同构）。不猜内容语义，只看结构。"""
    parts = []
    for tc in tool_calls:
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments", "{}") or "{}")
        except Exception:
            args = {"_raw": str(fn.get("arguments", ""))[:120]}
        parts.append(f"{fn.get('name', '?')}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}")
    return " || ".join(parts)

STALL_FIRST = 90        # 首 token 前静默上限

# ── 会话级工具目录一致性（对齐 Codex/DSH/ZCode 共识：意图过滤只增不删）──
# 三家调研定论：消息文本不参与工具目录的任何决策。Latiao 的意图关键词过滤
# 只负责「新增」相关工具；本会话已成功使用 / 已加载技能声明的工具，不因最后
# 一条消息的措辞离场（09-07 20:47 事故：32 轮行情会话中一句元问题把
# mx_query 过滤掉 → 模型无工具可调 → use_skill 死循环烧到步数上限）。
_SESSION_USED_TOOLS: dict[str, set] = {}
# 会话级工具表（只增不减，09-20）：工具表渲染在**提示头**，而混合/线性注意力模型
# 的缓存只在提示"严格续写"时复用。旧实现每轮按最后一条消息的措辞重算工具表
# （_filter_tools + cap 12 + _ensure_market_tools）→ 12/14 个工具交替出现 → 头部
# 逐轮变化 → 实测普通对话两轮就是 0% 复用。这里改成本会话出现过的工具不再离场。
_SESSION_TOOL_NAMES: dict[str, list] = {}
SKILL_DEPENDENT_TOOLS = {"mx-data": {"mx_query", "ak_finance"}}


_SESSION_NARRATIONS: dict[str, list] = {}  # session -> 最近过渡叙述（规范化后）


def _norm_narration(text: str) -> str:
    import re as _re
    return _re.sub(r"[^\u4e00-\u9fff\w]+", "", text.lower())[:120]


def _narration_seen(session_id: str, text: str) -> bool:
    """过渡叙述近重复判定：与最近 3 条任一相同 → True（结构去重）。"""
    n = _norm_narration(text)
    if not n:
        return False
    seen = _SESSION_NARRATIONS.setdefault(session_id, [])
    if n in seen:
        return True
    seen.append(n)
    del seen[:-3]
    return False


def mark_session_tools(session_id: str, names) -> None:
    """把工具标记为本会话已用/已承诺——后续轮次目录并集保留。"""
    if not session_id or not names:
        return
    s = _SESSION_USED_TOOLS.setdefault(session_id, set())
    s.update(n for n in names if n)


# ── 当日限额状态（工具当日免费额度，如 mx_query 150 次/日）────────
# 额度是账户级全局状态（上游按天统计）：把"今日已用尽"钉在应用数据
# 目录，日期比对天然跨天失效、重启不丢；成功调用即清除（防后续换
# Key 后标记错杀）。只用于提示注入，绝不改动工具目录。
_QUOTA_STATE_FILE: Path | None = None


def _quota_state_file() -> Path:
    global _QUOTA_STATE_FILE
    if _QUOTA_STATE_FILE is None:
        try:
            from config import PROGRESS_DIR
            _QUOTA_STATE_FILE = PROGRESS_DIR / "quota_state.json"
        except Exception:
            _QUOTA_STATE_FILE = Path.home() / ".local-ai-os" / "quota_state.json"
    return _QUOTA_STATE_FILE


def _quota_exhausted_today(tool: str) -> bool:
    try:
        data = json.loads(_quota_state_file().read_text(encoding="utf-8"))
        return isinstance(data, dict) and data.get(tool) == datetime.date.today().isoformat()
    except Exception:
        return False


def _mark_quota_exhausted(tool: str) -> None:
    try:
        f = _quota_state_file()
        data = {}
        if f.exists():
            data = json.loads(f.read_text(encoding="utf-8"))
        data[tool] = datetime.date.today().isoformat()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        logger.debug("quota state write failed", exc_info=True)


def _clear_quota_marker(tool: str) -> None:
    try:
        f = _quota_state_file()
        if f.exists():
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, dict) and tool in data:
                del data[tool]
                f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        logger.debug("quota state clear failed", exc_info=True)


# ── 终答轮数据摘要视图（工具结果内联，删除全部工具形态）──────────
_PLAN_VERB_RE = re.compile(r"查询|检索|调取|查看|获取|读取|搜索|搜集|抓取|调数据|调用工具")
_PLAN_INTENT_RE = re.compile(
    r"我来|让我|我先|我将|我会|接下来|下面我|现在(让我|我来)|先(去|来)?(查|调|搜|读|看看)")


def _needs_length_retry(finish_reason, native_calls, empty_body: bool,
                        retry_used: bool) -> bool:
    """是否该因"长度截断"重跑本轮（关思考）。

    两种情况：① 解析出了 native 工具调用但被截断（半截 JSON 不可执行）；
    ② 正文为空而思考非空（思考吃光预算 —— 09-20 实测 Bonsai 6144/6144）。
    """
    if finish_reason != "length" or retry_used:
        return False
    return bool(native_calls) or empty_body


def _looks_like_plan_only(text: str, has_tools: bool) -> bool:
    """正文是否只是"声明打算做什么"（无实质内容）——09-20 实测：27B 只输出
    "我来帮你分析…先调取相关数据和复盘方法论。"，工具调用=0，循环把它当终答
    交付，用户看到的就是"执行到一半就停"。长的/不含工具动词的不拦。"""
    if not has_tools:
        return False
    t = re.sub(r"\s+", " ", (text or "")).strip()
    if not t or len(t) > 240:
        return False
    return bool(_PLAN_VERB_RE.search(t) and _PLAN_INTENT_RE.search(t))


def _note_msg(text: str, label: str = "【系统提示】") -> dict:
    """循环内的临时提示 → **尾部用户消息**（绝不能是 system）。

    09-19 缓存实测：Spark 的聊天模板把所有 system 消息提到提示最前面渲染，于是
    "追加到消息末尾的 system 提示"实际落在提示**头部**——而这类提示每轮内容都不同
    （知识注入按问题召回、额度/重复提醒按状态），一变就把整段前缀缓存作废：同一会话
    第 2 轮读出"复用 0 / 共 5782 token"，而 head 指纹显示头部确实变了。改成 user
    角色后提示留在末尾，复用率回到 80–97%。
    """
    return {"role": "user", "content": label + text}


def _collect_finalize_data(current_msgs: list, max_results: int = 6,
                           max_chars: int = 700) -> list:
    """工具结果数据块（去重、仅尾部、限长）——供摘要视图与兜底交付。"""
    blocks = []
    for m in current_msgs:
        if m.get("role") == "tool":
            c = str(m.get("content") or "").strip()
            if c and len(c) > 20:
                blocks.append(c)
    seen, uniq = set(), []
    for c in blocks:
        k = c[:80]
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    return [c[:max_chars] for c in uniq[-max_results:]]


def _append_tail_note(msgs: list, text: str) -> list:
    """把一条提醒追加到请求**末尾**（纯追加，不改前缀 → 缓存可复用）。

    09-19 缓存实测（Spark-X2.5 原生 llama-server）：
    · 同一会话内纯追加的连续请求 → 复用 87–92%；
    · 把提醒拼进系统消息（提示头）或换用另一份提示（收口轮的"数据摘要版"）
      → 该轮 0%，且下一轮从陌生提示头开始，连系统提示都匹配不上。
    所以本轮起：一切每轮变化的文本（预算提醒/收口指令）都只能走这里。
    """
    if not text:
        return msgs
    out = list(msgs)
    if out and out[-1].get("role") == "user":
        # 合并进最后一条用户消息：token 序列仍是纯追加，且不产生连续两条 user
        last = dict(out[-1])
        last["content"] = str(last.get("content") or "") + text
        out[-1] = last
    else:
        out.append({"role": "user", "content": text.lstrip("\n")})
    return out


def _finalize_tail_directive(lang: str = "zh") -> str:
    """收口轮的尾部指令（四语）——替代旧的"重建数据摘要视图"。

    收口轮旧实现把 messages 整体换成 [收口system, 问题, 数据N..., 追问] 并撤掉
    tools，等于把提示头换成另一份内容：该轮必然 0% 复用，而且会把下一轮的
    缓存也打掉（下一轮的开头与这份陌生提示无公共前缀）。改为在既有请求尾部
    追加指令后，收口轮自身≈全命中，下一轮也仍能命中系统提示+历史。
    """
    return _msg("finalize_tail", lang)


def _finalize_directive() -> str:
    return ("📣 任务收尾：用户正在等待回答。下面以【数据N】逐条列出已收集到的工具结果。"
            "请仅基于这些数据直接用简体中文写出完整分析（包含关键数字与结论），直接输出正文；"
            "不要提到任何工具/调用过程，不要输出任何工具调用格式，写完即停。")


def _build_finalize_digest(current_msgs: list, max_results: int = 6,
                           max_chars: int = 700, lang: str = "zh") -> list:
    """终答轮请求视图：数据摘要式消息（无 tool 角色/无 tool_calls/无 tools 键）。

    09-08 19:28 实况：12 轮工具形态历史把模型锁死在"只出工具调用"——
    tools=[] 且温度抖动后仍 30s 只出 1 个工具调用、正文 0 字；同数据但无
    工具形态（B-DIGEST 实测）56s 产出 873 字完整分析、零工具调用。"""
    out = [{"role": "system", "content": _finalize_directive()}]
    users = [m for m in current_msgs if m.get("role") == "user"]
    if users:
        out.append({"role": "user", "content": str(users[-1].get("content") or "")[:1200]})
    for i, c in enumerate(_collect_finalize_data(current_msgs, max_results, max_chars), 1):
        out.append({"role": "assistant", "content": f"【数据{i}】{c}"})
    # 防回显：数据块以 assistant 结尾时，模型会"续写/原样回显"最后一条
    # （09-08 20:38 实况：终答 274 字就是【数据7】的整段抄写）——必须追加
    # 一条 user 提问，模型只能正面应答，结构上不可能续写。
    out.append({"role": "user", "content": _msg("finalize_ask", lang, **{"lang_name": {
        "zh": "简体中文", "en": "English", "ja": "日本語", "ru": "русский"}.get(lang, "English")})})
    return out

STALL_FIRST = 90        # 首 token 前静默上限
STALL_AFTER = 180       # 有输出后静默上限
HEARTBEAT = 60          # 心跳节拍

# ── steer 收件箱（session_id -> 队列；step 边界认领）────────────────
# 同步操作：单事件循环内 append/清空天然原子，且避免 Lock 绑定事件循环
# 导致的跨测试/跨请求 RuntimeError。
_steer_inbox: dict[str, list[str]] = {}


def queue_steer(session_id: str, text: str) -> int:
    """新消息插入队（不打断进行中的轮），返回当前队列长度。"""
    _steer_inbox.setdefault(session_id, []).append(text)
    return len(_steer_inbox[session_id])


def _claim_steer(session_id: str) -> list[str]:
    msgs = _steer_inbox.get(session_id) or []
    _steer_inbox[session_id] = []
    return msgs


def _clear_steer(session_id: str) -> None:
    _steer_inbox.pop(session_id, None)


def _record_engine_usage(session_id: str, line: str) -> None:
    """从原始 SSE 行提取引擎真实用量（云端 usage / llama.cpp timings）→ 上下文统计。

    薄循环此前把这些字段整体丢弃；缓存命中率与真实输入 token 数只能从这里来。
    """
    try:
        payload = line[6:].strip()
        if not payload or payload == "[DONE]":
            return
        event = json.loads(payload)
    except Exception:
        logger.debug("引擎用量行解析失败", exc_info=True)
        return
    if not isinstance(event, dict):
        return
    usage, timings = event.get("usage"), event.get("timings")
    if not usage and not timings:
        return
    try:
        import context_stats
        context_stats.record_usage(session_id, usage if isinstance(usage, dict) else None,
                                   timings if isinstance(timings, dict) else None)
    except Exception:
        logger.debug("上下文统计记录用量失败", exc_info=True)


class ThinAgentLoop:
    """单一 agent 循环：cloud/local 共用，差异只体现在请求组装与辅助层开关。"""

    def __init__(self, messages: list, model: str, api_url: str, headers: dict,
                 session_id: str = "", access_mode: str = "confirm",
                 thinking_level: str = "high", is_local: bool | None = None,
                 tool_whitelist: set | None = None):
        self.session_id = session_id
        self.model = model
        self.api_url = api_url
        self.headers = headers
        self.access_mode = _normalize_access(access_mode)
        self.thinking_level = thinking_level
        # is_local 由路由层显式传入（v1 同口径）；未传时按 URL 推导兜底
        self.is_local = _is_local_llm_url(api_url) if is_local is None else bool(is_local)
        self.current_msgs: list = _strip_transient_reminders([dict(m) for m in messages])
        self.last_user_text = _extract_last_user_text(self.current_msgs)
        _maybe_add_inline_file_note(self.current_msgs, self.last_user_text)
        # 语言真值：本回合只算一次（含历史交叉校验），下方所有消费点统一取用。
        # 旧实现各处各自调用、按字母数判定，"你的SOUL.md是什么"被判成英文用户，
        # 导致提示词/语言锚/翻译方向全按英文走（09-19 根因）。
        _hist = [m.get("content") for m in self.current_msgs
                 if m.get("role") == "user" and isinstance(m.get("content"), str)]
        if _hist and _hist[-1] == self.last_user_text:
            _hist = _hist[:-1]
        self.lang_decision = detect_language_decision(self.last_user_text, _hist)
        self.user_lang, self.user_lang_confident = self.lang_decision
        self.steps = 0
        self.native_tools = False
        self.native_fallback_used = False
        self.parallel_disabled = False  # 空名后下一轮关并行（deepseek quirk 缓解）
        self.tool_whitelist = tool_whitelist  # 子代理隔离：目录收窄 + 执行拒绝
        self._plan_injected = False
        self._len_retry_used = False           # max-tokens 截断重试（每轮一次）
        self._force_thinking_off_next = False  # 截断重试轮强制关思考
        self._recent_sigs: list[str] = []      # 重复同参调用检测（结构信号）
        self._repeat_reminders = 0             # 提醒注入计数（≤3 次/turn）
        self._gen_retry_used = False           # 复读/截断的确定性打破重试（每 turn 一次）
        self._retry_temp = None                # 重试轮温度抖动（None=默认 0.0）
        self._last_sig = None                  # 停滞闸门：上轮签名
        self._same_sig_rounds = 0              # 停滞闸门：同参连续轮数
        self._empty_gen_retry_used = False     # 引擎空生成重试（每 turn 一次）
        self._tool_rounds_no_answer = 0        # 连续工具轮（无实质正文交付）计数
        self._finalize_round = False           # 停滞闸门收口轮：无工具直接作答
        self._plan_nudge_used = 0              # 空转闸门：只声明计划不行动的一次纠正
        self._finalize_retry_used = False      # 终答轮空生成温度重采（一次）
        self._quota_hint_injected = False      # 当日额度用尽提示（每 turn 一次）
        # 可配置阈值：主循环用默认值，子代理实例化后收紧（09-13 子代理收紧）
        self.max_steps = MAX_STEPS             # 步数上限
        self._same_sig_limit = 5               # 同参空转收口：连续同参轮数
        self._tool_rounds_limit = 12           # 工具轮无正文收口：连续工具轮数
        # 信息增量饱和（每轮用户消息重置，见 run）
        self._info_seen: set = set()           # 本轮已收集的信息点
        self._info_rounds = 0                  # 已完成的信息轮
        self._gain_low_streak = 0              # 连续低增量轮数
        self._search_used = 0                  # 本轮检索类工具调用次数
        self._narrow_search = False            # 软饱和：下一轮撤下检索工具
        self._last_round_had_tools = False     # 低档思考判定：作答轮 = 上一轮无工具调用
        self._search_calls_by_name: dict = {}  # 同一检索工具本回合调用次数（换 query 也算）
        # 流式重复叙述闸门
        self._last_round_text = ""             # 上一轮正文（复读判定的基准）
        self._markup_state = 0                 # 0=正文 1=think 2=丢弃标记（流式标记状态）
        self._markup_closer = ""               # 当前状态的闭合标记
        self._think_carry = ""                 # 可能是标签前缀的尾巴（<thi + nk>）
        self._round_buf: list = []             # 本轮已缓冲但未下发的正文
        self._round_dup = False
        self._round_dup_decided = False
        self._retry_freq = None                # 重试轮频率惩罚（压制重复 token）
        # Scope：工具目录 + waterfall 宿主；loop 自身作为服务供钩子读取
        self.scope = Scope(name=f"agent:{session_id or uuid.uuid4().hex[:8]}")
        self.scope.provide("loop", self)
        setup_all(self.scope)
        try:
            from memory import _extract_learnings_heuristic
            _extract_learnings_heuristic(self.last_user_text, self.session_id)
        except Exception:
            logger.debug("learnings heuristic skipped", exc_info=True)

    # ── 工具集 ────────────────────────────────────────────
    def _active_tools(self) -> list:
        from agent_loop import TOOLS, _cap_tools, _filter_tools
        if self.is_local:
            # ── 本地引擎：工具表**从第 1 轮就固定**（= 全部可用工具，按注册表顺序）──
            # 09-20 实测链路：工具表渲染在**提示头** → 而混合/线性注意力模型（Spark、
            # Qwen3.8-27B、Bonsai 都是）的缓存只在提示"严格续写"时复用 → 只要工具表
            # 按措辞增删（旧实现 12↔14 交替、连"只增不减"的头几轮也在增长），
            # 该轮就是 0% 复用（用户实测：连续 4 轮普通对话全 0%）。
            # 因此本地一律用整套工具；"本轮不需要检索"这类约束改由**尾部提示**表达。
            # 云端不受影响（提示成本可忽略、且工具筛选对云端模型仍有价值）。
            tools = list(TOOLS)
        else:
            tools = _filter_tools(self.last_user_text, TOOLS) if self.last_user_text else list(TOOLS)
            tools = _cap_tools(tools, 14) if len(tools) > 14 else tools
        tools = _filter_tools_by_access(tools, self.access_mode)
        if not self.is_local and len(tools) > 12:
            # 元工具（委派/技能/定时）cap 保底——委派被裁掉会让模型"看不到"
            # 子代理机制而自己硬扛（09-06 真机验收 C 场景发现）
            _keep = ["delegate_task", "use_skill", "create_cron", "create_skill"]
            # app 意图保底（09-11 事故："打开相册"→意图含 open_app，但 cap 按
            # 优先级截断把它切在第 22 位 → 模型无工具只能空谈，0 次调用）
            _names = {t.get("function", {}).get("name") for t in tools}
            if _names & {"open_app", "open_folder"}:
                _keep += ["open_app", "open_folder"]
            tools = _cap_tools(tools, 12, keep_first=tuple(_keep))
        if not self.is_local:
            tools = _ensure_market_tools(tools, self.last_user_text)
        # 会话级目录一致性：本会话已成功使用 / 已加载技能声明的工具，不因最后
        # 一条消息的措辞离场（意图过滤只增不删——三家共识）。白名单（子代理）
        # 在下方仍照常过滤，隔离不受影响。
        used = _SESSION_USED_TOOLS.get(self.session_id) or set()
        if used:
            have = {t.get("function", {}).get("name") for t in tools}
            for name in used - have:
                _extra = [t for t in TOOLS if t.get("function", {}).get("name") == name]
                if _extra:
                    tools.append(_extra[0])
        if self.tool_whitelist is not None:
            tools = [t for t in tools if t.get("function", {}).get("name") in self.tool_whitelist]
        # ── 会话级定型（09-20）：只增不减 + 顺序固定 ──
        # 目的：让**提示头逐轮一致**（工具表参与头部渲染）。查询相关的过滤仍生效，
        # 但已在本会话出现过的工具不再因为换了措辞而离场；顺序按注册表顺序固定，
        # 避免"相关度排序"每轮抖动。新加入工具的那一轮头部会变（丢一次缓存），
        # 之后恢复稳定——能力优先于缓存，缓存是尽力而为。
        _locked = _SESSION_TOOL_NAMES.get(self.session_id)
        _now = {t.get("function", {}).get("name") for t in tools}
        if _locked:
            for _n in _locked:
                if _n not in _now:
                    _extra = [t for t in TOOLS if t.get("function", {}).get("name") == _n]
                    if _extra:
                        tools.append(_extra[0])
        _order = {t.get("function", {}).get("name"): i for i, t in enumerate(TOOLS)}
        tools.sort(key=lambda t: _order.get(t.get("function", {}).get("name"), 999))
        _SESSION_TOOL_NAMES[self.session_id] = [t.get("function", {}).get("name") for t in tools]
        return tools

    def _engine_model(self) -> str:
        # 云端请求必须用所选云端模型名——本地引擎的模型 id 发给云端必然 400
        # （09-06 19:29 事故：本地 Qwen3.8 加载中，"测试"被路由到 deepseek，
        # body.model 却是 Qwen 本地路径 → 400 "Model Not Exist"）
        if not self.is_local:
            return self.model
        import local_llm
        return getattr(local_llm._engine, "current_model_id", "") or self.model

    def _step_log(self, phase: str, detail: str = "") -> None:
        """终端式每步日志：统一前缀，进 sidecar.log 与 app 实况日志。"""
        logger.info("[THIN][step %s] %s%s", self.steps, phase,
                    f" | {detail}" if detail else "")

    # ── 请求组装（request waterfall 之前的宿主编排）──────────
    def _build_request(self, engine_model: str) -> dict:
        import local_llm  # 懒加载：与文件内其它用法一致，避免模块级循环依赖
        light = self.is_local and _is_light_query(self.last_user_text, self.current_msgs)
        native_ok = self.is_local and _local_native_tools_ok() and not self.native_fallback_used
        # 工具表必须**逐轮一致**：它渲染在提示头，而缓存（尤其混合/线性注意力模型，
        # 如 Spark-X2.5 / Qwen3.8-27B / Bonsai 都是）只在提示是"严格续写"时复用。
        # 旧实现让"闲聊"类问题（light）整轮撤掉工具 → 头部与上一轮不同 → 该轮 0%
        # 复用（09-20 用户实测：普通对话两轮就是 0%，对照实验：严格续写 74%）。
        # 闲聊的约束改由尾部提示表达，不再动工具表。
        tools = self._active_tools()
        # 收口轮与普通轮用**同一套工具、同一份提示形态**：tools 参与模板渲染，撤掉它们
        # 等于换提示头，收口轮自身与下一轮的缓存会一起失效（09-19 实测）。收口约束改由
        # 尾部指令表达（见 _finalize_tail_directive / _append_tail_note）。
        self.native_tools = bool(tools) and native_ok

        # 检索收紧只作为**尾部提醒**下发，不再从请求里撤工具：tools 参与提示头渲染，
        # 撤掉工具＝换提示头＝整段缓存失效（09-19 实测 0%）。约束仍由尾部提醒表达。
        if tools and self._narrow_search and not self._finalize_round:
            self._step_log("检索收紧", "尾部提醒：本轮不再检索，直接用已有数据作答")
        msgs = _merge_system_messages(_sanitize_tool_messages(list(self.current_msgs)))
        if self._narrow_search or self._search_used:
            msgs = _append_tail_note(msgs, self._search_reminder())
        body = {
            "model": engine_model,
            "messages": msgs,
            "stream": True,
            "temperature": self._retry_temp if self._retry_temp is not None else 0.0,
            "frequency_penalty": self._retry_freq if self._retry_freq is not None else 0.6,
            "max_tokens": _resolve_max_tokens(self.model, local=self.is_local,
                                              override=_custom_engine_max_tokens()),
            "stop": ["<|im_end|>", "<|endoftext|>", "<end_of_turn>", "<eos>"],
        }

        if not self.is_local:
            body["tools"] = [dict(t) for t in tools]
            if self.parallel_disabled:
                body["parallel_tool_calls"] = False
            # OpenAI 兼容端点要显式声明才在流式响应里回传 usage（缓存命中率来源）；
            # Anthropic 风格端点（/v1/messages）不接受该字段，按 URL 判别。
            if self.api_url.rstrip("/").endswith("/chat/completions"):
                body["stream_options"] = {"include_usage": True}
            try:
                import context_stats
                context_stats.record_request(
                    self.session_id, msgs, tools, model_path="",
                    limit=0, limit_source="unknown")
            except Exception:
                logger.debug("上下文统计快照失败（云端）", exc_info=True)
            return body

        # 本地：原生 tools + 精简纪律；legacy 回退围栏提示词；闲聊不加提示
        sys_prompt = ""
        if self.native_tools:
            body["tools"] = [dict(t) for t in tools]
            sys_prompt = _NATIVE_LEAN_PROMPT
        elif not light:
            # 围栏路径：工具说明放**最后一条消息尾部**（09-20 改）。
            # 放系统提示头部时，长提示会把它稀释掉：同一个 27B 模型在短提示下会
            # 老实按应用的 ```tool 格式发调用，在完整系统提示（本机约 4.6k token）下
            # 就漂移成自述/自创方言 → 循环收不到调用，把"我来帮你分析…先调取数据"
            # 当终答交付（"只预告不行动"）。尾部是模型注意力最近处，且属纯追加、
            # 不破坏前缀缓存。
            _fence = _build_local_tools_prompt(tools)
            if _fence:
                msgs = _append_tail_note(msgs, "\n\n【工具使用说明（系统）】\n" + _fence)
                body["messages"] = msgs      # body 已构造，必须写回（否则追加丢失）
        if sys_prompt:
            if msgs and msgs[0].get("role") == "system":
                msgs[0] = dict(msgs[0])
                msgs[0]["content"] = sys_prompt + "\n\n" + str(msgs[0].get("content", ""))
            else:
                msgs.insert(0, {"role": "system", "content": sys_prompt})
        if len(self.current_msgs[-1].get("content", "")) > 8000:
            msgs[0]["content"] += (
                "\n\n📏 思考预算（长输入）：请先简短思考（≤300 字），然后立刻在正文"
                "写出完整分析——关键数字和结论必须写进正文。")
        body["frequency_penalty"] = 0.6
        # 思考档位：尊重用户在界面的选择（关闭/高/最高）——09-19 修正：此前
        # `steps > 1` 一律强制关闭，等于用户的档位只在第一回合有效；关掉思考后
        # 模型把英文推理倒进正文（单轮上万字过程文本、逐字复读、交付英文）。
        _lvl = (self.thinking_level or "high").lower()
        if _lvl == "off" or self._force_thinking_off_next:
            body["chat_template_kwargs"] = {"enable_thinking": False}
        elif _lvl == "low":
            # 低档：思考照开（推理走思考通道，正文才干净），但下方对工具后续轮限长——
            # 09-19 实测：只关思考并不省时（推理倒进正文，token 量不变），且交付变英文
            body["chat_template_kwargs"] = {"enable_thinking": True}
        else:
            body["chat_template_kwargs"] = {"enable_thinking": True}
        if _lvl == "low" and self.steps > 1:
            # 低档：首轮不限长（规划要充分），工具后续轮限长——少生成 token 是唯一直接
            # 提速杠杆（每轮 1.5–3.5 分钟 → 几十秒）；超限时现有截断重试会兜一次
            body["max_tokens"] = min(int(body.get("max_tokens") or _LOW_MAX_TOKENS), _LOW_MAX_TOKENS)
        if self.parallel_disabled:
            body["parallel_tool_calls"] = False
        # MLX 引擎（mlx_lm.server）同样只在 include_usage 时才回传 usage（含 cached_tokens）；
        # python/原生 llama.cpp 引擎不认识该字段，不下发。
        if str(getattr(local_llm._engine, "_active_backend", "") or "") == "mlx":
            body["stream_options"] = {"include_usage": True}
        # 上下文统计：记录本轮实际发出的内容（分类快照；finalize 轮是数据摘要视图，
        # 不代表真实上下文，故在此之前记录）
        try:
            import context_stats
            if self.is_local:
                _lim = int(getattr(local_llm._engine, "model_token_limit", 0) or 0)
                # 精确计数要的是模型文件路径：engine_model 是解析后的引擎模型（GGUF 路径），
                # self.model 可能只是声明名（latiao-local-default）
                _mp = str(engine_model) if ("/" in str(engine_model)) else str(
                    getattr(local_llm._engine, "current_model_id", "") or "")
                context_stats.record_request(
                    self.session_id, msgs, tools, model_path=_mp,
                    limit=_lim, limit_source="local_engine")
            else:
                context_stats.record_request(
                    self.session_id, msgs, tools, model_path="",
                    limit=0, limit_source="unknown")
        except Exception:
            logger.debug("上下文统计快照失败", exc_info=True)
        if self._finalize_round:
            # 终答轮（09-19 改追加式）：保留同一份 tools 与消息，只在尾部追加收口指令。
            # 旧实现整体换用数据摘要视图，缓存代价极高（见 _append_tail_note 注释）；
            # 空正文/仍发工具调用的兜底不变：先用尾部提醒重采一次，仍空则退回
            # _collect_finalize_data 直接交付数据（见收口轮交付分支）。
            body["messages"] = _append_tail_note(
                body["messages"], _finalize_tail_directive(self.user_lang))
            body["parallel_tool_calls"] = False
            body["chat_template_kwargs"] = {"enable_thinking": False}
        if _is_custom_engine():
            # 第三方 fork 引擎（如 Prism/Bonsai）：**最后**统一去掉我们的采样参数，
            # 让引擎按模型自带默认采样（GGUF 元数据 general.sampling.*）。我们默认的
            # temp 0.0 + frequency_penalty 0.6 是为本地小模型压复读用的，对推理型
            # 模型会明显降质（Bonsai 2 推荐 thinking 模式 temp 1.0/top_p 0.95/top_k 20）。
            # 注：必须放在所有 body["..."]= 赋值之后，否则会被后面的赋值覆盖。
            body.pop("temperature", None)
            body.pop("frequency_penalty", None)
        return body

    # ── 流读取（本地走传输封装，云端直连；停滞/心跳共用）──────
    async def _stream(self, client: httpx.AsyncClient, body: dict):
        if not self.is_local:
            async with client.stream("POST", self.api_url, json=body,
                                     headers=self.headers) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    yield line
            return
        async with _local_llm_stream(client, self.api_url, body, self.headers) as r:
            aiter = r.aiter_lines()
            silent = 0
            any_out = False
            while True:
                try:
                    line = await asyncio.wait_for(anext(aiter), timeout=HEARTBEAT)
                    silent = 0
                except asyncio.TimeoutError:
                    silent += HEARTBEAT
                    stall = STALL_FIRST if not any_out else STALL_AFTER
                    if silent < stall:
                        yield ": keepalive\n\n"
                        continue
                    raise TimeoutError(f"模型输出停滞超 {stall}s")
                except StopAsyncIteration:
                    return
                if line and not line.startswith(": ") and "keepalive" not in line[:30]:
                    any_out = any_out or '"delta"' in line or '"reasoning"' in line
                yield line

    # ── 单 step：流式采样（实时流出 reasoning/content），结果经
    # __result__ 终端事件回传（消费者吞掉，不下发前端）──
    async def _sample(self, client, body):
        streamed, body_text, reasoning = "", "", ""
        native: dict[int, dict] = {}
        raw = 0
        finish_reason = None
        deadline = time.monotonic() + 900
        async for line in self._stream(client, body):
            if time.monotonic() > deadline:
                raise _GenerationLoopError("单步生成超时(900s)，已截断")
            if not line.startswith("data: "):
                continue
            if '"usage"' in line or '"timings"' in line:
                # 上下文统计：真实输入 token 数与缓存命中（云端 usage / llama.cpp timings）
                _record_engine_usage(self.session_id, line)
            try:
                done, delta = _parse_delta_line(line)
            except Exception:
                continue
            if done:
                break
            if not delta:
                continue
            fr = delta.pop("finish_reason", None)
            if fr:
                finish_reason = fr
            raw += 1
            for tc in delta.get("tool_calls", []) or []:
                idx = tc.get("index", 0)
                buf = native.setdefault(idx, {"id": "", "type": "function",
                                              "function": {"name": "", "arguments": ""}})
                if tc.get("id"):
                    buf["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    buf["function"]["name"] += fn["name"]
                if fn.get("arguments"):
                    buf["function"]["arguments"] += fn["arguments"]
            content = delta.get("content", "") or ""
            think = delta.get("reasoning") or delta.get("reasoning_content") or ""
            if raw % 40 == 0 and _detect_text_loop(streamed + think):
                streamed = _strip_repeat_tail(streamed)
                raise _GenerationLoopError("输出复读循环，已截断")
            if think:
                reasoning += think
                streamed += think
                yield {"reasoning": think, "ts": int(time.time() * 1000)}
            if content:
                streamed += content
                body_text += content
                # 真流式：正文 delta 即刻下发（09-06 用户反馈"直接蹦出答案"）
                yield {"content": content, "ts": int(time.time() * 1000)}
        yield {"__result__": (streamed, body_text, reasoning, native, finish_reason)}

    # ── 主循环 ────────────────────────────────────────────
    def _maybe_repeat_reminder(self, tool_calls: list) -> bool:
        """重复同参调用软提醒（ZCode/DSH 同构）：连续 3 轮「同工具+同参数」时
        注入一条系统提醒引导换路。结构信号、不拦截交付、每 turn ≤3 次。"""
        sig = _tool_calls_signature(tool_calls)
        self._recent_sigs.append(sig)
        if (len(self._recent_sigs) >= 3
                and len(set(self._recent_sigs[-3:])) == 1
                and self._repeat_reminders < 3):
            self._repeat_reminders += 1
            self._recent_sigs = []
            fname = (tool_calls[-1].get("function") or {}).get("name", "?")
            self.current_msgs.append(_note_msg(
                f"⚠️ 你已经连续 3 轮用完全相同的参数调用 `{fname}`。不要原样重复——"
                "请更换查询措辞/参数，或改用其他工具/方法达成目标。"))
            self._step_log("重复调用提醒", f"{fname} ×3 同参")
            logger.info("thin loop: 重复同参调用提醒 (%s ×3)", fname)
            return True
        return False

    def _maybe_quota_hint(self, result: str) -> None:
        """工具结果命中"当日额度已用尽" → 本轮注入一次提示（每 turn 一次）。

        事前引导而非事后靠错误文案：限额是当日全局的（上游账户共享），
        用尽后再调必然秒回失败。提示只进当前 turn 的消息流，天然跨天过期。
        同时写入持久标记，让当天后续任务第一轮就预注入（见 run() 开头）。
        检测用完整特征词，避免把"达到/上限"等普通报错误判成限额。"""
        if self._quota_hint_injected or not result:
            return
        if "当日调用次数已用完" not in result:
            return
        self._quota_hint_injected = True
        _mark_quota_exhausted("mx_query")
        self.current_msgs.append(_note_msg(
            "⏰ 提示：mx_query 当日的免费额度已用尽（150 次/日，接口方统计），当天不会再成功。"
            "请直接使用替代工具（ak_finance / tavily_search）继续补充数据；"
            "若数据已足够，就直接写出完整分析（简体中文，含关键数字与结论）。"))
        self._step_log("额度提示", "已注入当日限额提示（每 turn 一次）+ 持久标记写入")
        logger.warning("thin loop: 检测到工具当日额度已用尽，注入事前提示")

    def _note_tool_counts(self, tool_calls: list) -> bool:
        """同一检索类工具调用 ≥N 次 → 视为饱和（换 query 重搜绕开签名守卫的兜底）。"""
        names = _search_tool_names()
        for tc in tool_calls or []:
            nm = ((tc.get("function") or {}).get("name") or "")
            if nm in names:
                self._search_calls_by_name[nm] = self._search_calls_by_name.get(nm, 0) + 1
        return any(v >= _SEARCH_TOOL_REPEAT_LIMIT for v in self._search_calls_by_name.values())

    def _note_round_info(self, results: list) -> tuple:
        """信息增量饱和判定：返回 ("hard"|"soft"|None, 本轮增量比例)。

        hard：连续 2 轮增量低于阈值（且已完成 ≥2 个信息轮）→ 强制收口作答；
        soft：本轮增量低或检索预算用尽 → 下一轮撤下检索类工具。
        """
        text = "\n".join(r for r in (results or []) if r)
        if not text.strip():
            return None, 1.0
        gain, _new = _info_gain(text, self._info_seen)
        self._info_seen |= _info_points(text)
        self._info_rounds += 1
        self._gain_low_streak = self._gain_low_streak + 1 if gain < _INFO_GAIN_RATIO else 0
        if self._info_rounds >= 2 and self._gain_low_streak >= 2:
            return "hard", gain
        if self._info_rounds >= 2 and gain < _INFO_GAIN_RATIO:
            return "soft", gain
        return None, gain

    def _count_search_calls(self, tool_calls: list) -> int:
        names = _search_tool_names()
        return sum(1 for tc in tool_calls
                   if ((tc.get("function") or {}).get("name") in names))

    def _search_reminder(self) -> str:
        """检索预算提醒（四语）。措辞必须与"工具是否仍可用"一致——09-19 教训：
        工具还在却下发"不要再检索、立刻作答"的硬命令 → 模型原地复读（逐字重复三段）。"""
        remain = max(0, _SEARCH_BUDGET_CALLS - self._search_used)
        if self._narrow_search:
            return _msg("search_budget_closed", self.user_lang)
        return _msg("search_budget", self.user_lang, used=self._search_used,
                    total=_SEARCH_BUDGET_CALLS, remain=remain)

    # ── 流式重复叙述闸门（09-19）────────────────────────────
    # 既有机制只在**历史**里把近重复叙述截断，而流式早已把三段逐字复读推给用户。
    # 这里改为当轮拦截：本轮正文开头若与上一轮正文高度相似，整轮不再下发。
    def _split_think_stream(self, chunk: str) -> tuple:
        """拆流式 content：think 块 → 思考通道；工具调用标记 → 丢弃（多方言）。

        09-19 实测这个 GGUF 模型会随采样换方言：<think>、<tool_call><function=>、
        <function_calls><invoke name="x">（甚至漏左尖括号）。这里用表驱动状态机
        统一处理：进入任一"丢弃态"后一直吞到对应闭合标签，跨 chunk 用 carry 暂存
        可能是标签前缀的尾巴。工具调用标记是结构信息，真正的执行由解析器从存文本
        完成，不需要展示给用户。
        """
        data = self._think_carry + (chunk or "")
        self._think_carry = ""
        out, think = [], []
        i = 0
        while i < len(data):
            if self._markup_state == 0:
                best = None
                for marker, closer, mode in _MARKUP_MARKERS:
                    j = data.find(marker, i)
                    if j >= 0 and (best is None or j < best[0]):
                        best = (j, marker, closer, mode)
                if best is None:
                    tail, keep = data[i:], 0
                    for L in range(min(len(tail), 16), 0, -1):
                        if any(marker.startswith(tail[-L:]) for marker, _c, _m in _MARKUP_MARKERS):
                            keep = L
                            break
                    if keep:
                        out.append(tail[:-keep])
                        self._think_carry = tail[-keep:]
                    else:
                        out.append(tail)
                    break
                j, marker, closer, mode = best
                out.append(data[i:j])
                self._markup_state = 1 if mode == "think" else 2
                self._markup_closer = closer
                i = j + len(marker)
                continue
            closer = getattr(self, "_markup_closer", "</think>")
            j = data.find(closer, i)
            if j < 0:
                tail, keep = data[i:], 0
                for L in range(min(len(tail), len(closer)), 0, -1):
                    if closer.startswith(tail[-L:]):
                        keep = L
                        break
                if self._markup_state == 1:
                    think.append(tail[:-keep] if keep else tail)
                if keep:
                    self._think_carry = tail[-keep:]
                break
            if self._markup_state == 1:
                think.append(data[i:j])
            self._markup_state = 0
            self._markup_closer = ""
            i = j + len(closer)
        return "".join(out), "".join(think)

    def _take_think_carry(self) -> str:
        """轮结束时把暂存的标签前缀当普通文本处理（流已结束，不可能是完整标签）。"""
        carry, self._think_carry = getattr(self, "_think_carry", ""), ""
        return carry

    def _gate_round_content(self, text: str) -> str | None:
        """返回要下发的文本；None = 暂不发送（缓冲中/判定为复读）。"""
        if self._round_dup_decided:
            return None if self._round_dup else text
        self._round_buf.append(text)
        buffered = "".join(self._round_buf)
        if len(buffered) < _DUP_PROBE_CHARS:
            return None                       # 采样太少，先缓冲（几十毫秒级）
        self._round_dup_decided = True
        self._round_dup = _looks_like_replay(self._last_round_text, buffered)
        if self._round_dup:
            self._step_log("重复叙述拦截", f"{len(buffered)}字 与上一轮近重复，本轮不再下发")
            return None
        return buffered                       # 正常：把缓冲一次性放出

    def _flush_round_gate(self) -> str:
        """轮结束时的残余缓冲：未判定即按正常内容下发。"""
        if not self._round_dup_decided and self._round_buf:
            self._round_dup_decided = True
            self._round_dup = _looks_like_replay(self._last_round_text, "".join(self._round_buf))
            return "" if self._round_dup else "".join(self._round_buf)
        return ""

    async def _stagnation_gate(self, tool_calls: list):
        """同参空转 ≥5 轮 → 强制终答轮收口。返回要下发的事件或 None。

        09-08 09:06 事故：同参调用被护栏拒绝后模型空转至 40 步上限零交付。
        结构信号（规范化签名连续相同），不看内容语义；基于上下文已收集的
        全部数据强制产出最终回答。终答由主循环下一轮自身产生（tools=空、
        关思考 + 终答指令）——14:29 曾用独立 _final_answer_extraction 补答，
        它要排同一把引擎串行锁，与在途流互斥 → 等锁 120s 超时失败收尾。"""
        sig = _tool_calls_signature(tool_calls)
        if sig == self._last_sig:
            self._same_sig_rounds += 1
        else:
            self._last_sig = sig
            self._same_sig_rounds = 0
        self._tool_rounds_no_answer += 1
        if (self._same_sig_rounds < self._same_sig_limit
                and self._tool_rounds_no_answer < self._tool_rounds_limit):
            return None
        logger.warning("thin loop: 同参空转 %d 轮，停滞闸门收口", self._same_sig_rounds)
        if self._finalize_round:
            # 收口轮已用过一次仍被再次触发（理论上 tools=[] 不再产生调用）
            # → 直接交付诊断，不再无限轮转
            return {"content": "\n\n" + _msg("repeat_calls", self.user_lang)}
        self._finalize_round = True
        # 保证终答轮能挤进步数上限内（闸门在最后一轮触发时，continue 后
        # while 条件会直接退出、终答轮不会执行）
        if self.steps >= self.max_steps:
            self.steps = self.max_steps - 1
        self._same_sig_rounds = 0
        self._tool_rounds_no_answer = 0
        self._step_log("停滞闸门", "收口：下一轮强制直接作答（tools=空、关思考）")
        return None

    async def run(self):
        self.steps = 0
        self._info_seen = set()
        self._info_rounds = 0
        self._gain_low_streak = 0
        self._search_used = 0
        self._narrow_search = False
        self._last_round_text = ""
        self._last_round_had_tools = False
        self._search_calls_by_name = {}
        self._round_buf = []
        self._round_dup = False
        self._round_dup_decided = False
        mode = "native" if (self.is_local and _local_native_tools_ok()) else (
            "legacy-fence" if self.is_local else "cloud-native")
        # 本会话 id 供子代理后台完成通知回寻父会话（contextvars 随任务派生带入）
        from agent.subagent import _CURRENT_PARENT_SESSION, claim_bg_results
        _CURRENT_PARENT_SESSION.set(self.session_id)
        self._step_log("任务开始",
                       f"model={self.model} endpoint={self.api_url} "
                       f"is_local={self.is_local} access={self.access_mode} "
                       f"mode={mode} 消息={len(self.current_msgs)}")
        # 空闲期完成的后台子代理：结果通知在本轮启动时注入上下文（一次性）
        _bg_notes = claim_bg_results(self.session_id)
        if _bg_notes:
            _note_text = "\n\n".join(
                f"【后台子代理通知】{n['agent']} 已完成任务「{n['task']}」，结果：\n{n['result']}"
                for n in _bg_notes)
            self.current_msgs.append(_note_msg(_note_text, label="【后台通知】"))
            self._step_log("后台通知注入", f"{len(_bg_notes)} 条")
        # 跨会话知识注入（旧循环 _build_chat_messages 有、薄循环重构时遗漏
        # ——09-07 接回）：按当前问题语义检索学习库，高置信条目注入参考
        try:
            from memory import _retrieve_relevant_learnings
            if self.last_user_text:
                _rel = [r for r in _retrieve_relevant_learnings(self.last_user_text, limit=5)
                        if r.get("confidence", 0) >= 0.3]
                if _rel:
                    self.current_msgs.append(_note_msg(
                        "以下是 AI 从过去交互学到的相关知识（供参考，与当前任务不冲突时遵循）：\n"
                        + "\n".join(f"- {r['topic']}: {str(r['content'])[:200]}" for r in _rel),
                        label="【参考知识】"))
                    self._step_log("知识注入", f"{len(_rel)} 条相关学习")
        except Exception:
            logger.debug("learnings retrieval skipped", exc_info=True)
        # 当日额度状态预注入：今天已知 mx_query 用尽（持久标记，跨 turn/跨
        # 会话、按日期比对）→ 第一轮就告知模型，绕开注定失败的调用
        # （09-08 17:11：首轮 2 次 mx_query 秒错后才见提示；17:03 同款）
        if _quota_exhausted_today("mx_query") and not self._quota_hint_injected:
            self._quota_hint_injected = True
            self.current_msgs.append(_note_msg(
                "⏰ 提示：mx_query 今日免费额度已用尽（150 次/日，接口方统计，当日不会再成功）。"
                "请直接使用替代工具（ak_finance / tavily_search）查数据，不要调用 mx_query；"
                "数据足够就直接写出完整分析（简体中文，含关键数字与结论）。"))
            self._step_log("额度状态", "今日已标记用尽，首轮预注入提示（跨 turn）")
            logger.warning("thin loop: 当日额度状态预注入提示（跨 turn）")
        async with httpx.AsyncClient(timeout=httpx.Timeout(120)) as client:
            self._client = client
            while self.steps < self.max_steps:
                self.steps += 1
                yield {"event": "round_start", "iteration": self.steps}
                from agent_loop import _session_cancel_requested
                if _session_cancel_requested(self.session_id):
                    yield {"content": "\n\n" + _msg("task_stopped", self.user_lang)}
                    return
                if self.steps > 1:
                    for m in _claim_steer(self.session_id):
                        self.current_msgs.append({"role": "user", "content": m})
                        yield {"event": "steer_applied", "content": m[:80]}

                # pre_step waterfall：planning 门 / compaction（payload 携带预事件）
                payload = {"pre_events": [], "reject": False}
                payload = await self.scope.run_waterfall("pre_step", payload)
                for evt in payload.get("pre_events", []):
                    yield evt
                if payload.get("reject"):
                    yield {"content": "\n\n" + _msg("plan_rejected", self.user_lang)}
                    return

                # 规划等待：插件标记 plan_wait → 循环执行 yield+await（生成器语义）
                plan_wait = payload.get("plan_wait")
                if plan_wait and not self._plan_injected:
                    from agent_loop import _wait_plan_confirmation
                    approved, evts = await _wait_plan_confirmation(
                        plan_wait["plan_id"], plan_wait["event_obj"])
                    for evt in evts:
                        yield evt
                    if not approved:
                        yield {"content": "\n\n" + _msg("plan_rejected", self.user_lang)}
                        return
                    self.current_msgs.insert(0, {
                        "role": "system",
                        "content": "以下是已确认（用户批准）的执行计划，请严格按计划逐步执行（可调用工具）：\n"
                                   + plan_wait["plan"]})
                    self._plan_injected = True

                engine_model = self._engine_model()
                body = self._build_request(engine_model)
                tools_n = len(body.get("tools") or [])
                chars = sum(len(str(m.get("content") or "")) for m in body.get("messages", []))
                self._step_log("请求构建",
                               f"model={body.get('model')} 消息={len(body.get('messages', []))} "
                               f"字符={chars} tools={tools_n} "
                               f"思考={'关' if (body.get('chat_template_kwargs') or {}).get('enable_thinking') is False else '开'}")
                # request waterfall：fastpath（快车道）等插件改写请求
                body_keys_before = sorted(body.keys())
                body = await self.scope.run_waterfall("request", body)
                if sorted(body.keys()) != body_keys_before:
                    self._step_log("request 插件改写",
                                   f"键变化 {body_keys_before} → {sorted(body.keys())}")
                t_sample = time.monotonic()
                self._round_buf = []
                self._round_dup = False
                self._round_dup_decided = False
                t_first_token = None
                result = None
                raw_deltas = 0
                try:
                    async for evt in self._sample(client, body):
                        if "__result__" in evt:
                            result = evt["__result__"]
                            continue
                        _c = evt.get("content")
                        if _c:
                            if t_first_token is None:
                                t_first_token = time.monotonic()   # 首 token（TTFT）计时点
                            _c, _th = self._split_think_stream(_c)
                            if _th:
                                yield {"reasoning": _th}           # 推理走思考通道
                            if not _c:
                                raw_deltas += 1
                                continue
                            _emit = self._gate_round_content(_c)  # 只比对正文（思考不参与）
                            if _emit is None:
                                raw_deltas += 1
                                continue
                            if _emit != _c:
                                evt = dict(evt, content=_emit)
                        elif evt.get("reasoning") and t_first_token is None:
                            t_first_token = time.monotonic()
                        raw_deltas += 1
                        yield evt
                    _leftover = (self._flush_round_gate() or "") + self._take_think_carry()
                    if _leftover:
                        yield {"content": _leftover}
                    if result is None:
                        return
                    streamed, body_text, reasoning, native, finish_reason = result
                    self._last_round_had_tools = bool(native)   # 围栏路径稍后按 tool_calls 复核
                    # 复读/折叠一律**只看正文**：streamed 是"思考+正文"混合缓冲，
                    # 拿它做基准会把思考当成复读、还会把思考摘要当消息推给用户（09-19 实测）
                    self._last_round_text = body_text
                    # 工具轮的过程叙述折叠（评估在下方拿到 tool_calls 后再收口替换）
                    # max-tokens 截断完整性不变量（DSH BlockAssembler 同款）：
                    # finish=length 时模型"说到一半"，半截参数 JSON 不可执行——
                    # 丢弃全部 native 调用，关思考重跑本轮一次（思考是预算杀手，
                    # 09-06/09-07 推理模型 token 全进 <think> 同族根因）
                    _empty_body = not body_text.strip() and bool(reasoning)
                    if _needs_length_retry(finish_reason, bool(native), _empty_body,
                                           self._len_retry_used):
                        self._len_retry_used = True
                        self._force_thinking_off_next = True
                        self.steps -= 1
                        if native:
                            logger.warning("thin loop: finish=length 截断 %d 个 tool-call，"
                                           "已丢弃；关思考重跑本轮", len(native))
                            native = {}
                        else:
                            # 09-20：预算被思考吃光、正文为空（实测 Bonsai delta=6144、
                            # 思考 20864 字、正文 0 字 → "只输出了思考过程"）→关思考重跑，
                            # 并追加尾部要求直接给答案（不让用户手动点「继续」）
                            self.current_msgs.append(
                                _note_msg(_msg("length_retry", self.user_lang)))
                            logger.warning("thin loop: finish=length 且正文为空（思考吃光预算），"
                                           "关思考重跑本轮")
                        continue
                        logger.warning("thin loop: finish=length 重试仍截断，丢弃 %d 个 tool-call",
                                       len(native))
                        native = {}
                except _GenerationLoopError as e:
                    # 复读/截断 = mlx 确定性解码退化（温度 0.0 下「继续」必然复发，
                    # 09-07 22:10 事故）。先做一次温度抖动重采（资源级重试，
                    # 对齐 max-tokens 重试模式）；再失败才交付警告。
                    if self._gen_retry_used < 2:
                        self._gen_retry_used += 1
                        if self._gen_retry_used == 1:
                            self._retry_temp, self._retry_freq = 0.35, 1.0
                        else:
                            self._retry_temp, self._retry_freq = 0.6, 1.5
                        self.steps -= 1
                        logger.warning("thin loop: %s —— 重采本轮（temp=%s freq=%s）",
                                       e, self._retry_temp, self._retry_freq)
                        yield {"content": "\n\n---\n\n"}
                        continue
                    logger.warning("thin loop: 重试后仍复读/截断，交付警告收尾: %s", e)
                    yield {"content": f"\n\n⚠️ {e}"}
                    return
                except TimeoutError as e:
                    yield {"content": f"\n\n⚠️ {e}" + _msg("retry_hint", self.user_lang)}
                    return
                except httpx.HTTPStatusError as e:
                    status = getattr(e.response, "status_code", 0)
                    try:
                        logger.error("thin loop HTTP %s body: %s", status,
                                     (e.response.text or "")[:500])
                    except Exception:
                        pass
                    if (self.native_tools and not self.native_fallback_used
                            and status == 400):
                        # 引擎不支持 tools 参数（模板无工具能力）→ 回退围栏格式重跑本步
                        self.native_fallback_used = True
                        self.native_tools = False
                        self.steps -= 1
                        logger.warning("thin loop：引擎拒绝 tools 参数（400），回退围栏提示词")
                        continue
                    # 超长上下文特判：给出可执行指引（09-12：13.7 万字符 PDF 撞 400
                    # 只看到"模型服务返回错误"，用户无从下手）
                    _body = ""
                    try:
                        _body = (e.response.text or "").lower()
                    except Exception:
                        pass
                    if status == 400 and any(k in _body for k in (
                            "context", "too long", "length", "exceed", "maximum")):
                        try:
                            import local_llm
                            _tl = int(getattr(local_llm._engine, "model_token_limit", 0) or 0)
                        except Exception:
                            _tl = 0
                        yield {"content": (
                            "\n\n⚠️ 输入超出模型上下文"
                            + (f"（当前 {_tl:,} tokens）" if _tl else "")
                            + "。可选：① 到「模型」页调大上下文长度并点「重新加载模型」；"
                            "② 改用云端模型；③ 把文件分段提问（如“只看资格要求部分”）。")}
                        return
                    yield {"content": "\n\n" + _msg("http_error", self.user_lang, status=status)}
                    return
                # 引擎空生成重试：流结束且正文/思考/工具调用全空 → 引擎抽风
                # （09-08 14:21 会话：连续两轮空生成后任务以诊断结束）。重采
                # 一次；再空才落下方空响应诊断。
                if (finish_reason != "length" and not streamed and not body_text
                        and not reasoning and not native):
                    if not self._empty_gen_retry_used:
                        self._empty_gen_retry_used = True
                        self.steps -= 1
                        logger.warning("thin loop: 引擎空生成，重采本轮")
                        yield {"event": "heartbeat"}
                        continue
                    logger.warning("thin loop: 引擎连续空生成，交付诊断")
                try:
                    import context_stats
                    context_stats.record_step(
                        self.session_id, time.monotonic() - t_sample,
                        (t_first_token - t_sample) if t_first_token else None)
                except Exception:
                    logger.debug("记录采样步耗时失败", exc_info=True)
                self._step_log("流结束",
                               f"delta={raw_deltas} 思考={len(reasoning)}字 "
                               f"正文={len(body_text)}字 耗时={time.monotonic()-t_sample:.1f}s")

                # 工具调用：原生优先，围栏兜底
                tool_calls = []
                clean_text = streamed
                if native:
                    # 原生 delta（含空名——执行器按参数恢复，09-21 deepseek quirk）
                    tool_calls = [native[i] for i in sorted(native.keys())]
                self._step_log("工具解析",
                               f"原生={len(native)} 围栏待查")

                if not tool_calls:
                    clean_text, fence_calls = _parse_prompt_tool_calls(streamed)
                    if fence_calls:
                        tool_calls = fence_calls
                    else:
                        clean_text = streamed
                tool_names = {t.get("function", {}).get("name") for t in self._active_tools()}
                # 空名调用必须放行到执行器（恢复/守卫都在那边）。
                # "有名但不在册"（模型幻觉出的工具名）：不静默丢弃（09-07 17:40
                # 事故：原生=2 全被滤掉 → 模型以为调用了、用户拿到 3 字残答 +
                # 思考-only 提示）——回一条可见错误（附在册工具清单）让模型
                # 下一轮自行纠正。
                _known_calls, _unknown_named = [], []
                for tc in tool_calls:
                    nm = (tc.get("function") or {}).get("name", "")
                    if not nm or nm in tool_names:
                        _known_calls.append(tc)
                    else:
                        _unknown_named.append(tc)
                if _unknown_named:
                    for tc in _unknown_named:
                        nm = (tc.get("function") or {}).get("name", "?")
                        if not tc.get("id"):
                            tc["id"] = str(uuid.uuid4())
                        err = (f"⛔ 未知工具 '{nm}'。可用工具："
                               f"{', '.join(sorted(n for n in tool_names if n))}。"
                               f"请从以上清单中选择正确的工具重新调用。")
                        self.current_msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": err})
                        self._step_log("未知工具", f"{nm} → 已回错误清单")
                        logger.warning("thin loop: 未知工具调用 '%s' 已回错误（在册: %s）",
                                       nm, sorted(n for n in tool_names if n))
                        yield {"event": "tool_end", "call_id": tc["id"], "tool": nm,
                               "result": err, "ts": int(time.time() * 1000)}
                    if not _known_calls:
                        # 全部是幻觉调用 → 不交付正文，直接进下一轮让模型纠正
                        continue
                self._last_round_had_tools = bool(tool_calls)   # 低档思考判定用（含围栏路径）
                tool_calls = _known_calls

                if self._finalize_round:
                    # 终答轮：只交付正文。模型若仍发围栏工具调用 → 剥离后交付。
                    if tool_calls:
                        self._step_log("终答轮", f"剥离 {len(tool_calls)} 个工具调用，直接交付")
                        tool_calls = []
                        streamed = body_text = _parse_prompt_tool_calls(streamed)[0]
                    if len(body_text.strip()) < 10:
                        # 终答轮仍空 → 温度抖动重采一次（0.0 温度下模型被
                        # 工具调用形态锁定（09-08 17:43 实况：tools=[] 仍只
                        # 输出 1 个工具调用、正文 0 字））；再空才交付警告。
                        if not self._finalize_retry_used:
                            self._finalize_retry_used = True
                            self._retry_temp, self._retry_freq = 0.6, 1.5
                            if self.steps >= self.max_steps:
                                self.steps = self.max_steps - 1
                            # 追加为**尾部用户消息**：system 角色会改提示头 → 缓存全丢
                            self.current_msgs.append({
                                "role": "user",
                                "content": _finalize_tail_directive(self.user_lang)})
                            self._step_log("终答轮", "空生成 → 温度抖动重采（一次）")
                            yield {"event": "heartbeat"}
                            continue
                        _fb = _collect_finalize_data(self.current_msgs, max_results=8)
                        if _fb:
                            yield {"content": ("\n\n" + _msg("data_only", self.user_lang)
                                               + "\n\n" + "\n\n".join(_fb))[:6000]}
                        else:
                            yield {"content": "\n\n" + _msg("no_final_answer", self.user_lang)}
                    else:
                        # 正文已在 _sample 逐字流式下发，不再重复交付全文
                        self._step_log("终答轮", f"已交付 {len(body_text)} 字")
                    return

                self._step_log("采样完成",
                               f"正文={len(body_text)}字 思考={len(reasoning)}字 "
                               f"工具调用={len(tool_calls)}")

                if (not tool_calls and not self._finalize_round
                        and not self._plan_nudge_used
                        and _looks_like_plan_only(body_text, bool(self._active_tools()))):
                    # 空转闸门：只说了"我来/先调取"就收尾 → 要求真正行动或给出完整
                    # 答案（每 turn 一次，避免与"确实没工具可用"的场景互相顶住）
                    self._plan_nudge_used += 1
                    self.current_msgs.append(_note_msg(_msg("plan_nudge", self.user_lang)))
                    self._step_log("空转闸门", "只有计划声明、无工具调用 → 要求直接行动（一次）")
                    yield {"event": "heartbeat"}
                    continue

                if not tool_calls:
                    # deliver waterfall：弱模型辅助（思考-only/空响应诊断/语言替换）
                    # 正文已实时流出——辅助只能"替换"（content_revised）或补充，不得重复
                    payload = {
                        "text": body_text, "streamed": streamed,
                        "client": client, "api_url": self.api_url,
                        "headers": self.headers, "engine_model": engine_model,
                        "current_msgs": self.current_msgs, "user_text": self.last_user_text,
                        "user_lang": self.user_lang, "is_local": self.is_local,
                        "events": [], "handled": False,
                    }
                    payload = await self.scope.run_waterfall("deliver", payload)
                    for evt in payload.get("events", []):
                        yield evt
                    self._step_log("交付",
                                   f"handled={payload.get('handled')} "
                                   f"events={len(payload.get('events', []))}")
                    # dsh 语义：交付后若 inbox 有排队输入 → 续开下一轮
                    pending = _claim_steer(self.session_id)
                    if pending:
                        self._step_log("steer 认领", f"{len(pending)} 条")
                        for m in pending:
                            self.current_msgs.append({"role": "user", "content": m})
                        continue
                    return

                # 会话级工具标记：即将执行的工具记入会话集合（目录一致性：
                # 用过的工具后续轮次保持可用）
                mark_session_tools(self.session_id,
                                   [(tc.get("function") or {}).get("name") for tc in tool_calls
                                    if (tc.get("function") or {}).get("name")])

                # 工具执行：错误即结果（共享执行器含确认/事件/溯源）
                from agent_loop import _confirm_bypassed, _handle_tool_execution, _start_tool_confirmation
                from tool_executor import _resolve_permission
                if any(not (tc.get("function") or {}).get("name") for tc in tool_calls):
                    self.parallel_disabled = True
                _narr_text = (body_text or "").strip()   # 仅正文：思考不进折叠/去重判定
                # 工具轮的过程叙述折叠：关思考后模型把推理倒进正文（实测单轮 11171 字
                # 英文），既刷屏又让翻译轮失败。工具轮正文只是过程说明，折叠不影响结论；
                # 用 content_revised 让前端替换已下发的长文本，历史里也存折叠版免膨胀。
                if tool_calls and len(_narr_text) > _ROUND_CONTENT_MAX:
                    _folded = _trim_process_narration(_narr_text, lang=self.user_lang)
                    self._step_log("过程叙述折叠", f"{len(_narr_text)}字 → {len(_folded)}字（工具轮）")
                    yield {"event": "content_revised", "content": _folded}
                    _narr_text = _folded
                if _narr_text and _narration_seen(self.session_id, _narr_text):
                    # 近重复过渡叙述：截断为短摘要入历史（真流式已让用户看过
                    # 一次）——历史里不再累积相同叙述，打破逐轮复述吸引子
                    asst = {"role": "assistant", "content": _narr_text[:60] + "…（同前）"}
                    self._step_log("重复叙述抑制", f"{len(_narr_text)}字 近重复")
                else:
                    asst = {"role": "assistant", "content": clean_text or ""}
                if self.native_tools:
                    asst["tool_calls"] = tool_calls
                self.current_msgs.append(asst)

                # ── 并行委派（对齐 DSH 并行池 / Codex max_threads）──────────
                # 同一轮全部是 delegate_task 且 ≥2 个 → 并发执行：每个委派是
                # 独立 ThinAgentLoop（本地引擎串行排队、云端真并行），结果按
                # 调用顺序回灌。混有其他工具时维持串行（保守）。
                _all_delegate = bool(tool_calls) and all(
                    (tc.get("function") or {}).get("name") == "delegate_task"
                    for tc in tool_calls)
                if _all_delegate and len(tool_calls) >= 2:
                    import asyncio as _aio
                    import uuid as _uuid
                    from agent.subagent import _CURRENT_PARENT_SESSION as _cps
                    _cps.set(self.session_id)
                    for tc in tool_calls:
                        if not tc.get("id"):
                            tc["id"] = str(_uuid.uuid4())

                    async def _run_delegate(tc):
                        targs = json.loads(tc.get("function", {}).get("arguments", "{}") or "{}")
                        from agent.subagent import _delegate_task_fg, _delegate_task_bg
                        if targs.get("background"):
                            return await _delegate_task_bg(
                                targs.get("agent", "code-reviewer"), targs.get("task", ""))
                        return await _delegate_task_fg(
                            targs.get("agent", "code-reviewer"), targs.get("task", ""))

                    self._step_log("并行委派", f"{len(tool_calls)} 个子代理并发启动")
                    _t_del = time.monotonic()
                    _outs = await _aio.gather(*[_run_delegate(tc) for tc in tool_calls])
                    try:
                        import context_stats
                        context_stats.record_tool_time(self.session_id, time.monotonic() - _t_del)
                    except Exception:
                        logger.debug("记录子代理耗时失败", exc_info=True)
                    for tc, out in zip(tool_calls, _outs):
                        self.current_msgs.append({"role": "tool",
                                                  "tool_call_id": tc.get("id"),
                                                  "content": out})
                    yield {"content": "\n\n".join(
                        f"🧩 子代理 {i+1}/{len(_outs)}：{(out or '').strip()[:400]}"
                        for i, out in enumerate(_outs))}
                    self._step_log("并行委派完成", f"{len(_outs)} 个结果已回灌")
                    continue

                # ── 安全只读工具同轮并行（09-07 提速实验）────────────────
                # 本轮全部是并发安全只读工具且 ≥2 个 → asyncio.gather 并发，
                # 事件按调用顺序回灌（结果消息各自带 tool_call_id，顺序无关）。
                # 混有写/控制工具 → 维持串行。
                # mx_query 移出并行名单（09-20 实测）：同一步里 3 个 mx_query 并发时
                # 偶发 `'NoneType' object has no attribute 'get'`（单独调用同一查询正常）
                # —— 该工具在并发下不安全（共享状态或上游返回空时未兜底）。
                _SAFE_PARALLEL_TOOLS = {
                    "ak_finance", "tavily_search", "bing_search",
                    "dokobot_search",
                    "read_file", "list_dir", "search_files",
                }
                _all_safe = bool(tool_calls) and all(
                    (tc.get("function") or {}).get("name", "") in _SAFE_PARALLEL_TOOLS
                    for tc in tool_calls)
                if _all_safe and len(tool_calls) >= 2:
                    import asyncio as _aio2
                    self._step_log("并行工具", f"{len(tool_calls)} 个只读工具并发执行")
                    _safe_results = await _aio2.gather(*[
                        _handle_tool_execution(
                            tc, self.current_msgs, self.session_id, "latiao",
                            self.access_mode)
                        for tc in tool_calls
                    ])
                    for _vf, _evts in _safe_results:
                        for evt in _evts:
                            yield evt
                    for _vf, _evts in _safe_results:
                        for _e in _evts:
                            if isinstance(_e, dict) and "result" in _e:
                                self._maybe_quota_hint(str(_e.get("result")))
                    # mx_query 成功 → 清除当日限额标记（换 Key 后自纠正）
                    for _tc, (_vf, _evts) in zip(tool_calls, _safe_results):
                        if ((_tc.get("function") or {}).get("name") == "mx_query"):
                            _rr = next((str(e.get("result", "")) for e in _evts
                                        if isinstance(e, dict) and "result" in e), "")
                            if "Error" not in _rr:
                                _clear_quota_marker("mx_query")
                    self._search_used += self._count_search_calls(tool_calls)
                    _sat, _gain = self._note_round_info(
                        [str(_e.get("result", "")) for _vf, _evts in _safe_results
                         for _e in _evts if isinstance(_e, dict) and "result" in _e])
                    if self._search_used >= _SEARCH_BUDGET_CALLS:
                        self._narrow_search = True   # 预算耗尽＝软饱和：下轮撤检索工具
                    if self._note_tool_counts(tool_calls):
                        _sat = "hard"    # 同一检索工具反复调用（换 query 也算）
                    if _sat == "hard" and not self._finalize_round:
                        self._finalize_round = True
                        if self.steps >= self.max_steps:
                            self.steps = self.max_steps - 1
                        self._step_log("饱和收口", f"信息增量连续 {self._gain_low_streak} 轮低于 "
                                                   f"{_INFO_GAIN_RATIO:.0%}（本轮 {_gain:.0%}）→ 强制直接作答")
                        continue
                    if _sat == "soft":
                        self._narrow_search = True
                    self._maybe_repeat_reminder(tool_calls)
                    _gate = await self._stagnation_gate(tool_calls)
                    if _gate is not None:
                        yield _gate
                        return
                    continue

                _round_results: list = []
                for tc in tool_calls:
                    if not tc.get("id"):
                        tc["id"] = str(uuid.uuid4())
                    tname = tc.get("function", {}).get("name", "")
                    try:
                        targs = json.loads(tc.get("function", {}).get("arguments", "{}") or "{}")
                    except Exception:
                        targs = {}
                    pre = None
                    if _resolve_permission(tname, targs) == "confirm" \
                            and not _confirm_bypassed(tname, self.access_mode):
                        pre = await _start_tool_confirmation(tc["id"], tname, targs)
                        yield pre["event"]
                    _t_tool = time.monotonic()
                    verify_failed, events = await _handle_tool_execution(
                        tc, self.current_msgs, self.session_id, "latiao",
                        self.access_mode, pre_started=pre)
                    try:
                        import context_stats
                        context_stats.record_tool_time(self.session_id, time.monotonic() - _t_tool)
                    except Exception:
                        logger.debug("记录工具耗时失败", exc_info=True)
                    _res = next((str(e.get("result", "")) for e in events
                                 if isinstance(e, dict) and "result" in e), "")
                    self._maybe_quota_hint(_res)
                    if tname == "mx_query" and "Error" not in _res:
                        _clear_quota_marker("mx_query")
                    _round_results.append(_res)
                    self._step_log("工具结果", f"{tname} → {len(_res)}字符 "
                                               f"摘要: {_res[:80]!r}")
                    for evt in events:
                        yield evt
                    # use_skill 联动：加载技能时，该技能声明的依赖工具并入
                    # 会话集合（技能承诺的能力必须在场——mx-data → mx_query）
                    if tname == "use_skill":
                        try:
                            _sn = json.loads(tc.get("function", {}).get("arguments", "{}") or "{}").get("skill_name", "")
                            mark_session_tools(self.session_id, SKILL_DEPENDENT_TOOLS.get(_sn, set()))
                        except Exception:
                            pass
                self._search_used += self._count_search_calls(tool_calls)
                _sat, _gain = self._note_round_info(_round_results)
                if self._search_used >= _SEARCH_BUDGET_CALLS:
                    self._narrow_search = True   # 预算耗尽＝软饱和：下轮撤检索工具
                if self._note_tool_counts(tool_calls):
                    _sat = "hard"                # 同一检索工具反复调用（换 query 也算）
                if _sat == "hard" and not self._finalize_round:
                    self._finalize_round = True
                    if self.steps >= self.max_steps:
                        self.steps = self.max_steps - 1
                    self._step_log("饱和收口", f"信息增量连续 {self._gain_low_streak} 轮低于 "
                                               f"{_INFO_GAIN_RATIO:.0%}（本轮 {_gain:.0%}）→ 强制直接作答")
                    continue
                if _sat == "soft":
                    self._narrow_search = True
                self._maybe_repeat_reminder(tool_calls)
                _gate = await self._stagnation_gate(tool_calls)
                if _gate is not None:
                    yield _gate
                    return

            yield {"content": "\n\n" + _msg("max_steps", self.user_lang, n=self.max_steps)}
