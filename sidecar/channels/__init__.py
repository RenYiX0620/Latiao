"""通道包（2026-09-29）：把外部 IM 接进辣条。

- `channels_bridge`（上层模块）：通道无关的"收消息→跑 agent→拿回复"；
- `channels/feishu.py`：飞书长连接适配器（需 lark-oapi + 应用凭据）；
- 微信等无法直连的通道：经外部桥接器打 `/v1/channels/inbound`（见 api_routes_channels）。
"""
