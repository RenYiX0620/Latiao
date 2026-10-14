# 通道接入（飞书 / 微信 / 其它 IM）

辣条可以被 IM 里的消息驱动：你在飞书或微信里发一句“记一笔：打车 38 元”，
辣条用同一套大脑（工具、记忆、技能、财务台账流程）处理，把回复发回那条通道。

## 架构（一层通道 = 三个动作）

```
外部消息 → [通道适配器] → channels_bridge.run_channel_turn()
                              ↓ 复用前端完全相同的入口
                    _resolve_api_target → _build_chat_messages → ThinAgentLoop
                              ↓
                        回复文本 → [通道适配器] → 发回用户
```

- **通道会话**按 `channel:<通道>:<会话ID>` 存进**同一张会话表** —— 多轮上下文自动延续，
  并且你在辣条界面里能看到通道里聊过什么（审计/排障用）。
- 加通道不改 agent，改 agent 不影响通道（与 MCP 工具同一种分层思路）。

## 飞书（长连接，无需公网地址）—— 已实现

代码：`sidecar/channels/feishu.py`（依赖 `lark-oapi`，已在 requirements 与 latiao.spec 里）。

**你要做的（在飞书开放平台，只能你来）**：

1. 创建**企业自建应用** → 拿 `App ID` / `App Secret`；
2. 「事件与回调」→ 订阅方式选 **长连接**（不要选 HTTP 回调，那需要公网地址）；
3. 添加事件：`im.message.receive_v1`（接收消息）；
4. 「权限管理」开通：`im:message`、`im:message:send_as_bot`；
5. 把你自己（或群）加进应用的**可用范围**，否则机器人收不到消息；
6. 把凭据写进 `~/.local-ai-os/config.json`（0600）：

```json
"channels": {
  "feishu": {"enabled": true, "app_id": "cli_xxx", "app_secret": "xxx"}
}
```

7. 重启辣条 → `sidecar.log` 出现 `飞书通道已启动（长连接）` 即成功；
   然后**在飞书里给机器人发一句话**试试。

**已知边界**：只处理文字消息（图片/文件会收到一句“请先转成文字”）；群聊需在可用范围内。

## 微信（经桥接，需 OpenClaw）

微信官方通道是**腾讯为 OpenClaw 维护的插件**（`@tencent-weixin/openclaw-weixin`，
扫码登录个人微信），辣条装不了它。所以走桥接：

```
微信 → OpenClaw（装 openclaw-weixin 插件）→ 桥接脚本 → POST /v1/channels/inbound → 辣条
                                                     ↑ 返回 {reply}，脚本发回微信
```

辣条这侧**已经就绪**：`POST /v1/channels/inbound`（鉴权与其它端点相同，只监听 127.0.0.1）
接受 `{"channel": "wechat", "chat_id": "...", "text": "..."}`，返回 `{"reply": "..."}`。
`channels_bridge.push_to_sidecar()` 已封装好这个调用（供桥接脚本直接复用）。

**你要做的**：① 在 OpenClaw 里装微信插件并扫码；② 写一个桥接脚本（收到消息 → 调上面端点
→ 把 reply 用插件发回）。②可以让我在你装好插件后写。

## 其它通道（钉钉/企微/QQ/Telegram…）

同一模式：能直连的照飞书写一个适配器（放进 `sidecar/channels/`），不能直连的走
`/v1/channels/inbound` 桥接。前端「通道」页已有这些通道的凭据槽位（keychain 存储），
后续可以把凭据从 config.json 迁到 keychain（经 stdin 传给侧车，与 app token 同一套做法）。
