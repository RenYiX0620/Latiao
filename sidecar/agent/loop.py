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
import datetime
import json
import time
import uuid
import logging
from pathlib import Path

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


def _tool_calls_signature(tool_calls: list) -> str:
    """工具调用的规范化签名（名称 + 排序后参数 JSON）——重复同参调用检测的
    结构信号（ZCode buildRepeatedToolCallSignature / DSH repeat-tool-reminder
    同构）。不猜内容语义，只看结构。"""
    parts = []
    for tc in tool_calls:
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments", "{}") or "{}")
        except Exception:
            args = {"_raw": str(fn.get("arguments", ""))[:120]}
        parts.append(f"{fn.get('name', '?')}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}")
    return " || ".join(parts)

STALL_FIRST = 90        # 首 token 前静默上限

# ── 会话级工具目录一致性（对齐 Codex/DSH/ZCode 共识：意图过滤只增不删）──
# 三家调研定论：消息文本不参与工具目录的任何决策。Latiao 的意图关键词过滤
# 只负责「新增」相关工具；本会话已成功使用 / 已加载技能声明的工具，不因最后
# 一条消息的措辞离场（09-07 20:47 事故：32 轮行情会话中一句元问题把
# mx_query 过滤掉 → 模型无工具可调 → use_skill 死循环烧到步数上限）。
_SESSION_USED_TOOLS: dict[str, set] = {}
SKILL_DEPENDENT_TOOLS = {"mx-data": {"mx_query", "ak_finance"}}


_SESSION_NARRATIONS: dict[str, list] = {}  # session -> 最近过渡叙述（规范化后）


def _norm_narration(text: str) -> str:
    import re as _re
    return _re.sub(r"[^\u4e00-\u9fff\w]+", "", text.lower())[:120]


def _narration_seen(session_id: str, text: str) -> bool:
    """过渡叙述近重复判定：与最近 3 条任一相同 → True（结构去重）。"""
    n = _norm_narration(text)
    if not n:
        return False
    seen = _SESSION_NARRATIONS.setdefault(session_id, [])
    if n in seen:
        return True
    seen.append(n)
    del seen[:-3]
    return False


def mark_session_tools(session_id: str, names) -> None:
    """把工具标记为本会话已用/已承诺——后续轮次目录并集保留。"""
    if not session_id or not names:
        return
    s = _SESSION_USED_TOOLS.setdefault(session_id, set())
    s.update(n for n in names if n)


# ── 当日限额状态（工具当日免费额度，如 mx_query 150 次/日）────────
# 额度是账户级全局状态（上游按天统计）：把"今日已用尽"钉在应用数据
# 目录，日期比对天然跨天失效、重启不丢；成功调用即清除（防后续换
# Key 后标记错杀）。只用于提示注入，绝不改动工具目录。
_QUOTA_STATE_FILE: Path | None = None


def _quota_state_file() -> Path:
    global _QUOTA_STATE_FILE
    if _QUOTA_STATE_FILE is None:
        try:
            from config import PROGRESS_DIR
            _QUOTA_STATE_FILE = PROGRESS_DIR / "quota_state.json"
        except Exception:
            _QUOTA_STATE_FILE = Path.home() / ".local-ai-os" / "quota_state.json"
    return _QUOTA_STATE_FILE


def _quota_exhausted_today(tool: str) -> bool:
    try:
        data = json.loads(_quota_state_file().read_text(encoding="utf-8"))
        return isinstance(data, dict) and data.get(tool) == datetime.date.today().isoformat()
    except Exception:
        return False


def _mark_quota_exhausted(tool: str) -> None:
    try:
        f = _quota_state_file()
        data = {}
        if f.exists():
            data = json.loads(f.read_text(encoding="utf-8"))
        data[tool] = datetime.date.today().isoformat()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        logger.debug("quota state write failed", exc_info=True)


def _clear_quota_marker(tool: str) -> None:
    try:
        f = _quota_state_file()
        if f.exists():
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, dict) and tool in data:
                del data[tool]
                f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        logger.debug("quota state clear failed", exc_info=True)


# ── 终答轮数据摘要视图（工具结果内联，删除全部工具形态）──────────
def _collect_finalize_data(current_msgs: list, max_results: int = 6,
                           max_chars: int = 700) -> list:
    """工具结果数据块（去重、仅尾部、限长）——供摘要视图与兜底交付。"""
    blocks = []
    for m in current_msgs:
        if m.get("role") == "tool":
            c = str(m.get("content") or "").strip()
            if c and len(c) > 20:
                blocks.append(c)
    seen, uniq = set(), []
    for c in blocks:
        k = c[:80]
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    return [c[:max_chars] for c in uniq[-max_results:]]


def _finalize_directive() -> str:
    return ("📣 任务收尾：用户正在等待回答。下面以【数据N】逐条列出已收集到的工具结果。"
            "请仅基于这些数据直接用简体中文写出完整分析（包含关键数字与结论），直接输出正文；"
            "不要提到任何工具/调用过程，不要输出任何工具调用格式，写完即停。")


def _build_finalize_digest(current_msgs: list, max_results: int = 6,
                           max_chars: int = 700) -> list:
    """终答轮请求视图：数据摘要式消息（无 tool 角色/无 tool_calls/无 tools 键）。

    09-08 19:28 实况：12 轮工具形态历史把模型锁死在"只出工具调用"——
    tools=[] 且温度抖动后仍 30s 只出 1 个工具调用、正文 0 字；同数据但无
    工具形态（B-DIGEST 实测）56s 产出 873 字完整分析、零工具调用。"""
    out = [{"role": "system", "content": _finalize_directive()}]
    users = [m for m in current_msgs if m.get("role") == "user"]
    if users:
        out.append({"role": "user", "content": str(users[-1].get("content") or "")[:1200]})
    for i, c in enumerate(_collect_finalize_data(current_msgs, max_results, max_chars), 1):
        out.append({"role": "assistant", "content": f"【数据{i}】{c}"})
    # 防回显：数据块以 assistant 结尾时，模型会"续写/原样回显"最后一条
    # （09-08 20:38 实况：终答 274 字就是【数据7】的整段抄写）——必须追加
    # 一条 user 提问，模型只能正面应答，结构上不可能续写。
    out.append({"role": "user", "content": "请根据以上数据写出完整分析正文（简体中文，"
                                           "包含关键数字与结论）。"})
    return out

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
        self._len_retry_used = False           # max-tokens 截断重试（每轮一次）
        self._force_thinking_off_next = False  # 截断重试轮强制关思考
        self._recent_sigs: list[str] = []      # 重复同参调用检测（结构信号）
        self._repeat_reminders = 0             # 提醒注入计数（≤3 次/turn）
        self._gen_retry_used = False           # 复读/截断的确定性打破重试（每 turn 一次）
        self._retry_temp = None                # 重试轮温度抖动（None=默认 0.0）
        self._last_sig = None                  # 停滞闸门：上轮签名
        self._same_sig_rounds = 0              # 停滞闸门：同参连续轮数
        self._empty_gen_retry_used = False     # 引擎空生成重试（每 turn 一次）
        self._tool_rounds_no_answer = 0        # 连续工具轮（无实质正文交付）计数
        self._finalize_round = False           # 停滞闸门收口轮：无工具直接作答
        self._finalize_retry_used = False      # 终答轮空生成温度重采（一次）
        self._quota_hint_injected = False      # 当日额度用尽提示（每 turn 一次）
        self._retry_freq = None                # 重试轮频率惩罚（压制重复 token）
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
            _keep = ["delegate_task", "use_skill", "create_cron"]
            # app 意图保底（09-11 事故："打开相册"→意图含 open_app，但 cap 按
            # 优先级截断把它切在第 22 位 → 模型无工具只能空谈，0 次调用）
            _names = {t.get("function", {}).get("name") for t in tools}
            if _names & {"open_app", "open_folder"}:
                _keep += ["open_app", "open_folder"]
            tools = _cap_tools(tools, 12, keep_first=tuple(_keep))
        tools = _ensure_market_tools(tools, self.last_user_text)
        # 会话级目录一致性：本会话已成功使用 / 已加载技能声明的工具，不因最后
        # 一条消息的措辞离场（意图过滤只增不删——三家共识）。白名单（子代理）
        # 在下方仍照常过滤，隔离不受影响。
        used = _SESSION_USED_TOOLS.get(self.session_id) or set()
        if used:
            have = {t.get("function", {}).get("name") for t in tools}
            for name in used - have:
                _extra = [t for t in TOOLS if t.get("function", {}).get("name") == name]
                if _extra:
                    tools.append(_extra[0])
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
        if self._finalize_round:
            # 停滞闸门收口轮：不提供任何工具（模型只能直接作答）
            tools = []
            self.native_tools = True  # native 分支 tools=[] 下发，模板保持精简
        else:
            self.native_tools = bool(tools) and native_ok

        msgs = _merge_system_messages(_sanitize_tool_messages(list(self.current_msgs)))
        body = {
            "model": engine_model,
            "messages": msgs,
            "stream": True,
            "temperature": self._retry_temp if self._retry_temp is not None else 0.0,
            "frequency_penalty": self._retry_freq if self._retry_freq is not None else 0.6,
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
        if self.steps > 1 or self._force_thinking_off_next:
            # 工具后续轮关思考（09-06 13:23：工具后开思考 97s 零交付）；
            # max-tokens 截断重试轮强制关思考（思考是截断根因）
            body["chat_template_kwargs"] = {"enable_thinking": False}
        if self.parallel_disabled:
            body["parallel_tool_calls"] = False
        if self._finalize_round:
            # 终答轮：彻底换用数据摘要视图（09-08 19:28 实况：12 轮工具形态
            # 历史锁死模型——tools=[] 且温度抖动后仍 30s 只出 1 个工具调用、
            # 正文 0 字；B-DIGEST 实测同类数据没有工具消息时 56s 产出 873 字
            # 完整分析）。工具结果内联为普通文本，删除全部 tool 角色/调用，
            # 并且完全移除 tools 键（空列表也参与模板工具区渲染）。
            body["messages"] = _build_finalize_digest(self.current_msgs)
            body.pop("tools", None)
            body["parallel_tool_calls"] = False
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

    # ── 单 step：流式采样（实时流出 reasoning/content），结果经
    # __result__ 终端事件回传（消费者吞掉，不下发前端）──
    async def _sample(self, client, body):
        streamed, body_text, reasoning = "", "", ""
        native: dict[int, dict] = {}
        raw = 0
        finish_reason = None
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
            fr = delta.pop("finish_reason", None)
            if fr:
                finish_reason = fr
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
        yield {"__result__": (streamed, body_text, reasoning, native, finish_reason)}

    # ── 主循环 ────────────────────────────────────────────
    def _maybe_repeat_reminder(self, tool_calls: list) -> bool:
        """重复同参调用软提醒（ZCode/DSH 同构）：连续 3 轮「同工具+同参数」时
        注入一条系统提醒引导换路。结构信号、不拦截交付、每 turn ≤3 次。"""
        sig = _tool_calls_signature(tool_calls)
        self._recent_sigs.append(sig)
        if (len(self._recent_sigs) >= 3
                and len(set(self._recent_sigs[-3:])) == 1
                and self._repeat_reminders < 3):
            self._repeat_reminders += 1
            self._recent_sigs = []
            fname = (tool_calls[-1].get("function") or {}).get("name", "?")
            self.current_msgs.append({"role": "system", "content":
                f"⚠️ 你已经连续 3 轮用完全相同的参数调用 `{fname}`。不要原样重复——"
                "请更换查询措辞/参数，或改用其他工具/方法达成目标。"})
            self._step_log("重复调用提醒", f"{fname} ×3 同参")
            logger.info("thin loop: 重复同参调用提醒 (%s ×3)", fname)
            return True
        return False

    def _maybe_quota_hint(self, result: str) -> None:
        """工具结果命中"当日额度已用尽" → 本轮注入一次提示（每 turn 一次）。

        事前引导而非事后靠错误文案：限额是当日全局的（上游账户共享），
        用尽后再调必然秒回失败。提示只进当前 turn 的消息流，天然跨天过期。
        同时写入持久标记，让当天后续任务第一轮就预注入（见 run() 开头）。
        检测用完整特征词，避免把"达到/上限"等普通报错误判成限额。"""
        if self._quota_hint_injected or not result:
            return
        if "当日调用次数已用完" not in result:
            return
        self._quota_hint_injected = True
        _mark_quota_exhausted("mx_query")
        self.current_msgs.append({"role": "system", "content": (
            "⏰ 提示：mx_query 当日的免费额度已用尽（150 次/日，接口方统计），当天不会再成功。"
            "请直接使用替代工具（ak_finance / tavily_search）继续补充数据；"
            "若数据已足够，就直接写出完整分析（简体中文，含关键数字与结论）。")})
        self._step_log("额度提示", "已注入当日限额提示（每 turn 一次）+ 持久标记写入")
        logger.warning("thin loop: 检测到工具当日额度已用尽，注入事前提示")

    async def _stagnation_gate(self, tool_calls: list):
        """同参空转 ≥5 轮 → 强制终答轮收口。返回要下发的事件或 None。

        09-08 09:06 事故：同参调用被护栏拒绝后模型空转至 40 步上限零交付。
        结构信号（规范化签名连续相同），不看内容语义；基于上下文已收集的
        全部数据强制产出最终回答。终答由主循环下一轮自身产生（tools=空、
        关思考 + 终答指令）——14:29 曾用独立 _final_answer_extraction 补答，
        它要排同一把引擎串行锁，与在途流互斥 → 等锁 120s 超时失败收尾。"""
        sig = _tool_calls_signature(tool_calls)
        if sig == self._last_sig:
            self._same_sig_rounds += 1
        else:
            self._last_sig = sig
            self._same_sig_rounds = 0
        self._tool_rounds_no_answer += 1
        if self._same_sig_rounds < 5 and self._tool_rounds_no_answer < 12:
            return None
        logger.warning("thin loop: 同参空转 %d 轮，停滞闸门收口", self._same_sig_rounds)
        if self._finalize_round:
            # 收口轮已用过一次仍被再次触发（理论上 tools=[] 不再产生调用）
            # → 直接交付诊断，不再无限轮转
            return {"content": ("\n\n⚠️ 模型反复重复相同调用，已基于已收集数据尽力生成未果。"
                                "任务已收口。请重试或换用云端模型。")}
        self._finalize_round = True
        # 保证终答轮能挤进步数上限内（闸门在最后一轮触发时，continue 后
        # while 条件会直接退出、终答轮不会执行）
        if self.steps >= MAX_STEPS:
            self.steps = MAX_STEPS - 1
        self._same_sig_rounds = 0
        self._tool_rounds_no_answer = 0
        self._step_log("停滞闸门", "收口：下一轮强制直接作答（tools=空、关思考）")
        return None

    async def run(self):
        self.steps = 0
        mode = "native" if (self.is_local and _local_native_tools_ok()) else (
            "legacy-fence" if self.is_local else "cloud-native")
        # 本会话 id 供子代理后台完成通知回寻父会话（contextvars 随任务派生带入）
        from agent.subagent import _CURRENT_PARENT_SESSION, claim_bg_results
        _CURRENT_PARENT_SESSION.set(self.session_id)
        self._step_log("任务开始",
                       f"model={self.model} endpoint={self.api_url} "
                       f"is_local={self.is_local} access={self.access_mode} "
                       f"mode={mode} 消息={len(self.current_msgs)}")
        # 空闲期完成的后台子代理：结果通知在本轮启动时注入上下文（一次性）
        _bg_notes = claim_bg_results(self.session_id)
        if _bg_notes:
            _note_text = "\n\n".join(
                f"【后台子代理通知】{n['agent']} 已完成任务「{n['task']}」，结果：\n{n['result']}"
                for n in _bg_notes)
            self.current_msgs.append({"role": "system", "content": _note_text})
            self._step_log("后台通知注入", f"{len(_bg_notes)} 条")
        # 跨会话知识注入（旧循环 _build_chat_messages 有、薄循环重构时遗漏
        # ——09-07 接回）：按当前问题语义检索学习库，高置信条目注入参考
        try:
            from memory import _retrieve_relevant_learnings
            if self.last_user_text:
                _rel = [r for r in _retrieve_relevant_learnings(self.last_user_text, limit=5)
                        if r.get("confidence", 0) >= 0.3]
                if _rel:
                    self.current_msgs.append({"role": "system", "content":
                        "以下是 AI 从过去交互学到的相关知识（供参考，与当前任务不冲突时遵循）：\n"
                        + "\n".join(f"- {r['topic']}: {str(r['content'])[:200]}" for r in _rel)})
                    self._step_log("知识注入", f"{len(_rel)} 条相关学习")
        except Exception:
            logger.debug("learnings retrieval skipped", exc_info=True)
        # 当日额度状态预注入：今天已知 mx_query 用尽（持久标记，跨 turn/跨
        # 会话、按日期比对）→ 第一轮就告知模型，绕开注定失败的调用
        # （09-08 17:11：首轮 2 次 mx_query 秒错后才见提示；17:03 同款）
        if _quota_exhausted_today("mx_query") and not self._quota_hint_injected:
            self._quota_hint_injected = True
            self.current_msgs.append({"role": "system", "content": (
                "⏰ 提示：mx_query 今日免费额度已用尽（150 次/日，接口方统计，当日不会再成功）。"
                "请直接使用替代工具（ak_finance / tavily_search）查数据，不要调用 mx_query；"
                "数据足够就直接写出完整分析（简体中文，含关键数字与结论）。")})
            self._step_log("额度状态", "今日已标记用尽，首轮预注入提示（跨 turn）")
            logger.warning("thin loop: 当日额度状态预注入提示（跨 turn）")
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
                    streamed, body_text, reasoning, native, finish_reason = result
                    # max-tokens 截断完整性不变量（DSH BlockAssembler 同款）：
                    # finish=length 时模型"说到一半"，半截参数 JSON 不可执行——
                    # 丢弃全部 native 调用，关思考重跑本轮一次（思考是预算杀手，
                    # 09-06/09-07 推理模型 token 全进 <think> 同族根因）
                    if finish_reason == "length" and native:
                        if not self._len_retry_used:
                            self._len_retry_used = True
                            self._force_thinking_off_next = True
                            self.steps -= 1
                            logger.warning("thin loop: finish=length 截断 %d 个 tool-call，"
                                           "已丢弃；关思考重跑本轮", len(native))
                            continue
                        logger.warning("thin loop: finish=length 重试仍截断，丢弃 %d 个 tool-call",
                                       len(native))
                        native = {}
                except _GenerationLoopError as e:
                    # 复读/截断 = mlx 确定性解码退化（温度 0.0 下「继续」必然复发，
                    # 09-07 22:10 事故）。先做一次温度抖动重采（资源级重试，
                    # 对齐 max-tokens 重试模式）；再失败才交付警告。
                    if self._gen_retry_used < 2:
                        self._gen_retry_used += 1
                        if self._gen_retry_used == 1:
                            self._retry_temp, self._retry_freq = 0.35, 1.0
                        else:
                            self._retry_temp, self._retry_freq = 0.6, 1.5
                        self.steps -= 1
                        logger.warning("thin loop: %s —— 重采本轮（temp=%s freq=%s）",
                                       e, self._retry_temp, self._retry_freq)
                        yield {"content": "\n\n---\n\n"}
                        continue
                    logger.warning("thin loop: 重试后仍复读/截断，交付警告收尾: %s", e)
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
                    # 超长上下文特判：给出可执行指引（09-12：13.7 万字符 PDF 撞 400
                    # 只看到"模型服务返回错误"，用户无从下手）
                    _body = ""
                    try:
                        _body = (e.response.text or "").lower()
                    except Exception:
                        pass
                    if status == 400 and any(k in _body for k in (
                            "context", "too long", "length", "exceed", "maximum")):
                        try:
                            import local_llm
                            _tl = int(getattr(local_llm._engine, "model_token_limit", 0) or 0)
                        except Exception:
                            _tl = 0
                        yield {"content": (
                            "\n\n⚠️ 输入超出模型上下文"
                            + (f"（当前 {_tl:,} tokens）" if _tl else "")
                            + "。可选：① 到「模型」页调大上下文长度并点「重新加载模型」；"
                            "② 改用云端模型；③ 把文件分段提问（如“只看资格要求部分”）。")}
                        return
                    yield {"content": f"\n\n⚠️ 模型服务返回错误 HTTP {status}，请稍后重试。"}
                    return
                # 引擎空生成重试：流结束且正文/思考/工具调用全空 → 引擎抽风
                # （09-08 14:21 会话：连续两轮空生成后任务以诊断结束）。重采
                # 一次；再空才落下方空响应诊断。
                if (finish_reason != "length" and not streamed and not body_text
                        and not reasoning and not native):
                    if not self._empty_gen_retry_used:
                        self._empty_gen_retry_used = True
                        self.steps -= 1
                        logger.warning("thin loop: 引擎空生成，重采本轮")
                        yield {"event": "heartbeat"}
                        continue
                    logger.warning("thin loop: 引擎连续空生成，交付诊断")
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
                # "有名但不在册"（模型幻觉出的工具名）：不静默丢弃（09-07 17:40
                # 事故：原生=2 全被滤掉 → 模型以为调用了、用户拿到 3 字残答 +
                # 思考-only 提示）——回一条可见错误（附在册工具清单）让模型
                # 下一轮自行纠正。
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
                        continue
                tool_calls = _known_calls

                if self._finalize_round:
                    # 终答轮：只交付正文。模型若仍发围栏工具调用 → 剥离后交付。
                    if tool_calls:
                        self._step_log("终答轮", f"剥离 {len(tool_calls)} 个工具调用，直接交付")
                        tool_calls = []
                        streamed = body_text = _parse_prompt_tool_calls(streamed)[0]
                    if len(body_text.strip()) < 10:
                        # 终答轮仍空 → 温度抖动重采一次（0.0 温度下模型被
                        # 工具调用形态锁定（09-08 17:43 实况：tools=[] 仍只
                        # 输出 1 个工具调用、正文 0 字））；再空才交付警告。
                        if not self._finalize_retry_used:
                            self._finalize_retry_used = True
                            self._retry_temp, self._retry_freq = 0.6, 1.5
                            if self.steps >= MAX_STEPS:
                                self.steps = MAX_STEPS - 1
                            self.current_msgs.append({"role": "system", "content": (
                                "📣 上一轮你没有输出分析正文。工具已全部禁用——本轮请直接"
                                "用简体中文写出最终分析正文（基于以上已有数据，含关键数字与结论），"
                                "不要输出任何工具调用格式，不要提及工具名。")})
                            self._step_log("终答轮", "空生成 → 温度抖动重采（一次）")
                            yield {"event": "heartbeat"}
                            continue
                        _fb = _collect_finalize_data(self.current_msgs, max_results=8)
                        if _fb:
                            yield {"content": ("\n\n⚠️ 模型未能生成分析。以下为已收集的数据，"
                                               "可直接就此提问或换用云端模型继续解读：\n\n"
                                               + "\n\n".join(_fb))[:6000]}
                        else:
                            yield {"content": ("\n\n⚠️ 模型未生成最终分析，任务已收口。"
                                               "请重试或换用云端模型。")}
                    else:
                        # 正文已在 _sample 逐字流式下发，不再重复交付全文
                        self._step_log("终答轮", f"已交付 {len(body_text)} 字")
                    return

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

                # 会话级工具标记：即将执行的工具记入会话集合（目录一致性：
                # 用过的工具后续轮次保持可用）
                mark_session_tools(self.session_id,
                                   [(tc.get("function") or {}).get("name") for tc in tool_calls
                                    if (tc.get("function") or {}).get("name")])

                # 工具执行：错误即结果（共享执行器含确认/事件/溯源）
                from agent_loop import _confirm_bypassed, _handle_tool_execution, _start_tool_confirmation
                from tool_executor import _resolve_permission
                if any(not (tc.get("function") or {}).get("name") for tc in tool_calls):
                    self.parallel_disabled = True
                _narr_text = (clean_text or "").strip()
                if _narr_text and _narration_seen(self.session_id, _narr_text):
                    # 近重复过渡叙述：截断为短摘要入历史（真流式已让用户看过
                    # 一次）——历史里不再累积相同叙述，打破逐轮复述吸引子
                    asst = {"role": "assistant", "content": _narr_text[:60] + "…（同前）"}
                    self._step_log("重复叙述抑制", f"{len(_narr_text)}字 近重复")
                else:
                    asst = {"role": "assistant", "content": clean_text or ""}
                if self.native_tools:
                    asst["tool_calls"] = tool_calls
                self.current_msgs.append(asst)

                # ── 并行委派（对齐 DSH 并行池 / Codex max_threads）──────────
                # 同一轮全部是 delegate_task 且 ≥2 个 → 并发执行：每个委派是
                # 独立 ThinAgentLoop（本地引擎串行排队、云端真并行），结果按
                # 调用顺序回灌。混有其他工具时维持串行（保守）。
                _all_delegate = bool(tool_calls) and all(
                    (tc.get("function") or {}).get("name") == "delegate_task"
                    for tc in tool_calls)
                if _all_delegate and len(tool_calls) >= 2:
                    import asyncio as _aio
                    import uuid as _uuid
                    from agent.subagent import _CURRENT_PARENT_SESSION as _cps
                    _cps.set(self.session_id)
                    for tc in tool_calls:
                        if not tc.get("id"):
                            tc["id"] = str(_uuid.uuid4())

                    async def _run_delegate(tc):
                        targs = json.loads(tc.get("function", {}).get("arguments", "{}") or "{}")
                        from agent.subagent import _delegate_task_fg, _delegate_task_bg
                        if targs.get("background"):
                            return await _delegate_task_bg(
                                targs.get("agent", "code-reviewer"), targs.get("task", ""))
                        return await _delegate_task_fg(
                            targs.get("agent", "code-reviewer"), targs.get("task", ""))

                    self._step_log("并行委派", f"{len(tool_calls)} 个子代理并发启动")
                    _outs = await _aio.gather(*[_run_delegate(tc) for tc in tool_calls])
                    for tc, out in zip(tool_calls, _outs):
                        self.current_msgs.append({"role": "tool",
                                                  "tool_call_id": tc.get("id"),
                                                  "content": out})
                    yield {"content": "\n\n".join(
                        f"🧩 子代理 {i+1}/{len(_outs)}：{(out or '').strip()[:400]}"
                        for i, out in enumerate(_outs))}
                    self._step_log("并行委派完成", f"{len(_outs)} 个结果已回灌")
                    continue

                # ── 安全只读工具同轮并行（09-07 提速实验）────────────────
                # 本轮全部是并发安全只读工具且 ≥2 个 → asyncio.gather 并发，
                # 事件按调用顺序回灌（结果消息各自带 tool_call_id，顺序无关）。
                # 混有写/控制工具 → 维持串行。
                _SAFE_PARALLEL_TOOLS = {
                    "mx_query", "ak_finance", "tavily_search", "bing_search",
                    "dokobot_search",
                    "read_file", "list_dir", "search_files",
                }
                _all_safe = bool(tool_calls) and all(
                    (tc.get("function") or {}).get("name", "") in _SAFE_PARALLEL_TOOLS
                    for tc in tool_calls)
                if _all_safe and len(tool_calls) >= 2:
                    import asyncio as _aio2
                    self._step_log("并行工具", f"{len(tool_calls)} 个只读工具并发执行")
                    _safe_results = await _aio2.gather(*[
                        _handle_tool_execution(
                            tc, self.current_msgs, self.session_id, "latiao",
                            self.access_mode)
                        for tc in tool_calls
                    ])
                    for _vf, _evts in _safe_results:
                        for evt in _evts:
                            yield evt
                    for _vf, _evts in _safe_results:
                        for _e in _evts:
                            if isinstance(_e, dict) and "result" in _e:
                                self._maybe_quota_hint(str(_e.get("result")))
                    # mx_query 成功 → 清除当日限额标记（换 Key 后自纠正）
                    for _tc, (_vf, _evts) in zip(tool_calls, _safe_results):
                        if ((_tc.get("function") or {}).get("name") == "mx_query"):
                            _rr = next((str(e.get("result", "")) for e in _evts
                                        if isinstance(e, dict) and "result" in e), "")
                            if "Error" not in _rr:
                                _clear_quota_marker("mx_query")
                    self._maybe_repeat_reminder(tool_calls)
                    _gate = await self._stagnation_gate(tool_calls)
                    if _gate is not None:
                        yield _gate
                        return
                    continue

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
                    self._maybe_quota_hint(_res)
                    if tname == "mx_query" and "Error" not in _res:
                        _clear_quota_marker("mx_query")
                    self._step_log("工具结果", f"{tname} → {len(_res)}字符 "
                                               f"摘要: {_res[:80]!r}")
                    for evt in events:
                        yield evt
                    # use_skill 联动：加载技能时，该技能声明的依赖工具并入
                    # 会话集合（技能承诺的能力必须在场——mx-data → mx_query）
                    if tname == "use_skill":
                        try:
                            _sn = json.loads(tc.get("function", {}).get("arguments", "{}") or "{}").get("skill_name", "")
                            mark_session_tools(self.session_id, SKILL_DEPENDENT_TOOLS.get(_sn, set()))
                        except Exception:
                            pass
                self._maybe_repeat_reminder(tool_calls)
                _gate = await self._stagnation_gate(tool_calls)
                if _gate is not None:
                    yield _gate
                    return

            yield {"content": f"\n\n⚠️ 已达安全步数上限（{MAX_STEPS}）。请发送新消息继续。"}
