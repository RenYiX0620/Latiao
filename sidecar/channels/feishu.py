"""飞书通道（长连接模式，2026-09-29）。

为什么用长连接：飞书事件订阅有两种——HTTP 回调（要公网地址，本机做不到）与
**长连接**（应用主动连飞书，无需公网）。用户机器上 OpenClaw 的飞书通道就是
`connectionMode: websocket`，同一条路。

依赖：`lark-oapi`（pip 可装，纯 Python + 少量 C 扩展）。**没装不报错**——只记一条
可读的日志，其余功能照常（与 TTS/语义检索的降级契约一致）。

配置（`config.json`，0600，与 mx_api_key 同处）：
```json
"channels": {"feishu": {"enabled": true, "app_id": "cli_xxx", "app_secret": "xxx"}}
```

用户在飞书开放平台要做的事（**只能你来，我做不了**）：
1. 创建企业自建应用 → 拿 App ID / App Secret；
2. 「事件与回调」→ 订阅方式选**长连接** → 添加事件 `im.message.receive_v1`；
3. 「权限管理」开：`im:message`、`im:message:send_as_bot`（发消息）、
   `im:chat:readonly`（可选，读群信息）；
4. 把个人/群加进应用的可用范围（否则机器人收不到消息）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading

logger = logging.getLogger("latiao-sidecar")

_started = False
_lock = threading.Lock()


def _read_cfg() -> dict:
    """读 config.json 的 channels.feishu 段。"""
    try:
        import json as _json
        from config import CONFIG_FILE
        if not CONFIG_FILE.exists():
            return {}
        cfg = _json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        sec = ((cfg.get("channels") or {}).get("feishu") or {}) if isinstance(cfg, dict) else {}
        return sec if isinstance(sec, dict) else {}
    except Exception:
        logger.debug("飞书配置读取失败", exc_info=True)
        return {}


def _extract_text(message: dict) -> str:
    """从飞书消息体里取纯文本（只支持文本；其它类型返回空串由上层提示）。"""
    try:
        mtype = str(message.get("message_type") or "")
        if mtype != "text":
            return ""
        content = message.get("content") or "{}"
        if isinstance(content, str):
            content = json.loads(content)
        return str((content or {}).get("text") or "").strip()
    except Exception:
        return ""


def start() -> bool:
    """启动飞书长连接（幂等；未启用/未装依赖/未配置都返回 False 并留日志）。"""
    global _started
    with _lock:
        if _started:
            return True
        cfg = _read_cfg()
        if not cfg.get("enabled"):
            logger.info("飞书通道未启用（config.json → channels.feishu.enabled）")
            return False
        app_id, app_secret = str(cfg.get("app_id") or ""), str(cfg.get("app_secret") or "")
        if not app_id or not app_secret:
            logger.warning("飞书通道已启用但缺 app_id/app_secret —— 不启动")
            return False
        try:
            import lark_oapi as lark
        except ImportError:
            logger.warning("飞书通道需要 lark-oapi（pip install lark-oapi）—— 本机未装，通道不启动")
            return False

        def _on_message(data) -> None:
            """飞书事件回调（SDK 在自己的线程里调）。"""
            try:
                msg = getattr(getattr(data, "event", None), "message", None)
                sender = getattr(getattr(data, "event", None), "sender", None)
                m = {
                    "message_type": getattr(msg, "message_type", ""),
                    "content": getattr(msg, "content", "{}"),
                    "chat_id": getattr(msg, "chat_id", ""),
                    "message_id": getattr(msg, "message_id", ""),
                }
                text = _extract_text(m)
                chat_id = str(m["chat_id"] or "")
                user_id = str(getattr(getattr(sender, "sender_id", None), "user_id", "") or "")
                if not chat_id:
                    return
                if not text:
                    _send_text(lark, app_id, app_secret, chat_id,
                               "（我目前只认文字消息；图片/文件请先转成文字发我）")
                    return
                logger.info("飞书消息: chat=%s user=%s len=%d",
                            chat_id[:12], user_id[:12], len(text))
                import channels_bridge as cb
                reply = asyncio.run(cb.run_channel_turn("feishu", chat_id, text))
                _send_text(lark, app_id, app_secret, chat_id, reply)
            except Exception:
                logger.error("飞书消息处理失败", exc_info=True)

        handler = (lark.EventDispatcherHandler.builder("", "")
                   .register_p2_im_message_receive_v1(_on_message)
                   .build())
        client = lark.ws.Client(app_id, app_secret, event_handler=handler,
                                log_level=lark.LogLevel.INFO)
        threading.Thread(target=client.start, name="feishu-ws", daemon=True).start()
        _started = True
        logger.info("飞书通道已启动（长连接）")
        return True


def _send_text(lark, app_id: str, app_secret: str, chat_id: str, text: str) -> None:
    """发送文本消息（失败只记日志——用户已在通道里等，服务端不该因此崩）。"""
    try:
        client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
        body = (lark.im.v1.CreateMessageRequestBody.builder()
                .receive_id(chat_id).msg_type("text")
                .content(json.dumps({"text": text}, ensure_ascii=False)).build())
        req = (lark.im.v1.CreateMessageRequest.builder()
               .receive_id_type("chat_id").request_body(body).build())
        resp = client.im.v1.message.create(req)
        if not getattr(resp, "success", lambda: False)():
            logger.warning("飞书发送失败: code=%s msg=%s",
                           getattr(resp, "code", "?"), getattr(resp, "msg", "?"))
    except Exception:
        logger.error("飞书发送异常", exc_info=True)
