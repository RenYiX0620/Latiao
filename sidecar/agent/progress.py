"""进度文件（PROGRESS）读写——从 agent_loop.py 拆出的第一块纯接缝（2026-09-23）。

为什么单独成模块：这块是"按会话分文件 + 注入只取本会话"的契约落点（审计 P1：跨会话
提示词污染的唯一路径），也是 _build_chat_messages 每次组装上下文都要读的东西；
它自身只依赖 config/日志/正则，搬出来后单测可以直接打这一块（tests/test_progress_isolation.py）。

本模块是进度路径的**规范所有者**：PROGRESS_DIR / PROGRESS_FILE 从这里取。
agent_loop.py 仍 re-export 这些函数名（api_routes / main / 既有测试按旧路径导入）。
"""
import logging
import os
import re
from datetime import datetime
from pathlib import Path

from config import PROGRESS_DIR

logger = logging.getLogger("latiao-sidecar")   # 与 agent_loop 同名：日志格式不变

PROGRESS_FILE = PROGRESS_DIR / "PROGRESS.md"


def _progress_file(session_id: str | None = None) -> Path:
    """本会话的进度文件（09-23 按会话隔离）。

    此前所有会话共写一个 PROGRESS.md，而注入是把它的**尾部贴到最后一条用户消息**
    里（权重最高）——于是 B 会话会"看到"A 会话（含 A 的子代理）的文件/命令输出，
    表现为答非所问（用户实测："无论问什么都去找股市"）。按会话分文件后，注入只取
    本会话自己的记录。没有 session_id（cron/旧调用）时退回共享文件，行为同旧版。
    """
    sid = str(session_id or "").strip()
    if not sid:
        return PROGRESS_FILE
    # 会话 id 来自前端（session_<uuid>）或 uuid；白名单化避免路径穿越
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", sid)[:64] or "anon"
    return PROGRESS_DIR / f"PROGRESS.{safe}.md"


def _record_progress(entry: str, session_id: str | None = None):
    """Append a progress entry for cross-session continuity.

    写两处：本会话文件（注入只读它）+ 共享 PROGRESS.md（保持"断点续作"与
    read_file 启动协议不变——那是模型显式发起的读取，不是无条件注入）。
    """
    for path in {_progress_file(session_id), PROGRESS_FILE}:
        try:
            PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
            now = datetime.now().isoformat()
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"### {now}\n{entry}\n\n")
            # 体积轮转（审计 B10）：append-only 已长到 1.1MB 且无上限。
            # 超 1MB 时保留尾部 500KB——read_file 从头截断读最旧 5 万字符，
            # 轮转同时保证最近进度仍在文件里（尾部注入读的也是最新段）
            if path.stat().st_size > 1024 * 1024:
                _rotate_progress_file(path)
        except Exception:
            logger.warning("Failed to record progress", exc_info=True)


def _rotate_progress_file(path: Path | None = None):
    """把进度文件截到最近 500KB（保留尾部，即最新进度）。

    切点必须对齐 UTF-8 字符边界：按字节直切会在多字节汉字中间断开，
    文件从此不再是合法 UTF-8 → read_file 整个拒绝读取 → 断点续作失效
    （22:46 事故根因："文件编码不是 UTF-8"）。
    09-23：写临时文件再 os.replace——旧实现直接 "wb" 截断再写，同会话内
    并发写（后台子代理与父回合）会踩到这个窗口丢记录。
    """
    target = path or PROGRESS_FILE
    try:
        keep = 500 * 1024
        size = target.stat().st_size
        if size <= keep:
            return
        with open(target, "rb") as f:
            f.seek(size - keep)
            tail = f.read()
        # 对齐字符边界：跳过开头的残缺多字节字符（找下一个 UTF-8 合法起始字节）
        offset = 0
        while offset < len(tail):
            b = tail[offset]
            # 合法起始：ASCII(<0x80) 或 2/3/4 字节前缀(0xC0-0xF7)
            if b < 0x80 or (0xC0 <= b <= 0xF7):
                break
            offset += 1
        tail = tail[offset:]
        tmp = target.with_suffix(target.suffix + ".tmp")
        with open(tmp, "wb") as f:
            f.write("(早期进度已轮转)\n\n".encode("utf-8") + tail)
        os.replace(tmp, target)
    except Exception:
        logger.warning("PROGRESS 轮转失败", exc_info=True)


def _progress_tail(max_chars: int = 600, session_id: str | None = None) -> str:
    """读取**本会话**进度文件尾部（最新进度），用于注入。

    600 而非 2000：这段是 2/3 英文的工具日志，2000 字符的英文注入
    是新会话被带偏成英文回复的最大英文源（09-03 事故）。"""
    path = _progress_file(session_id)
    try:
        if not path.exists():
            return ""
        size = path.stat().st_size
        read = min(size, max_chars * 4)  # 多读些字节再截字符，中文占 3 字节
        with open(path, "rb") as f:
            f.seek(max(0, size - read))
            tail = f.read().decode("utf-8", errors="replace")
        return tail[-max_chars:]
    except Exception:
        return ""


# ── Native tool call format parser (for models like Gemma that use
#    <|tool_call|>call:name{args}<tool_call|> instead of OpenAI JSON) ──

def _clean_progress_tail(text: str, max_lines: int = 12) -> str:
    """过滤"进展尾巴"里的**工具调用日志**，只留人类可读的进展。

    09-20 事故：PROGRESS.md 尾部记着上一会话的 `**mx_query** / Args: {...} /
    Result: 半导体板块…` 工具日志，被贴到用户每条消息尾部（权重最高）→ 用户问
    任何事，弱模型都以为本轮要查行情（"我说什么他都去找股市"）。

    09-23 改为**按条目整块**判定：旧实现只按行丢弃（`### ` / `**` / `Args:` /
    `Result:` 开头的行），多行工具结果（JSON、表格）的续行不在这些前缀里 →
    模型看到半截数据。现在整块含工具日志标记就整块丢掉，`### ` 视为条目分界。
    """
    blocks: list[list[str]] = []
    cur: list[str] = []
    for ln in str(text or "").splitlines():
        if ln.lstrip().startswith("### "):
            blocks.append(cur)
            cur = []
            continue
        cur.append(ln)
    blocks.append(cur)
    keep: list[str] = []
    for blk in blocks:
        body = "\n".join(x for x in (l.strip() for l in blk) if x)
        if not body:
            continue
        if any(m in body for m in ("Args: `", "Result: `", "Tool result", "```")):
            continue
        keep.extend(body.splitlines())
    return "\n".join(keep[-max_lines:])
