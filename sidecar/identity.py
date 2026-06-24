"""Identity System — agent identity files, intents, and profile management."""
import logging
import os
import re
from datetime import datetime
from pathlib import Path

from config import PROGRESS_DIR

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
            txt = sf.read_text().strip()
            header = f"# {agent_id} - {section}"
            if txt and txt != header and txt != f"{header}\n\n（此部分内容待补充）":
                parts.append(f"## {section}\n{txt}")
    
    if parts:
        return "\n\n".join(parts)
    
    # Fall back to combined identity file
    agent_file = (agents_dir / f"{agent_id}.txt").resolve()
    if not str(agent_file).startswith(str(agents_dir.resolve()) + "/"):
        return fallback
    if agent_file.exists():
        try:
            return agent_file.read_text()
        except Exception as e:
            logger.warning("Failed to load agent identity from %s: %s", agent_file, e)
    return fallback


def _read_identity() -> list[dict]:
    """Read all identity files from ~/.local-ai-os/ and return system messages."""
    msgs = []
    for filename in IDENTITY_FILES:
        filepath = PROGRESS_DIR / filename
        try:
            if filepath.exists():
                content = filepath.read_text(encoding="utf-8").strip()
                if content:
                    msgs.append({"role": "system", "content": content})
        except Exception:
            logger.warning("Failed to read identity file", exc_info=True)
    return msgs



def _create_default_identity():
    """Create default identity files if the directory is empty."""
    try:
        PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
        defaults = {
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
        for filename, content in defaults.items():
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
    # Only trigger on explicit commands: "叫你XX", "改名为XX", "call me XX"
    (re.compile(r"(?:以后)?(?:叫|称呼)(?:我|你)(?:为|是)?[「『\s]*([^\s，。,.]{1,20})[」』]*", re.IGNORECASE), "IDENTITY.md", "name"),
    (re.compile(r"(?:改|换)(?:个)?(?:名字|名称)(?:叫|为|是)?[：:]*\s*[「『]*([^\s，。,.]{1,20})[」』]*", re.IGNORECASE), "IDENTITY.md", "name"),
    (re.compile(r"(?:call|name)\s+me\s+['\"]?(\w{1,20})['\"]?", re.IGNORECASE), "IDENTITY.md", "name"),
    # Style/tone: only explicit "回复要XX" or "说话风格XX"
    (re.compile(r"(?:回复|说话)(?:要|再|更)([^，。,！!]{2,30})", re.IGNORECASE), "SOUL.md", "style"),
    # Rule: "以后不要XX" / "从现在开始XX"
    (re.compile(r"(?:以后|从现在开始)[，,]*((?:不要|别|禁止|要|请|必须).{1,50})", re.IGNORECASE), "AGENTS.md", "rule"),
    # Preference: "我喜欢用XX" / "我常用XX"
    (re.compile(r"我(?:喜欢用|常用|习惯用|偏好|用)(.{2,50})", re.IGNORECASE), "USER.md", "pref"),
]



def _detect_identity_intent(text: str) -> list[dict]:
    """
    Detect identity-related intents in user message.
    Returns list of {file, action, content} dicts.
    """
    if not text or len(text) > 200:
        return []
    results = []
    for pattern, filename, action in _IDENTITY_INTENTS:
        m = pattern.search(text)
        if m:
            value = m.group(1).strip()
            if value and len(value) >= 1:
                results.append({"file": filename, "action": action, "value": value, "match": m.group(0)})
    return results


def _apply_name_change(new_name: str):
    """Update IDENTITY.md with new name."""
    filepath = PROGRESS_DIR / "IDENTITY.md"
    content = filepath.read_text(encoding="utf-8")
    # Replace existing name references
    content = re.sub(r"你的名字是「[^」]*」", f"你的名字是「{new_name}」", content)
    content = re.sub(r"你就是[^。\n]*", f"你就是{new_name}", content)
    content = re.sub(r"以「[^」]*」的身份", f"以「{new_name}」的身份", content)
    filepath.write_text(content, encoding="utf-8")


def _apply_style_change(value: str):
    """Append style preference to SOUL.md (no duplicates)."""
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
    "style": _apply_style_change,
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

