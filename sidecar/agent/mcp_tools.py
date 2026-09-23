"""MCP 工具加载（agent_loop.py 拆出的第四块，2026-09-23）。

把 mcp_client 里的 stdio MCP server 暴露成辣条工具：命名前缀、注册进 TOOLS、
惰性加载（首次用到才连）。与枢纽的其它职责（工具执行/确认/上下文）无关，
自身只依赖 TOOLS 注册表与 mcp_client。
"""
import logging


logger = logging.getLogger("latiao-sidecar")   # 与 agent_loop 同名：日志格式不变

async def _load_mcp_tools() -> None:
    # 依赖仍留在 agent_loop 的定义 → 函数内惰性导入（避免循环依赖）
    from agent_loop import TOOLS
    from agent_loop import TOOL_PERMISSIONS
    from agent_loop import TOOL_DISPATCH
    """扫描启用扩展的 mcpServers 声明，把远程工具并入 TOOLS/DISPATCH。"""
    try:
        from extension_manager import active_extension_dirs, _read_manifest
        from mcp_client import get_mcp_client
        for ext_dir in active_extension_dirs():
            manifest = _read_manifest(ext_dir) or {}
            servers = manifest.get("mcpServers") or {}
            for srv_name, cfg in servers.items():
                if not isinstance(cfg, dict):
                    continue
                entry = f"{ext_dir.parent.name}/{srv_name}"
                try:
                    client = get_mcp_client(entry, cfg)
                    tools = await client.list_tools()
                except Exception as e:
                    logger.warning("MCP 连接失败 %s: %s", entry, e)
                    continue
                for t in tools:
                    tname = t.get("name", "")
                    if not tname:
                        continue
                    fname = _mcp_tool_name(srv_name, tname)
                    schema = (t.get("inputSchema") or {}).copy()
                    desc = t.get("description") or f"MCP 工具 {srv_name}:{tname}"
                    # 已有同名工具时跳过（内置优先）
                    if any(td.get("function", {}).get("name") == fname for td in TOOLS):
                        continue
                    TOOLS.append({
                        "type": "function",
                        "function": {"name": fname, "description": desc, "parameters": schema},
                    })
                    # 审计 P2-18：MCP 工具此前一律注册 safe（免确认）。MCP 服务
                    # 可暴露任意能力（网络/文件/系统命令），且扩展本身无签名
                    # 校验——默认降为 confirm，由用户把关（与未知工具 fail-close 一致）
                    TOOL_PERMISSIONS[fname] = "confirm"
                    TOOL_DISPATCH[fname] = (
                        lambda args, _srv=srv_name, _tool=tname, _entry=entry:
                        _mcp_invoke(_entry, _tool, args)
                    )
                    logger.info("MCP 工具注册: %s (%s)", fname, entry)
    except Exception:
        logger.warning("MCP 扩展扫描失败", exc_info=True)

def ensure_mcp_loaded() -> None:
    """进程内一次性 MCP 注册（幂等）。api_routes 每个请求入口调用。"""
    global _MCP_LOADED
    if _MCP_LOADED:
        return
    _MCP_LOADED = True
    try:
        import asyncio as _asyncio
        try:
            _asyncio.get_running_loop()
        except RuntimeError:
            _asyncio.run(_load_mcp_tools())
        else:
            # 已在事件循环内（理论上 api 层 async 调用前不至此）
            _asyncio.create_task(_load_mcp_tools())
    except Exception:
        logger.warning("MCP 工具注册失败", exc_info=True)

async def _mcp_invoke(entry: str, tool: str, args: dict) -> str:
    try:
        from mcp_client import _MCP_CLIENTS
        client = _MCP_CLIENTS.get(entry)
        if client is None:
            return "⛔ MCP 连接已失效，请重启应用或重新安装扩展"
        return await client.call_tool(tool, args)
    except Exception as e:
        return f"⛔ MCP 工具调用失败: {e}"

# ── MCP 扩展：启用扩展声明 mcpServers → 动态注册远程工具 ──
# 工具命名 mcp_<server>_<tool>；dispatch 异步转发，连接失败返回错误文本（不崩整轮）。
def _mcp_tool_name(server: str, tool: str) -> str:
    from mcp_client import sanitize_tool_name
    return sanitize_tool_name(f"mcp_{server}_{tool}")
