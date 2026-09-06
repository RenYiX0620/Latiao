"""薄循环（对标 Codex turn.rs / dsh ReactLoopAgent）——Scope 容器的第一个消费者。

架构原则：
- 模型驱动终止：step 循环"采样 → 工具执行 → 结果回填"，模型不再调用
  工具即结束。无 nudge 链、无停滞计数、无 200 字闸门。
- 错误即结果：工具失败的结构化错误文本回填上下文，由模型自纠。
- 一切机制皆插件：本循环只消费 scope 的 waterfall（pre_step/request/
  deliver）与工具目录；快车道/弱模型辅助/规划门/压缩全部是插件
  （agent/plugins/builtin.py）。
- steer：新消息入队，step 边界/交付后认领（不再取消）。
- 门控：LATIAO_AGENT_LOOP_V3=1 启用；默认仍走 v1（Stage 4 切换删除）。
"""
import asyncio
import json
import time
import uuid
import logging

import httpx

from agent.core import Scope
from agent.transport import (
    _is_local_llm_url,
    _local_llm_stream,
    mark_llm_suspect,
)
from agent.parsing import (
    _parse_delta_line,
    _parse_prompt_tool_calls,
)
from agent.text_quality import (
    _GenerationLoopError,
    _detect_text_loop,
    _strip_repeat_tail,
    _strip_think_fences,
)
from agent.context import (
    _detect_user_language,
    _ensure_market_tools,
    _extract_last_user_text,
    _filter_tools_by_access,
    _is_light_query,
    _local_native_tools_ok,
    _maybe_add_inline_file_note,
    _merge_system_messages,
    _normalize_access,
    _resolve_max_tokens,
    _sanitize_tool_messages,
    _slim_history_for_local,
    _strip_transient_reminders,
)
from agent.gates import _build_local_tools_prompt
from agent.context import _NATIVE_LEAN_PROMPT
from agent.plugins.builtin import setup_all

logger = logging.getLogger("latiao-sidecar")

MAX_STEPS = 40          # 安全网（compaction 插件落地后放宽）
STALL_FIRST = 90        # 首 token 前静默上限
STALL_AFTER = 180       # 有输出后静默上限
HEARTBEAT = 60          # 心跳节拍

# ── steer 收件箱（session_id -> 队列；step 边界认领）────────────────
# 同步操作：单事件循环内 append/清空天然原子，且避免 Lock 绑定事件循环
# 导致的跨测试/跨请求 RuntimeError。
_steer_inbox: dict[str, list[str]] = {}


def queue_steer(session_id: str, text: str) -> int:
    """新消息插入队（不打断进行中的轮），返回当前队列长度。"""
    _steer_inbox.setdefault(session_id, []).append(text)
    return len(_steer_inbox[session_id])


def _claim_steer(session_id: str) -> list[str]:
    msgs = _steer_inbox.get(session_id) or []
    _steer_inbox[session_id] = []
    return msgs


def _clear_steer(session_id: str) -> None:
    _steer_inbox.pop(session_id, None)


class ThinAgentLoop:
    """单一 agent 循环：cloud/local 共用，差异只体现在请求组装与辅助层开关。"""

    def __init__(self, messages: list, model: str, api_url: str, headers: dict,
                 session_id: str = "", access_mode: str = "confirm",
                 thinking_level: str = "high", is_local: bool | None = None,
                 tool_whitelist: set | None = None):
        self.session_id = session_id
        self.model = model
        self.api_url = api_url
        self.headers = headers
        self.access_mode = _normalize_access(access_mode)
        self.thinking_level = thinking_level
        # is_local 由路由层显式传入（v1 同口径）；未传时按 URL 推导兜底
        self.is_local = _is_local_llm_url(api_url) if is_local is None else bool(is_local)
        self.current_msgs: list = _strip_transient_reminders([dict(m) for m in messages])
        self.last_user_text = _extract_last_user_text(self.current_msgs)
        _maybe_add_inline_file_note(self.current_msgs, self.last_user_text)
        self.user_lang = _detect_user_language(self.last_user_text)
        self.steps = 0
        self.native_tools = False
        self.native_fallback_used = False
        self.parallel_disabled = False  # 空名后下一轮关并行（deepseek quirk 缓解）
        self.tool_whitelist = tool_whitelist  # 子代理隔离：目录收窄 + 执行拒绝
        self._plan_injected = False
        # Scope：工具目录 + waterfall 宿主；loop 自身作为服务供钩子读取
        self.scope = Scope(name=f"agent:{session_id or uuid.uuid4().hex[:8]}")
        self.scope.provide("loop", self)
        setup_all(self.scope)
        try:
            from memory import _extract_learnings_heuristic
            _extract_learnings_heuristic(self.last_user_text, self.session_id)
        except Exception:
            logger.debug("learnings heuristic skipped", exc_info=True)

    # ── 工具集 ────────────────────────────────────────────
    def _active_tools(self) -> list:
        from agent_loop import TOOLS, _cap_tools, _filter_tools
        tools = _filter_tools(self.last_user_text, TOOLS) if self.last_user_text else list(TOOLS)
        tools = _filter_tools_by_access(tools, self.access_mode)
        if len(tools) > 12:
            # 元工具（委派/技能/定时）cap 保底——委派被裁掉会让模型"看不到"
            # 子代理机制而自己硬扛（09-06 真机验收 C 场景发现）
            tools = _cap_tools(tools, 12, keep_first=("delegate_task", "use_skill", "create_cron"))
        tools = _ensure_market_tools(tools, self.last_user_text)
        if self.tool_whitelist is not None:
            tools = [t for t in tools if t.get("function", {}).get("name") in self.tool_whitelist]
        return tools

    def _engine_model(self) -> str:
        # 云端请求必须用所选云端模型名——本地引擎的模型 id 发给云端必然 400
        # （09-06 19:29 事故：本地 Qwen3.8 加载中，"测试"被路由到 deepseek，
        # body.model 却是 Qwen 本地路径 → 400 "Model Not Exist"）
        if not self.is_local:
            return self.model
        import local_llm
        return getattr(local_llm._engine, "current_model_id", "") or self.model

    def _step_log(self, phase: str, detail: str = "") -> None:
        """终端式每步日志：统一前缀，进 sidecar.log 与 app 实况日志。"""
        logger.info("[THIN][step %s] %s%s", self.steps, phase,
                    f" | {detail}" if detail else "")

    # ── 请求组装（request waterfall 之前的宿主编排）──────────
    def _build_request(self, engine_model: str) -> dict:
        light = self.is_local and _is_light_query(self.last_user_text, self.current_msgs)
        native_ok = self.is_local and _local_native_tools_ok() and not self.native_fallback_used
        tools = self._active_tools() if not light else []
        self.native_tools = bool(tools) and native_ok

        msgs = _merge_system_messages(_sanitize_tool_messages(list(self.current_msgs)))
        body = {
            "model": engine_model,
            "messages": msgs,
            "stream": True,
            "temperature": 0.0,
            "max_tokens": _resolve_max_tokens(self.model),
            "stop": ["<|im_end|>", "<|endoftext|>", "<end_of_turn>", "<eos>"],
        }
        if not self.is_local:
            body["tools"] = [dict(t) for t in tools]
            if self.parallel_disabled:
                body["parallel_tool_calls"] = False
            return body

        # 本地：原生 tools + 精简纪律；legacy 回退围栏提示词；闲聊不加提示
        if self.native_tools:
            body["tools"] = [dict(t) for t in tools]
            sys_prompt = _NATIVE_LEAN_PROMPT
        else:
            sys_prompt = _build_local_tools_prompt(tools)
        if light:
            sys_prompt = ""
        if sys_prompt:
            if msgs and msgs[0].get("role") == "system":
                msgs[0] = dict(msgs[0])
                msgs[0]["content"] = sys_prompt + "\n\n" + str(msgs[0].get("content", ""))
            else:
                msgs.insert(0, {"role": "system", "content": sys_prompt})
        if len(self.current_msgs[-1].get("content", "")) > 8000:
            msgs[0]["content"] += (
                "\n\n📏 思考预算（长输入）：请先简短思考（≤300 字），然后立刻在正文"
                "写出完整分析——关键数字和结论必须写进正文。")
        body["frequency_penalty"] = 0.6
        if self.steps > 1:
            # 工具后续轮关思考（09-06 13:23：工具后开思考 97s 零交付）
            body["chat_template_kwargs"] = {"enable_thinking": False}
        if self.parallel_disabled:
            body["parallel_tool_calls"] = False
        return body

    # ── 流读取（本地走传输封装，云端直连；停滞/心跳共用）──────
    async def _stream(self, client: httpx.AsyncClient, body: dict):
        if not self.is_local:
            async with client.stream("POST", self.api_url, json=body,
                                     headers=self.headers) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    yield line
            return
        async with _local_llm_stream(client, self.api_url, body, self.headers) as r:
            aiter = r.aiter_lines()
            silent = 0
            any_out = False
            while True:
                try:
                    line = await asyncio.wait_for(anext(aiter), timeout=HEARTBEAT)
                    silent = 0
                except asyncio.TimeoutError:
                    silent += HEARTBEAT
                    stall = STALL_FIRST if not any_out else STALL_AFTER
                    if silent < stall:
                        yield ": keepalive\n\n"
                        continue
                    raise TimeoutError(f"模型输出停滞超 {stall}s")
                except StopAsyncIteration:
                    return
                if line and not line.startswith(": ") and "keepalive" not in line[:30]:
                    any_out = any_out or '"delta"' in line or '"reasoning"' in line
                yield line

    # ── 单 step：流式采样（实时流出 reasoning/content），结果经
    # __result__ 终端事件回传（消费者吞掉，不下发前端）──
    async def _sample(self, client, body):
        streamed, body_text, reasoning = "", "", ""
        native: dict[int, dict] = {}
        raw = 0
        deadline = time.monotonic() + 900
        async for line in self._stream(client, body):
            if time.monotonic() > deadline:
                raise _GenerationLoopError("单步生成超时(900s)，已截断")
            if not line.startswith("data: "):
                continue
            try:
                done, delta = _parse_delta_line(line)
            except Exception:
                continue
            if done:
                break
            if not delta:
                continue
            raw += 1
            for tc in delta.get("tool_calls", []) or []:
                idx = tc.get("index", 0)
                buf = native.setdefault(idx, {"id": "", "type": "function",
                                              "function": {"name": "", "arguments": ""}})
                if tc.get("id"):
                    buf["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    buf["function"]["name"] += fn["name"]
                if fn.get("arguments"):
                    buf["function"]["arguments"] += fn["arguments"]
            content = delta.get("content", "") or ""
            think = delta.get("reasoning") or delta.get("reasoning_content") or ""
            if raw % 40 == 0 and _detect_text_loop(streamed + think):
                streamed = _strip_repeat_tail(streamed)
                raise _GenerationLoopError("输出复读循环，已截断")
            if think:
                reasoning += think
                streamed += think
                yield {"reasoning": think, "ts": int(time.time() * 1000)}
            if content:
                streamed += content
                body_text += content
                # 真流式：正文 delta 即刻下发（09-06 用户反馈"直接蹦出答案"）
                yield {"content": content, "ts": int(time.time() * 1000)}
        yield {"__result__": (streamed, body_text, reasoning, native)}

    # ── 主循环 ────────────────────────────────────────────
    async def run(self):
        self.steps = 0
        mode = "native" if (self.is_local and _local_native_tools_ok()) else (
            "legacy-fence" if self.is_local else "cloud-native")
        self._step_log("任务开始",
                       f"model={self.model} endpoint={self.api_url} "
                       f"is_local={self.is_local} access={self.access_mode} "
                       f"mode={mode} 消息={len(self.current_msgs)}")
        async with httpx.AsyncClient(timeout=httpx.Timeout(120)) as client:
            self._client = client
            while self.steps < MAX_STEPS:
                self.steps += 1
                yield {"event": "round_start", "iteration": self.steps}
                from agent_loop import _session_cancel_requested
                if _session_cancel_requested(self.session_id):
                    yield {"content": "\n\n⏹️ 任务已停止。"}
                    return
                if self.steps > 1:
                    for m in _claim_steer(self.session_id):
                        self.current_msgs.append({"role": "user", "content": m})
                        yield {"event": "steer_applied", "content": m[:80]}

                # pre_step waterfall：planning 门 / compaction（payload 携带预事件）
                payload = {"pre_events": [], "reject": False}
                payload = await self.scope.run_waterfall("pre_step", payload)
                for evt in payload.get("pre_events", []):
                    yield evt
                if payload.get("reject"):
                    if payload.get("reject_reason") != "generate_failed":
                        yield {"content": "\n\n⏹️ 计划已被拒绝，任务未执行。你可以调整要求后重新发起。"}
                    return

                # 规划等待：插件标记 plan_wait → 循环执行 yield+await（生成器语义）
                plan_wait = payload.get("plan_wait")
                if plan_wait and not self._plan_injected:
                    from agent_loop import _wait_plan_confirmation
                    approved, evts = await _wait_plan_confirmation(
                        plan_wait["plan_id"], plan_wait["event_obj"])
                    for evt in evts:
                        yield evt
                    if not approved:
                        yield {"content": "\n\n⏹️ 计划已被拒绝，任务未执行。你可以调整要求后重新发起。"}
                        return
                    self.current_msgs.insert(0, {
                        "role": "system",
                        "content": "以下是已确认（用户批准）的执行计划，请严格按计划逐步执行（可调用工具）：\n"
                                   + plan_wait["plan"]})
                    self._plan_injected = True

                engine_model = self._engine_model()
                body = self._build_request(engine_model)
                tools_n = len(body.get("tools") or [])
                chars = sum(len(str(m.get("content") or "")) for m in body.get("messages", []))
                self._step_log("请求构建",
                               f"model={body.get('model')} 消息={len(body.get('messages', []))} "
                               f"字符={chars} tools={tools_n} "
                               f"思考={'关' if (body.get('chat_template_kwargs') or {}).get('enable_thinking') is False else '开'}")
                # request waterfall：fastpath（快车道）等插件改写请求
                body_keys_before = sorted(body.keys())
                body = await self.scope.run_waterfall("request", body)
                if sorted(body.keys()) != body_keys_before:
                    self._step_log("request 插件改写",
                                   f"键变化 {body_keys_before} → {sorted(body.keys())}")
                t_sample = time.monotonic()
                result = None
                raw_deltas = 0
                try:
                    async for evt in self._sample(client, body):
                        if "__result__" in evt:
                            result = evt["__result__"]
                            continue
                        raw_deltas += 1
                        yield evt
                    if result is None:
                        return
                    streamed, body_text, reasoning, native = result
                except _GenerationLoopError as e:
                    yield {"content": f"\n\n⚠️ {e}"}
                    return
                except TimeoutError as e:
                    yield {"content": f"\n\n⚠️ {e}，请重试或检查模型服务。"}
                    return
                except httpx.HTTPStatusError as e:
                    status = getattr(e.response, "status_code", 0)
                    try:
                        logger.error("thin loop HTTP %s body: %s", status,
                                     (e.response.text or "")[:500])
                    except Exception:
                        pass
                    if (self.native_tools and not self.native_fallback_used
                            and status == 400):
                        # 引擎不支持 tools 参数（模板无工具能力）→ 回退围栏格式重跑本步
                        self.native_fallback_used = True
                        self.native_tools = False
                        self.steps -= 1
                        logger.warning("thin loop：引擎拒绝 tools 参数（400），回退围栏提示词")
                        continue
                    yield {"content": f"\n\n⚠️ 模型服务返回错误 HTTP {status}，请稍后重试。"}
                    return
                self._step_log("流结束",
                               f"delta={raw_deltas} 思考={len(reasoning)}字 "
                               f"正文={len(body_text)}字 耗时={time.monotonic()-t_sample:.1f}s")

                # 工具调用：原生优先，围栏兜底
                tool_calls = []
                clean_text = streamed
                if native:
                    # 原生 delta（含空名——执行器按参数恢复，09-21 deepseek quirk）
                    tool_calls = [native[i] for i in sorted(native.keys())]
                self._step_log("工具解析",
                               f"原生={len(native)} 围栏待查")

                if not tool_calls:
                    clean_text, fence_calls = _parse_prompt_tool_calls(streamed)
                    if fence_calls:
                        tool_calls = fence_calls
                    else:
                        clean_text = streamed
                tool_names = {t.get("function", {}).get("name") for t in self._active_tools()}
                # 空名调用必须放行到执行器（恢复/守卫都在那边）。
                # "有名但不在册"（模型幻觉出的工具名）：不静默丢弃（09-06 21:19
                # 事故：丢弃后模型以为调用成功，用户只拿到 25 字残答）——
                # 回一条可见错误（附在册工具清单）让模型下一轮自行纠正。
                _known_calls, _unknown_named = [], []
                for tc in tool_calls:
                    nm = (tc.get("function") or {}).get("name", "")
                    if not nm or nm in tool_names:
                        _known_calls.append(tc)
                    else:
                        _unknown_named.append(tc)
                if _unknown_named:
                    for tc in _unknown_named:
                        nm = (tc.get("function") or {}).get("name", "?")
                        if not tc.get("id"):
                            tc["id"] = str(uuid.uuid4())
                        err = (f"⛔ 未知工具 '{nm}'。可用工具："
                               f"{', '.join(sorted(n for n in tool_names if n))}。"
                               f"请从以上清单中选择正确的工具重新调用。")
                        self.current_msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": err})
                        self._step_log("未知工具", f"{nm} → 已回错误清单")
                        logger.warning("thin loop: 未知工具调用 '%s' 已回错误（在册: %s）",
                                       nm, sorted(n for n in tool_names if n))
                        yield {"event": "tool_end", "call_id": tc["id"], "tool": nm,
                               "result": err, "ts": int(time.time() * 1000)}
                    if not _known_calls:
                        # 全部是幻觉调用 → 不交付正文，直接进下一轮让模型纠正
                        text_output_delivered = True
                        continue
                tool_calls = _known_calls

                self._step_log("采样完成",
                               f"正文={len(body_text)}字 思考={len(reasoning)}字 "
                               f"工具调用={len(tool_calls)}")

                if not tool_calls:
                    # deliver waterfall：弱模型辅助（思考-only/空响应诊断/语言替换）
                    # 正文已实时流出——辅助只能"替换"（content_revised）或补充，不得重复
                    payload = {
                        "text": body_text, "streamed": streamed,
                        "client": client, "api_url": self.api_url,
                        "headers": self.headers, "engine_model": engine_model,
                        "current_msgs": self.current_msgs, "user_text": self.last_user_text,
                        "user_lang": self.user_lang, "is_local": self.is_local,
                        "events": [], "handled": False,
                    }
                    payload = await self.scope.run_waterfall("deliver", payload)
                    for evt in payload.get("events", []):
                        yield evt
                    self._step_log("交付",
                                   f"handled={payload.get('handled')} "
                                   f"events={len(payload.get('events', []))}")
                    # dsh 语义：交付后若 inbox 有排队输入 → 续开下一轮
                    pending = _claim_steer(self.session_id)
                    if pending:
                        self._step_log("steer 认领", f"{len(pending)} 条")
                        for m in pending:
                            self.current_msgs.append({"role": "user", "content": m})
                        continue
                    return

                # 工具执行：错误即结果（共享执行器含确认/事件/溯源）
                from agent_loop import _confirm_bypassed, _handle_tool_execution, _start_tool_confirmation
                from tool_executor import _resolve_permission
                if any(not (tc.get("function") or {}).get("name") for tc in tool_calls):
                    self.parallel_disabled = True
                asst = {"role": "assistant", "content": clean_text or ""}
                if self.native_tools:
                    asst["tool_calls"] = tool_calls
                self.current_msgs.append(asst)
                for tc in tool_calls:
                    if not tc.get("id"):
                        tc["id"] = str(uuid.uuid4())
                    tname = tc.get("function", {}).get("name", "")
                    try:
                        targs = json.loads(tc.get("function", {}).get("arguments", "{}") or "{}")
                    except Exception:
                        targs = {}
                    pre = None
                    if _resolve_permission(tname, targs) == "confirm" \
                            and not _confirm_bypassed(tname, self.access_mode):
                        pre = await _start_tool_confirmation(tc["id"], tname, targs)
                        yield pre["event"]
                    verify_failed, events = await _handle_tool_execution(
                        tc, self.current_msgs, self.session_id, "latiao",
                        self.access_mode, pre_started=pre)
                    _res = next((str(e.get("result", "")) for e in events
                                 if isinstance(e, dict) and "result" in e), "")
                    self._step_log("工具结果", f"{tname} → {len(_res)}字符 "
                                               f"摘要: {_res[:80]!r}")
                    for evt in events:
                        yield evt

            yield {"content": f"\n\n⚠️ 已达安全步数上限（{MAX_STEPS}）。请发送新消息继续。"}
