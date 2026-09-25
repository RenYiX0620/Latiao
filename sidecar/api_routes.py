"""FastAPI routes — split from main.py (Section 10: FastAPI Routes).

Code is a verbatim move from main.py. Only import adjustments were made:
- `from main import app` + `import main` for app-owned state (_custom_permissions,
  _loaded_skills, _log_buffer) — rebindable state is accessed through the main
  module object so assignments stay visible to the owning module.
- cron state (_cron_jobs / _cron_lock) is accessed through the cron module
  object for the same reason.
"""
import asyncio
import json
import logging
import uuid
from datetime import datetime
from cmd_safety import redact_secrets   # 工具日志脱敏（09-23）

import httpx
from fastapi import Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

import cron
import local_llm
from agent_loop import (
    _NATIVE_TOOL_RE,
    CONFIG_FILE,
    TOOLS,
    _build_chat_messages,
    _build_local_tools_prompt,
    _cap_tools,
    _deduplicate_response,
    _extract_last_user_text,
    _filter_tools,
    _get_agent_tools,
    _last_cloud_config,
    _local_llm_serialized,
    _local_llm_stream,
    _parse_native_tool_calls,
    _parse_prompt_tool_calls,
    _pending_confirmations,
    _pending_lock,
    _record_tool_call_db,
    _clear_session_cancel,
    _event_log_for,
    _request_session_cancel,
    _resolve_api_target,
    _resolve_max_tokens,
    _spawn,
    _strip_native_tool_calls,
    execute_tool,
    _THINK_FENCE_RE,
)
from config import save_config
from onboarding import pending_question_suffix as _onboarding_suffix
from main import (
    SUBAGENT_MODEL,
    _log_buffer,
    app,
)
from loop_state import turn_state_for
from memory import (
    get_recent_learnings_for_ui,
    _refine_learnings,
)
from tool_executor import _resolve_permission

logger = logging.getLogger("latiao-sidecar")

from http_json import _json_body  # noqa: E402 — 唯一定义

# 单飞守卫（双发防御）：同会话同时允许一个回合在跑。add/discard 原子，无需锁。
_running_turns: set[str] = set()

# 已处理确认的 LRU（双击去重：第二次点击不再误报"确认已过期"）
import collections as _collections
# 注解是字符串、运行时不会求值，但名字得能解析：这里导入的是 _collections，
# 写成 "collections.deque" 会让任何求值注解的工具（get_type_hints 等）炸掉。
_recently_confirmed: "_collections.deque[str]" = _collections.deque(maxlen=200)


async def _logged_agent_turn(session_id: str, messages: list, inner):
    """阶段 1/2a 接线：turn 边界事件 + 相位状态机（灰度，见 session_log.py）。

    事件记录放在 SSE 消费层（而非两个生成器内部），零侵入地拿到完整 turn
    生命周期：进场记 turn/start + user/message（并 begin_turn），finally
    记 turn/end（并 end_turn）。

    结束原因与相位令牌同源（token.stop_requested）——停止按钮是唯一的中断
    语义（0.3.14 审计 P0 修复后的语义），事件日志与运行时行为同源，回放/
    审计能还原"用户何时按过停止"。
    """
    state = turn_state_for(session_id) if session_id else None
    token = None
    if state is not None:
        try:
            token = state.begin_turn()
        except Exception:
            logger.warning("turn begin rejected for %s", session_id, exc_info=True)
            token = None
    log = _event_log_for(session_id) if session_id else None
    if log is not None:
        try:
            log.append("turn/start", {"session_id": session_id})
            last_user = _extract_last_user_text(messages) or ""
            if last_user:
                log.append("user/message", {"text": last_user[:4000]}, surface_op="append")
        except Exception:
            logger.warning("failed to log turn/start", exc_info=True)
    reason = "completed"
    try:
        async for event in inner:
            yield event
    except Exception:
        reason = "error"
        raise
    finally:
        if log is not None:
            if reason == "completed" and (token is not None and token.stop_requested):
                reason = "aborted"
            try:
                log.append("turn/end", {"reason": reason})
            except Exception:
                logger.warning("failed to log turn/end", exc_info=True)
        if state is not None:
            try:
                state.end_turn(reason)
            except Exception:
                logger.warning("failed to end turn state for %s", session_id, exc_info=True)


def _get_cloud_model_names() -> list[dict]:
    """读 config.json 里配置的云端模型条目（路由透明化日志用，读不到返回空）。"""
    try:
        if CONFIG_FILE.exists():
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return cfg.get("cloud_models", []) or []
    except Exception:
        logger.debug("Failed to read cloud model names", exc_info=True)
    return []


def _doc_budget_chars(is_local: bool) -> int:
    """内联文件正文预算：随引擎上下文伸缩，避免"装不下"与"prefill 过慢"两极。

    本地：min(上下文 token×1.2, 24000)（27B 实测 2.4 万字符 ≈ 8-10K token）；
    云端：60000（前沿模型上下文宽裕，但仍不宜整篇灌入）。
    """
    if not is_local:
        return 60000
    try:
        import local_llm
        tl = int(getattr(local_llm._engine, "model_token_limit", 0) or 0)
        if tl > 0:
            return max(8000, min(int(tl * 1.2), 24000))
    except Exception:
        pass
    return 24000


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

    # ── 路由策略（09-06 起简化，原自动路由机制已删除）──
    # 此前"代码任务自动路由到云端"：用户加载并依赖本地 27B，代码类问题被
    # 本机制静默劫持到云端 → GLM 429 → deepseek 降级 → 空工具名 3 连击中止
    # （zcode 两连问事故）。现在：未选模型一律本地引擎；云端只走用户显式
    # 选择；429 降级保留但不再静默（弹提示 + 任务头引擎徽标）。
    cloud_config = body.get("cloud_config")
    user_selected_model = body.get("model")

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
            save_config(_cfg)   # ⑤ 0600 + 原子写（唯一入口）
        except Exception:
            logger.debug("Failed to persist cloud_config on request", exc_info=True)
    use_stream = body.get("stream", False)

    # Resolve API target
    protocol, api_url, headers, is_local = await _resolve_api_target(cloud_config)

    # Agent loop: LLM autonomously decides when to call tools
    if not skip_tools and protocol == "openai":
        session_id = body.get("session_id", str(uuid.uuid4()))
        if use_stream:
            _reflection_mode = body.get("reflection_mode", "off")
            _access_mode = body.get("access_mode", "confirm")
            _thinking_level = body.get("thinking_level", "high")

            async def _run_agent(_protocol, _api_url, _headers, _is_local, _model):
                # 长文档压缩（RAG-lite）：超预算的内联文件正文按问题相关性筛选——
                # 128KB PDF=13.7 万字符整段内联会直接 400（09-12 事故）
                _msgs = messages
                try:
                    from doc_condense import condense_messages
                    _msgs, _st = condense_messages(
                        messages, budget_chars=_doc_budget_chars(_is_local))
                    for _x in _st:
                        if _x.get("condensed"):
                            logger.warning(
                                "长文档压缩: %s %d→%d 字符（保留 %d/%d 段）",
                                _x["name"], _x["original"], _x["kept"],
                                _x["chunks_kept"], _x["chunks_total"])
                except Exception:
                    logger.warning("长文档压缩失败（按原文继续）", exc_info=True)
                # 薄循环（唯一循环）：cloud/local 共用，模型驱动终止，机制皆插件
                from agent.loop import ThinAgentLoop
                async for event in ThinAgentLoop(
                    _msgs, _model, _api_url, _headers, session_id,
                    _access_mode, _thinking_level, is_local=_is_local,
                ).run():
                    yield event

            async def agent_loop_wrapper():
                _fb_used = False  # 429 降级只允许一次，防循环
                try:
                    # P0 路由透明化：把实际落地的引擎与模型名在流开头回传给前端，
                    # 消除"选了云端模型名却静默跑本地最慢路径"的欺骗（08-25 事故根因）。
                    # model 名若不在云端配置里，会落到本地引擎；这里如实上报，用户可见。
                    # declared_model 只上报用户【显式】选择的模型名（user_selected_model）。
                    # 此前用带兜底的 model（body.get("model") or SUBAGENT_MODEL），自动检测
                    #（未选模型）时会冒出假名 gpt-4o-mini → 前端误弹"未在云端配置"警告。
                    # 模型名真实发给引擎仍用 model（208/214）；仅 UI 展示层不再暴露默认兜底名。
                    yield f"data: {json.dumps({'event': 'engine_route', 'is_local': is_local, 'engine': 'local' if is_local else 'cloud', 'declared_model': user_selected_model or '', 'resolved_endpoint': api_url}, ensure_ascii=False)}\n\n"
                    # 首启引导兜底：本地小模型服从性不稳，实测会把该问的问题换成寒暄
                    # （22:40 9B 把"语气"那一问丢了）——模型没问出来就由后端在流末尾补上。
                    _turn_text: list[str] = []
                    async for event in _run_agent(protocol, api_url, headers, is_local, model):
                        if isinstance(event, dict) and event.get("content"):
                            _turn_text.append(str(event["content"]))
                        yield f"data: {json.dumps(event)}\n\n"
                    _onb_suffix = _onboarding_suffix("".join(_turn_text))
                    if _onb_suffix:
                        yield f"data: {json.dumps({'content': _onb_suffix}, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"
                except (GeneratorExit, asyncio.CancelledError):
                    # 客户端断流/取消：立刻置位会话取消，主循环与子代理在其下一步停
                    # （09-13 事故：断连后服务端仍跑满算力，子代理留下僵尸记录）
                    try:
                        from agent_loop import _request_session_cancel
                        _request_session_cancel(session_id)
                        logger.warning("客户端断流：已请求取消会话 %s", session_id)
                    except Exception:
                        logger.warning("断流取消置位失败", exc_info=True)
                    raise
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
                    # 用路由级权威标志（云端配置指向 localhost 代理时，URL
                    # 推断会把云端 404 错标成"本地未就绪"——09-21 E2E 发现）
                    req_is_local = is_local or "127.0.0.1" in req_url or "localhost" in req_url
                    # 云端 429（限流/配额耗尽）→ 自动降级：换下一个云端模型
                    # （GLM→deepseek），没有则回退本地引擎。只降级一次。
                    # 09-06：显式选择的云端模型也降级（此前仅自动路由降级——
                    # 自动路由已删除）；降级不再静默：前端弹提示 + 引擎徽标。
                    if (e.response.status_code == 429 and not req_is_local
                            and not _fb_used):
                        _fb_used = True
                        _next_cfg = None
                        try:
                            _cur_ep = (cloud_config or {}).get("endpoint", "")
                            _cfg = {}
                            if CONFIG_FILE.exists():
                                _cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                            for _m in _cfg.get("cloud_models", []) or []:
                                if _m.get("endpoint") and _m.get("endpoint") != _cur_ep:
                                    _next_cfg = {
                                        "endpoint": _m["endpoint"],
                                        "key": _m.get("key", ""),
                                        "model": _m.get("name", ""),
                                        "protocol": _m.get("protocol", "openai"),
                                    }
                                    break
                        except Exception:
                            _next_cfg = None
                        _next_name = (_next_cfg or {}).get("model") or "本地引擎"
                        logger.warning(
                            "云端 429 降级: %s → %s",
                            (cloud_config or {}).get("model") or req_url, _next_name)
                        try:
                            _p2, _u2, _h2, _l2 = await _resolve_api_target(_next_cfg)
                            _m2 = _next_cfg.get("model") if _next_cfg else model
                            yield f"data: {json.dumps({'event': 'route_fallback', 'message': f'云端模型限流(429)，已自动切换到 {_next_name}', 'is_local': _l2, 'declared_model': _m2}, ensure_ascii=False)}\n\n"
                            async for event in _run_agent(_p2, _u2, _h2, _l2, _m2):
                                yield f"data: {json.dumps(event)}\n\n"
                            yield "data: [DONE]\n\n"
                            return
                        except Exception as _e2:
                            logger.error("429 降级重试失败: %s", _e2, exc_info=True)
                            yield f"data: {json.dumps({'error': '云端模型限流(429)，且备用模型/本地引擎暂不可用，请稍后重试'}, ensure_ascii=False)}\n\n"
                            yield "data: [DONE]\n\n"
                            return
                    if e.response.status_code == 404 and req_is_local:
                        err_msg = "本地模型服务未就绪：模型可能正在加载或已卸载，请到模型页重新加载"
                    elif e.response.status_code == 404:
                        err_msg = "模型服务返回 404：模型名或接口路径不存在，请检查模型名称"
                    elif e.response.status_code == 400:
                        err_msg = ("模型服务返回 HTTP 400：请求体被拒。若发过图片，请确认"
                                   "模型已重新加载（自动挂 mmproj-*.gguf）且与 GGUF 配套")
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
                except Exception as e:
                    logger.error("Agent loop unexpected error", exc_info=True)
                    # 露出真实异常（截断）：此前只给通用文案，现场无法定位（09-24）
                    _msg = f"Agent 循环内部错误：{type(e).__name__}: {e}"[:300]
                    yield f"data: {json.dumps({'error': _msg})}\n\n"
                    yield "data: [DONE]\n\n"
                finally:
                    # 单飞守卫释放：无论正常/异常/停止路径，回合结束即放行下一回合
                    _running_turns.discard(session_id)
            # 单飞守卫（双发防御）：同会话已有回合在跑时拒绝新回合，防止
            # 双击发送/重复发包导致并行双答（17:30 观测：同窗口两条完整回复）。
            # 停止后的重发不受影响（停止走 turn/end，回合已释放）。
            if session_id in _running_turns:
                return StreamingResponse(
                    iter([
                        f"data: {json.dumps({'content': '⏳ 上一轮任务仍在运行中，请稍候或先停止。'}, ensure_ascii=False)}\n\n",
                        "data: [DONE]\n\n",
                    ]),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache"},
                )
            _running_turns.add(session_id)
            # 顺序契约（P1-1 修复）：clear 必须先于 _logged_agent_turn 的 begin_turn
            # ——否则新开幕令牌会被 abandon() 作废，停止语义丢失。
            # 新请求清除上一次停止的取消标记（重发消息不受影响）
            _clear_session_cancel(session_id)
            return StreamingResponse(
                _logged_agent_turn(session_id, messages, agent_loop_wrapper()),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache"},
            )

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
                                logger.info("Tool executing (non-streaming): %s %s", tool_name,
                            redact_secrets(str(tool_args))[:100])
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




@app.get("/health")
async def health():
    return {"status": "ok", "mode": "stateless"}


@app.get("/v1/logs")
async def get_logs(limit: int = Query(default=100, ge=1, le=500)):
    """Return recent application log entries."""
    logs = list(_log_buffer)
    return {"status": "ok", "logs": logs[-limit:]}


def _engine_gate_snapshot() -> dict:
    """引擎闸门状态（容量/在飞/排队）——多会话并行的诊断入口。"""
    try:
        from agent.transport import stream_gate_snapshot
        return stream_gate_snapshot()
    except Exception:
        return {}


@app.get("/v1/heartbeat")
async def heartbeat(session_id: str = Query(default="", description="只返回该会话的子任务")):
    """Unified polling endpoint: returns downloads, LLM status, and learnings in one call."""
    from tool_executor import _subtask_snapshot
    return {
        "status": "ok",
        "downloads": await run_in_threadpool(local_llm.get_all_downloads),
        # get_status 内含 TCP/HTTP 探测（引擎忙时可达 10s+），必须线程池化，
        # 否则心跳冻结整个事件循环——所有聊天/确认全部卡死
        "local_llm": await run_in_threadpool(local_llm.get_status),
        "learnings": get_recent_learnings_for_ui(8),  # 对象格式（UI 需要 topic/confidence）
        "cron_events": cron.get_recent_cron_events(10),
        "subagents": _subtask_snapshot(session_id),
        # 引擎并发槽位/排队（多会话并行诊断：容量=N 时第 N+1 个请求排队）
        "engine_gate": _engine_gate_snapshot(),
    }


@app.get("/v1/subagents")
def list_subagents(session_id: str = Query(default="", description="只列出该会话的子任务")):
    """列出后台子智能体任务（含状态与结果摘要）。带 session_id 时只列本会话的。"""
    from tool_executor import _subtask_snapshot
    return {"status": "ok", "subagents": _subtask_snapshot(session_id)}


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

    logger.info("confirm_tool request: call_id=%s approved=%s", call_id, approved)
    async with _pending_lock:
        entry = _pending_confirmations.get(call_id)
        if entry:
            entry["approved"] = approved
            entry["event"].set()
            _recently_confirmed.append(call_id)
            return {"status": "ok", "call_id": call_id, "approved": approved}
    # 双击去重（09-21 实测：第一次批准并移除注册，第二次点击触发误报）
    if call_id in _recently_confirmed:
        return {"status": "already", "call_id": call_id, "approved": approved}
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



# ── 路由已按域拆出 ──
from api_routes_media import (  # noqa: E402
    router as _media_router,
)
from api_routes_admin import router as _admin_router  # noqa: E402
from api_routes_cron_local import router as _cron_local_router  # noqa: E402
from api_routes_extensions import router as _extensions_router  # noqa: E402

app.include_router(_media_router)
app.include_router(_admin_router)
app.include_router(_cron_local_router)
app.include_router(_extensions_router)
