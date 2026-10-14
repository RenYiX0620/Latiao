"""MCP stdio 分帧两制兼容（2026-09-28 首连 filesystem server 实测发现）。

MCP 规范 2024-11-05 的 stdio 是 Content-Length 头帧；官方 SDK 2026.8.31 起改成
按行分隔（每行一个 JSON）。旧客户端实现只说 Content-Length 制 → 新版 server
把帧头当坏行丢弃、永不回应 → 握手读取超时。_encode_frame/_read_frame 现在两制
都兼容。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LATIAO_AUTH_TOKEN", "mcp-framing-token")

from mcp_client import MCPClient, _encode_frame


def test_encode_frame_carries_both_framings():
    """发送帧同时带 Content-Length 头与行尾（两制 server 都能读到 JSON）。"""
    payload = b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'
    frame = _encode_frame(payload)
    assert frame.startswith(b"Content-Length: "), "旧式 server 需要头帧"
    assert frame.rstrip(b"\n").endswith(payload), "新式 server 需要裸 JSON 行"
    assert frame.endswith(b"\n"), "新式 server 以换行分帧"
    # 新式 server 逐行读：第一行是头（非 JSON，被跳过），第二行就是 JSON
    lines = frame.split(b"\n")
    assert lines[-2] == payload


def _client_with_buf(data: bytes) -> MCPClient:
    c = MCPClient("t", {})
    c._read_buf = data
    return c


def test_read_frame_content_length_mode():
    """读侧制式一：Content-Length 头帧（旧 server 响应）。"""
    body = b'{"result":"ok-old"}'
    c = _client_with_buf(b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body + b"\n")
    import asyncio

    async def once():
        return await c._read_frame(timeout=1)

    frame = asyncio.run(once())
    assert frame == body


def test_read_frame_line_delimited_mode():
    """读侧制式二：按行分隔（官方 SDK 2026.8.31+ 的响应）。"""
    c = _client_with_buf(b'{"result":"ok-new"}\n')
    import asyncio

    async def once():
        return await c._read_frame(timeout=1)

    frame = asyncio.run(once())
    assert frame == b'{"result":"ok-new"}'


def test_read_frame_skips_non_json_lines():
    """读侧容错：帧头残片/非 JSON 行被跳过，不误当响应。"""
    c = _client_with_buf(b"Content-Length: 99\r\n" + b'{"jsonrpc":"2.0"}\n')
    import asyncio

    async def once():
        return await c._read_frame(timeout=1)

    frame = asyncio.run(once())
    assert frame == b'{"jsonrpc":"2.0"}'


def test_content_length_prefers_header_over_line():
    """头帧与行混合时头帧优先（旧 server 的多行响应不被拆坏）。"""
    body = b'{"a":1,"b":2}'
    c = _client_with_buf(b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
    import asyncio

    async def once():
        return await c._read_frame(timeout=1)

    assert asyncio.run(once()) == body
