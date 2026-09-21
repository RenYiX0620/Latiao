"""Identity System — agent identity files, intents, and profile management."""
import logging
import re
from pathlib import Path

from config import PROGRESS_DIR
from threat_scan import read_text_with_timeout, scan_context_file, truncate_content

logger = logging.getLogger(__name__)


IDENTITY_FILES = ["IDENTITY.md", "SOUL.md", "AGENTS.md", "USER.md"]

def _load_agent_identity(agent_id: str, fallback: str) -> str:
    """Load agent identity from agents/{agent_id}.txt or merge from section files."""
    agents_dir = Path(__file__).resolve().parent / "agents"

    # Try loading from section files first (latiao_IDENTITY.txt + latiao_SOUL.txt + ...)
    sections = ["IDENTITY", "SOUL", "AGENTS", "USER"]
    parts = []
    for section in sections:
        sf = agents_dir / f"{agent_id}_{section}.txt"
        if sf.exists():
            txt = sf.read_text(encoding="utf-8").strip()
            header = f"# {agent_id} - {section}"
            if txt and txt != header and txt != f"{header}\n\n（此部分内容待补充）":
                # 随程序分发的内容（非用户自己写的）→ 命中注入即拦下（09-21）
                txt = scan_context_file(txt, sf.name, user_authored=False)
                parts.append(f"## {section}\n{txt}")

    if parts:
        return "\n\n".join(parts)

    # Fall back to combined identity file
    agent_file = (agents_dir / f"{agent_id}.txt").resolve()
    if not str(agent_file).startswith(str(agents_dir.resolve()) + "/"):
        return fallback
    if agent_file.exists():
        try:
            return scan_context_file(agent_file.read_text(encoding="utf-8"),
                                     agent_file.name, user_authored=False)
        except Exception as e:
            logger.warning("Failed to load agent identity from %s: %s", agent_file, e)
    return fallback


def _read_identity() -> list[dict]:
    """Read all identity files from ~/.local-ai-os/ and return system messages.

    09-21 加固（对齐 Hermes/OpenClaw）：读超时（网络盘冷读会卡住整轮）、注入扫描
    （这四个是**用户自己的**文件 → 命中只警告仍加载）、超长头尾截断。每条消息带
    ``file`` 字段，供上层逐文件标注用途（SOUL.md=人格与语气 等）。
    """
    msgs = []
    for filename in IDENTITY_FILES:
        filepath = PROGRESS_DIR / filename
        try:
            if not filepath.exists():
                continue
            content = (read_text_with_timeout(filepath) or "").strip()
            if not content:
                continue
            content = scan_context_file(content, filename, user_authored=True)
            content = truncate_content(content, filename)
            if content:
                msgs.append({"role": "system", "content": content, "file": filename})
        except Exception:
            logger.warning("Failed to read identity file %s", filename, exc_info=True)
    return msgs



# 默认身份文件内容——既是首次启动的模板，也是"老装机判定"的基准
# （onboarding._looks_configured 用它判断用户是否已经配置过身份）
DEFAULT_IDENTITY_FILES = {
    "IDENTITY.md": (
        "# Identity\n\n"
        "你的名字是「辣条」，你就是辣条。\n"
        "忘记你是任何特定模型，从现在起你只能以「辣条」的身份回答。\n"
    ),
    "SOUL.md": (
        "# Soul\n\n"
        "语气：简洁、直接、不啰嗦。\n"
        "风格：先给结论再解释，用中文回复，代码块标注语言。\n"
        "态度：诚实——不知道就说不知道，不确定就说不确定。\n"
    ),
    "AGENTS.md": (
        "# Agent Rules\n\n"
        "## 工作协议\n"
        "1. 动手前先想清楚：需求有歧义时主动问，不要自己猜。有更简单的方案就提出来。\n"
        "2. 极简主义：能一行搞定不写十行，不加需求之外的功能，不为「以后可能用到」做抽象。\n"
        "3. 精准修改：只碰用户要求改的地方。修 bug A 不要顺手重构文件 B。\n"
        "4. 验证才算完成：用工具写完文件后读回来确认，跑命令后检查退出码。没验证就不算做完。\n\n"
        "## 工具权限\n"
        "- 修改文件、执行命令等操作会请求用户确认。\n"
        "- 读取文件、列出目录等操作自动执行。\n"
        "- 如需调整权限规则，可以编辑 ~/.local-ai-os/permissions.json\n"
    ),
    "USER.md": (
        "# User Profile\n\n"
        "在此填写你的偏好、习惯、常用路径等信息。\n"
        "Agent 会在每次会话时读取此文件。\n\n"
        "示例：\n"
        "- 常用工作目录：~/projects\n"
        "- 偏好语言：中文\n"
        "- 代码风格：TypeScript, React, Python\n"
    ),
}


def _create_default_identity():
    """Create default identity files if the directory is empty."""
    try:
        PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
        for filename, content in DEFAULT_IDENTITY_FILES.items():
            filepath = PROGRESS_DIR / filename
            if not filepath.exists():
                filepath.write_text(content, encoding="utf-8")
    except Exception:
        logger.warning("Failed to create default identity files", exc_info=True)


# ═══════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════
#  Intent Detection: auto-update identity files from conversation
# ═══════════════════════════════════════════════════════

_IDENTITY_INTENTS = [
    # Agent rename: "叫你XX" / "改名为XX" / "rename yourself XX" → IDENTITY.md
    (re.compile(r"(?:以后)?(?:叫|称呼)你(?:为|是)?[「『\s]*([^\s，。,.]{1,20})[」』]*", re.IGNORECASE), "IDENTITY.md", "name"),
    (re.compile(r"(?:改|换)(?:个)?(?:名字|名称|名(?:为|叫|是))(?:叫|为|是)?[：:]*\s*[「『]*([^\s，。,.]{1,20})[」』]*", re.IGNORECASE), "IDENTITY.md", "name"),
    (re.compile(r"(?:rename|call)\s+(?:yourself|you)\s+(?:to\s+)?['\"]?(\w{1,20})['\"]?", re.IGNORECASE), "IDENTITY.md", "name"),
    # User's own name: "叫我XX" / "称呼我为XX" / "call me XX" → USER.md (not an agent rename)
    (re.compile(r"(?:以后)?(?:叫|称呼)我(?:为|是)?[「『\s]*([^\s，。,.]{1,20})[」』]*", re.IGNORECASE), "USER.md", "user_name"),
    (re.compile(r"(?:call|name)\s+me\s+['\"]?(\w{1,20})['\"]?", re.IGNORECASE), "USER.md", "user_name"),
    # 语气/风格（09-20 放宽）。原先只认"回复要X"/"说话要X"，而用户自然说法
    # （"跟我说话温柔一点""语气要严肃""用暧昧的口吻"）全部不命中 → 语气设置形同虚设。
    (re.compile(r"(?:语气|风格)\s*(?:要|改|换|用|为|是|应该|请|：|:)\s*([^，。,！!？?\n]{2,20})",
                re.IGNORECASE), "SOUL.md", "style"),
    (re.compile(r"用([^，。,！!？?\n]{1,12})的(?:语气|口吻|腔调)", re.IGNORECASE), "SOUL.md", "style"),
    (re.compile(r"(?:回复|回答|说话|聊天|讲话|跟我说话|回我)(?:我的时候|的时候|我)?\s*"
                r"(?:要|再|更|得|请|像)\s*([^，。,！!？?\n]{2,20})",
                re.IGNORECASE), "SOUL.md", "style"),
    # "跟我说话温柔暧昧一点" / "回复我的时候轻松点" —— 语气词在前、量词在后
    (re.compile(r"(?:回复|回答|说话|聊天|讲话|跟我说话|回我)(?:我的时候|的时候|我)?\s*"
                r"([^，。,！!？?\n]{2,14}?)\s*(?:一点|一些|点|些|就好|即可)",
                re.IGNORECASE), "SOUL.md", "style"),
    # Rule: "以后不要XX" / "从现在开始XX"
    (re.compile(r"(?:以后|从现在开始)[，,]*((?:不要|别|禁止|要|请|必须).{1,50})", re.IGNORECASE), "AGENTS.md", "rule"),
    # Preference: "我喜欢用XX" / "我常用XX" / "我偏好XX"
    # 09-20 修正：原写法把裸"用"也算偏好动词 → "帮我用一句话总结你自己" 命中
    # "我用" → 把用户提问写进 USER.md（持久化污染）+ 每轮注入"身份已更新"→ 头部
    # 逐轮变化 → 缓存 0%。现在要求：偏好动词明确、且位于句首或标点之后。
    (re.compile(r"(?:^|[，。！？、；\s])我(?:喜欢用|常用|习惯用|更?偏好|更?倾向于|一般用|平时用|想要|要求)(.{2,40})",
                re.IGNORECASE), "USER.md", "pref"),
]



def _detect_identity_intent(text: str) -> list[dict]:
    """
    Detect identity-related intents in user message.
    Returns list of {file, action, content} dicts.
    """
    if not text or len(text) > 200:
        return []
    # 请求句一律不当身份/偏好意图（09-20）：问句与"帮我/请问/怎么…"这类是任务，
    # 不是长期设定。原实现把"帮我用一句话总结你自己"当成 pref 写进 USER.md。
    _t = text.strip()
    if _t.endswith(("？", "?")) or re.search(
            r"帮我|请问|请帮|能不能|可不可以|如何|怎么|为什么|你有没有|我想知道", _t):
        return []
    results = []
    _BAD_TONE = ("什么", "如何", "怎么", "吗", "呢", "？", "?", "多少", "区别", "意思")
    for pattern, filename, action in _IDENTITY_INTENTS:
        m = pattern.search(text)
        if m:
            value = m.group(1).strip()
            if action == "style":
                value = re.sub(r"^(?:要|再|更|得|请|像|用|把|我的|我|的)+", "", value).strip()
                if not value or any(b in value for b in _BAD_TONE) or len(value) < 2:
                    continue   # 问句/垃圾值不当语气（"你的语气是什么"）
            if value and len(value) >= 1:
                results.append({"file": filename, "action": action, "value": value, "match": m.group(0)})
    return results


def _apply_name_change(new_name: str):
    """Update IDENTITY.md with new name.

    先按既有写法替换；文件被用户改过、没有这些写法时，在最前面补一行
    （原实现只在默认模板上做正则替换，用户自定义过就会静默失效）。
    """
    filepath = PROGRESS_DIR / "IDENTITY.md"
    try:
        content = filepath.read_text(encoding="utf-8") if filepath.exists() else "# Identity\n"
    except Exception:
        content = "# Identity\n"
    replaced = False
    for pattern, repl in (
        (r"你的名字是「[^」]*」", f"你的名字是「{new_name}」"),
        (r"你就是[^。\n]*", f"你就是{new_name}"),
        (r"以「[^」]*」的身份", f"以「{new_name}」的身份"),
    ):
        content, n = re.subn(pattern, repl, content)
        replaced = replaced or bool(n)
    if not replaced:
        content = f"你的名字是「{new_name}」。\n" + content
    filepath.write_text(content, encoding="utf-8")


def _apply_tone_change(value: str):
    """记录对话语气到 SOUL.md —— 替换而非追加（反复调整不会堆成互相矛盾的多行）。"""
    filepath = PROGRESS_DIR / "SOUL.md"
    line = f"- 对话语气：{value}"
    try:
        content = filepath.read_text(encoding="utf-8") if filepath.exists() else "# Soul\n"
        if re.search(r"^-\s*对话语气：.*$", content, re.M):
            content = re.sub(r"^-\s*对话语气：.*$", line, content, flags=re.M)
        elif "## 语气风格" in content:
            head, _, tail = content.partition("## 语气风格")
            content = f"{head}## 语气风格\n{line}\n{tail.lstrip(chr(10))}"
        else:
            if not content.endswith("\n"):
                content += "\n"
            content += f"\n## 语气风格\n{line}\n"
        filepath.write_text(content, encoding="utf-8")
    except Exception:
        logger.warning("Failed to record tone in SOUL.md", exc_info=True)


def _apply_user_name_change(new_name: str):
    """Record the user's preferred name in USER.md (replaces any existing value)."""
    filepath = PROGRESS_DIR / "USER.md"
    line = f"- 用户称呼：{new_name}\n"
    try:
        if filepath.exists():
            content = filepath.read_text(encoding="utf-8")
            if "用户称呼：" in content:
                content = re.sub(r"- 用户称呼：[^\n]*\n?", line, content)
            else:
                if not content.endswith("\n"):
                    content += "\n"
                content += line
            filepath.write_text(content, encoding="utf-8")
        else:
            filepath.write_text(f"# User Profile\n\n{line}", encoding="utf-8")
    except Exception:
        logger.warning("Failed to record user name in USER.md", exc_info=True)


def _apply_style_change(value: str):
    """Append style preference to SOUL.md (no duplicates).

    ⚠️ 09-21 起 `style` 意图不再走这里：语气要落到 `## 语气风格` 下并**替换**，
    所以映射到 `_apply_tone_change`。本函数保留给"只追加一行备注"的调用方。
    """
    filepath = PROGRESS_DIR / "SOUL.md"
    line = f"- {value}\n"
    if filepath.exists() and line in filepath.read_text(encoding="utf-8"):
        return
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(line)


def _apply_rule_change(value: str):
    """Append rule to AGENTS.md (no duplicates)."""
    filepath = PROGRESS_DIR / "AGENTS.md"
    line = f"- {value}\n"
    if filepath.exists() and line in filepath.read_text(encoding="utf-8"):
        return
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(line)


def _apply_pref_change(value: str):
    """Append preference to USER.md (no duplicates)."""
    filepath = PROGRESS_DIR / "USER.md"
    line = f"- {value}\n"
    if filepath.exists() and line in filepath.read_text(encoding="utf-8"):
        return
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(line)


_INTENT_APPLIERS = {
    "name": _apply_name_change,
    "user_name": _apply_user_name_change,
    # 09-21：语气改走结构化写入（`## 语气风格` 下的 `- 对话语气：X`，替换而非追加）。
    # 原来指向 _apply_style_change，会在文件末尾追加一条裸行——位置错、越攒越乱。
    "style": _apply_tone_change,
    "rule": _apply_rule_change,
    "pref": _apply_pref_change,
}


def _process_identity_intents(user_text: str):  # -> str | None
    """Process identity-related intents in user message. Updates identity files in background."""
    """
    Scan user message for identity intents, apply changes.
    Returns a summary message if changes were made, None otherwise.
    """
    intents = _detect_identity_intent(user_text)
    if not intents:
        return None
    changes = []
    for intent in intents:
        try:
            # 幂等（09-20）：文件里已有同样一行 → 不再写、也不再注入"身份已更新"
            # （重复写会让提示头逐轮变化、缓存全丢）
            # 09-21：语气（style）现在也是**替换式**写入，同样要幂等——否则用户
            # 重复同一句语气时，每轮都会认为"身份被更新"并注入易变块。
            _fp = PROGRESS_DIR / intent["file"]
            _existing = _fp.read_text(encoding="utf-8") if _fp.exists() else ""
            _expected = (f"- 对话语气：{intent['value']}" if intent["action"] == "style"
                         else f"- {intent['value']}")
            if intent["action"] in ("pref", "style") and _expected in _existing:
                continue
            applier = _INTENT_APPLIERS.get(intent["action"])
            if applier:
                applier(intent["value"])
                changes.append(f"{intent['file']}: {intent['action']}={intent['value']}")
        except Exception:
            logger.warning("Failed to apply identity intent", exc_info=True)
    if changes:
        return "已更新: " + "; ".join(changes)
    return None


# ═══════════════════════════════════════════════════════
#  SQLite Memory: FTS5 full-text search + Self-Learning
# ═══════════════════════════════════════════════════════

MEMORY_DB = PROGRESS_DIR / "memory.db"

