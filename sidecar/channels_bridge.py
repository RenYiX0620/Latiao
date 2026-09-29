"""通道桥（2026-09-29）：外部消息（飞书/微信/任意 IM）→ 辣条 agent → 回复文本。

为什么单独一层：通道是入口，**大脑只有一份**。飞书长连接适配器、微信经 OpenClaw
的桥接、以及未来任何 IM，都只做三件事——收到消息、调 `run_channel_turn()`、把返回
的文本发回去。这样加通道不改 agent，改 agent 不影响通道（对齐 mcp_tools 的分层思路）。

会话按 `channel:<通道>:<会话ID>` 存储，进**同一张会话表**：
- 多轮上下文自动延续（与前端会话同一套持久化）；
- 用户在辣条界面能直接看到通道里聊了什么（审计与排障都靠这个）；
- 重启不丢（DB 权威，非内存）。
"""
from __future__ import annotations

import json
import logging
import time

import httpx

logger = logging.getLogger("latiao-sidecar")   # 与其它模块同名：日志格式不变

# 通道会话的系统提示（覆盖默认身份里的"你是本机助手"——通道里没人看界面）
_CHANNEL_HINT = (
    "【通道消息】这条消息来自{channel}，你在那条通道里替用户干活。"
    "回复要**直接、简短、可读**（聊天窗口不是终端）：先给结论，需要时再给要点；"
    "不要输出 Markdown 表格（多数 IM 渲染不了），需要列数据时用短行或编号。"
    "需要用户决策时直接问，但一次只问一件最关键的事。"
)


def channel_session_id(channel: str, chat_id: str) -> str:
    """通道会话 id（稳定、可读、与前端会话同表）。"""
    ch = "".join(c for c in str(channel or "") if c.isalnum() or c in "_-")[:24] or "unknown"
    cid = "".join(c for c in str(chat_id or "") if c.isalnum() or c in "_-:@.")[:64] or "unknown"
    return f"channel:{ch}:{cid}"


def _load_history(session_id: str) -> list[dict]:
    """读该通道会话的历史消息（失败返回空——通道消息不该因为读不到历史就发不出去）。"""
    try:
        import sessions as _sessions
        got = _sessions.get_session(session_id)
        if got.get("status") == "ok":
            out = []
            for m in got.get("messages") or []:
                if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str):
                    out.append({"role": m["role"], "content": m["content"]})
            return out[-40:]   # 只带最近 20 轮：通道对话多为短问答，长历史只烧 token
    except Exception:
        logger.debug("通道历史读取失败（按新会话继续）", exc_info=True)
    return []


def _save_turn(session_id: str, channel: str, user_text: str,
               reply: str, history: list[dict]) -> None:
    """把这一轮写进会话表（快照式，与前端同一入口）。"""
    try:
        import sessions as _sessions
        msgs = list(history) + [
            {"id": f"msg_{int(time.time() * 1000)}_u", "role": "user",
             "content": user_text},
            {"id": f"msg_{int(time.time() * 1000)}_a", "role": "assistant",
             "content": reply},
        ]
        _sessions.save_session(session_id, name=f"{channel} 通道", messages=msgs)
    except Exception:
        logger.warning("通道会话落库失败（回复仍会发出）", exc_info=True)


async def run_channel_turn(channel: str, chat_id: str, text: str, *,
                           agent: str = "latiao", access_mode: str = "confirm",
                           timeout: float = 600.0) -> str:
    """跑一轮通道对话，返回最终回复文本（失败返回可读的错误说明，不抛异常）。

    走的是**与前端完全相同的入口**：`_resolve_api_target` 选引擎 →
    `_build_chat_messages` 组装身份/工具/技能 → `ThinAgentLoop` 跑循环。
    """
    text = (text or "").strip()
    if not text:
        return "（空消息：请在{ch}里发一句文字）".replace("{ch}", channel or "通道")
    session_id = channel_session_id(channel, chat_id)
    history = _load_history(session_id)
    try:
        from api_routes import _resolve_api_target
        from agent_loop import _build_chat_messages
        protocol, api_url, headers, is_local = await _resolve_api_target(None)
        # 通道提示走一条 frontend system 消息（_build_chat_messages 会把前端 system
        # 并入系统提示）——不新增 body 字段，避免"字段没人读、静默失效"。
        _hint = _CHANNEL_HINT.format(channel=channel)
        # non_interactive：通道消息不是"用户在回答首启引导"（否则引导期里会被记成
        # 称呼/名字——2026-09-29 审计的同一类问题）
        body = {"agent": agent, "session_id": session_id, "non_interactive": True,
                "messages": [{"role": "system", "content": _hint}]
                            + history + [{"role": "user", "content": text}]}
        msgs = _build_chat_messages(body, body["messages"])
        from agent.loop import ThinAgentLoop
        parts: list[str] = []
        async for evt in ThinAgentLoop(
                msgs, body.get("model") or "", api_url, headers, session_id,
                access_mode, "high", is_local=is_local).run():
            if isinstance(evt, dict) and evt.get("content"):
                parts.append(str(evt["content"]))
        reply = "".join(parts).strip() or "（这轮没有产出内容，请重发一次或换个说法）"
    except Exception as e:
        logger.error("通道轮次失败: %s", e, exc_info=True)
        reply = f"⚠️ 处理这条消息时出错：{type(e).__name__}。请重试；若持续失败请到辣条里看日志。"
    # 每轮指标落库（2026-09-29）：通道路径同样一行一轮（与 SSE 层同源口径）
    try:
        import turn_metrics
        turn_metrics.record_turn(session_id, model=body.get("model") or "",
                                 is_local=bool(is_local), ended_reason="channel")
    except Exception:
        logger.debug("turn_metrics 落库失败（通道）", exc_info=True)
    _save_turn(session_id, channel, text, reply, history)
    logger.info("通道消息: %s/%s → 回复 %d 字", channel, str(chat_id)[:12], len(reply))
    return reply


async def push_to_sidecar(channel: str, chat_id: str, text: str,
                          token: str, port: int = 8765) -> str:
    """通过 HTTP 让**正在运行的侧车**处理这条消息（外部进程/桥接器用）。

    与 `run_channel_turn` 分开的原因：微信那类经 OpenClaw 桥接的路径是**外部进程**，
    它不该 import 侧车代码，只该打这个端点（`/v1/channels/inbound`）。
    """
    url = f"http://127.0.0.1:{port}/v1/channels/inbound"
    async with httpx.AsyncClient(timeout=900) as c:
        r = await c.post(url, json={"channel": channel, "chat_id": chat_id, "text": text},
                         headers={"X-Latiao-Token": token, "Content-Type": "application/json"})
        r.raise_for_status()
        data = r.json()
    return str(data.get("reply") or json.dumps(data, ensure_ascii=False)[:300])
