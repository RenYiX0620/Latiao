"""identity/引导/Agent/工具权限/记忆/能力表/云端设置。

从 api_routes.py 拆出（2026-09-24）。APIRouter 由 api_routes.include_router 挂载。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

import local_llm
from config import PROGRESS_DIR, save_config
from config import CONFIG_FILE  # 与 agent_loop.CONFIG_FILE 同路径；走 config 避免枢纽 import
from db import MEMORY_DB, _db_write_lock, _get_db
import subprocess
from identity import IDENTITY_FILES
from memory import _extract_learnings_heuristic, _retrieve_preferences
from onboarding import complete as _onboarding_complete
from onboarding import reset as _onboarding_reset
from onboarding import status as _onboarding_status

logger = logging.getLogger("latiao-sidecar")

from http_json import _json_body  # noqa: E402 — 唯一定义


def _hub():
    """枢纽符号惰性取（agent_loop / main）。模块级 import 会造回环。"""
    import agent_loop
    import main
    return agent_loop, main

router = APIRouter()


@router.post("/v1/test_connection")
async def test_connection(request: Request):
    """测试云端 API 连接。key 可省略（密钥代理化：UI 不持明文，从 config 回填）。"""
    body = await _json_body(request)
    key = str(body.get("key", "") or "").strip()
    endpoint = str(body.get("endpoint", "") or "").strip()
    protocol = body.get("protocol", "openai")
    model = body.get("model", "") or body.get("name", "")

    if not endpoint:
        return {"status": "error", "message": "Key and endpoint required"}
    if not key:
        from agent.routing import _lookup_cloud_key
        key = _lookup_cloud_key({"endpoint": endpoint, "name": model, "model": model})
    if not key:
        return {"status": "error", "message": "Key and endpoint required"}

    timeout = httpx.Timeout(10.0)

    try:
        if protocol == "anthropic":
            api_url = endpoint.rstrip("/") + "/messages"
            headers = {
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            }
            req_body = {"model": model, "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]}
        elif protocol == "gemini":
            api_url = f"{endpoint.rstrip('/')}/models/{model}:generateContent?key={key}"
            headers = {"Content-Type": "application/json"}
            req_body = {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}
        else:
            api_url = endpoint.rstrip("/") + "/chat/completions"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            }
            req_body = {"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5}

        async with httpx.AsyncClient(timeout=timeout) as c:
            resp = await c.post(api_url, json=req_body, headers=headers)

        if resp.status_code in (200, 201):
            return {"status": "ok", "message": f"Connected (HTTP {resp.status_code})"}
        elif resp.status_code in (401, 403):
            return {"status": "error", "message": "Invalid API key"}
        else:
            return {"status": "error", "message": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except httpx.TimeoutException:
        return {"status": "error", "message": "Connection timed out"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/v1/identity")
async def get_identity():
    """Return status and content of all identity files."""
    files = []
    for filename in IDENTITY_FILES:
        filepath = PROGRESS_DIR / filename
        try:
            if filepath.exists():
                content = filepath.read_text(encoding="utf-8")
                files.append({"name": filename, "exists": True, "content": content})
            else:
                files.append({"name": filename, "exists": False, "content": ""})
        except Exception:
            logger.debug(f"Failed to read identity file {filename}", exc_info=True)
            files.append({"name": filename, "exists": False, "content": ""})
    return {"status": "ok", "files": files}


# ── 上下文统计（聊天状态栏悬浮面板）──

@router.get("/v1/context/stats")
async def get_context_stats(session_id: str = ""):
    """当前会话"上一轮实际发出内容"的 token 用量分类与缓存命中率。

    数据由 agent 循环在组装请求时快照（context_stats），这里只做读取与容量补全。
    """
    import context_stats
    limit, limit_source = 0, "unknown"
    try:
        engine = getattr(local_llm, "_engine", None)
        if engine is not None and not getattr(engine, "_external_engine", ""):
            limit = int(getattr(engine, "model_token_limit", 0) or 0)
            limit_source = "local_engine" if limit else "unknown"
        # 非本地引擎/无引擎：limit 维持默认（0/unknown）
    except Exception:
        logger.debug("读取本地引擎上下文上限失败", exc_info=True)
    return context_stats.stats(session_id, limit=limit, limit_source=limit_source)


# ── 首启引导（新安装第一次对话时自我介绍并收集 称呼/名字/语气）──
@router.get("/v1/onboarding")
async def get_onboarding():
    """引导状态 + 当前生效的 用户称呼/我的名字/对话语气（设置页卡片）。"""
    return {"status": "ok", **_onboarding_status()}


@router.post("/v1/onboarding/reset")
async def reset_onboarding():
    """重新运行引导：重置进度，下一轮对话即开始提问（不改动现有身份文件）。"""
    return {"status": "ok", **_onboarding_reset()}


@router.post("/v1/onboarding/complete")
async def complete_onboarding():
    """标记引导完成（用户选择跳过）。"""
    return {"status": "ok", **_onboarding_complete()}


# ── Agent management endpoints ──

@router.get("/v1/agents")
async def get_agents():
    """Return all agent profiles (built-in + custom)."""
    agents = []
    hub, _m = _hub()
    for key, cfg in hub.AGENT_PROFILES.items():
        agents.append({
            "id": key,
            "name": cfg.get("name", key),
            "display": cfg.get("display", ""),
            "role": cfg.get("role", "specialist"),
            "tools": cfg.get("tools", "all") if isinstance(cfg.get("tools"), list) else "all",
            "custom": cfg.get("custom", False),
        })
    return {"status": "ok", "agents": agents}

@router.post("/v1/agents/save")
async def save_agent(request: Request):
    """Create or update a custom agent profile."""
    body = await _json_body(request)
    agent_id = body.get("id", "").strip().lower().replace(" ", "-")
    if not agent_id or agent_id in ("latiao",):  # protect built-in orchestrator
        return {"status": "error", "message": "Invalid or reserved agent id"}
    hub, _m = _hub()
    custom = hub._load_custom_agents()
    custom[agent_id] = {
        "name": body.get("name", agent_id),
        "display": body.get("display", body.get("name", agent_id)),
        "role": "specialist",
        "identity": body.get("identity", f"You are {body.get('name', agent_id)}."),
        "tools": body.get("tools", ["read_file", "list_dir", "search_files"]),
    }
    hub._save_custom_agents(custom)
    # Reload into AGENT_PROFILES
    hub.AGENT_PROFILES[agent_id] = dict(custom[agent_id], custom=True)
    return {"status": "ok", "agent": hub.AGENT_PROFILES[agent_id]}

@router.delete("/v1/agents/{agent_id}")
async def delete_agent(agent_id: str):
    """Delete a custom agent profile."""
    hub, _m = _hub()
    custom = hub._load_custom_agents()
    if agent_id not in custom:
        return {"status": "error", "message": "Agent not found or not custom"}
    del custom[agent_id]
    hub._save_custom_agents(custom)
    hub.AGENT_PROFILES.pop(agent_id, None)
    return {"status": "ok"}


@router.get("/v1/tools")
async def get_tools():
    """工具列表（统一能力模型：kind=tool 读 capabilities 表，保留旧路径兼容）。"""
    import capability_registry
    caps = {c["name"]: c for c in capability_registry.list_capabilities("tool")}
    tools_info = []
    hub, _m = _hub()
    for tool in hub.TOOLS:
        fn = tool.get("function", {})
        name = fn.get("name", "unknown")
        cap = caps.get(name, {})
        tools_info.append({
            "name": name,
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters", {}),
            "permission": cap.get("permission", hub.TOOL_PERMISSIONS.get(name, "safe")),
            "usage_count": cap.get("usage_count", 0),
        })
    return {"status": "ok", "tools": tools_info}


@router.get("/v1/permissions")
async def get_permissions():
    """Return current custom permission rules (path 级规则，工具级权限已迁移至 capabilities 表)."""
    _h, m = _hub()
    return {"status": "ok", "rules": m._custom_permissions}


@router.post("/v1/permissions")
async def set_permissions(request: Request):
    """Save custom permission rules. Accepts {rules: [...]}（含 path_pattern 的路径级规则）。"""
    body = await _json_body(request)
    rules = body.get("rules", [])
    if not isinstance(rules, list):
        raise HTTPException(status_code=400, detail="rules must be a list")
    # 每条规则必须含合法 tool 字段，permission 必须在白名单内
    for rule in rules:
        if not isinstance(rule, dict) or not isinstance(rule.get("tool"), str) or not rule.get("tool"):
            raise HTTPException(status_code=400, detail="each rule must have a valid 'tool' field")
        if "permission" in rule and rule["permission"] not in ("safe", "confirm", "danger"):
            raise HTTPException(status_code=400, detail=f"invalid permission: {rule['permission']}")
    hub, m = _hub()
    m._save_permissions(rules)
    m._load_permissions()
    return {"status": "ok", "rules": m._custom_permissions}


@router.get("/v1/progress")
async def get_progress(session_id: str = Query(default="", description="按会话读该会话的进度文件")):
    """Return progress content for continuity.

    带 session_id 时读**该会话**的文件（09-23 起进度按会话分文件）；不带时读共享
    PROGRESS.md（旧行为，cron/外部调用兼容）。
    """
    try:
        hub, _m = _hub()
        path = hub._progress_file(session_id) if session_id else hub.PROGRESS_FILE
        if path.exists():
            content = path.read_text(encoding="utf-8")
            return {"status": "ok", "content": content, "session_id": session_id or "",
                    "file": path.name}
        return {"status": "ok", "content": "", "session_id": session_id or "", "file": path.name}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/v1/memory/search")
async def search_memory(q: str = Query(..., min_length=1), limit: int = Query(default=20, ge=1, le=100)):
    """Full-text search over tool call history using FTS5."""
    try:
        if not MEMORY_DB.exists():
            return {"status": "ok", "results": [], "query": q}
        conn = _get_db()
        # Sanitize FTS5 query: escape special chars, collapse whitespace
        safe_q = re.sub(r'[^\w\s"*]', '', q).strip()
        if not safe_q:
            return {"status": "ok", "results": [], "query": q}
        rows = conn.execute(
            "SELECT t.id, t.session_id, t.tool_name, t.args, t.result, t.created_at "
            "FROM tool_calls_fts f JOIN tool_calls t ON f.rowid = t.rowid "
            "WHERE tool_calls_fts MATCH ? ORDER BY rank LIMIT ?",
            (safe_q, limit),
        ).fetchall()
        results = [
            {"id": r[0], "session_id": r[1], "tool_name": r[2], "args": r[3], "result": r[4], "created_at": r[5]}
            for r in rows
        ]
        # LIKE fallback for CJK text that FTS5 unicode61 tokenizer misses
        if not results:
            like_q = f"%{q.strip()}%"
            rows = conn.execute(
                "SELECT id, session_id, tool_name, args, result, created_at FROM tool_calls "
                "WHERE tool_name LIKE ? OR args LIKE ? OR result LIKE ? ORDER BY created_at DESC LIMIT ?",
                (like_q, like_q, like_q, limit),
            ).fetchall()
            results = [
                {"id": r[0], "session_id": r[1], "tool_name": r[2], "args": r[3], "result": r[4], "created_at": r[5]}
                for r in rows
            ]
        return {"status": "ok", "results": results, "query": q}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/v1/memory/recent")
async def recent_memory(limit: int = Query(default=50, ge=1, le=200)):
    """Return most recent tool call records."""
    try:
        if not MEMORY_DB.exists():
            return {"status": "ok", "records": []}
        conn = _get_db()
        rows = conn.execute(
            "SELECT id, session_id, tool_name, args, result, created_at "
            "FROM tool_calls ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        records = [
            {"id": r[0], "session_id": r[1], "tool_name": r[2], "args": r[3], "result": r[4], "created_at": r[5]}
            for r in rows
        ]
        return {"status": "ok", "records": records}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ── Self-Learning endpoints ──

@router.get("/v1/memory/learnings")
async def search_learnings(q: str = Query(default="", min_length=0), limit: int = Query(default=20, ge=1, le=100)):
    """Search learned knowledge. Empty query returns recent high-confidence learnings."""
    try:
        if not MEMORY_DB.exists():
            return {"status": "ok", "learnings": []}
        conn = _get_db()
        if q.strip():
            safe_q = re.sub(r'[^\w\s"*]', '', q).strip()
            rows = []
            if safe_q:
                rows = conn.execute(
                    """SELECT l.id, l.topic, l.content, l.confidence, l.hit_count, l.source_type, l.created_at
                       FROM learnings l JOIN learnings_fts f ON l.rowid = f.rowid
                       WHERE learnings_fts MATCH ? ORDER BY l.confidence DESC LIMIT ?""",
                    (safe_q, limit),
                ).fetchall()
            # LIKE fallback for CJK text that FTS5 unicode61 tokenizer misses
            if not rows and q.strip():
                like_q = f"%{q.strip()}%"
                rows = conn.execute(
                    """SELECT id, topic, content, confidence, hit_count, source_type, created_at
                       FROM learnings WHERE topic LIKE ? OR content LIKE ?
                       ORDER BY confidence DESC LIMIT ?""",
                    (like_q, like_q, limit),
                ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, topic, content, confidence, hit_count, source_type, created_at
                   FROM learnings ORDER BY confidence DESC, updated_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        results = [
            {"id": r[0], "topic": r[1], "content": r[2], "confidence": r[3],
             "hit_count": r[4], "source_type": r[5], "created_at": r[6]}
            for r in rows
        ]
        return {"status": "ok", "learnings": results, "query": q}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.post("/v1/feedback")
async def api_feedback(request: Request):
    """点赞/点踩反馈 → 存入 learnings 表（高置信度），经既有 learnings
    注入影响后续回复（审计 B12：此前反馈只写 localStorage，永不回流）。"""
    body = await _json_body(request)
    content = str(body.get("content", "")).strip()
    kind = str(body.get("kind", ""))
    if not content or kind not in ("up", "down"):
        return {"status": "error", "message": "content and kind(up/down) required"}
    session_id = str(body.get("session_id", "")).strip()
    try:
        from memory import _store_learning
        topic = "用户点赞的回答" if kind == "up" else "用户点踩的回答"
        _store_learning("feedback", topic, content[:500],
                        confidence=0.9, source_type="feedback")
    except Exception:
        logger.warning("feedback 存储失败", exc_info=True)
    # ④ 标签回流：点赞/点踩 → 该会话最近一条注入记录标成 used
    tagged = 0
    try:
        from memory import mark_injection_used
        tagged = mark_injection_used(session_id, kind == "up")
    except Exception:
        logger.debug("注入标签回流失败", exc_info=True)
    return {"status": "ok", "tagged": tagged}


@router.post("/v1/memory/learn")
async def learn_from_conversation(request: Request):
    """Manually trigger knowledge extraction from a conversation."""
    body = await _json_body(request)
    user_text = body.get("text", "")
    session_id = body.get("session_id", str(uuid.uuid4()))
    if not user_text.strip():
        return {"status": "error", "message": "No text provided"}
    count = _extract_learnings_heuristic(user_text, session_id)
    return {"status": "ok", "extracted": count, "session_id": session_id}


@router.post("/v1/memory/forget")
async def forget_learning(request: Request):
    """Delete a learning by id or topic. Also decrements confidence for corrections."""
    body = await _json_body(request)
    lid = body.get("id", "")
    topic = body.get("topic", "")
    try:
        conn = _get_db()
        with _db_write_lock:  # 快速 sqlite 操作，持锁时间短，用同步锁即可
            if lid:
                conn.execute("DELETE FROM learnings WHERE id = ?", (lid,))
                from memory import _mark_tfidf_dirty
                _mark_tfidf_dirty()   # 删行必须让检索索引失效（此前不置脏 → 删掉的仍在结果里）
            elif topic:
                conn.execute("DELETE FROM learnings WHERE topic = ?", (topic,))
                from memory import _mark_tfidf_dirty
                _mark_tfidf_dirty()
            conn.commit()
        return {"status": "ok", "deleted": True}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/v1/memory/preferences")
async def get_preferences():
    """Get all learned user preferences."""
    try:
        prefs = _retrieve_preferences()
        return {"status": "ok", "preferences": prefs}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/v1/memory/reflections")
async def get_reflections(limit: int = Query(default=20, ge=1, le=100)):
    """Get recent tool execution reflections."""
    try:
        if not MEMORY_DB.exists():
            return {"status": "ok", "reflections": []}
        conn = _get_db()
        rows = conn.execute(
            """SELECT id, session_id, tool_name, tool_args, tool_result_summary, reflection, was_useful, created_at
               FROM reflections ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        results = [
            {"id": r[0], "session_id": r[1], "tool_name": r[2], "tool_args": r[3],
             "tool_result_summary": r[4], "reflection": r[5], "was_useful": bool(r[6]), "created_at": r[7]}
            for r in rows
        ]
        return {"status": "ok", "reflections": results}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ── 统一能力模型（capability registry）：工具与技能一套 API ──

@router.get("/v1/capabilities")
async def get_capabilities(request: Request):
    """统一能力列表：kind=tool|skill 过滤（默认全部）。工具与技能合并管理。"""
    import capability_registry
    kind = request.query_params.get("kind")
    kind = kind if kind in ("tool", "skill") else None
    capabilities = capability_registry.list_capabilities(kind)
    # tavily 提示：前端在 capabilities 里看到 tavily_search 时展示 key 配置区
    for c in capabilities:
        c["has_api_key"] = None
    return {"status": "ok", "capabilities": capabilities}


@router.post("/v1/capabilities/{name}/toggle")
async def toggle_capability(name: str):
    """启用/禁用任意能力（工具或技能），状态存 capabilities 表。"""
    import capability_registry
    row = capability_registry.get_capability(name)
    if row is None:
        return {"status": "error", "message": "Capability not found"}
    updated = capability_registry.set_enabled(name, not row["enabled"])
    return {"status": "ok", "name": name, "enabled": updated["enabled"] if updated else not row["enabled"]}


@router.post("/v1/capabilities/{name}/permission")
async def set_capability_permission(name: str, request: Request):
    """设置能力的权限级别（safe/confirm/danger/deny），覆盖插件默认值。"""
    import capability_registry
    body = await _json_body(request)
    perm = body.get("permission")
    if perm not in ("safe", "confirm", "danger", "deny"):
        return {"status": "error", "message": "invalid permission"}
    updated = capability_registry.set_permission(name, perm)
    if updated is None:
        return {"status": "error", "message": "Capability not found"}
    return {"status": "ok", "name": name, "permission": updated["permission"]}


@router.post("/v1/capabilities/skills")
async def create_capability_skill(request: Request):
    """新建用户技能（写表 + ~/.local-ai-os/skills/<key>.md 双写）。"""
    import capability_registry
    body = await _json_body(request)
    name = body.get("name", "").strip()
    content = body.get("content", "").strip()
    if not name or not content:
        return {"status": "error", "message": "Name and content required"}
    skill = capability_registry.create_skill(name, content)
    if skill is None:
        return {"status": "error", "message": "Skill already exists or invalid name"}
    return {"status": "ok", "skill": skill}


@router.delete("/v1/capabilities/skills/{name}")
async def delete_capability_skill(name: str):
    """删除用户自建技能。内置/扩展技能不可删除。"""
    import capability_registry
    ok = capability_registry.delete_skill(name)
    if not ok:
        return {"status": "error", "message": "Skill not found or not user-created"}
    return {"status": "ok"}


# ── Cloud model config endpoints (cron/auto-route 可见的持久化配置) ──


@router.get("/v1/settings/cloud-models")
async def get_cloud_models():
    """List configured cloud models (keys masked)."""
    models: list = []
    try:
        if CONFIG_FILE.exists():
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            models = cfg.get("cloud_models", [])
    except Exception:
        logger.warning("Failed to read cloud models config", exc_info=True)
    masked = []
    for m in models:
        key = m.get("key", "")
        masked.append({
            "name": m.get("name", ""), "endpoint": m.get("endpoint", ""),
            "protocol": m.get("protocol", "openai"),
            "has_key": bool(key),
            "key_masked": (key[:6] + "••••" + key[-4:]) if len(key) > 10 else ("••••" if key else ""),
            "max_tokens": int(m.get("max_tokens") or 32768),
        })
    return {"status": "ok", "models": masked}


@router.post("/v1/settings/cloud-models")
async def set_cloud_models(request: Request):
    """Persist cloud models to config.json so background tasks (cron, auto-route)
    can use them. 前端每次保存模型时同步一份过来（与 OS keychain 双写，同 tavily key 模式）。

    密钥代理化后前端**不回传明文 key**（只带 has_key）。空 key 表示"沿用已存"，
    绝不能写成空串覆盖——否则一次 max_tokens 改动就会把全部 API key 抹掉。
    """
    body = await _json_body(request)
    models = body.get("models", [])
    if not isinstance(models, list):
        return {"status": "error", "message": "models must be a list"}
    # 防意外整表清空：空表必须显式 clear=true（冷启动 GET 失败后前端曾误回写空表）
    if not models and not body.get("clear"):
        return {"status": "error", "message": "拒绝清空云端模型列表：如需删除全部请传 clear=true"}
    cfg = {}
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("Failed to read existing cloud models config", exc_info=True)
    prev_by_name: dict = {}
    prev_by_ep: dict = {}
    for m in (cfg.get("cloud_models") or []):
        if not isinstance(m, dict):
            continue
        if m.get("name"):
            prev_by_name[str(m["name"])] = m
        ep = (m.get("endpoint") or "").rstrip("/")
        if ep:
            prev_by_ep[ep] = m

    clean = []
    for m in models:
        if not isinstance(m, dict) or not m.get("endpoint"):
            continue
        name = str(m.get("name", ""))[:100]
        endpoint = str(m["endpoint"])[:500]
        key = str(m.get("key", "") or "")[:500]
        prev = prev_by_name.get(name) or prev_by_ep.get(endpoint.rstrip("/"))
        if not key and prev:
            key = str(prev.get("key", "") or "")[:500]
        try:
            max_tokens = int(m.get("max_tokens") or 0)
        except (TypeError, ValueError):
            max_tokens = 0
        if max_tokens <= 0 and prev:
            try:
                max_tokens = int(prev.get("max_tokens") or 0)
            except (TypeError, ValueError):
                max_tokens = 0
        clean.append({
            "name": name,
            "endpoint": endpoint,
            "key": key,
            "protocol": str(m.get("protocol", "openai"))[:30],
            "max_tokens": max_tokens or 32768,
        })
    try:
        cfg["cloud_models"] = clean
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        save_config(cfg, path=CONFIG_FILE)   # ⑤ 0600 + 原子写；path 钉死本模块 CONFIG_FILE
    except Exception:
        logger.warning("Failed to save cloud models config", exc_info=True)
        return {"status": "error", "message": "写入配置失败"}
    return {"status": "ok", "count": len(clean)}


# ── Tavily API Key management endpoints ──


@router.get("/v1/debug/tasks")
async def debug_tasks():
    """诊断：转储当前所有 asyncio 任务的 await 栈。

    背景（09-06 16:48 事故）：agent 循环卡在某个永不再唤醒的 await 上时，
    日志零输出、faulthandler 只能看到线程级栈（asyncio 任务栈不可见），
    "为什么停了"无法回答。此端点直接转储任务级调用栈，一眼定位卡点。"""
    out = []
    for t in asyncio.all_tasks():
        try:
            if t.done():
                continue
            stack = t.get_stack(limit=None)
            frames = []
            for f in stack:
                if f.f_code.co_name == "run":
                    continue
                frames.append(f"{f.f_code.co_filename.split('/')[-1]}:{f.f_lineno} in {f.f_code.co_name}")
            out.append({"task": t.get_name(), "repr": repr(t)[:400], "frames": frames})
        except Exception as e:
            out.append({"task": t.get_name(), "error": str(e)})
    return {"tasks": out, "count": len(out)}


@router.get("/v1/settings/tavily-key")
async def get_tavily_key():
    """Get Tavily API key status (masked, never returns full key). Reads from keychain first, then config.json."""
    key = ""
    # Try macOS Keychain first
    try:
        from starlette.concurrency import run_in_threadpool
        def _sec_read():
            return subprocess.run(
                ["security", "find-generic-password", "-s", "com.latiao.desktop", "-a", "tavily_api_key", "-w"],
                capture_output=True, text=True, timeout=5,
            )
        result = await run_in_threadpool(_sec_read)
        if result.returncode == 0 and result.stdout.strip():
            key = result.stdout.strip()
    except Exception:
        logger.debug("Tavily keychain read failed", exc_info=True)
    # Fallback to config.json
    if not key:
        try:
            if CONFIG_FILE.exists():
                cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                key = cfg.get("tavily_api_key", "")
        except Exception:
            logger.warning("Failed to read Tavily key from config.json", exc_info=True)
    if key:
        masked = key[:7] + "••••" + key[-4:] if len(key) > 11 else "••••"
        return {"status": "ok", "has_key": True, "masked": masked}
    return {"status": "ok", "has_key": False, "masked": None}


@router.post("/v1/settings/tavily-key")
async def set_tavily_key(request: Request):
    """Save Tavily API key to macOS Keychain (primary) + config.json (fallback)."""
    body = await _json_body(request)
    key = body.get("key", "").strip()
    if not key:
        return {"status": "error", "message": "API key is required"}
    try:
        # First write config.json (cross-platform primary storage)
        cfg = {}
        if CONFIG_FILE.exists():
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        cfg["tavily_api_key"] = key
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        save_config(cfg)   # ⑤ 0600 + 原子写（唯一入口）
        # Then update macOS Keychain (best-effort)
        try:
            # 密钥经 stdin 传给 security（-w 不带值时从 stdin 读），
            # 避免 key 出现在 argv 里被 `ps` 读到；同时线程池化防冻结。
            # 2026-09-23 实测修正两处：① -U 必须排在 -w 之前（-w 会把紧跟的参数
            # 当成密码值 —— 旧写法把字面量 "-U" 存进了 keychain）；
            # ② security 会连问两遍（password + retype），stdin 必须送两行。
            from starlette.concurrency import run_in_threadpool
            def _sec_write():
                return subprocess.run(
                    ["security", "add-generic-password", "-s", "com.latiao.desktop",
                     "-a", "tavily_api_key", "-U", "-w"],
                    input=f"{key}\n{key}\n".encode(), capture_output=True, timeout=10,
                )
            await run_in_threadpool(_sec_write)
        except Exception:
            logger.debug("Failed to write Tavily key to keychain", exc_info=True)
        masked = key[:7] + "••••" + key[-4:] if len(key) > 11 else "••••"
        return {"status": "ok", "has_key": True, "masked": masked}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.delete("/v1/settings/tavily-key")
async def delete_tavily_key():
    """Remove Tavily API key from keychain and config."""

    def _sec_delete():
        subprocess.run(
            ["security", "delete-generic-password", "-s", "com.latiao.desktop", "-a", "tavily_api_key"],
            capture_output=True, timeout=5,
        )
    try:
        # security 子进程会阻塞事件循环（P2-13）→ 线程池
        await run_in_threadpool(_sec_delete)
    except Exception:
        logger.debug("Failed to delete Tavily key from keychain", exc_info=True)
    try:
        if CONFIG_FILE.exists():
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            cfg.pop("tavily_api_key", None)
            save_config(cfg)   # ⑤ 0600 + 原子写（唯一入口）
        return {"status": "ok", "has_key": False}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/v1/memory/stats")
async def memory_stats():
    """Get self-learning statistics."""
    try:
        if not MEMORY_DB.exists():
            return {"status": "ok", "stats": {}}
        conn = _get_db()
        learnings_count = conn.execute("SELECT COUNT(*) FROM learnings").fetchone()[0]
        prefs_count = conn.execute("SELECT COUNT(*) FROM preferences").fetchone()[0]
        reflections_count = conn.execute("SELECT COUNT(*) FROM reflections").fetchone()[0]
        tool_calls_count = conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
        avg_confidence = conn.execute("SELECT AVG(confidence) FROM learnings").fetchone()[0] or 0
        return {"status": "ok", "stats": {
            "learnings": learnings_count,
            "preferences": prefs_count,
            "reflections": reflections_count,
            "tool_calls": tool_calls_count,
            "avg_learning_confidence": round(avg_confidence, 3),
        }}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/v1/memory/session-progress")
async def get_session_progress(session_id: str = Query(default="")):
    """Get agent state transition history for stagnation analysis."""
    hub, _m = _hub()
    if session_id and session_id in hub._session_states:
        s = hub._session_states[session_id]
        return {"status": "ok", "session_id": session_id, "phase": s["phase"],
                "round": s["round"], "stalled_rounds": s["stalled_rounds"],
                "last_action": s["last_action"], "history": s["history"]}
    sessions = {}
    for sid, s in list(hub._session_states.items())[-10:]:
        sessions[sid] = {"phase": s["phase"], "round": s["round"],
                         "stalled_rounds": s["stalled_rounds"], "last_action": s["last_action"]}
    return {"status": "ok", "sessions": sessions}


