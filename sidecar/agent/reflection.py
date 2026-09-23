"""计划与反思（agent_loop.py 拆出的第六块，2026-09-23）。

- _generate_plan：计划模式下让模型先产出执行计划（plan 确认流的上游）；
- _reflect_output：交付前的反思清单与"未验证数字"扫描（数据诚实性守卫）。
两者都是"对模型输出的二次加工"，与工具/会话簿记无关。
"""
import logging

import httpx

logger = logging.getLogger("latiao-sidecar")   # 与 agent_loop 同名：日志格式不变

async def _generate_plan(user_text: str, model: str, api_url: str, headers: dict,
                         client: httpx.AsyncClient) -> str:
    """生成执行计划（3-8 步编号列表）。失败返回空串（降级为普通执行）。"""
    sys_prompt = (
        "你是任务规划器。用户给了一个复杂任务，请输出一份简洁、可执行的计划。\n"
        "要求：\n"
        "1. 用编号列表列出 3-8 个步骤\n"
        "2. 每步说明具体要做什么（可提及将使用的工具，如查询行情、读取文件、运行命令、生成报告）\n"
        "3. 步骤具体可执行，不要空话，不要重复用户原文\n"
        "4. 只输出计划本身，不要任何前后缀说明"
    )
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_text},
        ],
        "max_tokens": 1024,
        "stream": False,
        "temperature": 0.3,
    }
    try:
        resp = await client.post(api_url, json=body, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        plan = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
        return plan.strip()
    except Exception as e:
        logger.warning("Plan generation failed (fallback to direct execution): %s", e)
        return ""

async def _reflect_output(text: str, model: str, api_url: str, headers: dict,
                          mode: str, client: httpx.AsyncClient,
                          tool_outputs: list[str] | None = None) -> tuple[str, bool]:
    """对最终文本做一轮（light）或两轮（deep）自查反思。
    返回 (最终文本, 是否有修正)。有修正时前端替换最后一条消息。"""
    checklist = _REFLECT_CHECKLISTS.get(mode, _REFLECT_CHECKLISTS["light"])
    rounds = 2 if mode == "deep" else 1
    current = text
    changed = False
    # 机制化溯源核查：报告里的数字若在本会话工具查询结果中找不到来源，
    # 列出供反思模型逐项核实（不硬删，交给模型判断口径）
    unverified = _find_unverified_numbers(current, tool_outputs or [])
    unverified_note = ""
    if unverified:
        unverified_note = (
            "\n\n⚠️ 数字溯源核查：以下数字在本会话的**工具查询结果中未找到来源**，"
            "请逐项处理：\n"
            + "\n".join(f"- {n}" for n in unverified[:15])
            + "\n处理规则：属于查询数据（可能因口径/表述不同而未匹配）→ 保留；"
              "属于宏观/外部数据且本次**没有查询过** → 删除该数字或改为不带具体数字的定性描述。"
        )
    for _ in range(rounds):
        sys_prompt = (
            "你是输出质检员。检查下面这份回答，严格按清单逐项核对。\n"
            f"检查清单：\n{checklist}{unverified_note}\n\n"
            "规则：\n"
            "- 如果发现实质问题（数据错误、遗漏关键结论、自相矛盾、格式损坏、明显不完整），"
            "输出修正后的完整版本。\n"
            "- 如果没有问题，**原样输出原文**，不要添加任何说明。\n"
            "- 只输出最终版本本身，不要输出检查过程、不要加任何前缀。"
        )
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": current},
            ],
            "max_tokens": max(2048, len(current) + 2000),
            "stream": False,
            "temperature": 0.2,
        }
        try:
            resp = await client.post(api_url, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            revised = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
            revised = revised.strip()
            if revised and revised != current:
                current = revised
                changed = True
        except Exception as e:
            logger.warning("Reflection failed (keep original): %s", e)
            break
    return current, changed

_REFLECT_CHECKLISTS = {
    "light": (
        "1. 事实/数据与提供的上下文一致，没有编造数字\n"
        "2. 结构完整，有明确的结论\n"
        "3. 没有明显截断、乱码或格式损坏"
    ),
    "deep": (
        "1. 事实/数据与提供的上下文一致，没有编造数字\n"
        "2. 逻辑自洽，前后不矛盾\n"
        "3. 结论完整，回应了用户的所有诉求\n"
        "4. 建议/步骤可执行、无歧义\n"
        "5. 语言通顺，格式规范\n"
        "6. 篇幅合适，不啰嗦也不过于简略"
    ),
}

def _find_unverified_numbers(text: str, tool_outputs: list[str]) -> list[str]:
    # 依赖仍留在 agent_loop 的定义 → 函数内惰性导入（避免循环依赖）
    from agent_loop import _extract_numbers
    """报告中出现的、但未能在本次工具查询结果中找到来源的数字。
    用于反思环节逐项核实——机制化防编造，不依赖模型自觉。"""
    haystack = "\n".join(tool_outputs)
    return [n for n in _extract_numbers(text) if n not in haystack]
