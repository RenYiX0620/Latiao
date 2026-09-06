"""弱模型辅助层与语言链：交付闸门、终答提取、翻译、规划识别、本地工具提示词。"""
import asyncio
import json
import re
import time
import logging

import httpx

from agent.transport import _is_local_llm_url, _local_llm_serialized
from agent.context import _detect_user_language, _get_localized_text
from agent.text_quality import _strip_think_fences

logger = logging.getLogger("latiao-sidecar")


_PENDING_INTENT_PATTERNS = (
    "改用", "我再单独查", "我单独查", "我改", "我要调用",
    "我马上", "我这就", "我先查", "我重新查",
    "让我用", "我来用", "让我读取", "让我先", "让我再",
    "我先做", "先看看",
)


# 规划话术信号：模型把"我打算做什么"当成最终回答发出来（13:58 事故：
# 891 字符英文规划 "Let me plan the subtasks: 1…4" 被收尾闸门当完整答案
# 放行，任务半途而停）。≥2 个信号才算规划——真实回答里带一个
# "下一步/step" 不应被误拦（17:07 重复堆叠事故的教训）。
_PLANNING_SIGNALS = (
    "tool", "工具", "读取", "查询", "搜索", "执行", "调用",
    "read_file", "list_dir", "run_cmd", "write_file",
    "search", "tavily", "mx_query", "step", "步骤", "下一步",
    "let me", "i'll", "i will", "让我", "我先", "接下来",
    "再分析", "稍后", "马上", "look at", "check the",
    "i need to", "let's", "let me plan", "my plan", "subtask",
    "according to the rules", "first, i", "let's get started",
    "get started", "plan the", "then i", "after that",
)


def _looks_like_planning(text: str) -> bool:
    """判断回复文本是否只是"计划/声明"而非实质回答。≥2 个规划信号才成立。"""
    if not text or len(text.strip()) < 10:
        return False
    low = text.lower()
    return sum(1 for sig in _PLANNING_SIGNALS if sig in low) >= 2


def _strip_nonprose_for_lang(t: str) -> str:
    """语言计数前剥离非散文 token：URL、行内代码、路径、文件名。

    09-06 13:27 事故：桌面文件清单的英文件名（.DS_Store、
    10Eros_T2V_Simple.json…）把纯中文答案的字母计数拉到 en>zh，
    误判成英文回复 → 强制翻译（返回原文）→ 语言修正轮重跑全模型，
    正确答案被扔掉、180s 看门狗超时。文件名/路径/代码是数据不是语言。"""
    t = re.sub(r"https?://\S+|www\.\S+", " ", t)            # URL
    t = re.sub(r"`[^`\n]*`", " ", t)                        # 行内代码
    t = re.sub(r"(?<![\w])[\w.\-]*[\w\-]/[\w.\-/]{1,}", " ", t)   # 路径
    t = re.sub(r"[\w.\-]+\.[A-Za-z0-9]{1,5}\b", " ", t)     # 文件名（含扩展名）
    return t


def _reply_lang_mismatch(user_text: str, reply_text: str) -> bool:
    """回复语言与用户语言明显不符（中文用户收到英文/英文占优回复）→ True。

    判定：回复中用户语言的字符数，远少于外来语言字母数（英文占优）。
    891 字符英文规划（0 汉字）命中；14:45 重放中 598 字母 vs 42 汉字 的
    混合英文回答命中；20:09 重放中 440 字母 vs 170 汉字（英文主体+中文
    股票名镶入）也命中（en>zh 即判，不要求 3 倍——此前 3 倍阈值放过 2.6 倍
    的漏网）；"NVIDIA涨5%"（字母 6 < 80）与中文为主的正常回答（汉字多于
    字母）不误伤。计数前剥离文件名/路径/代码（_strip_nonprose_for_lang）。"""
    user_lang = _detect_user_language(user_text)
    if not reply_text:
        return False
    # 尾部段落窗口（09-21 实测）：nudge 轮的英文尾巴以段落为单位接在长中文
    # 正文后，全文判定（中文多、英文<80字母）会漏——对最后 2 段单独判定。
    def _lang_counts(t: str):
        t = _strip_nonprose_for_lang(t)
        return (len(re.findall(r'[\u4e00-\u9fff]', t)),
                len(re.findall(r'[\u3040-\u309f\u30a0-\u30ff]', t)),
                len(re.findall(r'[a-zA-Z]', t)))
    paras = [p for p in re.split(r'\n\s*\n', reply_text) if p.strip()]
    tail_parts = paras[-2:] if len(paras) >= 2 else paras
    tail_text = "\n".join(tail_parts)
    tzh, tkana, ten = _lang_counts(tail_text)
    tail_en_heavy = ten >= 40 and ten > tzh + tkana
    _full = _strip_nonprose_for_lang(reply_text)
    zh = len(re.findall(r'[\u4e00-\u9fff]', _full))
    ja_kana = len(re.findall(r'[\u3040-\u309f\u30a0-\u30ff]', _full))
    en = len(re.findall(r'[a-zA-Z]', _full))
    if user_lang == "zh":
        return (en >= 80 and en > zh) or tail_en_heavy
    if user_lang == "ja":
        return (en >= 80 and en > (zh + ja_kana)) or tail_en_heavy
    if user_lang == "en":
        other = zh + ja_kana
        return other >= 80 and other > en
    return False


# 允许重复调用的工具（持续监测类），防重复护栏对它们不生效
async def _final_answer_extraction(client, api_url: str, headers: dict, engine_model: str,
                                   current_msgs: list, user_lang: str) -> str:
    """非流式单轮"终答提取"：基于已有资料强制直接输出最终回答。

    本地 27B 模型被自身思维链卡住、只声明不动手时的强制收口（09-02 09:56、
    17:21 事故）。不带工具、单一指令，产出由调用方判断是否交付；失败返回空串。

    两个此前静默失败的原因（09-05 13:06 事故深度排查）：
    1) system 消息必须放开头——mlx 引擎校验"System message must be at the
       beginning"，放末尾直接 404/报错，提取永远返回空；
    2) stop 里不能有 "</think>"——本模型思考以 </think> 收尾，该 stop 词把
       生成掐断在思考结束处，正文（真正的分析）永远生成不出来（实测去掉后
       一次产出 1548 字完整分析）。
    消息只保留最后一条 user + 其后 tool 结果（剔除 model 自己的规划/元叙述
    消息，防止模型接着"角色扮演工具"而非写分析）。"""
    try:
        _name = {"zh": "简体中文", "en": "English", "ja": "日本語"}.get(user_lang, "简体中文")
        _smsgs: list = []
        _ua = -1
        for _i, _m in enumerate(current_msgs):
            if _m.get("role") == "user":
                _ua = _i
        if _ua >= 0:
            _smsgs.append(current_msgs[_ua])
            for _m in current_msgs[_ua + 1:]:
                if _m.get("role") in ("tool", "tool_result"):
                    _smsgs.append(_m)
        _smsgs = [{"role": "system", "content":
            f"任务收尾：用户还在等待回答。请基于上方用户消息里的内容（以及工具结果）直接写出最终回答"
            f"（必须用{_name}，包含关键数字与结论）。不要规划、不要提及任何工具/命令/脚本/执行过程，"
            f"不要写'让我…''我先…'。限制思考，把分析直接写进回答正文。"}] + _smsgs
        _sb = {"model": engine_model, "messages": _smsgs, "max_tokens": 4096,
               "stream": False, "temperature": 0.4,
               "stop": ["<|im_end|>", "<eos>"]}
        # 引擎忙（主循环持有 serialized 锁）时快退——终答提取只是兜底，
        # 等锁/读等满会拖死收尾（09-21 22:58 实测 ReadTimeout 2 分钟）
        try:
            async with asyncio.timeout(20):
                async with _local_llm_serialized(api_url):
                    _sr = await client.post(api_url, json=_sb, headers=headers)
        except TimeoutError:
            logger.info("终答提取跳过：引擎正忙（serialized 锁占用）")
            return ""
        if _sr.status_code == 200:
            return ((_sr.json().get("choices") or [{}])[0]
                    .get("message", {}).get("content", "") or "").strip()
    except Exception:
        logger.warning("终答提取失败", exc_info=True)
    return ""


_LANG_RETRY_HINT = "⚠️ 模型本次生成了英文回复（翻译暂不可用）。"


async def _ensure_final_language(client, api_url: str, headers: dict, engine_model: str,
                                 text: str, user_text: str) -> str:
    """交付前语言确保：回复语言与用户消息不符时走翻译轮，返回可交付文本。

    缓冲交付后所有 return 路径统一经过这里——即使收尾闸门被跳过
    （如工具失败分支），英文也不会原样到达用户（16:55 事故）。
    翻译轮失败时返回短提示 _LANG_RETRY_HINT 并记日志（09-21 实测：此前把
    整段英文"原文如下"贴给用户——改为短提示，各调用点据此触发语言修正轮）。"""
    if text and _reply_lang_mismatch(user_text, text):
        translated = await _force_translate(client, api_url, headers, engine_model, text,
                                            _detect_user_language(user_text))
        if translated == text:
            logger.warning("语言确保失败（翻译返回原文），触发语言修正轮: %.200s",
                           text.replace("\n", " "))
            return _LANG_RETRY_HINT
        return translated
    return text


async def _ensure_final_language_with_retry(client, api_url: str, headers: dict,
                                            engine_model: str, text: str, user_text: str,
                                            msgs: list, *, lang_retry_done: bool) -> tuple[str, bool]:
    """语言确保 + 语言修正轮（一次性）：翻译失败返回短提示时注入重写指令。

    返回 (deliver_text, retry_now)。retry_now=True 且未做过修正轮时，调用方
    应继续下一轮（注入的提醒已在 msgs 中）；否则以短提示交付。"""
    deliver = await _ensure_final_language(client, api_url, headers, engine_model,
                                           text, user_text)
    if deliver.startswith(_LANG_RETRY_HINT) and not lang_retry_done:
        msgs.append({"role": "system", "content":
                     "必须用简体中文重新输出正文（不要英文，不要解释，直接重写完整回答）。"})
        return deliver, True
    return deliver, False


async def _force_translate(client, api_url: str, headers: dict, engine_model: str,
                           text: str, user_lang: str) -> str:
    """一轮强制翻译：把模型回复翻译成用户语言（本地引擎非流式单轮）。

    模型对翻译任务的执行远比"用某语言重新分析"稳定——语言兜底的最后一公里
    （09-03 事故：两轮中文规则+3 次 nudge 后 27B 模型仍输出英文）。失败时
    返回原文（不阻断交付）。"""
    lang_name = {"zh": "简体中文", "en": "English", "ja": "日本語"}.get(user_lang, "简体中文")
    _tmsgs = [
        {"role": "system",
         "content": (f"你是翻译器。把用户提供的文本完整翻译成{lang_name}，"
                     "直接输出译文。不要调用工具，不要输出任何解释、注释或前后缀。")},
        {"role": "user", "content": text[:6000]},
    ]
    _tb = {"model": engine_model, "messages": _tmsgs, "max_tokens": 4096,
           "stream": False, "temperature": 0.2, "stop": ["<|im_end|>", "<eos>"]}
    # 引擎长流刚结束时偶发连接重置（17:17 实测 608ms 内 read 失败）——重试一次
    for _attempt in range(2):
        try:
            async with _local_llm_serialized(api_url):
                _tr = await client.post(api_url, json=_tb, headers=headers)
            if _tr.status_code == 200:
                out = ((_tr.json().get("choices") or [{}])[0]
                       .get("message", {}).get("content", "") or "").strip()
                if out and len(out) >= 40:
                    return _strip_think_fences(out)
            return text
        except Exception:
            if _attempt == 0:
                await asyncio.sleep(2)
                continue
            logger.warning("强制翻译轮失败，返回原文", exc_info=True)
    return text


def _build_local_tools_prompt(active_tools: list[dict]) -> str:
    """Build a concise tool prompt for local models with strong few-shot examples."""
    lines = ["# 可用工具\n"]
    lines.append("你可以使用以下工具来完成任务。不需要工具时直接回复用户。\n")
    for t in active_tools:
        fn = t.get("function", {})
        name = fn.get("name", "")
        desc = fn.get("description", "")
        params = fn.get("parameters", {}).get("properties", {})
        param_hints = ""
        if params:
            required = fn.get("parameters", {}).get("required", [])
            parts = []
            for pk, pv in params.items():
                req = "*" if pk in required else ""
                ptype = pv.get("type", "string")
                if ptype == "string":
                    ptype = "str"
                elif ptype == "integer":
                    ptype = "int"
                elif ptype == "boolean":
                    ptype = "bool"
                parts.append(f'{pk}{req}: {ptype}')
            param_hints = "(" + ", ".join(parts) + ")"
        lines.append(f"- {name}{param_hints}: {desc}")

    lines.append("\n# 调用格式\n")
    lines.append("调用工具时，必须严格使用以下格式：\n")
    lines.append("```tool 工具名")
    lines.append('{"参数名": "参数值"}')
    lines.append("```")
    lines.append("")
    lines.append("# 示例\n")
    lines.append("用户：帮我看看当前目录有什么文件")
    lines.append("助手：```tool list_dir")
    lines.append('{"path": "."}')
    lines.append("```")
    lines.append("")
    lines.append("用户：搜索今天A股行情")
    lines.append("助手：```tool tavily_search")
    lines.append('{"query": "今天A股大盘走势 上证指数"}')
    lines.append("```")
    lines.append("")
    lines.append("用户：读取 main.py 的内容")
    lines.append("助手：```tool read_file")
    lines.append('{"path": "main.py"}')
    lines.append("```")
    lines.append("")
    lines.append("重要规则：")
    lines.append("1. 每次只调用一个工具")
    lines.append("2. 必须用 ```tool 代码块格式，不要用其他格式")
    lines.append("3. 参数必须是合法 JSON")
    lines.append("4. 等待工具结果后再决定下一步")
    lines.append("5. 不要在 ```tool 块外面写工具调用")
    lines.append("")
    lines.append("⚠️ 强制要求：当用户的问题需要搜索、读取文件、执行命令时，你必须使用工具。")
    lines.append("不可以用文字描述来代替工具调用。直接写出 ```tool 代码块。")
    lines.append("")
    return "\n".join(lines)


