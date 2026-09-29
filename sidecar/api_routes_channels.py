"""通道入向端点（2026-09-29）：外部桥接器把消息投进来，拿回复文本回去。

给谁用：
- **微信**（及任何无法直连的 IM）：经 OpenClaw 通道插件收到消息后，由桥接脚本调
  这个端点（`channels_bridge.push_to_sidecar()` 已封装），把 reply 发回用户；
- 自研脚本 / 快捷指令 / 其它设备：同一形状的 POST 即可。

安全：与其它端点同一套鉴权（X-Latiao-Token / Bearer），只监听 127.0.0.1。
"谁能发消息"由桥接侧决定——本端点不做用户白名单，**通道凭据不出本机**。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from http_json import _json_body  # noqa: E402 — 唯一定义（禁各模块自带副本）

logger = logging.getLogger("latiao-sidecar")

router = APIRouter()


@router.post("/v1/channels/inbound")
async def channels_inbound(request: Request):
    """收一条通道消息 → 跑一轮 agent → 返回 {reply}。

    请求：{"channel": "wechat", "chat_id": "...", "text": "...", "agent": "latiao"?}
    响应：{"status": "ok", "reply": "...", "session_id": "channel:wechat:..."}
    """
    body = await _json_body(request)
    channel = str(body.get("channel") or "").strip()
    chat_id = str(body.get("chat_id") or "").strip()
    text = str(body.get("text") or "").strip()
    agent = str(body.get("agent") or "latiao").strip() or "latiao"
    if not channel or not chat_id:
        return {"status": "error", "message": "channel 与 chat_id 必填"}
    if not text:
        return {"status": "error", "message": "text 为空（图片/文件等非文本消息暂不支持，请在桥接侧先转文字）"}

    import channels_bridge as cb
    reply = await cb.run_channel_turn(channel, chat_id, text, agent=agent)
    return {"status": "ok", "reply": reply,
            "session_id": cb.channel_session_id(channel, chat_id)}
