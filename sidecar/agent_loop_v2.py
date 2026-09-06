"""阶段 2b：统一 AgentLoop 驱动（云/本地双循环合并）。

合并依据（两循环逐行对照结论）：
- 相同骨架（两循环重复实现）：迭代循环、取消检查、流式读取（180s 停滞阈值
  + 60s 心跳保活 + 超时后本地引擎杀进重载）、内容门控（复读节流截尾/去重
  一次性截断/think 围栏缓冲过滤/native control token 过滤/正文交付）、工具分发
  （pre_started 确认死锁修复/deny 检测/recent_tool_calls 停滞机制）。
- 三个真实差异点（策略化）：① body 构建（cloud=OpenAI tools 数组；
  local=提示词工具清单+tool→user 转换+300s 单轮墙钟上限）；② 工具调用提取
  （cloud=delta tool_calls 缓冲；local=栅栏/native 解析）；③ 收尾闸门链。

灰度：LATIAO_AGENT_LOOP_V2=1 走本驱动；v1 双循环保持不动，直至真机验收
（机检矩阵：tests/test_loop_scenarios.py）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

import httpx

import local_llm
from agent_loop import (
    _append_loop_log,
    _append_unique_system,
    _build_local_tools_prompt,
    _cap_tools,
    _check_access,
    _confirm_bypassed,
    _deduplicate_response,
    _detect_text_loop,
    _detect_user_language,
    _ensure_final_language,
    _ensure_final_language_with_retry,
    _extract_last_user_text,
    _extract_think_body,
    _filter_tools,
    _filter_tools_by_access,
    _final_answer_extraction,
    _find_unsourced_numbers,
    _force_translate,
    _get_agent_tools,
    _get_localized_text,
    _handle_tool_execution,
    _is_chat_query,
    _is_light_query,
    _local_native_tools_ok,
    _maybe_add_inline_file_note,
    _is_local_llm_url,
    _strip_transient_reminders,
    _is_meta_wrapup,
    _looks_like_planning,
    _looks_like_tool_fantasy,
    _merge_system_messages,
    _NATIVE_CONTROL_RE,
    _NATIVE_FOLLOWUP_PROMPT,
    _NATIVE_LEAN_PROMPT,
    _NATIVE_TOOL_RE,
    _PENDING_INTENT_PATTERNS,
    _parse_native_tool_calls,
    _parse_prompt_tool_calls,
    _parse_delta_line,
    _reply_lang_mismatch,
    _resolve_permission,
    _session_cancel_requested,
    _start_tool_confirmation,
    _strip_native_tool_calls,
    _strip_repeat_tail,
    _strip_think_fences,
    _track_progress,
    TOOLS,
)

logger = logging.getLogger("latiao-sidecar")

MAX_ITERATIONS = 50
MAX_STAGNATION = 3
_STALL_HEARTBEAT = 60.0
_STALL_FIRST_TOKEN = 90.0
_STALL_AFTER_FIRST = 180.0
_GEN_DEADLINE = 900.0  # 09-21 22:48 实测：27B + 1.2 万字符输入，单轮需 8-15 分钟；
# 300s 到点白烧截断再循环（用户感知"卡死 10 分钟"）；900s 与停滞/零交付
# 双层护栏并行（180s 无字节才判死），不会无限拖。
_TASK_KW = ("运行", "执行", "做", "帮我", "写", "创建", "查", "搜", "找", "分析",
            "修复", "构建", "部署", "安装", "配置", "列出", "读取", "读", "总结",
            "生成", "打开", "查看", "解释", "整理", "统计", "告诉",
            "run", "build", "fix", "create", "search", "analyze", "deploy",
            "list", "read", "summar", "write", "explain", "tell")


class _GenerationLoopAbort(Exception):
    """复读/单轮超时截断：停止消费本轮流，文本交给工具解析/闸门（v1 18:01 语义）。"""


class _ThinkFenceFilter:
    """think 围栏剥除（````think>…```think<```` 跨 tokenizer 拆分时缓冲补全）。"""

    def __init__(self):
        self.buf = ""
        self.inside = False

    def feed(self, clean: str) -> str:
        self.buf += clean
        while True:
            if self.inside:
                end = self.buf.find("```think<")
                if end < 0:
                    return ""
                self.buf = self.buf[end + len("```think<"):]
                self.inside = False
                continue
            start = self.buf.find("```think>")
            if start < 0:
                out = self.buf
                self.buf = ""
                return out
            out = self.buf[:start]
            self.buf = self.buf[start + len("```think>"):]
            self.inside = True
            if out:
                return out


class IterState:
    def __init__(self, loop: "AgentLoop"):
        self.loop = loop
        self.streamed_text = ""
        self.reasoning_text = ""
        self.body_text = ""
        self.raw_delta_count = 0
        self.dedup_fired = False
        self.body_out = False
        self.tool_call_bufs: dict[int, dict] = {}
        self.tool_calls: list[dict] = []
        self.cancelled = False
        self.gen_deadline_ts = (time.monotonic() + _GEN_DEADLINE
                                if loop.mode.gen_deadline() is not None else None)


class TailCtx:
    def __init__(self, loop: "AgentLoop", state: IterState, *, iteration, streak,
                 has_called_tool, text_output_delivered, pending_tool_analysis,
                 intent_nudges, fabrication_nudges, think_only_nudges, fab_cap,
                 brief_answer_nudged: bool):
        self.loop = loop
        self.state = state
        self.iteration = iteration
        self.streak = streak
        self.has_called_tool = has_called_tool
        self.text_output_delivered = text_output_delivered
        self.pending_tool_analysis = pending_tool_analysis
        self.intent_nudges = intent_nudges
        self.fabrication_nudges = fabrication_nudges
        self.think_only_nudges = think_only_nudges
        self.fab_cap = fab_cap
        self.brief_answer_nudged = brief_answer_nudged
        self.events: list[dict] = []


class ModeStrategy:
    def __init__(self, loop: "AgentLoop"):
        self.loop = loop

    def engine_model(self) -> str:
        return self.loop.model

    def gen_deadline(self) -> float | None:
        return None

    def build_body(self, current_msgs: list) -> dict:
        raise NotImplementedError

    def ingest_tool_calls(self, delta: dict, state: IterState) -> None:
        """delta 工具调用增量（cloud 缓冲累积；local 忽略，走栅栏解析）。"""

    def note_tool_calls(self, state: IterState) -> list[dict]:
        raise NotImplementedError

    def emit_revision(self) -> bool:
        return False

    async def finish_tail(self, ctx: TailCtx) -> tuple | None:
        raise NotImplementedError


class AgentLoop:
    """统一驱动（灰度：LATIAO_AGENT_LOOP_V2=1）。"""

    def __init__(self, engine_kind: str, messages: list, model: str, api_url: str,
                 headers: dict, session_id: str = "", agent_id: str = "latiao",
                 reflection_mode: str = "off", access_mode: str = "confirm",
                 thinking_level: str = "high"):
        if engine_kind not in ("cloud", "local"):
            raise ValueError(f"unknown engine_kind: {engine_kind}")
        self.engine_kind = engine_kind
        self.messages = messages
        self.model = model
        self.api_url = api_url
        self.headers = headers
        self.session_id = session_id
        self.agent_id = agent_id
        self.reflection_mode = reflection_mode
        self.access_mode = access_mode
        self.thinking_level = thinking_level
        self.mode = (_CloudMode(self) if engine_kind == "cloud" else _LocalMode(self))
        self.current_msgs = _strip_transient_reminders([dict(m) for m in messages])
        self.last_user_text = _extract_last_user_text(self.current_msgs)
        _maybe_add_inline_file_note(self.current_msgs, self.last_user_text)
        self.lang = _detect_user_language(self.last_user_text)
        self.agent_tools = _get_agent_tools(agent_id, TOOLS)
        active = (_filter_tools(self.last_user_text, self.agent_tools)
                  if self.last_user_text else self.agent_tools)
        active = _filter_tools_by_access(active, access_mode)
        if len(active) > 8:
            active = _cap_tools(active, 12)
        from agent_loop import _ensure_market_tools
        active = _ensure_market_tools(active, self.last_user_text)
        self.active_tools = active
        self.tool_names = {t.get("function", {}).get("name") for t in active}
        # 09-06 三档模式（与 v1 本地循环同口径）：
        # light=闲聊快车道（不传工具+关思考）；native=原生 tools 参数（自管
        # mlx）；legacy=围栏提示词。native 遇 400 自动回退 legacy。
        self.light_query = _is_light_query(self.last_user_text, self.current_msgs)
        self.native_tools = (not self.light_query) and _local_native_tools_ok() and bool(self.active_tools)
        self.native_fallback_used = False
        self.has_called_tool = False  # 工具后续轮关思考用（与 v1 同口径）
        self._ctx_warned = False

    async def run(self):
        yield {"event": "v2_engine", "engine": self.engine_kind,
               "model": self.model, "resolved_endpoint": self.api_url}
        iteration = 0
        recent_tool_calls: set[str] = set()
        stagnation = 0
        streak = 0
        empty_name_streak = 0  # 空名连续失败计数（3 次中止，09-21 实测）
        empty_name_seen = False  # 空名发生→下一轮并行关闭
        has_called_tool = False
        text_output_delivered = False
        pending_tool_analysis = False
        intent_nudges = 0
        fabrication_nudges = 0
        think_only_nudges = 0
        brief_answer_nudged = False
        fab_cap = 1 if _is_local_llm_url(self.api_url) else 2

        while iteration < MAX_ITERATIONS:
            iteration += 1
            # 轮次透明化（09-05 23:52：8 分钟无正文用户以为卡死——每轮可见）
            yield {"event": "round_start", "iteration": iteration}
            self.current_iteration = iteration
            if _session_cancel_requested(self.session_id):
                _track_progress(self.session_id, "cancelled", "user_stop")
                yield {"content": "\n\n⏹️ 任务已停止。"}
                return
            total_chars = sum(len(str(m.get("content", ""))) for m in self.current_msgs)
            if total_chars > 39000 and not self._ctx_warned:
                self._ctx_warned = True
                yield {"content": f"\n\n💡 **上下文接近上限**（~{total_chars // 2} tokens）。请考虑开新会话。"}
            try:
                body = self.mode.build_body(self.current_msgs)
                if empty_name_seen and self.engine_kind == "cloud":
                    body["parallel_tool_calls"] = False
            except Exception as e:
                logger.error("v2 build_body failed: %s", e, exc_info=True)
                yield {"content": "\n\n⚠️ 内部错误：请求构建失败。"}
                return
            state = IterState(self)
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(120)) as client:
                    async for evt in self._stream_once(client, body, state, text_output_delivered):
                        yield evt
                        if state.cancelled:
                            return
            except _GenerationLoopAbort:
                pass  # 截断语义：文本交给我后续的提取/闸门（v1 18:01 同款）
            except httpx.HTTPStatusError as e:
                # 原生 tools 被引擎拒绝（模板不支持工具，400）→ 回退围栏格式重跑本轮
                if (self.native_tools and not self.native_fallback_used
                        and getattr(e.response, "status_code", 0) == 400):
                    self.native_fallback_used = True
                    self.native_tools = False
                    iteration -= 1  # 回退迭代号：重试轮拿完整围栏提示词
                    logger.warning("v2 引擎拒绝 tools 参数（HTTP 400），回退围栏提示词格式")
                    continue
                yield {"content": f"\n\n⚠️ 模型服务返回错误 HTTP {e.response.status_code}，请稍后重试。"}
                return
            except TimeoutError:
                yield {"content": f"\n\n⚠️ 流式响应超时（{_STALL_AFTER_FIRST:.0f}s 无进展）。"}
                return
            except Exception as e:
                logger.error("v2 iteration %s stream failed: %s", iteration, e, exc_info=True)
                yield {"content": f"\n\n⚠️ 内部错误（{type(e).__name__}），请重试。"}
                return

            if self.mode.emit_revision() and (text_output_delivered or state.body_out) \
                    and state.streamed_text.strip():
                yield {"event": "content_revised", "content": _strip_think_fences(state.streamed_text)}

            state.tool_calls = self.mode.note_tool_calls(state)
            if not state.tool_calls:
                _track_progress(self.session_id, "text_round", "text_only")

            if state.tool_calls:
                _append_loop_log(f"V2 Iteration {iteration}: found "
                                 f"{len(state.tool_calls)} tool(s): "
                                 f"{[tc.get('function', {}).get('name') for tc in state.tool_calls]}\n")
                _track_progress(self.session_id, "tool_calling", f"{len(state.tool_calls)} tool(s)")
                self.current_msgs.append({
                    "role": "assistant",
                    "content": _deduplicate_response(state.streamed_text) if state.streamed_text else None,
                    "reasoning_content": state.reasoning_text,
                    "tool_calls": state.tool_calls,
                })
                has_called_tool = True
                self.has_called_tool = True  # 后续轮关思考（v1 同口径）
                text_output_delivered = False
                any_new = False
                round_failed = False
                for tc in state.tool_calls:
                    sig = f"{tc.get('function', {}).get('name', '')}:" \
                          f"{hash(str(tc.get('function', {}).get('arguments', '')))}"
                    if sig not in recent_tool_calls:
                        recent_tool_calls.add(sig)
                        any_new = True
                    if _session_cancel_requested(self.session_id):
                        _track_progress(self.session_id, "cancelled", "user_stop")
                        yield {"content": "\n\n⏹️ 任务已停止。"}
                        return
                    if not tc.get("id"):
                        tc["id"] = str(uuid.uuid4())
                    pre_started = None
                    try:
                        tname = tc.get("function", {}).get("name", "unknown")
                        targs = json.loads(tc.get("function", {}).get("arguments", "{}") or "{}")
                        # 空名快速中止（与 v1 同口径：3 次即中止并给可操作诊断）
                        if not tname.strip():
                            empty_name_streak += 1
                            empty_name_seen = True
                            logger.warning("v2 empty tool name streak=%s args=%s",
                                           empty_name_streak, json.dumps(targs, ensure_ascii=False)[:200])
                            if empty_name_streak >= 3:
                                yield {"content": ("\n\n⛔ 模型连续 3 次输出空工具名（工具调用格式异常）。"
                                                   "任务已中止。请切换为其他模型，或在模型页重新加载后重试。")}
                                _track_progress(self.session_id, "stalled",
                                                f"empty_tool_name x{empty_name_streak}")
                                return
                            # 1-2 次不 continue：守卫反馈（空名提示）进上下文，第 3 次才中止
                        if _resolve_permission(tname, targs) == "confirm" \
                                and not _confirm_bypassed(tname, self.access_mode) \
                                and not _check_access(tname, self.access_mode):
                            pre_started = await _start_tool_confirmation(tc["id"], tname, targs)
                            yield pre_started["event"]
                    except Exception:
                        pre_started = None
                    verify_failed, events = await _handle_tool_execution(
                        tc, self.current_msgs, self.session_id, self.agent_id,
                        self.access_mode, pre_started=pre_started)
                    for evt in events:
                        yield evt
                    denied = any(isinstance(e, dict) and str(e.get("result", "")).startswith("⛔ User denied")
                                 for e in events)
                    if verify_failed and not denied:
                        round_failed = True
                if any_new or round_failed:
                    stagnation = 0
                    if any_new and self.engine_kind == "local":
                        # v1 本地同款：工具产出新结果 → 实质性回答前不允许一句话收尾
                        # （"让我读取数据再分析"式声明不算完成；17:52 事故：
                        # 计数不重置防"不断换查询无限拖延"）
                        pending_tool_analysis = True
                else:
                    stagnation += 1
                    if stagnation >= MAX_STAGNATION:
                        yield {"content": f"\n\n⚠️ 连续 {stagnation} 轮无新进展，Agent 停止。如需继续请发新消息。"}
                        return
                continue

            ctx = TailCtx(
                self, state, iteration=iteration, streak=streak,
                has_called_tool=has_called_tool,
                text_output_delivered=text_output_delivered,
                pending_tool_analysis=pending_tool_analysis,
                intent_nudges=intent_nudges, fabrication_nudges=fabrication_nudges,
                think_only_nudges=think_only_nudges, fab_cap=fab_cap,
                brief_answer_nudged=brief_answer_nudged,
            )
            verdict = await self.mode.finish_tail(ctx)
            for evt in ctx.events:
                yield evt
            if verdict is None:
                return
            if verdict == "fallthrough":
                # v1 同款：闸门链整体跳过（如 recent_failed）→ 原样进入下一轮
                continue
            (streak, text_output_delivered, pending_tool_analysis,
             intent_nudges, fabrication_nudges, think_only_nudges,
             brief_answer_nudged) = verdict

        tool_count = sum(1 for m in self.current_msgs if m.get("role") == "tool")
        yield {"content": f"\n\n⚠️ 已达到硬上限 ({MAX_ITERATIONS} 轮)。本会话共执行了 {tool_count} 次工具调用。如需继续，请发送新消息。"}

    async def _stream_once(self, client, body: dict, state: IterState,
                           text_output_delivered: bool):
        """统一流式段：180s 停滞/60s 心跳/本地 300s 单轮/超时杀本地引擎。"""
        fence = _ThinkFenceFilter()
        stream_ctx = client.stream("POST", self.api_url, json=body, headers=self.headers)
        silence = 0.0
        try:
            async with asyncio.timeout(_STALL_AFTER_FIRST + 120):
                async with stream_ctx as r:
                    if r.status_code != 200:
                        try:
                            err = (await r.aread()).decode("utf-8", errors="replace")[:800]
                        except Exception:
                            err = "<read failed>"
                        logger.error("v2 stream HTTP %d body: %s", r.status_code, err)
                    r.raise_for_status()
                    aiter = r.aiter_lines()
                    while True:
                        try:
                            line = await asyncio.wait_for(anext(aiter), timeout=_STALL_HEARTBEAT)
                            silence = 0.0
                        except asyncio.TimeoutError:
                            silence += _STALL_HEARTBEAT
                            stall = (_STALL_FIRST_TOKEN
                                     if (not state.body_out and state.raw_delta_count == 0)
                                     else _STALL_AFTER_FIRST)
                            if silence < stall:
                                yield {"event": "heartbeat"}
                                continue
                            raise TimeoutError("模型输出停滞")
                        except StopAsyncIteration:
                            break
                        if not line or not line.startswith("data: "):
                            continue
                        done, delta = _parse_delta_line(line)
                        if done:
                            break
                        if delta is None:
                            continue
                        state.raw_delta_count += 1
                        self.mode.ingest_tool_calls(delta, state)
                        event = self._ingest_delta(state, delta, fence, text_output_delivered)
                        if event is not None:
                            yield event
                        if state.gen_deadline_ts and time.monotonic() > state.gen_deadline_ts:
                            logger.warning("[V2] 单轮生成 300s 超时，截断本轮")
                            state.streamed_text = _strip_repeat_tail(state.streamed_text)
                            raise _GenerationLoopAbort("单轮生成超时(300s)，已截断")
                        if _session_cancel_requested(self.session_id):
                            state.cancelled = True
                            return
        except TimeoutError:
            if _is_local_llm_url(self.api_url):
                logger.warning("v2 本地流超时，判定引擎挂起，杀进程并触发重载")
                try:
                    eng = local_llm._engine
                    if eng.current_model_id:
                        eng._kill_port(eng.server_port)
                        eng.server_status = "stopped"
                        eng._request_reload(eng.current_model_id)
                except Exception:
                    logger.warning("v2 超时后引擎重载触发失败", exc_info=True)
            raise

    def _ingest_delta(self, state: IterState, delta: dict, fence,
                      text_output_delivered: bool) -> dict | None:
        content = delta.get("content", "")
        reasoning = delta.get("reasoning", "")
        if content:
            state.streamed_text += content
            state.body_text += content
            if state.raw_delta_count % 40 == 0 and _detect_text_loop(state.streamed_text):
                logger.warning("[V2] 检测到输出复读循环，截断本轮生成")
                state.streamed_text = _strip_repeat_tail(state.streamed_text)
                raise _GenerationLoopAbort("输出复读循环，已截断")
            if not state.dedup_fired:
                ded = _deduplicate_response(state.streamed_text)
                if len(ded) < len(state.streamed_text):
                    state.dedup_fired = True
                    state.streamed_text = ded + content
            if text_output_delivered:
                if state.raw_delta_count % 40 == 0 and self.engine_kind == "cloud":
                    return {"event": "content_revised",
                            "content": _strip_think_fences(state.streamed_text)}
                return None
            clean = _NATIVE_CONTROL_RE.sub("", content)
            clean = fence.feed(clean)
            if clean:
                state.body_out = True
                if len(state.streamed_text) < 5:
                    _track_progress(self.session_id, "generating", "text_start")
                return {"content": clean}
        elif reasoning:
            state.reasoning_text += reasoning
            state.streamed_text += reasoning
            if state.raw_delta_count % 40 == 0 and _detect_text_loop(state.streamed_text):
                logger.warning("[V2] 检测到输出复读循环，截断本轮生成")
                state.streamed_text = _strip_repeat_tail(state.streamed_text)
                raise _GenerationLoopAbort("输出复读循环，已截断")
            if not state.dedup_fired:
                ded = _deduplicate_response(state.streamed_text)
                if len(ded) < len(state.streamed_text):
                    state.dedup_fired = True
                    state.streamed_text = ded + reasoning
            if not text_output_delivered:
                return {"reasoning": reasoning, "ts": int(time.time() * 1000)}
        return None


# ══ 云模式：OpenAI function calling ═══════════════════════════════════
class _CloudMode(ModeStrategy):
    def build_body(self, current_msgs: list) -> dict:
        return {
            "model": self.loop.model,
            "messages": current_msgs,
            "tools": [dict(t) for t in self.loop.active_tools],
            "stream": True,
        }

    def ingest_tool_calls(self, delta: dict, state: IterState) -> None:
        for tc_delta in delta.get("tool_calls", []):
            idx = tc_delta.get("index", 0)
            if idx not in state.tool_call_bufs:
                state.tool_call_bufs[idx] = {"id": "", "type": "function",
                                             "function": {"name": "", "arguments": ""}}
            buf = state.tool_call_bufs[idx]
            if "id" in tc_delta:
                buf["id"] = tc_delta["id"]
            if "function" in tc_delta:
                if "name" in tc_delta["function"]:
                    buf["function"]["name"] = tc_delta["function"]["name"]
                if "arguments" in tc_delta["function"]:
                    buf["function"]["arguments"] += tc_delta["function"]["arguments"]

    def note_tool_calls(self, state: IterState) -> list[dict]:
        if state.tool_call_bufs:
            return [state.tool_call_bufs[i] for i in sorted(state.tool_call_bufs.keys())]
        if state.streamed_text and _NATIVE_TOOL_RE.search(state.streamed_text):
            parsed = _parse_native_tool_calls(state.streamed_text)
            if parsed:
                state.streamed_text = _strip_native_tool_calls(state.streamed_text)
                return parsed
        return []

    def emit_revision(self) -> bool:
        return True

    async def finish_tail(self, ctx: TailCtx) -> tuple | None:
        loop, state = ctx.loop, ctx.state
        text = state.streamed_text.strip()
        msgs = loop.current_msgs
        has_task = (any(kw in (loop.last_user_text or "").lower() for kw in _TASK_KW)
          and not _is_chat_query(loop.last_user_text))
        # 闲聊/能力介绍：模型乱调工具不能进入追问链（17:11 事故根治，agent_loop._is_chat_query）
        has_recent_tool_result = any(
            m.get("role") == "tool" or (isinstance(m.get("content"), str)
                                        and m["content"].startswith("[工具结果]"))
            for m in msgs[-3:])
        # 非任务消息 + 模型乱调了工具：工具结果后的"短回答/无实质"追问对闲聊
        # 不成立（17:11 事故同根）——直接收官，不得 nudge。
        if has_recent_tool_result and not has_task:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120)) as client:
                deliver, _lang_retry = await _ensure_final_language_with_retry(client, loop.api_url, loop.headers, loop.model, text, loop.last_user_text, msgs, lang_retry_done=True)
            msgs.append({"role": "assistant", "content": deliver})
            if deliver != text:
                ctx.events.append({"event": "content_revised", "content": deliver})
            _track_progress(loop.session_id, "completed", f"text_response ({len(deliver)} chars)")
            return None
        if has_recent_tool_result and ctx.streak < 1 and text:
            if len(text) >= 200:
                async with httpx.AsyncClient(timeout=httpx.Timeout(120)) as client:
                    deliver, _lang_retry = await _ensure_final_language_with_retry(client, loop.api_url, loop.headers, loop.model, text, loop.last_user_text, msgs, lang_retry_done=True)
                if deliver != text:
                    ctx.events.append({"event": "content_revised", "content": deliver})
                msgs.append({"role": "assistant", "content": deliver})
                _track_progress(loop.session_id, "completed", f"text_response ({len(deliver)} chars)")
                return None
            msgs.append({"role": "user", "content":
                         "⚠️ 你刚才收到了工具的执行结果，但只回复了文字而没有继续调用工具。\n"
                         "请检查：用户的任务是否真的完全完成了？\n"
                         "如果还没完成，请继续调用工具。如果确实完成了，请回复最终结果。"})
            return (ctx.streak + 1, True, ctx.pending_tool_analysis, ctx.intent_nudges,
                    ctx.fabrication_nudges, ctx.think_only_nudges, ctx.brief_answer_nudged)
        if not ctx.has_called_tool and ctx.streak < 3 and text:
            msgs.append({"role": "assistant", "content": text})
            user_q = loop.last_user_text.strip().rstrip("?？") if loop.last_user_text else ""
            if not any(kw in user_q for kw in _TASK_KW):
                async with httpx.AsyncClient(timeout=httpx.Timeout(120)) as client:
                    deliver, _lang_retry = await _ensure_final_language_with_retry(client, loop.api_url, loop.headers, loop.model, text, user_q, msgs, lang_retry_done=True)
                if deliver != text:
                    ctx.events.append({"event": "content_revised", "content": deliver})
                _track_progress(loop.session_id, "completed", f"text_response ({len(deliver)} chars)")
                return None
            msgs.append({"role": "user", "content":
                         "不要写执行计划，直接行动。需要用什么工具就立即调用；"
                         "若本次任务基于用户消息里已提供的资料即可完成，请直接给出完整回答，不要做声明或收尾。"})
            return (ctx.streak + 1, True, ctx.pending_tool_analysis, ctx.intent_nudges,
                    ctx.fabrication_nudges, ctx.think_only_nudges, ctx.brief_answer_nudged)
        if not text and ctx.streak < MAX_STAGNATION:
            msgs.append({"role": "user", "content": _get_localized_text(loop.lang, {
                "zh": "⚠️ 你上一轮的回复是空的。请直接回复用户，或者使用工具完成任务。",
                "en": "⚠️ Your last response was empty. Please respond to the user directly, or use a tool.",
                "ja": "⚠️ 前回の応答が空でした。ユーザーに直接返信するか、ツールを使用してください。",
            })})
            return (ctx.streak + 1, ctx.text_output_delivered, ctx.pending_tool_analysis,
                    ctx.intent_nudges, ctx.fabrication_nudges, ctx.think_only_nudges, ctx.brief_answer_nudged)
        if not text:
            ctx.events.append({"content": ("\n\n⚠️ **模型连续多次返回空响应，任务已中止。**\n"
                                           "可能原因：云端服务限流/降级或上下文超限被截断。\n"
                                           "建议：检查云端配置或稍后重试。")})
            _track_progress(loop.session_id, "stalled", f"empty_response x{ctx.streak}")
            return None
        _track_progress(loop.session_id, "completed", f"text_response ({len(text)} chars)")
        return None


# ══ 本地模式：提示词工具调用 ═══════════════════════════════════════════
class _LocalMode(ModeStrategy):
    def engine_model(self) -> str:
        return getattr(local_llm._engine, "current_model_id", "") or self.loop.model

    def gen_deadline(self) -> float:
        return _GEN_DEADLINE

    def build_body(self, current_msgs: list) -> dict:
        loop = self.loop
        merged = _merge_system_messages(current_msgs)
        if loop.light_query:
            # 闲聊快车道：不传工具、不注入提示、关思考（27B 实测 7.0s→1.3s）
            return {
                "model": self.engine_model(),
                "messages": merged,
                "stream": True,
                "temperature": 0.0,
                "frequency_penalty": 0.6,
                "stop": ["<|im_end|>", "<|endoftext|>", "<end_of_turn>", "<eos>"],
                "chat_template_kwargs": {"enable_thinking": False},
            }
        iteration = getattr(loop, "current_iteration", 1)
        if loop.native_tools:
            # 原生 function calling：工具经 API tools 参数，系统提示只留纪律
            prompt = _NATIVE_LEAN_PROMPT if iteration == 1 else _NATIVE_FOLLOWUP_PROMPT
            if iteration == 1 and (current_msgs and len(current_msgs[-1].get("content", "")) > 8000):
                # 09-05 23:52 事故：长输入首轮纯思考 8 分钟不写正文
                prompt = (
                    prompt
                    + "\n\n📏 思考预算（长输入）：用户输入内容很长（表格/文档全文）。"
                    "请先简短思考（≤300 字），然后立刻在正文写出完整分析——"
                    "关键数字和结论必须写进正文。禁止长时间只思考不写正文。"
                    "Think briefly (≤300 chars), then write the full analysis "
                    "with key numbers and conclusions in the reply body."
                )
            if merged and merged[0].get("role") == "system":
                merged[0] = dict(merged[0])
                merged[0]["content"] = prompt + "\n\n" + str(merged[0].get("content", ""))
            else:
                merged.insert(0, {"role": "system", "content": prompt})
            body = {
                "model": self.engine_model(),
                "messages": merged,
                "stream": True,
                "temperature": 0.0,
                "frequency_penalty": 0.6,
                "stop": ["<|im_end|>", "<|endoftext|>", "<end_of_turn>", "<eos>"],
                "tools": [dict(t) for t in loop.active_tools],
            }
            if loop.has_called_tool:
                # 工具后续轮关思考（09-06 13:23：工具后仍开思考 97s 零交付）
                body["chat_template_kwargs"] = {"enable_thinking": False}
            return body
        # legacy 围栏路径：首轮全量提示，后续轮轻量提醒（此前每轮重发
        # 4800 字符全量工具清单——27B 下每轮多付数千 token prefill + 更长思考）
        tools_prompt = _build_local_tools_prompt(loop.active_tools)
        if iteration > 1:
            names_str = ", ".join(sorted(n for n in loop.tool_names if n)) or "无"
            tools_prompt = (
                f"⚠️ 任务尚未完成，你必须继续！可用工具: {names_str}。\n"
                "格式：```tool 工具名\n{\"参数\":\"值\"}\n```\n"
                "如果当前任务的所有步骤都已完成，才可以直接回复用户。否则必须继续使用工具。"
            )
        if merged and merged[0].get("role") == "system":
            merged[0] = dict(merged[0])
            merged[0]["content"] = tools_prompt + "\n\n" + str(merged[0].get("content", ""))
        else:
            merged.insert(0, {"role": "system", "content": tools_prompt})
        body = {
            "model": self.engine_model(),
            "messages": merged,
            "stream": True,
            # 工具调用确定性（0 温度）
            "temperature": 0.0,
            "frequency_penalty": 0.6,
            "stop": ["<|im_end|>", "<|endoftext|>", "<end_of_turn>", "<eos>"],
        }
        if loop.has_called_tool:
            # 工具后续轮关思考（外部引擎会忽略该字段，无害）
            body["chat_template_kwargs"] = {"enable_thinking": False}
        return body

    def ingest_tool_calls(self, delta: dict, state: IterState) -> None:
        # 原生模式：mlx ToolParser 的 delta.tool_calls（收尾包整体送达）
        if not self.loop.native_tools:
            return  # legacy：工具走围栏解析
        for tc_delta in delta.get("tool_calls", []) or []:
            idx = tc_delta.get("index", 0)
            if idx not in state.tool_call_bufs:
                state.tool_call_bufs[idx] = {"id": "", "type": "function",
                                             "function": {"name": "", "arguments": ""}}
            buf = state.tool_call_bufs[idx]
            if tc_delta.get("id"):
                buf["id"] = tc_delta["id"]
            fn = tc_delta.get("function") or {}
            if fn.get("name"):
                buf["function"]["name"] += fn["name"]
            if fn.get("arguments"):
                buf["function"]["arguments"] += fn["arguments"]

    def note_tool_calls(self, state: IterState) -> list[dict]:
        # 原生 API tool_calls 优先（09-06）
        if state.tool_call_bufs:
            tcs = [state.tool_call_bufs[i] for i in sorted(state.tool_call_bufs.keys())]
            if any(tc.get("function", {}).get("name") for tc in tcs):
                return tcs
        clean, calls = _parse_prompt_tool_calls(state.streamed_text)
        if not calls and _NATIVE_TOOL_RE.search(state.streamed_text):
            native = _parse_native_tool_calls(state.streamed_text)
            if native:
                state.streamed_text = _strip_native_tool_calls(state.streamed_text)
                return [tc for tc in native
                        if tc.get("function", {}).get("name") in self.loop.tool_names]
        if calls:
            state.streamed_text = clean
        return [tc for tc in calls if tc.get("function", {}).get("name") in self.loop.tool_names]

    async def finish_tail(self, ctx: TailCtx) -> tuple | None:
        loop, state = ctx.loop, ctx.state
        body = state.body_text.strip()
        text = state.streamed_text.strip()
        msgs = loop.current_msgs
        # 非任务消息门槛（17:11 事故根治）：用户消息不含任务词时，工具是模型
        # 自己乱调的（如"你能做什么"的演示式 list_dir），绝不能因此进入
        # pending_tool_analysis 追问链——否则"思考→演示工具→nudge→再思考"循环，
        # 用户看到思考闪现三次 + 重复能力清单。闲聊零追问，直接交付。
        has_task = (any(kw in (loop.last_user_text or "").lower() for kw in _TASK_KW)
          and not _is_chat_query(loop.last_user_text))
        # 闲聊/能力介绍：模型乱调工具不能进入追问链（17:11 事故根治，agent_loop._is_chat_query）
        recent_failed = any(
            "Error" in str(m.get("content", "")) or "⚠️" in str(m.get("content", ""))
            for m in msgs[-4:])
        if ctx.pending_tool_analysis and body and not has_task:
            # 闲聊交付不依赖 recent_failed（⚠️ 常驻系统提示词会让它恒真，
            # 曾把闲聊分支整个跳过——17:11 复现根因之一）
            async with httpx.AsyncClient(timeout=httpx.Timeout(120)) as client:
                deliver, _lang_retry = await _ensure_final_language_with_retry(client, loop.api_url, loop.headers, self.engine_model(), body, loop.last_user_text, msgs, lang_retry_done=True)
            msgs.append({"role": "assistant", "content": deliver})
            ctx.events.append({"content": "\n\n" + _strip_think_fences(deliver)})
            _track_progress(loop.session_id, "completed", f"text_response ({len(deliver)} chars)")
            return None
        if ctx.pending_tool_analysis and body and not recent_failed:
            unsourced = _find_unsourced_numbers(body, msgs)
            if unsourced and ctx.fabrication_nudges < ctx.fab_cap:
                msgs.append({"role": "assistant", "content": body})
                msgs.append({"role": "system", "content":
                             f"⚠️ 数据来源校验：你回复中的这些数字未出现在本会话用户消息或任何工具结果中："
                             f"{'、'.join(unsourced[:8])}。关键数字必须来自用户消息或工具结果——"
                             f"若这些数字是工具数据的换算（如百万→亿），请注明'按工具数据折算'；"
                             f"否则删除该数值。请修正后重新作答。"})
                ctx.events.append({"event": "heartbeat"})
                return (ctx.streak + 1, True, ctx.pending_tool_analysis, ctx.intent_nudges,
                        ctx.fabrication_nudges + 1, ctx.think_only_nudges, ctx.brief_answer_nudged)
            pending_intent = any(k in body.lower() for k in _PENDING_INTENT_PATTERNS)
            lang_ok = not _reply_lang_mismatch(loop.last_user_text, body)
            if (len(body) >= 200 and not _is_meta_wrapup(body) and not pending_intent
                    and not _looks_like_planning(body) and lang_ok):
                async with httpx.AsyncClient(timeout=httpx.Timeout(120)) as client:
                    deliver, _lang_retry = await _ensure_final_language_with_retry(client, loop.api_url, loop.headers, self.engine_model(), body, loop.last_user_text, msgs, lang_retry_done=True)
                msgs.append({"role": "assistant", "content": deliver})
                ctx.events.append({"content": "\n\n" + _strip_think_fences(deliver)})
                _track_progress(loop.session_id, "completed", f"text_response ({len(deliver)} chars)")
                return None
            if not lang_ok and not _looks_like_planning(body) and not pending_intent:
                async with httpx.AsyncClient(timeout=httpx.Timeout(120)) as client:
                    deliver = await _force_translate(
                        client, loop.api_url, loop.headers, self.engine_model(), body,
                        _detect_user_language(loop.last_user_text))
                ctx.events.append({"content": "\n\n" + _strip_think_fences(deliver)})
                _track_progress(loop.session_id, "completed", f"translated_response ({len(deliver)} chars)")
                return None
            think_body = _extract_think_body(text)
            if (len(think_body) >= 200 and not _is_meta_wrapup(think_body)
                    and not _looks_like_tool_fantasy(think_body)
                    and not _reply_lang_mismatch(loop.last_user_text, think_body)):
                msgs.append({"role": "assistant", "content": think_body})
                ctx.events.append({"event": "content_revised", "content": think_body})
                _track_progress(loop.session_id, "completed", f"think_body ({len(think_body)} chars)")
                return None
            intent_cap = 1 if _is_local_llm_url(loop.api_url) else 3
            if ctx.intent_nudges < intent_cap:
                msgs.append({"role": "assistant", "content": body or "（未输出正文）"})
                if _looks_like_planning(body):
                    _append_unique_system(msgs, _get_localized_text(
                        _detect_user_language(loop.last_user_text),
                        {"zh": "⚠️ 这不是用户的新消息，而是系统提醒（上一轮回复未完成）：\n"
                               "必须用简体中文回复。不要只发声明或道歉。你刚才说还要继续——现在就调用工具去执行；"
                               "如果数据其实已经足够，就把完整分析写进回复正文（含关键数字与结论）。",
                         "en": "You MUST reply in English. ⚠️ System reminder (previous reply incomplete):\n"
                               "Don't just announce. Call the tool NOW; if data is sufficient, "
                               "write the full analysis in your reply.",
                         "ja": "必ず日本語で返信してください。⚠️ システム通知（前の回答が未完了）。\n"
                               "宣言だけでなく、今すぐツールを呼び出すか、完全な分析を本文に書いてください。"}))
                else:
                    _append_unique_system(msgs, _get_localized_text(
                        _detect_user_language(loop.last_user_text),
                        {"zh": "⚠️ 必须用简体中文回复。\n"
                               "⚠️ 这不是用户的新消息，而是系统提醒：你刚才收到了工具的执行结果，但你的回复里没有给出实质内容。\n"
                               "如果还需要数据，直接调用工具；否则把完整的分析写进回复正文。\n"
                               "调用工具格式：```tool 工具名\n{\"参数\":\"值\"}\n```",
                         "en": "You MUST reply in English. ⚠️ System reminder: you received tool results "
                               "but your reply contained no real content. Write the FULL analysis.\n"
                               "Tool format: ```tool tool_name\n{\"param\":\"value\"}\n```",
                         "ja": "必ず日本語で返信してください。⚠️ ツール結果を受け取りましたが、"
                               "返信に実質的な内容がありません。完全な分析を本文に書いてください。"}))
                ctx.events.append({"event": "heartbeat"})
                return (ctx.streak + 1, True, ctx.pending_tool_analysis,
                        ctx.intent_nudges + 1, ctx.fabrication_nudges, ctx.think_only_nudges, ctx.brief_answer_nudged)
        # 思考-only 轮 → 终答提取；2 轮后干净收尾
        if not body and text:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120)) as client:
                final = await _final_answer_extraction(
                    client, loop.api_url, loop.headers, self.engine_model(), msgs,
                    _detect_user_language(loop.last_user_text))
            if len(final) >= 200:
                ctx.events.append({"content": "\n\n" + _strip_think_fences(final)})
                _track_progress(loop.session_id, "completed", f"think_only_extract ({len(final)} chars)")
                return None
            if ctx.think_only_nudges + 1 >= 2:
                ctx.events.append({"content": ("\n\n⚠️ 本地模型连续两轮只输出了思考过程、没有生成分析正文。"
                                               "请回复「继续」重试，或换云端模型重发本任务。")})
                _track_progress(loop.session_id, "completed", "think_only_abort")
                return None
            msgs.append({"role": "assistant", "content": "（未输出正文）"})
            msgs.append({"role": "user", "content": _get_localized_text(
                _detect_user_language(loop.last_user_text),
                {"zh": "你上一轮只输出了思考过程，回复正文是空的。请在正文中直接输出完整回答（含关键数字与结论）。不要只思考不输出正文。",
                 "en": "Your last turn produced only reasoning with an empty reply body. "
                       "Write the full answer directly.",
                 "ja": "前回は思考のみで本文が空でした。主要な数字と結論を含む完全な回答を本文に直接書いてください。"})})
            ctx.events.append({"event": "heartbeat"})
            return (ctx.streak + 1, True, ctx.pending_tool_analysis, ctx.intent_nudges,
                    ctx.fabrication_nudges, ctx.think_only_nudges + 1, ctx.brief_answer_nudged)
        if not ctx.has_called_tool and ctx.streak < 3 and body:
            msgs.append({"role": "assistant", "content": body})
            user_q = (loop.last_user_text or "").strip().rstrip("?？")
            if not any(kw in user_q for kw in _TASK_KW):
                async with httpx.AsyncClient(timeout=httpx.Timeout(120)) as client:
                    deliver, _lang_retry = await _ensure_final_language_with_retry(client, loop.api_url, loop.headers, self.engine_model(), body, user_q, msgs, lang_retry_done=True)
                ctx.events.append({"content": "\n\n" + _strip_think_fences(deliver)})
                _track_progress(loop.session_id, "completed", f"text_response ({len(deliver)} chars)")
                return None
        if not text and ctx.streak < MAX_STAGNATION:
            msgs.append({"role": "system", "content": _get_localized_text(
                _detect_user_language(loop.last_user_text),
                {"zh": "⚠️ 你上一轮的回复是空的。请直接回复用户，或者使用工具完成任务。如果需要调用工具，使用 ```tool 格式。",
                 "en": "⚠️ Your last response was empty. Use the ```tool format to call a tool.",
                 "ja": "⚠️ 前回の応答が空でした。ツールを使用するには ```tool 形式を使ってください。"})})
            return (ctx.streak + 1, ctx.text_output_delivered, ctx.pending_tool_analysis,
                    ctx.intent_nudges, ctx.fabrication_nudges, ctx.think_only_nudges, ctx.brief_answer_nudged)
        if not text:
            ctx.events.append({"content": ("\n\n⚠️ **本地模型连续多次无响应，任务已中止。**\n"
                                           "可能原因：模型上下文不足、模型不支持工具调用格式。"
                                           "建议：换用更大的模型（7B+），或重启模型服务后重试。")})
            _track_progress(loop.session_id, "stalled", f"empty_response x{ctx.streak}")
            return None
        # 短回答 + 工具失败：追加一轮绕过失败的提示（v1 4279 同款）
        if len(body) < 200 and not ctx.has_called_tool and any(
                "Error" in str(m.get("content", "")) or "⚠️" in str(m.get("content", ""))
                for m in msgs[-4:]):
            msgs.append({"role": "system", "content":
                         "上一个工具调用失败了。请换一个工具或调整参数重试，"
                         "不要因一次失败就直接给简短结论；若全部工具不可用，再如实告知。"})
            ctx.events.append({"event": "heartbeat"})
            return (ctx.streak + 1, True, ctx.pending_tool_analysis, ctx.intent_nudges,
                    ctx.fabrication_nudges, ctx.think_only_nudges, ctx.brief_answer_nudged)
        # 短回答 + 实质资料：追问充分回答一轮（v1 4295；只追加一次防死循环）
        tool_out_total = sum(len(str(m.get("content") or "")) for m in msgs
                             if m.get("role") == "tool")
        if (len(body) < 200 and ctx.has_called_tool and tool_out_total > 600
                and not ctx.brief_answer_nudged):
            msgs.append({"role": "system", "content":
                         "你已通过工具获得了实质数据（见上方工具结果），"
                         "但刚才的回答太简短。请基于这些数据给出充分的回答："
                         "包含关键数字与必要的展开说明，让用户不需要再追问。"
                         "若任务确已完成且无需展开，再如实收尾。"})
            ctx.events.append({"event": "heartbeat"})
            return (ctx.streak + 1, True, ctx.pending_tool_analysis, ctx.intent_nudges,
                    ctx.fabrication_nudges, ctx.think_only_nudges, True)
        # 兜底路径（v1 4317-4361）：规划/未执行意图/元评论 → 有界 nudge；
        # cap 满 → 终答提取替换；最终只交付正文（思考-only 绝不给用户）。
        fb_cap = 1 if _is_local_llm_url(loop.api_url) else 3
        pending_intent2 = any(k in body.lower() for k in _PENDING_INTENT_PATTERNS)
        if ((_looks_like_planning(body) or pending_intent2 or _is_meta_wrapup(body))
                and ctx.intent_nudges < fb_cap):
            msgs.append({"role": "assistant", "content": body or "（未输出正文）"})
            msgs.append({"role": "system", "content":
                         "⚠️ 你上一轮只说了计划/声明而没有执行。刚才的查询可能失败或未覆盖全部数据——"
                         "如果需要数据，立即调用相应的工具；如果数据已足够（包括用户消息里已提供的内容），"
                         "就把完整分析（含关键数字与结论）写进回复正文。不要只重复计划。"})
            ctx.events.append({"event": "heartbeat"})
            return (ctx.streak + 1, True, ctx.pending_tool_analysis, ctx.intent_nudges + 1,
                    ctx.fabrication_nudges, ctx.think_only_nudges, ctx.brief_answer_nudged)
        unsourced_fb = _find_unsourced_numbers(body, msgs)
        if unsourced_fb and ctx.fabrication_nudges < ctx.fab_cap:
            msgs.append({"role": "assistant", "content": body or "（未输出正文）"})
            msgs.append({"role": "system", "content":
                         f"⚠️ 数据来源校验：回复中的数字 {'、'.join(unsourced_fb[:8])} "
                         f"未出现在本会话用户消息或工具结果中——若为换算请注明'按工具数据折算'，"
                         f"否则删除或写'工具未返回该数据'。请修正后重新作答。"})
            ctx.events.append({"event": "heartbeat"})
            return (ctx.streak + 1, True, ctx.pending_tool_analysis, ctx.intent_nudges,
                    ctx.fabrication_nudges + 1, ctx.think_only_nudges, ctx.brief_answer_nudged)
        if _is_meta_wrapup(body) or _looks_like_planning(body) or pending_intent2:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120)) as client:
                sout = await _final_answer_extraction(
                    client, loop.api_url, loop.headers, self.engine_model(), msgs,
                    _detect_user_language(loop.last_user_text))
            if (len(sout) >= 200 and not _is_meta_wrapup(sout)
                    and not _looks_like_planning(sout)
                    and not any(k in sout.lower() for k in _PENDING_INTENT_PATTERNS)):
                body = sout
        # 最终交付：只交付正文（09-05 13:29 事故：思考-only 绝不把思考 dump 给用户）
        if not body:
            ctx.events.append({"content": "\n\n⚠️ 本地模型未能生成有效正文。请回复「继续」重试。"})
            _track_progress(loop.session_id, "completed", "body_empty_fallback")
            return None
        ctx.events.append({"content": "\n\n" + _strip_think_fences(body)})
        msgs.append({"role": "assistant", "content": body})
        _track_progress(loop.session_id, "completed", f"text_response ({len(body)} chars)")
        return None
