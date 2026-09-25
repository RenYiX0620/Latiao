"""扩展市场 / 会话存储 / 应用更新路由。

从 api_routes.py 拆出（2026-09-24）。APIRouter 由 api_routes.include_router 挂载。
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool


logger = logging.getLogger("latiao-sidecar")

from http_json import _json_body  # noqa: E402 — 唯一定义

router = APIRouter()


#  Extensions: Latiao 扩展市场体系（安装/卸载/启用/禁用）
# ═══════════════════════════════════════════════════════

@router.get("/v1/extensions")
async def api_extensions_list():
    """已安装扩展列表。"""
    try:
        from agent_loop import ensure_mcp_loaded
        ensure_mcp_loaded()
        from extension_manager import list_extensions
        return {"status": "ok", "extensions": list_extensions()}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ═══════════════════════════════════════════════════════
#  会话持久化（2026-09-23，审查⑩）：前端 localStorage → 后端权威存储
#  列表只回元数据+预览；消息按需取；写入是"整会话快照"（前端防抖后 PUT）。
# ═══════════════════════════════════════════════════════

@router.post("/v1/sessions/report")
async def api_sessions_report(request: Request):
    """前端会话同步的诊断上报（2026-09-23）。

    为什么需要：⑩ 上线时前端把"后端不可用"降级成 console.warn —— 日志里什么都
    看不到，迁移静默不发生，只能靠读 webview 的 localStorage 才查出根因。会话
    持久化是用户数据，它出问题必须在服务端日志里留痕。
    """
    body = await _json_body(request)
    msg = str(body.get("message", ""))[:500]
    level = str(body.get("level", "info")).lower()
    if level in ("error", "warn", "warning"):
        logger.warning("会话同步(前端): %s", msg)
    else:
        logger.info("会话同步(前端): %s", msg)
    return {"status": "ok"}


@router.get("/v1/sessions")
async def api_sessions_list(limit: int = Query(default=500, ge=1, le=2000),
                            offset: int = Query(default=0, ge=0)):
    logger.info("会话列表请求")
    import sessions as _sessions
    return await run_in_threadpool(_sessions.list_sessions, limit, offset)


@router.get("/v1/sessions/{session_id}")
async def api_sessions_get(session_id: str):
    logger.info("会话内容请求: %s", session_id[:18])
    import sessions as _sessions
    return await run_in_threadpool(_sessions.get_session, session_id)


@router.post("/v1/sessions/{session_id}")
async def api_sessions_save(session_id: str, request: Request):
    """整会话快照 upsert：{name, selectedModel, lastActive, messages:[…]}。

    用 POST 而非 PUT：前端走 Rust IPC 代理（sidecar_proxy），它只转发
    GET/POST/DELETE；而 Tauri HTTP 插件那条路实测**挂住不返回**（⑩ 首版调试：
    请求既不到服务端、也不报错、还没有超时，前端静默停住）。
    """
    body = await _json_body(request)
    import sessions as _sessions
    return await run_in_threadpool(
        _sessions.save_session, session_id,
        str(body.get("name", "")), str(body.get("selectedModel", "")),
        int(body.get("lastActive", 0) or 0), body.get("messages") or [])


@router.delete("/v1/sessions/{session_id}")
async def api_sessions_delete(session_id: str):
    import sessions as _sessions
    return await run_in_threadpool(_sessions.delete_session, session_id)


@router.post("/v1/sessions/import")
async def api_sessions_import(request: Request):
    """一次性迁移：把前端 localStorage 的会话整体搬进后端（幂等，默认只补不覆盖）。"""
    body = await _json_body(request)
    import sessions as _sessions
    return await run_in_threadpool(_sessions.import_sessions,
                                   body.get("sessions") or [],
                                   bool(body.get("replace", False)))


@router.post("/v1/extensions/install")
async def api_extensions_install(request: Request):
    """⑦ 安装第一阶段：取回内容 + 校验 + 算摘要，返回**预览**（不写盘）。

    用户在前端看到预览（名称/版本/权限/文件数/sha256）并确认后，前端再调
    /v1/extensions/install/confirm（带 pending_id + sha256）才真正安装。
    """
    body = await _json_body(request)
    source = str(body.get("source", "")).strip()
    sha256 = str(body.get("sha256", "")).strip()
    label = str(body.get("label", "")).strip()
    if not source:
        return {"status": "error", "message": "source required"}
    from starlette.concurrency import run_in_threadpool
    from extension_manager import stage_install
    return await run_in_threadpool(stage_install, source, sha256, label)


@router.post("/v1/extensions/install/confirm")
async def api_extensions_install_confirm(request: Request):
    """⑦ 安装第二阶段：回传预览里的 sha256 → 校验一致后落盘（⑥ 摘要绑定）。"""
    body = await _json_body(request)
    pending_id = str(body.get("pending_id", "")).strip()
    digest = str(body.get("sha256", "")).strip()
    label = str(body.get("label", "")).strip()
    from starlette.concurrency import run_in_threadpool
    from extension_manager import confirm_install
    result = await run_in_threadpool(confirm_install, pending_id, digest, label)
    if isinstance(result, dict) and result.get("status") == "ok":
        # 热重载：插件/技能/MCP 立即可用，无需重启
        try:
            from main import _hot_reload_extensions
            result["reload"] = await _hot_reload_extensions()
        except Exception:
            logger.warning("扩展热重载失败", exc_info=True)
    return result


@router.post("/v1/extensions/source-policy")
async def api_extensions_source_policy(request: Request):
    """封锁/解封市场来源（最小治理：安装前拦截）。body: {url, blocked}"""
    body = await _json_body(request)
    from extension_manager import set_source_blocked
    return set_source_blocked(str(body.get("url", "")), bool(body.get("blocked", True)))


@router.get("/v1/extensions/blocked-sources")
def api_extensions_blocked():
    from extension_manager import blocked_sources
    return {"status": "ok", "blocked": blocked_sources()}


@router.post("/v1/extensions/uninstall")
async def api_extensions_uninstall(request: Request):
    body = await _json_body(request)
    name = str(body.get("name", "")).strip()
    if not name:
        return {"status": "error", "message": "name required"}
    from extension_manager import uninstall_extension
    result = uninstall_extension(name)
    if isinstance(result, dict) and result.get("status") == "ok":
        try:
            # 直接移除该扩展的能力行（工具+技能），热重载 prune 作为兜底
            import capability_registry
            capability_registry.remove_extension_caps(name)
        except Exception:
            logger.warning("扩展能力清理失败", exc_info=True)
        try:
            from main import _hot_reload_extensions
            result["reload"] = await _hot_reload_extensions()
        except Exception:
            logger.warning("扩展热重载失败", exc_info=True)
    return result


@router.post("/v1/extensions/set-enabled")
async def api_extensions_set_enabled(request: Request):
    body = await _json_body(request)
    name = str(body.get("name", "")).strip()
    enabled = bool(body.get("enabled", True))
    if not name:
        return {"status": "error", "message": "name required"}
    from extension_manager import set_extension_enabled
    result = set_extension_enabled(name, enabled)
    if isinstance(result, dict) and result.get("status") == "ok":
        # 启停同样热重载：启用后工具立即可用，禁用后立即移除
        try:
            from main import _hot_reload_extensions
            result["reload"] = await _hot_reload_extensions()
        except Exception:
            logger.warning("扩展热重载失败", exc_info=True)
    return result


@router.get("/v1/marketplace")
async def api_marketplace(url: str = Query(default="")):
    """读取市场清单（默认官方市场；可选 url 覆盖）。"""
    try:
        from starlette.concurrency import run_in_threadpool
        from extension_manager import get_marketplace_cached
        # 走预热缓存：命中即秒回；miss 才线程池拉取（不阻塞事件循环）
        return await run_in_threadpool(get_marketplace_cached, url)
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/v1/marketplace/sources")
async def api_market_sources():
    """市场源列表（官方内置 + 用户添加）。"""
    from extension_manager import list_market_sources
    return {"status": "ok", "sources": list_market_sources()}


@router.post("/v1/marketplace/sources")
async def api_market_source_add(request: Request):
    """添加市场源：marketplace.json URL 或 GitHub 仓库地址。"""
    body = await _json_body(request)
    url = str(body.get("url", "")).strip()
    name = str(body.get("name", "")).strip()
    kind = str(body.get("kind", "")).strip()
    from extension_manager import add_market_source
    return add_market_source(url, name, kind)


@router.delete("/v1/marketplace/sources")
async def api_market_source_remove(request: Request):
    """移除市场源（内置源不可删）。"""
    body = await _json_body(request)
    url = str(body.get("url", "")).strip()
    from extension_manager import remove_market_source
    return remove_market_source(url)


@router.get("/v1/marketplace/all")
async def api_market_all():
    """聚合所有源的条目：官方 marketplace + 生态源实时发现（线程池）。"""
    try:
        from starlette.concurrency import run_in_threadpool
        from extension_manager import fetch_all_markets
        return await run_in_threadpool(fetch_all_markets)
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/v1/marketplace/discover")
async def api_market_discover(url: str = Query(default="")):
    """发现任意 GitHub 仓库的可安装内容（格式自动识别）。"""
    from starlette.concurrency import run_in_threadpool
    from adapters import discover_auto
    if not url.strip():
        return {"status": "error", "message": "url 参数不能为空"}
    try:
        return await run_in_threadpool(discover_auto, url.strip())
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ── 应用自更新预下载代理（根治大文件断流：sidecar 续传 + updater 本地下载）──

@router.post("/v1/update/prepare")
async def api_update_prepare(request: Request):
    """启动预下载（后台线程，幂等）。body: {current_version: "x.y.z"}"""
    import update_service
    try:
        body = await _json_body(request)
    except Exception:
        body = {}
    current = str(body.get("current_version", "")).strip()
    # 同步调用全部放线程池：update_service 内部持锁做网络 IO，
    # 直接在事件循环里调会把它整体卡死（/health 都无响应）
    st = await asyncio.to_thread(update_service.start_prepare, current)
    return {"status": "ok", "progress": st}


@router.get("/v1/update/progress")
async def api_update_progress():
    """预下载进度（前端轮询显示百分比）。"""
    import update_service
    return {"status": "ok", "progress": await asyncio.to_thread(update_service.get_progress)}


@router.get("/v1/update-latest.json")
async def api_update_latest_json():
    """Tauri updater 的清单源（本地生成，url 指向本地文件流式端点）。
    免鉴权：updater 插件请求不带自定义 token（main._check_auth 豁免）。
    无更新时返回 204 No Content——updater 在版本比较之前就查找
    platforms 条目，返回空 platforms 的 JSON 会直接报错。"""
    import update_service
    from starlette.responses import Response
    manifest = await asyncio.to_thread(
        update_service.get_tauri_manifest, update_service.current_app_version())
    if manifest is None:
        return Response(status_code=204)
    return JSONResponse(manifest)


@router.get("/v1/update-file")
async def api_update_file():
    """流式返回本地安装包给 updater（本地回环，无断流可能）。免鉴权。"""
    import update_service
    from starlette.responses import FileResponse
    path = await asyncio.to_thread(update_service.get_update_file_path)
    if not path:
        raise HTTPException(status_code=404, detail="update package not ready")
    return FileResponse(str(path), filename=path.name, media_type="application/octet-stream")


@router.get("/v1/marketplace/discovered")
async def api_market_discovered():
    """GitHub 自动发现索引快照。"""
    from discovery import discover_status, get_discovered_entries
    st = discover_status()
    st["entries"] = get_discovered_entries()
    return st


@router.post("/v1/marketplace/discover-refresh")
async def api_market_discover_refresh():
    """触发一轮强制全量抓取（后台线程，立即返回进度）。"""
    import threading
    from discovery import run_discovery
    # 用后台线程跑（抓取 3-5 分钟），不阻塞请求；结果写索引，前端可轮询 discovered
    def _worker():
        try:
            run_discovery(force=True)
        except Exception:
            import logging
            logging.getLogger("latiao-sidecar").warning("手动刷新抓取失败", exc_info=True)
    threading.Thread(target=_worker, daemon=True).start()
    return {"status": "ok", "message": "GitHub 抓取已开始，稍后刷新市场可看到新条目"}


@router.get("/v1/marketplace/discover-status")
async def api_market_discover_status():
    from discovery import discover_status
    return discover_status()


@router.post("/v1/extensions/install-github")
async def api_extensions_install_github(request: Request):
    """⑦ 生态源条目第一阶段：下载+打包 → 返回预览（不写盘）。

    落盘走 /v1/extensions/install/confirm；原逻辑是"打包成本地临时文件直接安装"，
    那会绕过"网络来源必须 sha256"的闸门（⑥）。
    """
    body = await _json_body(request)
    repo = str(body.get("repo", "")).strip()
    skill_path = str(body.get("skill_path", "")).strip()
    kind = str(body.get("kind", "")).strip() or "openclaw-skill"
    expect_sha256 = str(body.get("sha256", "")).strip()
    if not repo:
        return {"status": "error", "message": "repo 不能为空"}
    from extension_manager import is_source_blocked
    if is_source_blocked(repo):
        return {"status": "error", "message": f"该来源已被封锁，拒绝安装：{repo}"}
    try:
        from starlette.concurrency import run_in_threadpool
        from extension_manager import install_github_item
        result = await run_in_threadpool(install_github_item, repo, skill_path, kind,
                                         expect_sha256)
        # 安装成功后热重载：技能/插件注册进能力表与工具表（否则要手动重启才生效）
        if isinstance(result, dict) and result.get("status") == "ok":
            try:
                from main import _hot_reload_extensions
                result["reload"] = await _hot_reload_extensions()
            except Exception:
                logger.warning("生态安装热重载失败", exc_info=True)
        return result
    except Exception as e:
        return {"status": "error", "message": str(e)}
