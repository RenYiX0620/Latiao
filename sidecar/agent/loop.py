"""薄循环（对标 Codex turn.rs / dsh ReactLoopAgent）。

架构原则：
- 模型驱动终止：step 循环"采样 → 工具执行 → 结果回填"，模型不再调用
  工具即结束。无 nudge 链、无停滞计数、无 200 字闸门。
- 错误即结果：工具失败的结构化错误文本回填上下文，由模型自纠。
- 弱模型辅助（gates，按引擎开关）：语言交付闸门 + 思考-only 终答提取，
  仅本地引擎启用；云端前沿模型零干预。
- steer：新消息入队，step 边界认领为同轮续跑输入（不再取消）。
- 门控：LATIAO_AGENT_LOOP_V3=1 启用；默认仍走 v1（Stage 4 切换删除）。
"""
import asyncio
import json
import time
import uuid
import logging

import httpx

from agent.transport import (
    _is_local_llm_url,
    _local_llm_stream,
    mark_llm_suspect,
)
from agent.parsing import (
    _parse_delta_line,
    _parse_native_tool_calls,
    _parse_prompt_tool_calls,
    _strip_native_tool_calls,
)
from agent.text_quality import (
    _GenerationLoopError,
    _ThinkFenceFilter,
    _deduplicate_response,
    _detect_text_loop,
    _extract_think_body,
    _strip_repeat_tail,
    _strip_think_fences,
)
from agent.context import (
    _extract_last_user_text,
    _TASK_KW,
    _detect_user_language,
    _ensure_market_tools,
    _filter_tools_by_access,
    _get_localized_text,
    _is_light_query,
    _local_native_tools_ok,
    _maybe_add_inline_file_note,
    _merge_system_messages,
    _normalize_access,
    _resolve_max_tokens,
    _sanitize_tool_messages,
    _strip_transient_reminders,
)
from agent.context import _NATIVE_LEAN_PROMPT
from agent.gates import (_build_local_tools_prompt, _final_answer_extraction,
                         _reply_lang_mismatch)

logger = logging.getLogger("latiao-sidecar")

MAX_STEPS = 40          # 安全网（Stage 3 压缩落地后放宽）
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


class _StepAbort(Exception):
    """传输层语义异常：本轮失败且不可恢复（上层转错误事件）。"""


def _err_text(result: str) -> str:
    """工具失败 = 结果（Codex RespondToModel 语义）：原文回填，模型自纠。"""
    return result


class ThinAgentLoop:
    """单一 agent 循环：cloud/local 共用，差异只体现在请求组装与辅助层开关。"""

    def __init__(self, messages: list, model: str, api_url: str, headers: dict,
                 session_id: str = "", access_mode: str = "confirm",
                 thinking_level: str = "high"):
        self.session_id = session_id
        self.model = model
        self.api_url = api_url
        self.headers = headers
        self.access_mode = _normalize_access(access_mode)
        self.thinking_level = thinking_level
        self.is_local = _is_local_llm_url(api_url)
        self.current_msgs: list = _strip_transient_reminders([dict(m) for m in messages])
        self.last_user_text = _extract_last_user_text(self.current_msgs)
        _maybe_add_inline_file_note(self.current_msgs, self.last_user_text)
        self.user_lang = _detect_user_language(self.last_user_text)
        self.steps = 0
        self.native_tools = False
        self.native_fallback_used = False

    # ── 工具集 ────────────────────────────────────────────
    def _active_tools(self) -> list:
        from agent_loop import TOOLS, _cap_tools, _filter_tools
        tools = _filter_tools(self.last_user_text, TOOLS) if self.last_user_text else list(TOOLS)
        tools = _filter_tools_by_access(tools, self.access_mode)
        if len(tools) > 12:
            tools = _cap_tools(tools, 12)
        return _ensure_market_tools(tools, self.last_user_text)

    def _all_tools(self) -> list:
        from agent_loop import TOOLS
        return _filter_tools_by_access(list(TOOLS), self.access_mode)

    def _engine_model(self) -> str:
        import local_llm
        return getattr(local_llm._engine, "current_model_id", "") or self.model

    # ── 请求组装（request waterfall 的第一个内置钩子）────────
    def _build_request(self, engine_model: str) -> dict:
        light = self.is_local and _is_light_query(self.last_user_text, self.current_msgs)
        native_ok = self.is_local and _local_native_tools_ok() and not self.native_fallback_used
        tools = self._active_tools() if not light else []
        self.native_tools = bool(tools) and native_ok

        body = {
            "model": engine_model,
            "messages": _merge_system_messages(_sanitize_tool_messages(list(self.current_msgs))),
            "stream": True,
            "temperature": 0.0,
            "max_tokens": _resolve_max_tokens(self.model),
            "stop": ["<|im_end|>", "<|endoftext|>", "<end_of_turn>", "<eos>"],
        }
        if not self.is_local:
            body["tools"] = [dict(t) for t in tools]
            body["messages"] = _sanitize_tool_messages(list(self.current_msgs))
            return body

        # 本地：原生 tools + 精简纪律；闲聊快车道关思考；legacy 回退围栏提示词
        if self.native_tools:
            body["tools"] = [dict(t) for t in tools]
            sys_prompt = _NATIVE_LEAN_PROMPT
        else:
            from agent.gates import _build_local_tools_prompt
            sys_prompt = _build_local_tools_prompt(tools)
        if light:
            sys_prompt = ""
        msgs = _merge_system_messages(_sanitize_tool_messages(list(self.current_msgs)))
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
        body["messages"] = msgs
        body["frequency_penalty"] = 0.6
        if light or self.steps > 1:
            body["chat_template_kwargs"] = {"enable_thinking": False}
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

    # ── 单 step：流式采样，返回 (正文, 思考, 原生工具调用) ────
    async def _sample(self, client, body, state):
        streamed, body_text, reasoning = "", "", ""
        native: dict[int, dict] = {}
        fence = _ThinkFenceFilter()
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
                any_out = True
                reasoning += think
                streamed += think
                state["events"].append({"reasoning": think, "ts": int(time.time() * 1000)})
            if content:
                any_out = True
                streamed += content
                body_text += content
        return streamed, body_text, reasoning, native

    # ── 主循环 ────────────────────────────────────────────
    async def run(self):
        self.steps = 0
        async with httpx.AsyncClient(timeout=httpx.Timeout(120)) as client:
            while self.steps < MAX_STEPS:
                self.steps += 1
                yield {"event": "round_start", "iteration": self.steps}
                from agent_loop import _session_cancel_requested
                if _session_cancel_requested(self.session_id):
                    yield {"content": "\n\n⏹️ 任务已停止。"}
                    return
                if self.steps > 1:
                    steered = _claim_steer(self.session_id)
                    for m in steered:
                        self.current_msgs.append({"role": "user", "content": m})
                        yield {"event": "steer_applied", "content": m[:80]}
                engine_model = self._engine_model()
                body = self._build_request(engine_model)
                state = {"events": [], "raw_deltas": 0}
                try:
                    streamed, body_text, reasoning, native = await self._sample(client, body, state)
                except _GenerationLoopError as e:
                    yield {"content": f"\n\n⚠️ {e}"}
                    return
                except TimeoutError as e:
                    yield {"content": f"\n\n⚠️ {e}，请重试或检查模型服务。"}
                    return
                except httpx.HTTPStatusError as e:
                    status = getattr(e.response, "status_code", 0)
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
                for evt in state["events"]:
                    yield evt

                # 工具调用：原生优先，围栏兜底
                tool_calls = []
                if native:
                    tcs = [native[i] for i in sorted(native)]
                    if any(tc.get("function", {}).get("name") for tc in tcs):
                        tool_calls = tcs
                clean_text, fence_calls = _parse_prompt_tool_calls(streamed)
                if not tool_calls and fence_calls:
                    tool_calls = fence_calls
                tool_names = {t.get("function", {}).get("name") for t in self._active_tools()}
                tool_calls = [tc for tc in tool_calls
                              if tc.get("function", {}).get("name") in tool_names]

                if not tool_calls:
                    async for evt in self._deliver(client, body_text or streamed,
                                                   reasoning, streamed):
                        yield evt
                    # dsh 语义：交付后若 inbox 有排队输入 → 续开下一轮
                    pending = _claim_steer(self.session_id)
                    if pending:
                        for m in pending:
                            self.current_msgs.append({"role": "user", "content": m})
                        continue
                    return

                # 工具执行：错误即结果（_handle_tool_execution 已含确认/事件/溯源）
                from agent_loop import _handle_tool_execution
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
                    from agent_loop import _confirm_bypassed, _start_tool_confirmation
                    from tool_executor import _resolve_permission
                    pre = None
                    if _resolve_permission(tname, targs) == "confirm" \
                            and not _confirm_bypassed(tname, self.access_mode):
                        pre = await _start_tool_confirmation(tc["id"], tname, targs)
                        yield pre["event"]
                    verify_failed, events = await _handle_tool_execution(
                        tc, self.current_msgs, self.session_id, "latiao",
                        self.access_mode, pre_started=pre)
                    for evt in events:
                        yield evt

            yield {"content": f"\n\n⚠️ 已达安全步数上限（{MAX_STEPS}）。请发送新消息继续。"}

    # ── 交付（弱模型辅助挂载点）────────────────────────────
    async def _deliver(self, client, body_text: str, reasoning: str, streamed: str):
        text = (body_text or "").strip()
        # 辅助 1：思考-only → 终答提取（27B 推理模型故障形态，仅本地）
        if not text and streamed.strip() and self.is_local:
            final = await _final_answer_extraction(
                client, self.api_url, self.headers, self._engine_model(),
                self.current_msgs, self.user_lang)
            if len(final) >= 120:
                yield {"content": "\n\n" + _strip_think_fences(final)}
                return
            yield {"content": "\n\n⚠️ 本地模型本轮只输出了思考过程。请回复「继续」重试，或换用其他模型。"}
            return
        if not text:
            yield {"content": ("\n\n⚠️ 模型返回了空响应。可能原因：上下文超限被截断、"
                               "模型不支持当前请求格式。建议换用更大的模型或重试。")}
            return
        # 辅助 2：语言交付闸门
        if _reply_lang_mismatch(self.last_user_text, text) and self.is_local:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60)) as c2:
                translated = await _force_translate_quiet(
                    c2, self.api_url, self.headers, self._engine_model(), text, self.user_lang)
            if translated and translated != text:
                yield {"content": "\n\n" + translated}
                return
        yield {"content": "\n\n" + _strip_think_fences(text)}


async def _force_translate_quiet(client, api_url, headers, engine_model, text, lang):
    """交付前翻译（本地引擎直发；失败返回原文）。"""
    from agent.gates import _force_translate
    try:
        return await _force_translate(client, api_url, headers, engine_model, text, lang)
    except Exception:
        return text
