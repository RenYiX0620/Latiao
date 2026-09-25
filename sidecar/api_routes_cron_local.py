"""Cron 定时任务 + 本地模型引擎路由。

从 api_routes.py 拆出（2026-09-24）。APIRouter 由 api_routes.include_router 挂载。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

import cron
import local_llm

logger = logging.getLogger("latiao-sidecar")

from http_json import _json_body  # noqa: E402 — 唯一定义

router = APIRouter()


# ── Cron API endpoints ──

@router.get("/v1/cron")
async def get_cron_jobs():
    """List all cron jobs (with running flag for the UI)."""
    with cron._cron_lock:
        jobs = [dict(j, running=j["id"] in cron._running_jobs) for j in cron._cron_jobs]
    return {"status": "ok", "jobs": jobs}


@router.post("/v1/cron")
async def create_cron_job(request: Request):
    """Create a new cron job."""
    body = await _json_body(request)
    invalid = cron._validate_schedule(body.get("schedule", ""))
    if invalid:
        return JSONResponse({"status": "error", "message": f"cron 表达式无效: {invalid}"}, status_code=400)
    job = {
        "id": str(uuid.uuid4()),
        "schedule": body.get("schedule", "0 9 * * *"),
        "task": body.get("task", "新建任务"),
        "action": body.get("action", "notify"),  # notify | execute
        "enabled": body.get("enabled", True),
        "created_at": datetime.now().isoformat(),
    }
    with cron._cron_lock:
        cron._cron_jobs.append(job)
        cron._save_cron(cron._cron_jobs)
    return {"status": "ok", "job": job}


@router.post("/v1/cron/{job_id}/run")
async def run_cron_job_now(job_id: str):
    """立即手动触发一次任务执行（不等待计划时间）。
    防重入：同任务已在执行中时拒绝——两个实例并发会互相覆盖结果/抢引擎
    （09-01 事故：手动触发 + 定时触发并发，后完成的用原始工具调用覆盖了
    先完成的总结）。"""
    with cron._cron_lock:
        job = next((j for j in cron._cron_jobs if j["id"] == job_id), None)
        if job and job["id"] in cron._running_jobs:
            return {"status": "error", "message": "该任务正在执行中，请等待完成后再触发"}
    if not job:
        return {"status": "error", "message": "任务不存在"}
    from agent_loop import _spawn
    _spawn(cron._run_cron_job_guarded(job))
    return {"status": "ok", "message": "已触发执行"}


@router.put("/v1/cron/{job_id}")
async def update_cron_job(job_id: str, request: Request):
    """Update a cron job."""
    body = await _json_body(request)
    with cron._cron_lock:
        for job in cron._cron_jobs:
            if job["id"] == job_id:
                if "schedule" in body:
                    invalid = cron._validate_schedule(body["schedule"])
                    if invalid:
                        return JSONResponse({"status": "error", "message": f"cron 表达式无效: {invalid}"}, status_code=400)
                    job["schedule"] = body["schedule"]
                if "task" in body:
                    job["task"] = body["task"]
                if "name" in body:
                    job["name"] = body["name"]
                if "enabled" in body:
                    job["enabled"] = body["enabled"]
                if "action" in body:
                    job["action"] = body["action"]
                cron._save_cron(cron._cron_jobs)
                return {"status": "ok", "job": job}
    return {"status": "error", "message": "Job not found"}


@router.delete("/v1/cron/{job_id}")
async def delete_cron_job(job_id: str):
    """Delete a cron job."""
    with cron._cron_lock:
        cron._cron_jobs = [j for j in cron._cron_jobs if j["id"] != job_id]
        cron._save_cron(cron._cron_jobs)
    return {"status": "ok"}


@router.post("/v1/cron/{job_id}/toggle")
async def toggle_cron_job(job_id: str):
    """Toggle a cron job enabled/disabled."""
    with cron._cron_lock:
        for job in cron._cron_jobs:
            if job["id"] == job_id:
                job["enabled"] = not job.get("enabled", True)
                cron._save_cron(cron._cron_jobs)
                return {"status": "ok", "job": job}
    return {"status": "error", "message": "Job not found"}


@router.get("/v1/cron/history")
async def api_cron_history(limit: int = Query(default=20, ge=1, le=200),
                           offset: int = Query(default=0, ge=0),
                           job: str = Query(default="")):
    """定时任务的历史产出（memory 表 type='cron_job'）。

    2026-09-23 补：这张表此前**只写不读**（155 行跑完即失，前端只有最近一次摘要）。
    """
    import cron as _cron
    return await run_in_threadpool(_cron.list_cron_history, limit, offset, job)


@router.get("/v1/cron/due")
async def get_due_jobs():
    """Check and return currently due cron jobs (纯查询，不标记执行状态)。"""
    due = cron._get_due_jobs(datetime.now())
    with cron._cron_lock:
        total = len(cron._cron_jobs)
    return {"status": "ok", "due": due, "total_jobs": total}


# ── Local LLM Engine endpoints ──

@router.get("/v1/local-llm/setup")
async def local_llm_setup():
    """Check system environment and report missing dependencies."""
    return local_llm.check_setup()


@router.get("/v1/local-llm/detect")
async def local_llm_detect():
    """Auto-detect system environment and recommend config."""
    return local_llm.detect_system()


@router.get("/v1/local-llm/search")
async def local_llm_search(q: str = Query(default=""), library: str = Query(default=""), limit: int = Query(default=20, le=30)):
    """Search HuggingFace for models. Empty q returns trending models."""
    from starlette.concurrency import run_in_threadpool
    def _search():
        return local_llm.search_huggingface(q, limit, library) if q else local_llm.search_huggingface("gguf", limit, library)
    results = await run_in_threadpool(_search)
    return {"status": "ok", "results": results, "query": q}


@router.post("/v1/local-llm/fix")
async def local_llm_fix(request: Request):
    """Execute a fix for an environment issue."""
    body = await _json_body(request)
    fix_type = body.get("fix_type", "")
    fix_pkg = body.get("fix_pkg", "")
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(local_llm.run_fix, fix_type, fix_pkg)


@router.post("/v1/local-llm/download")
async def local_llm_download(request: Request):
    """Download a model from HuggingFace."""
    body = await _json_body(request)
    model_id = body.get("model_id", "")
    if not model_id:
        return {"status": "error", "message": "model_id required"}
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(local_llm.download_model, model_id)


@router.get("/v1/local-llm/downloads")
async def local_llm_downloads():
    """Get all download states."""
    return local_llm.get_all_downloads()


@router.post("/v1/local-llm/download/pause")
async def local_llm_pause(request: Request):
    body = await _json_body(request)
    return local_llm.pause_download(body.get("model_id", ""))


@router.post("/v1/local-llm/download/resume")
async def local_llm_resume(request: Request):
    body = await _json_body(request)
    return local_llm.resume_download(body.get("model_id", ""))


@router.post("/v1/local-llm/download/cancel")
async def local_llm_cancel(request: Request):
    body = await _json_body(request)
    return local_llm.cancel_download(body.get("model_id", ""))


@router.post("/v1/local-llm/download/clear")
async def local_llm_clear(request: Request):
    body = await _json_body(request)
    return local_llm.clear_downloads(body.get("status", ""))


@router.post("/v1/local-llm/open-path")
async def local_llm_open_path(request: Request):
    """Open a path in Finder/Explorer."""
    body = await _json_body(request)
    path = body.get("path", "")
    if not path:
        # No path specified — open the Models directory so user can browse local files
        return local_llm.open_path(str(Path.home() / "Models"))
    return local_llm.open_path(path)


@router.get("/v1/local-llm/status")
async def local_llm_status():
    """Get local LLM engine status."""
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(local_llm.get_status)


@router.get("/v1/local-llm/models")
def local_llm_models():
    """List downloaded local models."""
    return {"status": "ok", "models": local_llm.list_local_models()}


@router.get("/v1/local-llm/model-detail")
async def local_llm_model_detail(model_id: str = Query(..., min_length=1)):
    """Fetch HuggingFace model detail: metadata, files, README."""
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(local_llm.get_model_detail, model_id)


@router.get("/v1/local-llm/recommended")
async def local_llm_recommended():
    """List recommended models with download status."""
    return {"status": "ok", "models": local_llm.get_recommended_models(), "backend": local_llm.get_backend()}


@router.get("/v1/local-llm/estimate-context")
async def local_llm_estimate_context(model_path: str = Query(default="")):
    """Estimate max context based on available memory and model size."""
    return local_llm.estimate_max_context(model_path)


@router.post("/v1/local-llm/context-limit")
async def local_llm_set_context(request: Request):
    """Set context limit (applies to next model start)."""
    body = await _json_body(request)
    limit = body.get("limit", 8192)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 8192  # 非法值回退默认，避免 500
    return local_llm.set_context_limit(limit)


@router.get("/v1/local-llm/context-limit")
async def local_llm_get_context():
    """Get current context limit."""
    return {"status": "ok", "context_limit": local_llm._engine.model_token_limit}


@router.post("/v1/local-llm/benchmark")
async def local_llm_benchmark(request: Request):
    """一键基准测试：生成速度/首字延迟/预填充/内存 + 建议（对当前已加载引擎实测）。"""
    body = await _json_body(request)
    large = bool(body.get("large", True))
    from agent_loop import _resolve_api_target
    protocol, api_url, headers, is_local = await _resolve_api_target(None)
    if not api_url:
        return {"status": "error", "message": "模型未加载或引擎不可用"}
    import local_llm
    ctx = int(getattr(local_llm._engine, "model_token_limit", 0) or 0)
    model = getattr(local_llm._engine, "current_model_id", "") or "local-model"
    port = getattr(local_llm._engine, "server_port", 1235)
    try:
        import bench_service
        res = await bench_service.run_benchmark(api_url, headers, model, ctx,
                                                port=port, large=large, lang=str(body.get("lang") or "zh"))
        return {"status": "ok", **res}
    except httpx.HTTPStatusError as e:
        return {"status": "error", "message": f"引擎返回 HTTP {e.response.status_code}（需先加载模型）"}
    except Exception as e:
        logger.warning("benchmark failed", exc_info=True)
        return {"status": "error", "message": f"基准测试失败：{type(e).__name__}: {e}"}


@router.get("/v1/local-llm/benchmarks")
def local_llm_benchmarks():
    """历史基准结果（新→旧）。"""
    import bench_service
    return {"status": "ok", "items": bench_service.history(20)}


def _busy_local_turns() -> int:
    """当前正在使用本地引擎的回合数（闸门在飞计数）。"""
    try:
        from agent.transport import stream_gate_snapshot
        return int(stream_gate_snapshot().get("in_flight") or 0)
    except Exception:
        return 0


@router.post("/v1/local-llm/start")
async def local_llm_start(request: Request):
    """Start a local model.

    async 解析 body，但引擎启动（Popen + 300s 轮询）放线程池执行，
    不冻结事件循环（否则加载期心跳/停止全部排队）。"""
    body = await _json_body(request)
    model_id = body.get("model_id", "")
    port = body.get("port", 1235)
    if not model_id:
        return {"status": "error", "message": "model_id required"}
    # 09-23 收口：加载/切换模型会杀掉当前引擎进程，正在跑的其他会话会当场断流。
    # 有其他回合在用本地引擎时拒绝（用户先停止那些回合，或等它们跑完）。
    _busy = _busy_local_turns()
    if _busy > 0 and not body.get("force"):
        return {"status": "error", "code": "engine_busy",
                "message": f"还有 {_busy} 个回合正在使用本地模型，切换/加载会让它们中断。"
                           "请先停止那些会话的回合，或等它们完成。",
                "busy": _busy}
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(local_llm.start_model, model_id, port)


@router.post("/v1/local-llm/stop")
async def local_llm_stop(request: Request):
    """Stop the running local model. stop_model 内含 lsof/kill 子进程调用，
    放线程池避免阻塞事件循环（停止必须即时响应）。

    09-23：有其他回合正在用本地模型时拒绝（除非 force）——停止引擎会让那些
    会话当场断流，而用户点的是"停止模型"，未必知道别的会话在跑。"""
    _busy = _busy_local_turns()
    _force = False
    try:
        _force = bool((await _json_body(request)).get("force"))
    except Exception:
        _force = False
    if _busy > 0 and not _force:
        return {"status": "error", "code": "engine_busy",
                "message": f"还有 {_busy} 个回合正在使用本地模型，停止引擎会让它们中断。"
                           "请先停止那些会话的回合。",
                "busy": _busy}
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(local_llm.stop_model)


@router.post("/v1/engine/detach")
def engine_detach():
    """sidecar 重启/部署前调用：放弃引擎子进程所有权，令其独立存活。

    模型加载耗时巨大（数十 GB 冷启动），sidecar 重启后 get_status 的
    reconnect 探测会接管幸存的引擎服务，避免"部署一次模型就没了"。"""
    local_llm.detach_engine()
    return {"status": "ok"}


@router.post("/v1/local-llm/delete-model")
async def local_llm_delete_model(request: Request):
    """Delete a local model file from ~/Models/ or download cache."""
    body = await _json_body(request)
    model_id = body.get("model_id", "")
    if not model_id:
        return {"status": "error", "message": "model_id required"}
    return local_llm.delete_model_file(model_id)


@router.post("/v1/identity/open/{agent_id}")
async def api_open_identity(agent_id: str, section: str = ""):
    """Open the agent identity file (or section file) with the system default editor."""
    agents_dir = Path(__file__).resolve().parent / "agents"
    if section:
        agent_file = (agents_dir / f"{agent_id}_{section}.txt").resolve()
    else:
        agent_file = (agents_dir / f"{agent_id}.txt").resolve()
    # Path traversal protection — 必须在任何写文件操作之前校验
    if not str(agent_file).startswith(str(agents_dir.resolve()) + "/"):
        return {"status": "error", "message": "Invalid agent_id"}
    if section and not agent_file.exists():
        agent_file.write_text(f"# {agent_id} - {section}\n\n（此部分内容待补充）\n")
    if not agent_file.exists():
        return {"status": "error", "message": f"Not found: {agent_id}" + (f"_{section}" if section else "")}
    try:
        import subprocess
        subprocess.Popen(["open", str(agent_file)])
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ═══════════════════════════════════════════════════════
