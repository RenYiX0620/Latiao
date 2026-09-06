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
        # 辅助 2：语言交付闸门（本地引擎英文漂移是高频真实问题）
        if _reply_lang_mismatch(payload["user_text"], text):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(60)) as c2:
                    translated = await _force_translate(
                        c2, payload["api_url"], payload["headers"], payload["engine_model"],
                        text, payload["user_lang"])
                if translated and translated != text:
                    events.append({"content": "\n\n" + translated})
                    payload["handled"] = True
                    return payload
            except Exception:
                logger.debug("语言闸门翻译失败，按原文交付", exc_info=True)
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
        payload["pre_events"].append(started["event"])
        payload["plan_wait"] = {"plan_id": plan_id,
                                "event_obj": started["event_obj"], "plan": plan}
        return payload

    scope.waterfall("pre_step").register(pre_step_hook, order=5, name="planning")


# ══ compaction：上下文压缩（pre_step 钩子）══════════════════════════
def setup_compaction(scope, *, local_threshold=30000, cloud_threshold=80000):
    """字符总量超阈值 → 裁剪历史（保 system + 最近轮次）。

    阈值压缩（Codex 式摘要压缩）的过渡实现：先裁剪防溢出，摘要化后续。"""
    async def pre_step_hook(payload, ctx):
        loop = ctx.get_service("loop") if ctx is not None else None
        if loop is None:
            return payload
        msgs = loop.current_msgs
        total = sum(len(str(m.get("content") or "")) for m in msgs)
        threshold = local_threshold if loop.is_local else cloud_threshold
        if total <= threshold:
            return payload
        if loop.is_local:
            loop.current_msgs = _slim_history_for_local(msgs)
            logger.info("compaction: 本地历史 %d 字符超阈值，已裁剪", total)
        else:
            sys = [m for m in msgs if m.get("role") == "system"]
            rest = [m for m in msgs if m.get("role") != "system"]
            loop.current_msgs = sys + rest[-8:]
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
