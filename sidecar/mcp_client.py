"""轻量 MCP（Model Context Protocol）客户端——无官方 SDK 依赖。

支持两种 transport：
- stdio : 本地子进程（command/args/env），Content-Length 帧协议（MCP 规范）
- http  : 远程服务（url），JSON-RPC POST（Streamable HTTP 单请求-响应模式）

生命周期：懒连接 + 缓存；连接失败返回错误文本而非崩溃（工具级容错）。
工具命名映射：mcp_<server>_<tool>，schema 转 OpenAI function 格式。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import uuid

import httpx

logger = logging.getLogger("latiao-sidecar")

PROTOCOL_VERSION = "2024-11-05"
_CLIENT_INFO = {"name": "latiao-sidecar", "version": "0.2.0"}
_HEADER_RE = re.compile(rb"([A-Za-z-]+):\s*([^\r\n]*)")


class MCPError(Exception):
    pass


class MCPClient:
    def __init__(self, server_name: str, config: dict, connect_timeout: float = 6.0):
        self.name = server_name
        self.config = config or {}
        self._timeout = connect_timeout
        self._proc: asyncio.subprocess.Process | None = None
        self._read_buf = b""
        self._next_id = 0
        self._tools_cache: list[dict] | None = None
        self._connected = False

    # ── transport 生命周期 ──

    async def connect(self) -> None:
        if self._connected:
            return
        if self.config.get("type") == "http" or self.config.get("url"):
            await self._attach_http()
        else:
            await self._attach_stdio()
        # 握手
        try:
            await self._rpc("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": _CLIENT_INFO,
            }, timeout=self._timeout)
            # stdio 需要 initialized notification
            if self._proc is not None:
                await self._notify("notifications/initialized", {})
            self._connected = True
        except Exception:
            await self.close()
            raise

    async def _attach_stdio(self) -> None:
        cmd = self.config.get("command")
        if not cmd:
            raise MCPError(f"MCP {self.name}: 缺少 command")
        args = [str(a) for a in (self.config.get("args") or [])]
        env = dict(self.config.get("env") or {})
        try:
            self._proc = await asyncio.create_subprocess_exec(
                cmd, *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env={**dict(__import__("os").environ), **env},
            )
        except FileNotFoundError as e:
            raise MCPError(f"MCP {self.name}: 命令不存在 {cmd}") from e

    async def _attach_http(self) -> None:
        # HTTP transport 无需持久连接；仅校验 url 存在
        if not self.config.get("url"):
            raise MCPError(f"MCP {self.name}: 缺少 url")

    async def close(self) -> None:
        if self._proc is not None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=2)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
        self._connected = False

    # ── 消息收发（stdio 帧 / http POST） ──

    async def _rpc(self, method: str, params: dict, timeout: float = 120) -> dict:
        self._next_id += 1
        rpc_id = self._next_id
        msg = {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params}
        if self._proc is not None:
            raw = await self._stdio_request(msg, timeout)
        else:
            raw = await self._http_request(msg, timeout)
        if raw is None:
            raise MCPError(f"MCP {self.name}: 无响应（method={method}）")
        if "error" in raw:
            err = raw["error"]
            raise MCPError(f"MCP {self.name} {method}: {err.get('message', err)}")
        return raw.get("result") or {}

    async def _notify(self, method: str, params: dict) -> None:
        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        payload = json.dumps(msg).encode()
        if self._proc is None:
            return  # http 下 notification 无必要
        body = f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload
        self._proc.stdin.write(body)
        await self._proc.stdin.drain()

    async def _stdio_request(self, msg: dict, timeout: float) -> dict | None:
        payload = json.dumps(msg).encode()
        body = f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload
        self._proc.stdin.write(body)
        try:
            await asyncio.wait_for(self._proc.stdin.drain(), timeout=timeout)
        except asyncio.TimeoutError:
            raise MCPError(f"MCP {self.name}: 发送超时")
        frame = await self._read_frame(timeout)
        if frame is None:
            return None
        try:
            return json.loads(frame)
        except json.JSONDecodeError:
            raise MCPError(f"MCP {self.name}: 响应非 JSON")

    async def _read_frame(self, timeout: float) -> bytes | None:
        """读取一个 Content-Length 帧（循环处理响应的字节流）。"""
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remain = deadline - asyncio.get_event_loop().time()
            if remain <= 0:
                raise MCPError(f"MCP {self.name}: 读取超时")
            idx = self._read_buf.find(b"\r\n\r\n")
            if idx >= 0:
                header = self._read_buf[:idx].decode(errors="replace")
                length = 0
                for line in header.split("\r\n"):
                    m = _HEADER_RE.match(line.encode())
                    if m and m.group(1).lower() == b"content-length":
                        length = int(m.group(2))
                body_start = idx + 4
                if len(self._read_buf) >= body_start + length:
                    frame = self._read_buf[body_start:body_start + length]
                    self._read_buf = self._read_buf[body_start + length:]
                    return frame
            try:
                chunk = await asyncio.wait_for(self._proc.stdout.read(65536), timeout=max(0.1, remain))
            except asyncio.TimeoutError:
                raise MCPError(f"MCP {self.name}: 读取超时")
            if not chunk:
                raise MCPError(f"MCP {self.name}: 进程已退出")
            self._read_buf += chunk

    async def _http_request(self, msg: dict, timeout: float) -> dict | None:
        url = self.config["url"]
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    url, json=msg,
                    headers={"Accept": "application/json, text/event-stream",
                             "Content-Type": "application/json"},
                )
                resp.raise_for_status()
                text = resp.text.strip()
                # Streamable HTTP 可能返回 SSE 格式（event: message\ndata: {...}）
                if text.startswith("event:") or text.startswith("data:"):
                    for line in text.splitlines():
                        if line.startswith("data:"):
                            return json.loads(line[5:].strip())
                    return None
                return json.loads(text)
        except httpx.HTTPStatusError as e:
            raise MCPError(f"MCP {self.name}: HTTP {e.response.status_code}") from e
        except Exception as e:
            raise MCPError(f"MCP {self.name}: 请求失败 {e}") from e

    # ── 工具发现与调用 ──

    async def list_tools(self) -> list[dict]:
        """返回 MCP 工具列表（原始 schema 字典）。"""
        if self._tools_cache is not None:
            return self._tools_cache
        await self.connect()
        result = await self._rpc("tools/list", {})
        tools = result.get("tools") or []
        self._tools_cache = tools
        return tools

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        await self.connect()
        result = await self._rpc("tools/call", {
            "name": tool_name, "arguments": arguments or {},
        })
        content = result.get("content") or []
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                else:
                    parts.append(f"[{item.get('type', 'data')}]")
            else:
                parts.append(str(item))
        text = "\n".join(p for p in parts if p).strip()
        if result.get("isError"):
            return f"⛔ MCP 工具错误: {text or '未知错误'}"
        return text or "(空结果)"


# ── 注册表：server_name -> MCPClient（进程存活期内缓存） ──
_MCP_CLIENTS: dict[str, MCPClient] = {}


def get_mcp_client(server_name: str, config: dict) -> MCPClient:
    client = _MCP_CLIENTS.get(server_name)
    if client is None:
        client = MCPClient(server_name, config)
        _MCP_CLIENTS[server_name] = client
    return client


def sanitize_tool_name(raw: str) -> str:
    """MCP 工具名转合法函数名（只保留字母数字下划线）。"""
    s = re.sub(r"[^a-zA-Z0-9_]", "_", raw)
    if not s or s[0].isdigit():
        s = "t_" + s
    return s[:64]
