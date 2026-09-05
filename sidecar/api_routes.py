"""FastAPI routes — split from main.py (Section 10: FastAPI Routes).

Code is a verbatim move from main.py. Only import adjustments were made:
- `from main import app` + `import main` for app-owned state (_custom_permissions,
  _loaded_skills, _log_buffer) — rebindable state is accessed through the main
  module object so assignments stay visible to the owning module.
- cron state (_cron_jobs / _cron_lock) is accessed through the cron module
  object for the same reason.
"""
import asyncio
import base64
import io
import json
import logging
import os
import re
import subprocess
import uuid
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

import cron
import local_llm
import main
from agent_loop import (
    _NATIVE_TOOL_RE,
    AGENT_PROFILES,
    CONFIG_FILE,
    PROGRESS_FILE,
    TOOL_PERMISSIONS,
    TOOLS,
    _agent_loop_stream,
    _build_chat_messages,
    _build_local_tools_prompt,
    _cap_tools,
    _deduplicate_response,
    _detect_task_intent,
    _extract_last_user_text,
    _filter_tools,
    _get_agent_tools,
    _get_best_cloud_config,
    _has_cloud_models,
    _last_cloud_config,
    _load_custom_agents,
    _local_agent_loop_stream,
    _local_llm_serialized,
    _local_llm_stream,
    _parse_native_tool_calls,
    _parse_prompt_tool_calls,
    _pending_confirmations,
    _pending_lock,
    _record_tool_call_db,
    _clear_session_cancel,
    _request_session_cancel,
    _resolve_api_target,
    _resolve_max_tokens,
    _save_custom_agents,
    _session_states,
    _spawn,
    _strip_native_tool_calls,
    execute_tool,
    _THINK_FENCE_RE,
)
from config import PROGRESS_DIR
from db import MEMORY_DB, _db_write_lock, _get_db
from identity import IDENTITY_FILES
from main import (
    MAX_UPLOAD_SIZE,
    SUBAGENT_MODEL,
    _load_permissions,
    _log_buffer,
    _save_permissions,
    app,
)
from memory import (
    _extract_learnings_heuristic,
    _get_recent_learnings,
    _refine_learnings,
    _retrieve_preferences,
)
from tool_executor import _resolve_permission

logger = logging.getLogger("latiao-sidecar")


def _get_cloud_model_names() -> list[dict]:
    """读 config.json 里配置的云端模型条目（路由透明化日志用，读不到返回空）。"""
    try:
        if CONFIG_FILE.exists():
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return cfg.get("cloud_models", []) or []
    except Exception:
        logger.debug("Failed to read cloud model names", exc_info=True)
    return []


async def _json_body(request: Request) -> dict:
    """解析请求 JSON body；非法 JSON 或非对象时返回 400，而不是让端点抛 500。"""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected JSON object")
    return body


@app.post("/v1/chat/completions")
async def chat_completion(request: Request):
    """Main chat endpoint. Routes to agent loop (OpenAI-compatible) or simple streaming.
    Auto-routes to best model based on task type when no specific model is selected."""
    body = await _json_body(request)
    messages = body.get("messages", [])
    last_user_text = _extract_last_user_text(messages)

    # Assemble full message context (identity, env, skill catalog, agent, image)
    # 技能目录由 capability_registry 在 _build_chat_messages 内注入，模型按需调用 use_skill
    messages = _build_chat_messages(body, messages)
    # DeepSeek 推理模型(thinking mode)要求: content 为 null 的 assistant
    # 消息(工具调用轮)必须带 reasoning_content,否则下一轮请求 400。
    # 旧会话历史缺失该字段 → 补空值兜底(实测空字符串可接受)。
    for _m in messages:
        if (_m.get("role") == "assistant" and _m.get("tool_calls")
                and "reasoning_content" not in _m):
            _m["reasoning_content"] = ""
    model = body.get("model") or SUBAGENT_MODEL

    # ── Auto-route: if no explicit model selected, pick based on task intent ──
    cloud_config = body.get("cloud_config")
    user_selected_model = body.get("model")  # User explicitly chose a model?
    if not user_selected_model and not cloud_config and last_user_text:
        intent = _detect_task_intent(last_user_text)
        if intent == "code" and _has_cloud_models():
            logger.info("Auto-route: code task → using cloud model")
            # Try to use an available cloud model for code tasks
            cloud_config = _get_best_cloud_config()
            if cloud_config:
                model = cloud_config.get("model", model)
        elif intent == "chat":
            from starlette.concurrency import run_in_threadpool as _rtp
            if await _rtp(local_llm.get_api_url):
                logger.info("Auto-route: chat task → using local model (free)")
            # Keep local model for casual chat
            pass

    logger.info("Chat request: model=%s, msg_count=%d, stream=%s", model, len(messages), body.get("stream", False))
    # 路由透明化：请求声明了具体模型名但既没带 cloud_config、名字也不匹配任何
    # 已配置的云端模型时，实际会走本地引擎（如 gpt-4o-mini 落到本地 35B）。
    # 此前这一事实只藏在日志里--用户以为在用云端快模型，实际跑的是最慢的路径。
    if not cloud_config and user_selected_model:
        try:
            _names = {str(m.get("name", "")) for m in _get_cloud_model_names()}
            if user_selected_model not in _names:
                # 属正常路由信息（本地模型选择是常态），INFO 级即可——
                # 实况日志默认只看 WARNING+，不刷屏（0.3.14 起降级）
                logger.debug(
                    "模型 %r 不在云端配置中（已配置: %s），本请求将使用本地模型引擎",
                    user_selected_model, ", ".join(sorted(n for n in _names if n)) or "无")
        except Exception:
            pass

    skip_tools = body.get("skip_tools", False)
    agent_id = body.get("agent", "latiao")
    # 不要在这里重新读取 cloud_config：上面的自动路由可能已为代码任务
    # 选中了云端模型，重新从 body 取值会把路由结果覆盖回 None → 又落回本地模型。
    _last_cloud_config.set(cloud_config)
    # 兜底持久化：后台任务（cron/自动路由）读 config.json，看不到请求级配置。
    # 前端启动同步可能因 sidecar 未就绪而失败，这里在首个真实请求时补写。
    if cloud_config and cloud_config.get("endpoint"):
        try:
            _cfg = {}
            if CONFIG_FILE.exists():
                _cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            _entry = {
                "name": str(cloud_config.get("model", ""))[:100],
                "endpoint": str(cloud_config["endpoint"])[:500],
                "key": str(cloud_config.get("key", ""))[:500],
                "protocol": str(cloud_config.get("protocol", "openai"))[:30],
            }
            _models = [m for m in _cfg.get("cloud_models", [])
                       if not (m.get("name") == _entry["name"] and m.get("endpoint") == _entry["endpoint"])]
            _models.append(_entry)
            _cfg["cloud_models"] = _models[-10:]
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(json.dumps(_cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            logger.debug("Failed to persist cloud_config on request", exc_info=True)
    use_stream = body.get("stream", False)

    # Resolve API target
    protocol, api_url, headers, is_local = await _resolve_api_target(cloud_config)

    # Agent loop: LLM autonomously decides when to call tools
    if not skip_tools and protocol == "openai":
        session_id = body.get("session_id", str(uuid.uuid4()))
        if use_stream:
            async def agent_loop_wrapper():
                try:
                    _reflection_mode = body.get("reflection_mode", "off")
                    _access_mode = body.get("access_mode", "confirm")
                    _thinking_level = body.get("thinking_level", "high")
                    # 新请求清除上一次停止的取消标记（重发消息不受影响）
                    _clear_session_cancel(session_id)
                    # P0 路由透明化：把实际落地的引擎与模型名在流开头回传给前端，
                    # 消除"选了云端模型名却静默跑本地最慢路径"的欺骗（08-25 事故根因）。
                    # model 名若不在云端配置里，会落到本地引擎；这里如实上报，用户可见。
                    # declared_model 只上报用户【显式】选择的模型名（user_selected_model）。
                    # 此前用带兜底的 model（body.get("model") or SUBAGENT_MODEL），自动检测
                    #（未选模型）时会冒出假名 gpt-4o-mini → 前端误弹"未在云端配置"警告。
                    # 模型名真实发给引擎仍用 model（208/214）；仅 UI 展示层不再暴露默认兜底名。
                    yield f"data: {json.dumps({'event': 'engine_route', 'is_local': is_local, 'engine': 'local' if is_local else 'cloud', 'declared_model': user_selected_model or '', 'resolved_endpoint': api_url}, ensure_ascii=False)}\n\n"
                    if is_local:
                        # 本地模型：用 prompt-based tool calling（不依赖 OpenAI function calling API）
                        async for event in _local_agent_loop_stream(messages, model, api_url, headers, session_id, agent_id, _reflection_mode, _access_mode, _thinking_level):
                            yield f"data: {json.dumps(event)}\n\n"
                    else:
                        # 云端模型：原生 OpenAI function calling
                        # 此前漏传 thinking_level → 恒用默认 high，
                        # UI 的 off/max 档对云端完全无效（审计 B6）
                        async for event in _agent_loop_stream(messages, model, api_url, headers, session_id, agent_id, _reflection_mode, _access_mode, _thinking_level):
                            yield f"data: {json.dumps(event)}\n\n"
                    yield "data: [DONE]\n\n"
                except httpx.TransportError as e:
                    logger.error(f"Agent stream 连接错误: {type(e).__name__}: {e}", exc_info=True)
                    # 优先透传底层带指引的具体原因（手动停止/自动重载失败/外部引擎等），
                    # 兜底才是无上下文的通用提示
                    _msg = str(e).strip() or "无法连接模型服务。请检查后端是否已启动。"
                    yield f"data: {json.dumps({'error': _msg}, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"
                except httpx.HTTPStatusError as e:
                    # 记录 doubao/openai 等云端 API 返回的 HTTP 错误（429限流/401鉴权/500服务端）
                    # 含响应体片段，便于诊断"任务中断"的真实原因
                    resp_body = ""
                    try:
                        resp_body = e.response.text[:500] if e.response is not None else ""
                    except Exception:
                        pass
                    logger.error(
                        f"Agent stream HTTP {e.response.status_code} "
                        f"(url={e.request.url if e.request else '?'}): {resp_body}",
                        exc_info=True,
                    )
                    # 本地引擎（Latiao/LM Studio）404 = 模型未就绪（启动中/崩溃后未重载），
                    # 云端 404 = 模型名或路径不存在——分别给出可操作的提示
                    req_url = str(e.request.url) if e.request else "?"
                    # 注意：不能复用外层 is_local（内层赋值会把外层变量遮蔽为局部 → UnboundLocalError）
                    req_is_local = "127.0.0.1" in req_url or "localhost" in req_url
                    if e.response.status_code == 404 and req_is_local:
                        err_msg = "本地模型服务未就绪：模型可能正在加载或已卸载，请到模型页重新加载"
                    elif e.response.status_code == 404:
                        err_msg = "模型服务返回 404：模型名或接口路径不存在，请检查模型名称"
                    else:
                        err_msg = f"模型服务返回错误 HTTP {e.response.status_code}"
                    yield f"data: {json.dumps({'error': err_msg})}\n\n"
                    yield "data: [DONE]\n\n"
                except TimeoutError as e:
                    # 复读循环截断 / 总时长看门狗等主动中止——正常收尾而非报错
                    logger.warning(f"Agent stream 主动中止: {e}")
                    _note = f"\n\n⚠️ {e}"
                    yield f"data: {json.dumps({'content': _note})}\n\n"
                    yield "data: [DONE]\n\n"
                except httpx.TimeoutException as e:
                    logger.error(f"Agent stream 超时: {type(e).__name__}: {e}", exc_info=True)
                    yield f"data: {json.dumps({'error': '模型服务响应超时，请检查网络或模型是否过大。'})}\n\n"
                    yield "data: [DONE]\n\n"
                except Exception:
                    logger.error("Agent loop unexpected error", exc_info=True)
                    yield f"data: {json.dumps({'error': 'Agent 循环内部错误，请查看日志。'})}\n\n"
                    yield "data: [DONE]\n\n"
            return StreamingResponse(agent_loop_wrapper(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    # Non-streaming agent loop (for Tauri HTTP plugin compatibility)
    if not skip_tools and protocol == "openai":
        # Run agent loop synchronously — collect all content + tool results
        current_msgs = [dict(m) for m in messages]
        # Truncate to prevent context overflow (8K token limit)
        if len(current_msgs) > 30:
            # Keep system messages + last 20 exchanges
            system_msgs = [m for m in current_msgs if m.get("role") == "system"]
            other_msgs = [m for m in current_msgs if m.get("role") != "system"]
            current_msgs = system_msgs + other_msgs[-20:]
        agent_tools_ns = _get_agent_tools(agent_id, TOOLS)
        active_tools_ns = _filter_tools(last_user_text, agent_tools_ns) if last_user_text else agent_tools_ns
        use_prompt_tools = is_local  # Local models use prompt-based tool calling
        # Cap tools: 7 for native function calling, 8 for prompt-based (less overhead)
        tool_cap = 8 if use_prompt_tools else 5
        if len(active_tools_ns) > tool_cap:
            active_tools = _cap_tools(active_tools_ns, tool_cap)
        else:
            active_tools = active_tools_ns
        full_content = ""
        tool_count = 0
        local_tools_prompt = _build_local_tools_prompt(active_tools) if use_prompt_tools else ""

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120)) as client:
                for _ in range(30):  # max iterations, non-streaming
                    loop_msgs = list(current_msgs)
                    # Convert role:"tool" → role:"user" for Qwen chat format compatibility
                    loop_msgs = [
                        {"role": "user", "content": f"[工具结果] {m['content']}"}
                        if m.get("role") == "tool" else dict(m)
                        for m in loop_msgs
                    ]
                    if use_prompt_tools:
                        # Inject tool prompt into the LAST system message (append, don't create new)
                        # Creating a second system message triggers a llama-cpp bug → empty response
                        last_sys_idx = -1
                        for i, m in enumerate(loop_msgs):
                            if m.get("role") == "system":
                                last_sys_idx = i
                        if last_sys_idx >= 0:
                            loop_msgs[last_sys_idx]["content"] += "\n\n" + local_tools_prompt
                        else:
                            loop_msgs.insert(0, {"role": "system", "content": local_tools_prompt})

                    if use_prompt_tools:
                        async with _local_llm_serialized(api_url):
                            resp = await client.post(api_url, json={
                            "model": model, "messages": loop_msgs,
                            "max_tokens": _resolve_max_tokens(model), "stream": False,
                            "temperature": 0.5,
                            "frequency_penalty": 0.6,
                "stop": ["<|im_end|>", "<|endoftext|>", "<end_of_turn>", "<eos>"],
                        }, headers=headers)
                    else:
                        resp = await client.post(api_url, json={
                            "model": model, "messages": current_msgs,
                            "tools": active_tools, "tool_choice": "auto",
                            "max_tokens": 2048, "stream": False,
                            "temperature": 0.5,
                            "frequency_penalty": 0.6,
                "stop": ["<|im_end|>", "<|endoftext|>", "<end_of_turn>", "<eos>"],
                        }, headers=headers)
                    resp.raise_for_status()  # httpx 不自动抛 4xx/5xx，必须显式检查
                    resp_data = resp.json()
                    choices = resp_data.get("choices", [])
                    if not choices:
                        break
                    msg = choices[0].get("message", {})
                    content = msg.get("content", "") or ""
                    reasoning = msg.get("reasoning", "") or ""
                    tc_data = msg.get("tool_calls", [])

                    # Native tool call detection for Gemma
                    if not tc_data and content and _NATIVE_TOOL_RE.search(content):
                        native_tcs = _parse_native_tool_calls(content)
                        if native_tcs:
                            content = _strip_native_tool_calls(content)
                            tc_data = native_tcs

                    # Prompt-based tool call detection for local models
                    if not tc_data and content and use_prompt_tools:
                        clean_text, prompt_tcs = _parse_prompt_tool_calls(content)
                        if prompt_tcs:
                            content = clean_text
                            tc_data = prompt_tcs

                    if tc_data:
                        tool_count += 1
                        current_msgs.append({
                            "role": "assistant",
                            "content": content or None,
                            "tool_calls": tc_data,
                        })
                        for tc in tc_data:
                            call_id = tc.get("id", str(uuid.uuid4()))
                            tool_name = tc.get("function", {}).get("name", "")
                            tool_args_str = tc.get("function", {}).get("arguments", "{}")
                            try:
                                tool_args = json.loads(tool_args_str) if isinstance(tool_args_str, str) else tool_args_str
                            except json.JSONDecodeError:
                                tool_args = {}
                            # Respect permissions — non-streaming can't ask for user confirmation
                            perm = _resolve_permission(tool_name, tool_args)
                            if perm == "confirm":
                                result = f"⛔ 操作需要用户确认: {tool_name}。请在流式模式下重试。"
                            elif perm in ("danger", "deny", "blocked"):
                                result = f"⛔ 权限规则已阻止: {tool_name}（级别 {perm}）。"
                            else:
                                logger.info("Tool executing (non-streaming): %s %s", tool_name, str(tool_args)[:100])
                                result = await execute_tool(tool_name, tool_args)
                                # Self-evolution: record + background-refine learning
                                _record_tool_call_db(session_id, tool_name, tool_args, result)
                                _spawn(_refine_learnings(tool_name, tool_args, result, session_id))
                            if len(result) > 5000:
                                result = result[:5000] + "\n...(截断)"
                            current_msgs.append({
                                "role": "tool",
                                "tool_call_id": call_id,
                                "content": result,
                            })
                        continue  # Loop again with tool results

                    # Text response
                    if content:
                        full_content += content
                    elif reasoning:
                        full_content += reasoning
                    break  # Done
        except Exception as e:
            logger.error("Non-streaming agent loop error: %s", e)
            return JSONResponse({"error": f"Agent 循环错误: {e}"}, status_code=500)

        if not full_content:
            # Model may return empty when context is too long or only thinking tokens
            logger.warning("Non-streaming agent loop: model returned empty content, tool_count=%d", tool_count)
            full_content = "（模型未生成文本回复。可能是上下文过长。请开启新会话或缩短对话历史。）"
        return {
            "id": "chatcmpl-sidecar",
            "object": "chat.completion",
            "created": int(datetime.now().timestamp()),
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": full_content},
                         "finish_reason": "stop"}],
            "usage": {"total_tokens": 0},
        }

    # Simple streaming fallback (skip_tools=True or non-OpenAI protocol or sync mode)
    if use_stream:
        async def stream():
            # Truncate long history to prevent context overflow
            msgs_for_model = messages
            if len(msgs_for_model) > 30:
                system_msgs = [m for m in msgs_for_model if m.get("role") == "system"]
                other_msgs = [m for m in msgs_for_model if m.get("role") != "system"]
                msgs_for_model = system_msgs + other_msgs[-20:]
            lm_body = {"model": model, "messages": msgs_for_model, "stream": True, "max_tokens": 2048, "temperature": 0.5, "frequency_penalty": 0.6, "stop": ["<|im_end|>", "<|endoftext|>"]}
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(120)) as c:
                    # 复用 _local_llm_stream：内部含串行锁 + 引擎死亡时触发自动
                    # 重载并排队等待（此前单次连接失败即报错，重载窗口内必秒死）。
                    # 注意不能再包一层 _local_llm_serialized——锁不可重入会死锁。
                    async with _local_llm_stream(c, api_url, lm_body, headers) as r:
                        # raise_for_status 已由 _local_llm_stream 在 yield 前调用
                        async for line in r.aiter_lines():
                            if line and line.startswith("data: "):
                                data_str = line[6:]
                                if data_str == "[DONE]":
                                    yield "data: [DONE]\n\n"
                                    return
                                try:
                                    event = json.loads(data_str)
                                    delta = event.get("choices", [{}])[0].get("delta", {})
                                    text = delta.get("content", "")
                                    reasoning = delta.get("reasoning", "")
                                    if reasoning:
                                        yield f"data: {json.dumps({'content': _THINK_FENCE_RE.sub('', reasoning)})}\n\n"
                                    if text:
                                        yield f"data: {json.dumps({'content': _THINK_FENCE_RE.sub('', text)})}\n\n"
                                except (json.JSONDecodeError, KeyError, IndexError):
                                    pass  # Malformed SSE event — skip, try next
                                except Exception:
                                    logger.warning("Unexpected error in SSE stream fallback", exc_info=True)
                                    raise
            except httpx.TransportError as e:
                _msg = str(e).strip() or "无法连接模型服务。请检查 LM Studio 或本地 LLM 是否已启动。"
                yield f"data: {json.dumps({'error': _msg}, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
            except httpx.HTTPStatusError as e:
                logger.error(f"Stream fallback HTTP {e.response.status_code}", exc_info=True)
                yield f"data: {json.dumps({'error': f'模型服务返回错误 HTTP {e.response.status_code}'})}\n\n"
                yield "data: [DONE]\n\n"
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    # Sync fallback (rarely used)
    resp_data = {}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120)) as c:
            # 本地引擎并发请求会崩溃 → 与 agent loop 共用串行锁
            async with _local_llm_serialized(api_url):
                resp = await c.post(api_url, json={
                    "model": model, "messages": messages, "max_tokens": 1024,
                    "temperature": 0.5,
                    "frequency_penalty": 0.6,
                    "stop": ["<|im_end|>", "<|endoftext|>", "<end_of_turn>", "<eos>"],
                }, headers=headers)
                resp.raise_for_status()  # httpx 不自动抛 4xx/5xx，必须显式检查
                resp_data = resp.json()
    except (httpx.ConnectError, httpx.RemoteProtocolError):
        return JSONResponse(
            {"error": "无法连接模型服务。请检查 LM Studio 或本地 LLM 是否已启动。"},
            status_code=503,
        )
    except httpx.HTTPStatusError as e:
        logger.error("Sync chat fallback HTTP %s: %s", e.response.status_code, e.response.text[:300])
        return JSONResponse(
            {"error": f"模型服务返回错误 HTTP {e.response.status_code}"},
            status_code=502,
        )
    except Exception:
        logger.error("Sync chat fallback failed", exc_info=True)
        return JSONResponse(
            {"error": "模型请求失败，请查看日志。"},
            status_code=500,
        )

    # Handle malformed responses (model may return only reasoning, no choices)
    choices = resp_data.get("choices", [])
    if choices:
        ai_content = choices[0].get("message", {}).get("content", "") or ""
        ai_reasoning = choices[0].get("message", {}).get("reasoning_content", "") or ""
        # Also check top-level reasoning field (used by some MLX models)
        if not ai_reasoning:
            ai_reasoning = resp_data.get("reasoning", "") or ""
    else:
        # Model returned no choices — might be an error or all-reasoning response
        ai_content = ""
        ai_reasoning = resp_data.get("reasoning", "") or ""
        if not ai_content and not ai_reasoning:
            # Check for error field
            err = resp_data.get("error", "")
            if isinstance(err, str) and err:
                return JSONResponse({"error": f"模型返回错误: {err[:300]}"}, status_code=502)
            return JSONResponse({"error": "模型返回了空的响应。"}, status_code=502)

    if not ai_content and ai_reasoning:
        ai_content = "(思考过程太长，以下是部分推理内容)\n\n" + ai_reasoning[-500:]

    return {
        "id": "chatcmpl-sidecar",
        "object": "chat.completion",
        "created": int(datetime.now().timestamp()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": _deduplicate_response(ai_content) if ai_content else None, "reasoning": ai_reasoning},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


@app.post("/v1/upload_file")
async def upload_file(request: Request, file: UploadFile = File(...)):
    """文件上传：图片转 base64，PDF 提取文本，文本直接读取"""
    # 先检查 Content-Length，超限直接 413，避免把超大文件读进内存
    cl = request.headers.get("content-length", "")
    if cl.isdigit() and int(cl) > MAX_UPLOAD_SIZE:
        return JSONResponse(
            {"status": "error", "message": f"文件过大 (上限 {MAX_UPLOAD_SIZE / 1024 / 1024:.0f}MB)"},
            status_code=413,
        )
    try:
        # 限量读取：最多读 MAX_UPLOAD_SIZE+1 字节，超限即拒绝
        content = await file.read(MAX_UPLOAD_SIZE + 1)
        if len(content) > MAX_UPLOAD_SIZE:
            return JSONResponse(
                {"status": "error", "message": f"文件过大 (上限 {MAX_UPLOAD_SIZE / 1024 / 1024:.0f}MB)"},
                status_code=413,
            )
        file_type = file.content_type or ""
        is_image = file_type.startswith("image/")
        is_pdf = file_type == "application/pdf" or (file.filename or "").lower().endswith(".pdf")

        if is_image:
            base64_content = base64.b64encode(content).decode("utf-8")
            return {
                "status": "success",
                "content": f"图片已上传: {file.filename}",
                "filename": file.filename,
                "is_image": True,
                "base64_data": base64_content,
                "content_type": file_type,
                "size": len(content),
            }
        elif is_pdf:
            reader = None
            try:
                from PyPDF2 import PdfReader
                reader = PdfReader(io.BytesIO(content))
                pages = []
                for page in reader.pages:
                    text = page.extract_text()
                    if text:
                        pages.append(text)
                pdf_text = "\n\n".join(pages)
                if not pdf_text.strip():
                    pdf_text = "(PDF 中没有可提取的文字，可能是扫描件或图片型 PDF)"
            except Exception as e:
                pdf_text = f"(PDF 解析失败: {e})"

            return {
                "status": "success",
                "content": _translate_to_english(pdf_text),
                "filename": file.filename,
                "is_pdf": True,
                "page_count": len(reader.pages) if reader is not None else 0,
                "size": len(content),
            }
        else:
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                text = content.decode("latin-1", errors="replace")

            return {
                "status": "success",
                "content": _translate_to_english(text),
                "filename": file.filename,
                "is_image": False,
                "size": len(content),
            }
    except Exception as e:
        return {"status": "error", "message": str(e)}


def _translate_to_english(text: str) -> str:
    """用户偏好（config.upload_text_en=true）：上传的文字部分以英文上传。

    检测到中文时，用已配置的第一个云端模型翻译为英文保持原格式；
    未配置云端/翻译失败/无中文时保留原文（fail-open，不阻塞上传）。
    """
    if not text or not re.search(r"[\u4e00-\u9fff]", text):
        return text
    try:
        cfg = json.loads((Path.home() / ".local-ai-os" / "config.json").read_text(encoding="utf-8"))
        if not cfg.get("upload_text_en"):
            return text
        models = cfg.get("cloud_models", []) or []
        if not models:
            return text
        prompt = (
            "Translate the following text into English. Keep code blocks, tables and "
            "formatting unchanged. Output only the translation:\n\n" + text[:6000]
        )
        # 依次尝试所有云端模型（如 GLM 配额耗尽 429 时自动换 deepseek）
        for m in models:
            try:
                url = (m.get("endpoint") or "").rstrip("/") + "/chat/completions"
                resp = httpx.post(
                    url,
                    headers={"Authorization": "Bearer " + str(m.get("key") or "")},
                    json={"model": m.get("name"),
                          "messages": [{"role": "user", "content": prompt}],
                          "max_tokens": 4096},
                    timeout=120, follow_redirects=True,
                )
                if resp.status_code != 200:
                    logger.info("upload translate: %s -> HTTP %s, try next", m.get("name"), resp.status_code)
                    continue
                out = (resp.json().get("choices") or [{}])[0].get("message", {}).get("content", "")
                if out and out.strip():
                    return out.strip()
            except Exception as e:
                logger.warning("upload translate via %s failed: %s", m.get("name"), e)
                continue
        return text
    except Exception as e:
        logger.warning("upload translate failed, keep original: %s", e)
        return text


# ── Whisper model cache (lazy-load once, reuse across requests) ──
_whisper_model = None

_WHISPER_DIR = Path.home() / ".local-ai-os" / "whisper-tiny"


def _get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        # 优先本地目录加载: huggingface.co 在国内不可达(502),huggingface_hub
        # 的 httpx 也不吃 SSL_CERT_FILE 环境变量。已预下载模型文件到
        # ~/.local-ai-os/whisper-tiny/(镜像下载,见部署脚本),离线可用。
        local_model = _WHISPER_DIR / "model.bin"
        if local_model.exists():
            _whisper_model = WhisperModel(str(_WHISPER_DIR), device="cpu", compute_type="int8")
        else:
            # fallback: 尝试镜像在线下载
            os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
            _whisper_model = WhisperModel("tiny", device="cpu", compute_type="int8")
    return _whisper_model


@app.post("/v1/recognize_speech")
async def recognize_speech(request: Request):
    """语音识别：前端发 WAV base64 → faster-whisper 本地识别"""
    import tempfile

    try:
        body = await _json_body(request)
        audio_base64 = body.get("audio_base64", "")

        if not audio_base64:
            return {"status": "error", "message": "No audio data provided"}

        # 解码前先按 base64 长度做大小检查（约 25MB 原始音频上限）
        if len(audio_base64) > 25 * 1024 * 1024 * 4 // 3:
            return {"status": "error", "message": "音频过大（上限约 25MB）"}

        audio_bytes = base64.b64decode(audio_base64)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".webm") as tmp:
            tmp.write(audio_bytes)
            wav_path = tmp.name

        try:
            # 模型加载与推理都是 CPU 密集阻塞操作，放到线程执行避免卡住事件循环
            model = await asyncio.to_thread(_get_whisper_model)
            segments, info = await asyncio.to_thread(model.transcribe, wav_path, language="zh", beam_size=5)

            text = " ".join(s.text.strip() for s in segments)

            if text:
                return {"status": "success", "text": text}
            else:
                return {"status": "success", "text": "(未识别到语音内容)"}

        finally:
            if os.path.exists(wav_path):
                os.unlink(wav_path)
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/v1/test_connection")
async def test_connection(request: Request):
    """测试云端 API 连接"""
    body = await _json_body(request)
    key = body.get("key", "")
    endpoint = body.get("endpoint", "")
    protocol = body.get("protocol", "openai")
    model = body.get("model", "")

    if not key or not endpoint:
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


@app.get("/v1/identity")
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


# ── Agent management endpoints ──

@app.get("/v1/agents")
async def get_agents():
    """Return all agent profiles (built-in + custom)."""
    agents = []
    for key, cfg in AGENT_PROFILES.items():
        agents.append({
            "id": key,
            "name": cfg.get("name", key),
            "display": cfg.get("display", ""),
            "role": cfg.get("role", "specialist"),
            "tools": cfg.get("tools", "all") if isinstance(cfg.get("tools"), list) else "all",
            "custom": cfg.get("custom", False),
        })
    return {"status": "ok", "agents": agents}

@app.post("/v1/agents/save")
async def save_agent(request: Request):
    """Create or update a custom agent profile."""
    body = await _json_body(request)
    agent_id = body.get("id", "").strip().lower().replace(" ", "-")
    if not agent_id or agent_id in ("latiao",):  # protect built-in orchestrator
        return {"status": "error", "message": "Invalid or reserved agent id"}
    custom = _load_custom_agents()
    custom[agent_id] = {
        "name": body.get("name", agent_id),
        "display": body.get("display", body.get("name", agent_id)),
        "role": "specialist",
        "identity": body.get("identity", f"You are {body.get('name', agent_id)}."),
        "tools": body.get("tools", ["read_file", "list_dir", "search_files"]),
    }
    _save_custom_agents(custom)
    # Reload into AGENT_PROFILES
    AGENT_PROFILES[agent_id] = dict(custom[agent_id], custom=True)
    return {"status": "ok", "agent": AGENT_PROFILES[agent_id]}

@app.delete("/v1/agents/{agent_id}")
async def delete_agent(agent_id: str):
    """Delete a custom agent profile."""
    custom = _load_custom_agents()
    if agent_id not in custom:
        return {"status": "error", "message": "Agent not found or not custom"}
    del custom[agent_id]
    _save_custom_agents(custom)
    AGENT_PROFILES.pop(agent_id, None)
    return {"status": "ok"}


@app.get("/v1/tools")
async def get_tools():
    """工具列表（统一能力模型：kind=tool 读 capabilities 表，保留旧路径兼容）。"""
    import capability_registry
    caps = {c["name"]: c for c in capability_registry.list_capabilities("tool")}
    tools_info = []
    for tool in TOOLS:
        fn = tool.get("function", {})
        name = fn.get("name", "unknown")
        cap = caps.get(name, {})
        tools_info.append({
            "name": name,
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters", {}),
            "permission": cap.get("permission", TOOL_PERMISSIONS.get(name, "safe")),
            "usage_count": cap.get("usage_count", 0),
        })
    return {"status": "ok", "tools": tools_info}


@app.get("/v1/permissions")
async def get_permissions():
    """Return current custom permission rules (path 级规则，工具级权限已迁移至 capabilities 表)."""
    return {"status": "ok", "rules": main._custom_permissions}


@app.post("/v1/permissions")
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
    _save_permissions(rules)
    _load_permissions()
    return {"status": "ok", "rules": main._custom_permissions}


@app.get("/v1/progress")
async def get_progress():
    """Return PROGRESS.md content for cross-session continuity."""
    try:
        if PROGRESS_FILE.exists():
            content = PROGRESS_FILE.read_text(encoding="utf-8")
            return {"status": "ok", "content": content}
        return {"status": "ok", "content": ""}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/v1/memory/search")
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


@app.get("/v1/memory/recent")
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

@app.get("/v1/memory/learnings")
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


@app.post("/v1/feedback")
async def api_feedback(request: Request):
    """点赞/点踩反馈 → 存入 learnings 表（高置信度），经既有 learnings
    注入影响后续回复（审计 B12：此前反馈只写 localStorage，永不回流）。"""
    body = await _json_body(request)
    content = str(body.get("content", "")).strip()
    kind = str(body.get("kind", ""))
    if not content or kind not in ("up", "down"):
        return {"status": "error", "message": "content and kind(up/down) required"}
    try:
        from memory import _store_learning
        topic = "用户点赞的回答" if kind == "up" else "用户点踩的回答"
        _store_learning("feedback", topic, content[:500],
                        confidence=0.9, source_type="feedback")
    except Exception:
        logger.warning("feedback 存储失败", exc_info=True)
    return {"status": "ok"}


@app.post("/v1/memory/learn")
async def learn_from_conversation(request: Request):
    """Manually trigger knowledge extraction from a conversation."""
    body = await _json_body(request)
    user_text = body.get("text", "")
    session_id = body.get("session_id", str(uuid.uuid4()))
    if not user_text.strip():
        return {"status": "error", "message": "No text provided"}
    count = _extract_learnings_heuristic(user_text, session_id)
    return {"status": "ok", "extracted": count, "session_id": session_id}


@app.post("/v1/memory/forget")
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
            elif topic:
                conn.execute("DELETE FROM learnings WHERE topic = ?", (topic,))
            conn.commit()
        return {"status": "ok", "deleted": True}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/v1/memory/preferences")
async def get_preferences():
    """Get all learned user preferences."""
    try:
        prefs = _retrieve_preferences()
        return {"status": "ok", "preferences": prefs}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/v1/memory/reflections")
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

@app.get("/v1/capabilities")
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


@app.post("/v1/capabilities/{name}/toggle")
async def toggle_capability(name: str):
    """启用/禁用任意能力（工具或技能），状态存 capabilities 表。"""
    import capability_registry
    row = capability_registry.get_capability(name)
    if row is None:
        return {"status": "error", "message": "Capability not found"}
    updated = capability_registry.set_enabled(name, not row["enabled"])
    return {"status": "ok", "name": name, "enabled": updated["enabled"] if updated else not row["enabled"]}


@app.post("/v1/capabilities/{name}/permission")
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


@app.post("/v1/capabilities/skills")
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


@app.delete("/v1/capabilities/skills/{name}")
async def delete_capability_skill(name: str):
    """删除用户自建技能。内置/扩展技能不可删除。"""
    import capability_registry
    ok = capability_registry.delete_skill(name)
    if not ok:
        return {"status": "error", "message": "Skill not found or not user-created"}
    return {"status": "ok"}


# ── Cloud model config endpoints (cron/auto-route 可见的持久化配置) ──


@app.get("/v1/settings/cloud-models")
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
        })
    return {"status": "ok", "models": masked}


@app.post("/v1/settings/cloud-models")
async def set_cloud_models(request: Request):
    """Persist cloud models to config.json so background tasks (cron, auto-route)
    can use them. 前端每次保存模型时同步一份过来（与 OS keychain 双写，同 tavily key 模式）。"""
    body = await _json_body(request)
    models = body.get("models", [])
    if not isinstance(models, list):
        return {"status": "error", "message": "models must be a list"}
    clean = []
    for m in models:
        if not isinstance(m, dict) or not m.get("endpoint"):
            continue
        clean.append({
            "name": str(m.get("name", ""))[:100],
            "endpoint": str(m["endpoint"])[:500],
            "key": str(m.get("key", ""))[:500],
            "protocol": str(m.get("protocol", "openai"))[:30],
        })
    try:
        cfg = {}
        if CONFIG_FILE.exists():
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        cfg["cloud_models"] = clean
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        logger.warning("Failed to save cloud models config", exc_info=True)
        return {"status": "error", "message": "写入配置失败"}
    return {"status": "ok", "count": len(clean)}


# ── Tavily API Key management endpoints ──


@app.get("/v1/settings/tavily-key")
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


@app.post("/v1/settings/tavily-key")
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
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        # Then update macOS Keychain (best-effort)
        try:
            # 密钥经 stdin 传给 security（-w 不带值时从 stdin 读），
            # 避免 key 出现在 argv 里被 `ps` 读到；同时线程池化防冻结
            from starlette.concurrency import run_in_threadpool
            def _sec_write():
                return subprocess.run(
                    ["security", "add-generic-password", "-s", "com.latiao.desktop",
                     "-a", "tavily_api_key", "-w", "-U"],
                    input=key.encode(), capture_output=True, timeout=10,
                )
            await run_in_threadpool(_sec_write)
        except Exception:
            logger.debug("Failed to write Tavily key to keychain", exc_info=True)
        masked = key[:7] + "••••" + key[-4:] if len(key) > 11 else "••••"
        return {"status": "ok", "has_key": True, "masked": masked}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.delete("/v1/settings/tavily-key")
async def delete_tavily_key():
    """Remove Tavily API key from keychain and config."""
    from starlette.concurrency import run_in_threadpool

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
            CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        return {"status": "ok", "has_key": False}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/v1/memory/stats")
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


@app.get("/v1/memory/session-progress")
async def get_session_progress(session_id: str = Query(default="")):
    """Get agent state transition history for stagnation analysis."""
    if session_id and session_id in _session_states:
        s = _session_states[session_id]
        return {"status": "ok", "session_id": session_id, "phase": s["phase"],
                "round": s["round"], "stalled_rounds": s["stalled_rounds"],
                "last_action": s["last_action"], "history": s["history"]}
    sessions = {}
    for sid, s in list(_session_states.items())[-10:]:
        sessions[sid] = {"phase": s["phase"], "round": s["round"],
                         "stalled_rounds": s["stalled_rounds"], "last_action": s["last_action"]}
    return {"status": "ok", "sessions": sessions}


@app.get("/health")
async def health():
    return {"status": "ok", "mode": "stateless"}


@app.get("/v1/logs")
async def get_logs(limit: int = Query(default=100, ge=1, le=500)):
    """Return recent application log entries."""
    logs = list(_log_buffer)
    return {"status": "ok", "logs": logs[-limit:]}


@app.get("/v1/heartbeat")
async def heartbeat():
    """Unified polling endpoint: returns downloads, LLM status, and learnings in one call."""
    from starlette.concurrency import run_in_threadpool
    from tool_executor import _subtask_snapshot
    return {
        "status": "ok",
        "downloads": await run_in_threadpool(local_llm.get_all_downloads),
        # get_status 内含 TCP/HTTP 探测（引擎忙时可达 10s+），必须线程池化，
        # 否则心跳冻结整个事件循环——所有聊天/确认全部卡死
        "local_llm": await run_in_threadpool(local_llm.get_status),
        "learnings": _get_recent_learnings(10),
        "cron_events": cron.get_recent_cron_events(10),
        "subagents": _subtask_snapshot(),
    }


@app.get("/v1/subagents")
def list_subagents():
    """列出后台子智能体任务（含状态与结果摘要）。"""
    from tool_executor import _subtask_snapshot
    return {"status": "ok", "subagents": _subtask_snapshot()}


@app.get("/v1/subagents/{task_id}")
def get_subagent(task_id: str):
    """查询单个后台子任务详情（含完整结果）。"""
    from tool_executor import _SUBTASKS
    s = _SUBTASKS.get(task_id)
    if not s:
        return {"status": "error", "message": "task not found"}
    return {"status": "ok", "subagent": {**s, "id": task_id}}


@app.delete("/v1/subagents/{task_id}")
def delete_subagent(task_id: str):
    """手动清除一条子任务记录（仅限已结束的条目；正在执行的不允许删）。"""
    from tool_executor import _SUBTASKS
    s = _SUBTASKS.get(task_id)
    if not s:
        return {"status": "error", "message": "task not found"}
    if s.get("status") == "running":
        return {"status": "error", "message": "任务正在执行中，无法清除"}
    _SUBTASKS.pop(task_id, None)
    return {"status": "ok", "message": "已清除"}


@app.post("/v1/confirm_tool")
async def confirm_tool(request: Request):
    """Frontend sends tool confirmation decision."""
    body = await _json_body(request)
    call_id = body.get("call_id", "")
    approved = body.get("approved", False)

    async with _pending_lock:
        entry = _pending_confirmations.get(call_id)
        if entry:
            entry["approved"] = approved
            entry["event"].set()
            return {"status": "ok", "call_id": call_id, "approved": approved}
    return {"status": "not_found", "message": f"No pending confirmation for call_id: {call_id}"}


@app.post("/v1/chat/cancel")
async def cancel_chat(request: Request):
    """停止按钮：置位会话级取消标记。前端 abort 的同时调用本端点，
    agent 循环在每轮迭代与每次工具执行前检查并中止——此前停止按钮只断
    前端流，服务端循环继续烧 GPU/执行工具/扣云端费用（P0）。"""
    body = await _json_body(request)
    session_id = str(body.get("session_id") or "").strip()
    if not session_id:
        return {"status": "error", "message": "missing session_id"}
    _request_session_cancel(session_id)
    return {"status": "ok", "session_id": session_id}


# ── Cron API endpoints ──

@app.get("/v1/cron")
async def get_cron_jobs():
    """List all cron jobs (with running flag for the UI)."""
    with cron._cron_lock:
        jobs = [dict(j, running=j["id"] in cron._running_jobs) for j in cron._cron_jobs]
    return {"status": "ok", "jobs": jobs}


@app.post("/v1/cron")
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


@app.post("/v1/cron/{job_id}/run")
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


@app.put("/v1/cron/{job_id}")
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


@app.delete("/v1/cron/{job_id}")
async def delete_cron_job(job_id: str):
    """Delete a cron job."""
    with cron._cron_lock:
        cron._cron_jobs = [j for j in cron._cron_jobs if j["id"] != job_id]
        cron._save_cron(cron._cron_jobs)
    return {"status": "ok"}


@app.post("/v1/cron/{job_id}/toggle")
async def toggle_cron_job(job_id: str):
    """Toggle a cron job enabled/disabled."""
    with cron._cron_lock:
        for job in cron._cron_jobs:
            if job["id"] == job_id:
                job["enabled"] = not job.get("enabled", True)
                cron._save_cron(cron._cron_jobs)
                return {"status": "ok", "job": job}
    return {"status": "error", "message": "Job not found"}


@app.get("/v1/cron/due")
async def get_due_jobs():
    """Check and return currently due cron jobs (纯查询，不标记执行状态)。"""
    due = cron._get_due_jobs(datetime.now())
    with cron._cron_lock:
        total = len(cron._cron_jobs)
    return {"status": "ok", "due": due, "total_jobs": total}


# ── Local LLM Engine endpoints ──

@app.get("/v1/local-llm/setup")
async def local_llm_setup():
    """Check system environment and report missing dependencies."""
    return local_llm.check_setup()


@app.get("/v1/local-llm/detect")
async def local_llm_detect():
    """Auto-detect system environment and recommend config."""
    return local_llm.detect_system()


@app.get("/v1/local-llm/search")
async def local_llm_search(q: str = Query(default=""), library: str = Query(default=""), limit: int = Query(default=20, le=30)):
    """Search HuggingFace for models. Empty q returns trending models."""
    from starlette.concurrency import run_in_threadpool
    def _search():
        return local_llm.search_huggingface(q, limit, library) if q else local_llm.search_huggingface("gguf", limit, library)
    results = await run_in_threadpool(_search)
    return {"status": "ok", "results": results, "query": q}


@app.post("/v1/local-llm/fix")
async def local_llm_fix(request: Request):
    """Execute a fix for an environment issue."""
    body = await _json_body(request)
    fix_type = body.get("fix_type", "")
    fix_pkg = body.get("fix_pkg", "")
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(local_llm.run_fix, fix_type, fix_pkg)


@app.post("/v1/local-llm/download")
async def local_llm_download(request: Request):
    """Download a model from HuggingFace."""
    body = await _json_body(request)
    model_id = body.get("model_id", "")
    if not model_id:
        return {"status": "error", "message": "model_id required"}
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(local_llm.download_model, model_id)


@app.get("/v1/local-llm/downloads")
async def local_llm_downloads():
    """Get all download states."""
    return local_llm.get_all_downloads()


@app.post("/v1/local-llm/download/pause")
async def local_llm_pause(request: Request):
    body = await _json_body(request)
    return local_llm.pause_download(body.get("model_id", ""))


@app.post("/v1/local-llm/download/resume")
async def local_llm_resume(request: Request):
    body = await _json_body(request)
    return local_llm.resume_download(body.get("model_id", ""))


@app.post("/v1/local-llm/download/cancel")
async def local_llm_cancel(request: Request):
    body = await _json_body(request)
    return local_llm.cancel_download(body.get("model_id", ""))


@app.post("/v1/local-llm/download/clear")
async def local_llm_clear(request: Request):
    body = await _json_body(request)
    return local_llm.clear_downloads(body.get("status", ""))


@app.post("/v1/local-llm/open-path")
async def local_llm_open_path(request: Request):
    """Open a path in Finder/Explorer."""
    body = await _json_body(request)
    path = body.get("path", "")
    if not path:
        # No path specified — open the Models directory so user can browse local files
        return local_llm.open_path(str(Path.home() / "Models"))
    return local_llm.open_path(path)


@app.get("/v1/local-llm/status")
async def local_llm_status():
    """Get local LLM engine status."""
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(local_llm.get_status)


@app.get("/v1/local-llm/models")
def local_llm_models():
    """List downloaded local models."""
    return {"status": "ok", "models": local_llm.list_local_models()}


@app.get("/v1/local-llm/model-detail")
async def local_llm_model_detail(model_id: str = Query(..., min_length=1)):
    """Fetch HuggingFace model detail: metadata, files, README."""
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(local_llm.get_model_detail, model_id)


@app.get("/v1/local-llm/recommended")
async def local_llm_recommended():
    """List recommended models with download status."""
    return {"status": "ok", "models": local_llm.get_recommended_models(), "backend": local_llm.get_backend()}


@app.get("/v1/local-llm/estimate-context")
async def local_llm_estimate_context(model_path: str = Query(default="")):
    """Estimate max context based on available memory and model size."""
    return local_llm.estimate_max_context(model_path)


@app.post("/v1/local-llm/context-limit")
async def local_llm_set_context(request: Request):
    """Set context limit (applies to next model start)."""
    body = await _json_body(request)
    limit = body.get("limit", 8192)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 8192  # 非法值回退默认，避免 500
    return local_llm.set_context_limit(limit)


@app.get("/v1/local-llm/context-limit")
async def local_llm_get_context():
    """Get current context limit."""
    return {"status": "ok", "context_limit": local_llm._engine.model_token_limit}


@app.post("/v1/local-llm/start")
async def local_llm_start(request: Request):
    """Start a local model.

    async 解析 body，但引擎启动（Popen + 300s 轮询）放线程池执行，
    不冻结事件循环（否则加载期心跳/停止全部排队）。"""
    body = await _json_body(request)
    model_id = body.get("model_id", "")
    port = body.get("port", 1235)
    if not model_id:
        return {"status": "error", "message": "model_id required"}
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(local_llm.start_model, model_id, port)


@app.post("/v1/local-llm/stop")
async def local_llm_stop():
    """Stop the running local model. stop_model 内含 lsof/kill 子进程调用，
    放线程池避免阻塞事件循环（停止必须即时响应）。"""
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(local_llm.stop_model)


@app.post("/v1/engine/detach")
def engine_detach():
    """sidecar 重启/部署前调用：放弃引擎子进程所有权，令其独立存活。

    模型加载耗时巨大（数十 GB 冷启动），sidecar 重启后 get_status 的
    reconnect 探测会接管幸存的引擎服务，避免"部署一次模型就没了"。"""
    local_llm.detach_engine()
    return {"status": "ok"}


@app.post("/v1/local-llm/delete-model")
async def local_llm_delete_model(request: Request):
    """Delete a local model file from ~/Models/ or download cache."""
    body = await _json_body(request)
    model_id = body.get("model_id", "")
    if not model_id:
        return {"status": "error", "message": "model_id required"}
    return local_llm.delete_model_file(model_id)


@app.post("/v1/identity/open/{agent_id}")
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
#  Extensions: Latiao 扩展市场体系（安装/卸载/启用/禁用）
# ═══════════════════════════════════════════════════════

@app.get("/v1/extensions")
async def api_extensions_list():
    """已安装扩展列表。"""
    try:
        from agent_loop import ensure_mcp_loaded
        ensure_mcp_loaded()
        from extension_manager import list_extensions
        return {"status": "ok", "extensions": list_extensions()}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/v1/extensions/install")
async def api_extensions_install(request: Request):
    """安装扩展：source=本地路径 | URL | GitHub repo，可选 sha256 校验。"""
    body = await _json_body(request)
    source = str(body.get("source", "")).strip()
    sha256 = str(body.get("sha256", "")).strip()
    label = str(body.get("label", "")).strip()
    if not source:
        return {"status": "error", "message": "source required"}
    from starlette.concurrency import run_in_threadpool
    from extension_manager import install_extension
    result = await run_in_threadpool(install_extension, source, sha256, label)
    if isinstance(result, dict) and result.get("status") == "ok":
        # 热重载：插件/技能/MCP 立即可用，无需重启
        try:
            from main import _hot_reload_extensions
            result["reload"] = await _hot_reload_extensions()
        except Exception:
            logger.warning("扩展热重载失败", exc_info=True)
    return result


@app.post("/v1/extensions/uninstall")
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


@app.post("/v1/extensions/set-enabled")
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


@app.get("/v1/marketplace")
async def api_marketplace(url: str = Query(default="")):
    """读取市场清单（默认官方市场；可选 url 覆盖）。"""
    try:
        from starlette.concurrency import run_in_threadpool
        from extension_manager import get_marketplace_cached
        # 走预热缓存：命中即秒回；miss 才线程池拉取（不阻塞事件循环）
        return await run_in_threadpool(get_marketplace_cached, url)
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/v1/marketplace/sources")
async def api_market_sources():
    """市场源列表（官方内置 + 用户添加）。"""
    from extension_manager import list_market_sources
    return {"status": "ok", "sources": list_market_sources()}


@app.post("/v1/marketplace/sources")
async def api_market_source_add(request: Request):
    """添加市场源：marketplace.json URL 或 GitHub 仓库地址。"""
    body = await _json_body(request)
    url = str(body.get("url", "")).strip()
    name = str(body.get("name", "")).strip()
    kind = str(body.get("kind", "")).strip()
    from extension_manager import add_market_source
    return add_market_source(url, name, kind)


@app.delete("/v1/marketplace/sources")
async def api_market_source_remove(request: Request):
    """移除市场源（内置源不可删）。"""
    body = await _json_body(request)
    url = str(body.get("url", "")).strip()
    from extension_manager import remove_market_source
    return remove_market_source(url)


@app.get("/v1/marketplace/all")
async def api_market_all():
    """聚合所有源的条目：官方 marketplace + 生态源实时发现（线程池）。"""
    try:
        from starlette.concurrency import run_in_threadpool
        from extension_manager import fetch_all_markets
        return await run_in_threadpool(fetch_all_markets)
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/v1/marketplace/discover")
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

@app.post("/v1/update/prepare")
async def api_update_prepare(request: Request):
    """启动预下载（后台线程，幂等）。body: {current_version: "x.y.z"}"""
    import asyncio
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


@app.get("/v1/update/progress")
async def api_update_progress():
    """预下载进度（前端轮询显示百分比）。"""
    import asyncio
    import update_service
    return {"status": "ok", "progress": await asyncio.to_thread(update_service.get_progress)}


@app.get("/v1/update-latest.json")
async def api_update_latest_json():
    """Tauri updater 的清单源（本地生成，url 指向本地文件流式端点）。
    免鉴权：updater 插件请求不带自定义 token（main._check_auth 豁免）。
    无更新时返回 204 No Content——updater 在版本比较之前就查找
    platforms 条目，返回空 platforms 的 JSON 会直接报错。"""
    import asyncio
    import update_service
    from starlette.responses import Response
    manifest = await asyncio.to_thread(
        update_service.get_tauri_manifest, update_service.current_app_version())
    if manifest is None:
        return Response(status_code=204)
    return JSONResponse(manifest)


@app.get("/v1/update-file")
async def api_update_file():
    """流式返回本地安装包给 updater（本地回环，无断流可能）。免鉴权。"""
    import asyncio
    import update_service
    from starlette.responses import FileResponse
    path = await asyncio.to_thread(update_service.get_update_file_path)
    if not path:
        raise HTTPException(status_code=404, detail="update package not ready")
    return FileResponse(str(path), filename=path.name, media_type="application/octet-stream")


@app.get("/v1/marketplace/discovered")
async def api_market_discovered():
    """GitHub 自动发现索引快照。"""
    from discovery import discover_status, get_discovered_entries
    st = discover_status()
    st["entries"] = get_discovered_entries()
    return st


@app.post("/v1/marketplace/discover-refresh")
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


@app.get("/v1/marketplace/discover-status")
async def api_market_discover_status():
    from discovery import discover_status
    return discover_status()


@app.post("/v1/extensions/install-github")
async def api_extensions_install_github(request: Request):
    """安装生态源条目（github 仓库 + skill_path + kind）。"""
    body = await _json_body(request)
    repo = str(body.get("repo", "")).strip()
    skill_path = str(body.get("skill_path", "")).strip()
    kind = str(body.get("kind", "")).strip() or "openclaw-skill"
    if not repo:
        return {"status": "error", "message": "repo 不能为空"}
    try:
        from starlette.concurrency import run_in_threadpool
        from extension_manager import install_github_item
        result = await run_in_threadpool(install_github_item, repo, skill_path, kind)
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
