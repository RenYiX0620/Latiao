"""HTTP 请求 JSON body 解析（唯一定义）。

api_routes* 五个模块曾各持一份 `_json_body`——同逻辑五副本，漂移温床
（2026-09-24 审计点名）。改动只改这里。
"""
from __future__ import annotations

from fastapi import HTTPException, Request


async def json_body(request: Request) -> dict:
    """解析请求 JSON body；非法 JSON 或非对象时返回 400，而不是让端点抛 500。"""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected JSON object")
    return body


# 旧名别名：调用点保留 `_json_body`，与拆分前一致
_json_body = json_body
