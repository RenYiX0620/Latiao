"""内置插件：薄循环的既有能力表达为 scope 注册（fastpath/assists/planning/compaction）。

每个插件 = setup(scope) -> None，向容器注册 waterfall 钩子。
启用条件在钩子内部自检（引擎能力/档位），保持 Scope 通用。
"""
import logging
import uuid

import httpx

from agent.context import (
    _is_light_query,
    _normalize_access,
    _slim_history_for_local,
)
from agent.gates import _final_answer_extraction, _force_translate, _reply_lang_mismatch

logger = logging.getLogger("latiao-sidecar")


# ══ fastpath：闲聊快车道（request 钩子）══════════════════════════════
def setup_fastpath(scope):
    """本地引擎 + 闲聊短消息：不挂工具、关思考——秒级直达（27B 实测 1.3s）。"""
    async def request_hook(body, ctx):
        loop = ctx.get_service("loop") if ctx is not None else None
        if loop is None or not getattr(loop, "is_local", False):
            return body
        if not _is_light_query(loop.last_user_text, loop.current_msgs):
            return body
        body.pop("tools", None)
        body["chat_template_kwargs"] = {"enable_thinking": False}
        return body

    scope.waterfall("request").register(request_hook, order=10, name="fastpath")


# ══ assists：弱模型辅助（deliver 钩子，仅本地引擎）══════════════════
def setup_assists(scope):
    """仅本地引擎启用：思考-only 终答提取、语言交付闸门、空响应诊断。

    云端前沿模型零干预（Codex 语义：信任模型）。"""
    async def deliver_hook(payload, ctx):
        events = payload["events"]
        text = (payload["text"] or "").strip()
        is_local = payload["is_local"]
        # 辅助 1：思考-only → 终答提取（27B 推理模型典型故障形态）
        if not text and payload["streamed"].strip() and is_local:
            final = await _final_answer_extraction(
                payload["client"], payload["api_url"], payload["headers"],
                payload["engine_model"], payload["current_msgs"], payload["user_lang"])
            if len(final) >= 120:
                events.append({"content": "\n\n" + final})
                payload["handled"] = True
                return payload
            events.append({"content": ("\n\n⚠️ 本地模型本轮只输出了思考过程。"
                                       "请回复「继续」重试，或换用其他模型。")})
            payload["handled"] = True
            return payload
        # 空响应诊断
        if not text:
            events.append({"content": ("\n\n⚠️ 模型返回了空响应。可能原因：上下文超限被截断、"
                                       "模型不支持当前请求格式。建议换用更大的模型或重试。")})
            payload["handled"] = True
            return payload
        # 辅助 2：语言交付闸门（本地引擎英文漂移）——正文已实时流出，
        # 用 content_revised 整体替换（前端把最后一条回答替换为译文）
        if _reply_lang_mismatch(payload["user_text"], text):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(60)) as c2:
                    translated = await _force_translate(
                        c2, payload["api_url"], payload["headers"], payload["engine_model"],
                        text, payload["user_lang"])
                if translated and translated != text:
                    events.append({"event": "content_revised", "content": translated})
                    payload["handled"] = True
                    return payload
            except Exception:
                logger.debug("语言闸门翻译失败，按已流出原文交付", exc_info=True)
        return payload

    async def guard(payload, ctx):
        if not payload.get("is_local"):
            return payload  # 云端前沿模型零干预
        return await deliver_hook(payload, ctx)

    scope.waterfall("deliver").register(guard, order=10, name="assists")


# ══ planning：规划模式门（pre_step 钩子，access_mode=plan 时）════════
def setup_planning(scope):
    """plan 档：先产计划 → 用户确认 → 循环注入已批准计划再执行。

    契约：payload["plan_wait"]={"plan_id","event_obj","plan"} 由循环执行
    yield+await（生成器语义，钩子不能 yield）。"""
    async def pre_step_hook(payload, ctx):
        loop = ctx.get_service("loop") if ctx is not None else None
        if loop is None:
            return payload
        if _normalize_access(loop.access_mode) != "plan" or loop.steps > 1:
            return payload
        if getattr(loop, "_plan_injected", False):
            return payload
        from agent_loop import _generate_plan, _start_plan_confirmation
        plan = await _generate_plan(loop.last_user_text, loop.model,
                                    loop.api_url, loop.headers, loop._client)
        if not plan:
            payload["reject"] = True
            payload["pre_events"].append({"content": (
                "\n\n⚠️ 计划模式：计划生成失败，任务未执行。请重试或切换到其他模式。")})
            return payload
        plan_id = f"plan_{uuid.uuid4()}"
        started = await _start_plan_confirmation(plan_id, plan)
        payload["pre_events"].append({"event": "agent_plan", "content": plan})
        payload["pre_events"].append(started["event"])
        payload["plan_wait"] = {"plan_id": plan_id,
                                "event_obj": started["event_obj"], "plan": plan}
        return payload

    scope.waterfall("pre_step").register(pre_step_hook, order=5, name="planning")


# ══ compaction：上下文压缩（pre_step 钩子）══════════════════════════
def _collapse_duplicate_assistants(msgs: list) -> list:
    """完全相同的 assistant 声明折叠为一条（结构性去重，不猜内容）。

    隔着短 user 续聊（"继续"）的重复声明同样折叠——复读事故的真实形态就是
    「声明 → 继续 → 同一声明 → 继续 → …」。遇到长 user 消息或工具轮才重置。"""
    out: list = []
    last_content = None
    foldable = False
    for m in msgs:
        role = m.get("role")
        if role == "assistant":
            c = str(m.get("content") or "").strip()
            if m.get("tool_calls"):
                last_content = None
                foldable = False
                out.append(m)
                continue
            if c and c == last_content and foldable:
                out.append({**m, "content": "（同一声明已重复，已折叠）"})
                continue
            last_content = c
            foldable = True
            out.append(m)
            continue
        if role == "user":
            c = str(m.get("content") or "").strip()
            foldable = foldable and 0 < len(c) <= 10  # 短续聊不打断；长消息重置
            out.append(m)
            continue
        out.append(m)
    return out


def setup_compaction(scope, *, local_threshold=18000, cloud_threshold=80000):
    """字符总量超阈值 → 裁剪历史（保 system + 最近轮次）+ 轮内工具结果链压缩。

    09-07 提速实验：单轮多工具任务（如板块分析 9 次查询）中，工具结果链
    永不被跨轮裁剪覆盖 → 请求 27K→33K 每轮膨胀，prefill 占每轮耗时 80%。
    降阈值 + 轮内裁剪 + 滞回（压完留出增长空间，避免每轮空转触发破坏
    mlx 前缀缓存）。"""
    async def pre_step_hook(payload, ctx):
        loop = ctx.get_service("loop") if ctx is not None else None
        if loop is None:
            return payload
        if getattr(loop, "_finalize_round", False):
            # 停滞闸门收口轮：只做结构折叠，跳过数据压缩——压缩会截早/历轮
            # 工具结果，终答需要原始数据（09-08 14:29 事故链路）
            msgs = _collapse_duplicate_assistants(loop.current_msgs)
            loop.current_msgs = msgs
            return payload
        msgs = loop.current_msgs
        total = sum(len(str(m.get("content") or "")) for m in msgs)
        threshold = local_threshold if loop.is_local else cloud_threshold
        # 连续重复声明折叠：始终运行（结构去重，与大小无关）——复读轮次留在
        # 历史里的重复声明是复读吸引子（09-07 22:2x 事故：17K 字符未达阈值，
        # 折叠被跳过 → 毒历史每轮把模型拖回复读）
        msgs = _collapse_duplicate_assistants(msgs)
        loop.current_msgs = msgs
        total = sum(len(str(m.get("content") or "")) for m in msgs)
        if total <= threshold:
            return payload
        # 滞回：距上次压缩增长不足 2000 字符 → 没有实质新增，跳过（防每轮
        # 空转触发改写历史、打掉 mlx 前缀缓存）
        last = getattr(loop, "_last_compact_total", 0)
        if last and total - last < 2000:
            return payload
        if loop.is_local:
            msgs = _slim_history_for_local(msgs)
            # 轮内压缩：当前 turn 的工具结果链——保留最近 2 条完整，更早的
            # 压到 400 字符（头 300 + 尾 100）；中间轮 <50 字的过渡正文清空
            # （assistant 的 tool_calls 字段原样保留，模板兼容）。
            tool_idx = [i for i, m in enumerate(msgs) if m.get("role") == "tool"]
            for i in tool_idx[:-2]:
                c = str(msgs[i].get("content") or "")
                if len(c) > 420:
                    msgs[i] = {**msgs[i], "content": c[:300] + "\n…(轮内已压缩)…" + c[-100:]}
            last_asst = max((i for i, m in enumerate(msgs) if m.get("role") == "assistant"), default=-1)
            for i, m in enumerate(msgs):
                if m.get("role") == "assistant" and i != last_asst:
                    c = str(m.get("content") or "")
                    if m.get("tool_calls"):
                        continue  # native 工具轮消息保持原样（与 tool 结果配对）
                    if 0 < len(c.strip()) < 50:
                        msgs[i] = {**m, "content": ""}
            # 连续完全相同的 assistant 声明折叠为一条（复读轮次留进历史的
            # 毒数据——历史里的重复模式是复读吸引子，结构去重不断供）
            _seen_sig = None
            for i, m in enumerate(msgs):
                if m.get("role") != "assistant" or m.get("tool_calls"):
                    _seen_sig = None
                    continue
                c = str(m.get("content") or "").strip()
                if not c:
                    continue
                if c == _seen_sig:
                    msgs[i] = {**m, "content": f"（与前一条相同的声明，已出现多次）"}
                else:
                    _seen_sig = c
            loop.current_msgs = msgs
            loop._last_compact_total = sum(len(str(m.get("content") or "")) for m in msgs)
            logger.info("compaction: 本地历史 %d → %d 字符（含轮内压缩）", total, loop._last_compact_total)
        else:
            sys = [m for m in msgs if m.get("role") == "system"]
            rest = [m for m in msgs if m.get("role") != "system"]
            loop.current_msgs = sys + rest[-8:]
            loop._last_compact_total = 0
            logger.info("compaction: 云端历史 %d 字符超阈值，保留最近 8 条", total)
        return payload

    scope.waterfall("pre_step").register(pre_step_hook, order=20, name="compaction")


BUILTIN_PLUGINS = {
    "fastpath": setup_fastpath,
    "assists": setup_assists,
    "planning": setup_planning,
    "compaction": setup_compaction,
}


def setup_all(scope, names=None):
    for name, setup in BUILTIN_PLUGINS.items():
        if names is None or name in names:
            setup(scope)
