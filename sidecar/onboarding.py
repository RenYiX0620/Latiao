"""首启引导（Onboarding）——自我介绍后依次收集：用户称呼 / 我的名字 / 对话语气。

职责划分：本模块只决定「问什么、什么时候问」，并把用户的自由回答归一化成可落盘的值；
四个身份文件的读写仍全部走 identity.py（IDENTITY.md / USER.md / SOUL.md），
所以引导结束后「叫我X」「说话要Y」这类直接改口的旧路径照常生效。

不打扰老用户（三重保险）：
  ① 状态文件存在且 done → 不再提问；
  ② 状态文件不存在但身份文件已不是默认模板（老装机）→ 启动时直接补记完成；
  ③ 即便状态文件被手动删除，只要 USER.md 里已有「用户称呼：」或 SOUL.md 里已有
     「对话语气：」（说明引导已完成或用户自己写过）→ 同样补记完成。
版本更新只替换 .app 包，~/.local-ai-os/ 不受影响，因此升级不会触发重新引导。
"""
import json
import logging
import re
import time
from pathlib import Path

import identity
from config import PROGRESS_DIR

logger = logging.getLogger(__name__)

STATE_FILE = PROGRESS_DIR / ".onboarding.json"

# 提问顺序：用户称呼 → 我的名字 → 对话语气
FIELD_ORDER = ("user_name", "agent_name", "tone")

_LABELS = {"user_name": "用户称呼", "agent_name": "我的名字", "tone": "对话语气"}

_QUESTIONS = {
    "user_name": {
        "zh": "我该怎么称呼你？",
        "en": "What should I call you?",
        "ja": "何とお呼びすればよいですか？",
    },
    "agent_name": {
        "zh": "那我叫什么名字好？（默认「辣条」，说「就用辣条」也可以）",
        "en": "What would you like to name me? (default is “Latiao”)",
        "ja": "私の名前は何にしますか？（既定は「辣条」）",
    },
    "tone": {
        "zh": "你希望我用什么语气？比如「简洁直接」「轻松幽默」「正式专业」",
        "en": "What tone should I use? e.g. concise, playful, formal",
        "ja": "どんな口調がよいですか？（例：簡潔、ユーモア、丁寧）",
    },
}

_INTRO = {
    "zh": ("【首次使用引导】这是用户第一次和你对话。严格按下面格式回复，不要寒暄、不要列清单、"
           "不要调用工具、不要跳过提问：先用一两句话介绍自己（名字、在本机 Mac 上运行、"
           "能读写文件/执行命令/联网检索），**最后一句话必须是这个问题（原样问出）**：{q}"),
    "en": ("[First-run onboarding] This is the user's first conversation with you. Follow this format exactly — "
           "no small talk, no lists, no tools, do not skip the question: introduce yourself in one or two sentences "
           "(your name, you run locally on this Mac, you can read/write files, run commands, search the web), then "
           "**end your reply with exactly this question**: {q}"),
    "ja": ("【初回ガイド】ユーザーとの初めての会話です。次の形式を厳守してください（雑談・箇条書き・ツール呼び出しは不要）："
           "まず 1〜2 文で自己紹介し（名前、この Mac 上でローカル動作、ファイル読み書き・コマンド実行・Web 検索が可能）、"
           "**最後に必ずこの質問で締めくくる**：{q}"),
}

_CONFIRM_NEXT = {
    "zh": "系统已把{label}记为「{value}」并写入档案。",
    "en": "The system has recorded {label} as “{value}”.",
    "ja": "システムが{label}を「{value}」として記録しました。",
}

_ASK_NEXT = {
    "zh": ("用一句话确认后，**最后一句话必须是下一个问题（原样问出）**：{q}"
           "——不要重复自我介绍，不要列清单，不要调用工具。"),
    "en": ("Confirm in one sentence, then **end your reply with exactly this question**: {q} "
           "— no self-introduction again, no lists, no tools."),
    "ja": "一文で確認し、**最後に必ず次の質問で締めくくる**：{q}",
}

_CONFIRM_DONE = {
    "zh": "系统已把{label}记为「{value}」并写入档案，引导到此结束。",
    "en": "The system recorded {label} as “{value}”; onboarding is complete.",
    "ja": "システムが{label}を「{value}」として記録しました。ガイドは以上です。",
}

_RETRY = {
    "zh": ("用户还没给出明确答案（或答非所问）。先满足 ta 当前的需求，再自然地重问一次：{q}"
           "——不要假装已记录任何内容。"),
    "en": ("The user has not answered clearly yet. Address what they asked first, then ask again naturally: {q} "
           "— do not pretend anything was recorded."),
    "ja": "ユーザーはまだ明確に答えていません。まず要望に応え、そのうえで自然にもう一度聞いてください：{q}",
}

_DONE_NOTE = {
    "zh": "引导已完成，之后不要再问这些。用户以后想改，可以说「叫我X」「说话要Y」，或在设置里重新运行引导。",
    "en": "Onboarding is done — do not ask again. The user can later say “call me X” or use Settings to re-run it.",
    "ja": "ガイドは完了です。今後は聞かないでください。設定から再実行もできます。",
}

_SKIP_ALL_NOTE = {
    "zh": "用户表示现在不想设置这些。用一句话致意，然后照常回答 ta 的需求，不要再问这些问题。",
    "en": "The user declined setup. Acknowledge briefly and continue normally — do not ask again.",
    "ja": "ユーザーは設定を希望していません。一言で応じ、通常どおり対応してください。",
}

# ── 答案归一化用词表 ─────────────────────────────────────
# 前缀按长度倒序剥离（"你可以叫我" 必须先于 "叫我"）
_NAME_PREFIXES = (
    "my name is", "call me", "you can call me", "name's",
    "我的名字是", "我名字是", "你可以叫我", "可以叫我", "以后叫我", "直接就叫我",
    "直接叫我", "你就叫我", "请叫我", "称呼我", "叫我", "我是", "我叫", "i am", "i'm",
)
# 尾部语气词：只剥离一层（防"老王好了"→"老王"，又不误伤真名）
_NAME_TAILS = (
    "就可以了", "就行", "即可", "好了", "吧", "啊", "呀", "哦", "呢", "了", "就好",
    "please", "pls", "thanks", "thank you",
)
# 给"我的名字"这一问专用的前缀（回答常是"叫你小助手吧""以后叫你X"）
_AGENT_NAME_PREFIXES = (
    "给你起名叫", "给你起名", "给你取名", "以后叫你", "你的名字是", "名字是", "你叫", "叫你",
    # 英文用户常见表达（实测 "you can call yourself Nova" 原先不识别）
    "you can call yourself", "i'll call you", "i will call you", "let's call you", "lets call you",
    "call yourself", "name yourself", "your name is", "your name's", "name's",
)
_GREETINGS = (
    "你好", "您好", "哈喽", "在吗", "在么", "喂", "嗯", "哦", "好的", "好", "行", "谢谢",
    "hi", "hello", "hey", "ok", "okay", "yes", "no", "thanks", "thank you", "yo",
)
# 更名线索：首条消息里出现这些才尝试直接捕获（否则"你好"会被当成名字）
_NAME_CUES = ("我叫", "我是", "叫我", "称呼我", "my name is", "call me", "i am", "i'm")

_SKIP_ALL = ("都跳过", "全都跳过", "全部跳过", "不用设置", "别问了", "不想设置", "以后再说",
             "skip all", "skip everything", "no thanks")
_SKIP_FIELD = ("跳过", "不用", "不需要", "随便", "无所谓", "都行", "看你", "你来定", "随意",
               "默认", "skip", "default", "up to you", "whichever", "anything")

# 语气关键词 → 规范写法（先匹配关键词，再退回用户原话）
_TONE_PRESETS = (
    # (关键词, (中文规范写法, 英文规范写法))——按用户回答的语言二选一，
    # 避免英文用户的人设文件里出现一句中文
    (("简洁", "简短", "简单", "直接", "精炼", "干练", "concise", "brief", "short"),
     ("简洁直接，先给结论再解释", "concise and direct: conclusion first, then the reasoning")),
    (("幽默", "搞笑", "风趣", "俏皮", "playful", "humor", "humour", "funny"),
     ("轻松幽默，可以开玩笑但不影响信息密度",
      "light and playful, jokes are fine as long as the information stays dense")),
    (("正式", "专业", "严谨", "严肃", "formal", "professional", "serious"),
     ("正式专业，措辞严谨", "formal and professional, precise wording")),
    (("轻松", "随意", "口语", "casual", "relaxed", "chill"),
     ("轻松随意，像朋友聊天一样", "relaxed and casual, like talking with a friend")),
    (("耐心", "详细", "细致", "多解释", "patient", "detailed", "thorough"),
     ("耐心细致，多解释背景与原因", "patient and thorough, explaining background and reasons")),
    (("温柔", "体贴", "gentle", "kind"),
     ("温和体贴，语气柔软", "warm and gentle")),
)

_QUESTION_LIKE = ("？", "?", "怎么", "如何", "为什么", "为什么", "what", "how", "why", "when")

# 本轮该出现在回复里的问题（供后端兜底）：本地小模型服从性不稳，
# 22:40 实测 9B 模型把"语气"那一问换成了寒暄 → 由这里在流末尾补上。
_pending: dict | None = None

# 判定"模型是否问出了该问题"的关键词：必须同时含关键词与问号才算问了
_QUESTION_KEYS = {
    "user_name": ("称呼", "叫我", "call you", "address you"),
    "agent_name": ("名字", "名字", "name"),
    "tone": ("语气", "口吻", "tone"),
}


def pending_question_suffix(reply_text: str) -> str | None:
    """模型没问出该问的问题时返回要补的文本，否则 None。"""
    global _pending
    if not _pending:
        return None
    keys = _QUESTION_KEYS.get(_pending["field"], ())
    asked = any(k in (reply_text or "") for k in keys) and ("？" in (reply_text or "") or "?" in (reply_text or ""))
    if asked:
        return None
    return "\n\n" + _pending["question"]


def _set_pending(field: str, lang: str) -> str:
    """记录本轮该问的问题，并返回要注入系统提示的提问指令。"""
    global _pending
    q = _question(field, lang)
    _pending = {"field": field, "question": q}
    return q


def _L(lang: str, texts: dict) -> str:
    return texts.get(lang) or texts.get("zh", "")


def _question(field: str, lang: str) -> str:
    return _L(lang, _QUESTIONS.get(field, {}))


def _label(field: str) -> str:
    return _LABELS.get(field, field)


def _next_field(field: str):
    try:
        i = FIELD_ORDER.index(field)
    except ValueError:
        return None
    return FIELD_ORDER[i + 1] if i + 1 < len(FIELD_ORDER) else None


def _has_kw(text: str, kws) -> bool:
    """中文直接子串匹配；ASCII 关键词要求词边界（避免 no 命中 not/now）。"""
    low = (text or "").lower()
    for kw in kws:
        if kw.isascii():
            if re.search(rf"(?<![a-z]){re.escape(kw)}(?![a-z])", low):
                return True
        elif kw in low:
            return True
    return False


# ── 状态读写 ────────────────────────────────────────────

def _fresh_state() -> dict:
    return {"done": False, "field": FIELD_ORDER[0], "awaiting": False, "created_at": time.time()}


def load_state() -> dict:
    try:
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "done" in data:
                return data
    except Exception:
        logger.warning("Failed to read onboarding state", exc_info=True)
    return _fresh_state()


def save_state(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except Exception:
        logger.warning("Failed to save onboarding state", exc_info=True)


def _mark_done(reason: str) -> dict:
    state = _fresh_state()
    state.update(done=True, field=None, awaiting=False, reason=reason)
    save_state(state)
    return state


def init_onboarding() -> dict:
    """启动时调用（必须早于 identity._create_default_identity）。"""
    if STATE_FILE.exists():
        return load_state()
    if _looks_configured():
        logger.info("Onboarding: 检测到已有身份配置，跳过首启引导")
        return _mark_done("existing_install")
    logger.info("Onboarding: 全新安装，已启用首启引导")
    state = _fresh_state()
    save_state(state)
    return state


def _looks_configured() -> bool:
    """老装机判定：任一身份文件存在且已不是默认模板内容。"""
    for name, default in identity.DEFAULT_IDENTITY_FILES.items():
        p = PROGRESS_DIR / name
        try:
            if not p.exists():
                continue
            content = p.read_text(encoding="utf-8").strip()
            if content and content != default.strip():
                return True
        except Exception:
            continue
    return False


def reset() -> dict:
    """重新运行引导（设置页按钮 / API）。身份文件保持现状，只重置提问进度。"""
    state = _fresh_state()
    save_state(state)
    return state


def complete() -> dict:
    return _mark_done("manual")


def status() -> dict:
    state = load_state()
    return {
        "done": bool(state.get("done")),
        "field": state.get("field"),
        "awaiting": bool(state.get("awaiting")),
        "user_name": _read_user_name(),
        "agent_name": _read_agent_name(),
        "tone": _read_tone(),
    }


def _read_first(path: Path, pattern: str) -> str:
    try:
        if path.exists():
            m = re.search(pattern, path.read_text(encoding="utf-8"), re.M)
            if m:
                return m.group(1).strip()
    except Exception:
        pass
    return ""


def _read_user_name() -> str:
    return _read_first(PROGRESS_DIR / "USER.md", r"^-\s*用户称呼：\s*(.+)$")


def _read_agent_name() -> str:
    name = _read_first(PROGRESS_DIR / "IDENTITY.md", r"你的名字是「([^」]+)」")
    if name:
        return name
    name = _read_first(PROGRESS_DIR / "IDENTITY.md", r"你就是([^。\n]+)")
    return name or "辣条"


def _read_tone() -> str:
    return _read_first(PROGRESS_DIR / "SOUL.md", r"^-\s*对话语气：\s*(.+)$")


# ── 答案归一化 ──────────────────────────────────────────

def _clean_name(raw: str, field: str = ""):
    """返回 值 / "" (跳过) / None (不是答案)。"""
    v = (raw or "").strip()
    v = v.strip("「」『』“”\"'`《》()（）[]【】 ")
    if not v:
        return None
    if _has_kw(v, _GREETINGS) and len(v) <= 4:
        return None
    if field == "user_name" and "叫你" in v:
        # 这是给 Agent 改名，不是回答"怎么称呼你" → 交给常规意图识别处理
        return None
    prefixes = _NAME_PREFIXES
    if field == "agent_name":
        prefixes = _AGENT_NAME_PREFIXES + _NAME_PREFIXES
    low = v.lower()
    # 线索可能不在句首（"你好，我叫老王"）——先切到线索之后，再按句首前缀处理
    cut = None
    for p in prefixes:
        i = low.find(p.lower())
        if i > 0 and (cut is None or i < cut[0]):
            cut = (i, p)
    if cut:
        v = v[cut[0] + len(cut[1]):].strip(" ，,、:：")
        low = v.lower()
    for p in prefixes:
        if low.startswith(p.lower()):
            v = v[len(p):].strip(" 「」『』“”\"'`《》()（）")
            break
    if not v:
        return None
    if _has_kw(v, _SKIP_FIELD):
        return ""
    for _ in range(2):  # 最多剥两层（"老王好了" → "老王"）
        for t in _NAME_TAILS:
            if v.lower().endswith(t.lower()) and len(v) > len(t):
                v = v[: -len(t)].strip()
                break
        else:
            break
    if not v or _has_kw(v, _GREETINGS):
        return None
    if any(ch in v for ch in "？?！!。，,、；;：:\n\t"):
        return None
    if not re.fullmatch(r"[0-9A-Za-z\u4e00-\u9fff\u3040-\u30ff·\-\s]{1,24}", v):
        return None
    cjk = len(re.findall(r"[\u4e00-\u9fff\u3040-\u30ff]", v))
    if cjk:
        if len(v) > 12:
            return None
    elif len(v) > 20:
        return None
    return v


def _clean_tone(raw: str):
    v = (raw or "").strip().strip("「」『』“”\"'` ")
    if not v:
        return None
    if _has_kw(v, _SKIP_FIELD) and len(v) <= 6:
        return ""
    low = v.lower()
    # 回答以拉丁字母为主 → 取英文写法（英文用户的人设文件里不该出现中文句）
    ascii_dominant = len(re.findall(r"[a-zA-Z]", v)) > len(re.findall(r"[\u4e00-\u9fff]", v))
    for keys, preset in _TONE_PRESETS:
        if any(k in low for k in keys):
            return preset[1] if ascii_dominant else preset[0]
    if any(ch in v for ch in "？?\n") or len(v) > 24:
        return None
    return v


def _looks_like_tone(raw: str) -> bool:
    """文本是在描述语气偏好（而非名字）——引导问名字时用户先答了语气属常见错位。"""
    low = (raw or "").strip().lower()
    if not low or len(low) > 24:
        return False
    return any(k in low for keys, _preset in _TONE_PRESETS for k in keys)


def _normalize(field: str, raw: str):
    if field in ("user_name", "agent_name"):
        # 防错位：把"keep it concise"记成名字（实测）——语气描述且无起名线索时不算答案
        if _looks_like_tone(raw) and not _has_kw(raw, _NAME_CUES if field == "user_name"
                                                 else _AGENT_NAME_PREFIXES):
            return None
        return _clean_name(raw, field)
    if field == "tone":
        return _clean_tone(raw)
    return None


def _capture(field: str, value: str):
    """写入身份文件；失败返回 None（不推进状态）。"""
    try:
        if field == "user_name":
            identity._apply_user_name_change(value)
        elif field == "agent_name":
            identity._apply_name_change(value)
        elif field == "tone":
            identity._apply_tone_change(value)
        else:
            return None
        return value
    except Exception:
        logger.warning("Onboarding: 写入身份文件失败 (%s=%s)", field, value, exc_info=True)
        return None


# ── 主入口 ──────────────────────────────────────────────

def process_message(user_text: str, lang: str = "zh"):
    """返回 (注入系统提示的指令片段 | None, 是否消费了本轮消息)。

    被消费（True）时调用方应跳过常规身份意图识别，避免同一句话被两套规则重复写盘。
    """
    global _pending
    _pending = None
    state = load_state()
    if state.get("done"):
        return None, False

    text = (user_text or "").strip()
    field = state.get("field") or FIELD_ORDER[0]

    if text and _has_kw(text, _SKIP_ALL):
        _mark_done("skipped")
        return _L(lang, _SKIP_ALL_NOTE), True

    if state.get("awaiting"):
        if not text:
            return _L(lang, _RETRY).format(q=_set_pending(field, lang)), False
        if any(k in text for k in _QUESTION_LIKE) and len(text) > 6:
            # 用户在提问，而不是回答 → 别把问题当名字
            return _L(lang, _RETRY).format(q=_set_pending(field, lang)), False
        value = _normalize(field, text)
        if value is None:
            return _L(lang, _RETRY).format(q=_set_pending(field, lang)), False
        captured = None if value == "" else _capture(field, value)
        nxt = _next_field(field)
        if nxt is None:
            _mark_done("completed")
            note = _L(lang, _DONE_NOTE)
            if captured:
                note = _L(lang, _CONFIRM_DONE).format(label=_label(field), value=captured) + " " + note
            return note, True
        # 本轮回复里就要问下一个问题 → 直接置 awaiting，下一轮即视作答
        state.update(field=nxt, awaiting=True)
        save_state(state)
        prefix = ""
        if captured:
            prefix = _L(lang, _CONFIRM_NEXT).format(label=_label(field), value=captured) + " "
        return prefix + _L(lang, _ASK_NEXT).format(q=_set_pending(nxt, lang)), True

    # 未处于等待：开场（或刚进入下一步）
    if field == "user_name" and _has_kw(text, _NAME_CUES):
        value = _clean_name(text, field)
        if value:
            captured = _capture(field, value)
            nxt = _next_field(field) or "agent_name"
            state.update(field=nxt, awaiting=True)
            save_state(state)
            prefix = ""
            if captured:
                prefix = _L(lang, _CONFIRM_NEXT).format(label=_label(field), value=captured) + " "
            return prefix + _L(lang, _INTRO).format(q=_set_pending(nxt, lang)), True
    state.update(awaiting=True)
    save_state(state)
    return _L(lang, _INTRO).format(q=_set_pending(field, lang)), False
