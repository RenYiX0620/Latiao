"""系统提示词组装（_build_chat_messages）——从 agent_loop.py 拆出的第二块（2026-09-23）。

为什么单列：这是全项目事故最集中的一段（薄循环重构时曾漏掉过跨会话知识注入，
见 agent/loop.py 的注释化石），而它本身是**纯组装**：读身份/偏好/技能/进度，
拼成一条 system 消息，不碰网络也不写盘。单列后可独立测试这些注入契约
（tests/test_language.py、test_onboarding.py、test_context_stats.py 都在打它）。

边界说明：`AGENT_PROFILES` 归 agent_loop（枢纽）所有，这里**不复制**，
只在调用时惰性取一次——复制会产生两份会漂移的数据（审计点名的"静默漂移"）。
"""
import logging
import os
import platform
import re
from datetime import datetime
from pathlib import Path

from agent.context import (
    _extract_last_user_text,
    _get_localized_text,
    _inject_image,
    _is_chat_query,
    detect_language_decision,
)
from agent.progress import _clean_progress_tail, _progress_tail
from agent.transport import _safe_cwd
from identity import _process_identity_intents, _read_identity
from memory import _get_high_confidence_preferences
from onboarding import process_message as _process_onboarding

logger = logging.getLogger("latiao-sidecar")   # 与 agent_loop 同名：日志格式不变

# 跨会话进展（PROGRESS）的注入节奏：首轮 + 每 N 个用户轮一次（2026-09-23）。
# 1 = 每轮都注入。数字越大越省 token，但长会话里"我之前干过什么"的记忆越淡。
_PROGRESS_INJECT_EVERY_TURNS = 6   # 与 agent_loop 同名：日志格式不变


PROGRESSIVE_DELIVERY_PROMPT = """
## 渐进式交付协议（必须遵守）

将每个任务拆为 3 个最小可验证单元，逐步交付：

**阶段 1 — 骨架**：仅生成接口定义、类型声明、空函数体。不实现逻辑。
**阶段 2 — 核心**：填充核心逻辑，跳过边界处理和异常分支。
**阶段 3 — 完善**：补充异常处理、边界检查、注释、测试用例。

每阶段 token 预算 ≤ 上下文窗口 30%。完成一阶段后明确报告进度，再进入下一阶段。
禁止单次输出完整功能——会被截断且质量下降。
"""

GOAL_MODE_PROMPT = """
## 目标导向模式

你收到的是一个**目标**而非指令。你需要：
1. 分析目标 → 拆解为可执行步骤
2. 按渐进式交付协议逐步执行
3. 遇到阻塞主动报告，不强行推进
4. 每完成一步报告进度

用户只关心目标是否达成，不关心你用什么工具。
"""

def _build_chat_messages(body: dict, messages: list) -> list:
    # AGENT_PROFILES 归 agent_loop（枢纽）所有；此处惰性取用，避免复制第二份数据
    from agent_loop import _get_agent_config
    """Assemble the full message array with identity, env, skills, agent, and image injections.
    All system prompts are merged into ONE message to work around a llama-cpp bug
    where multiple system messages cause empty responses."""
    # 技能目录由 capability_registry 提供（统一能力模型）→ lazy import 避免循环依赖
    import capability_registry
    last_user_text = _extract_last_user_text(messages)
    # 语言真值：本回合只算一次（含历史交叉校验），下方所有语言相关注入统一取用
    _hist = [m.get("content") for m in messages
             if m.get("role") == "user" and isinstance(m.get("content"), str)]
    if _hist and _hist[-1] == last_user_text:
        _hist = _hist[:-1]
    _lang, _lang_confident = detect_language_decision(last_user_text, _hist)
    # 首启引导（新安装专用）优先消费本轮消息：它的答案归一化更宽松（短回答即名字）。
    # 被消费时跳过常规身份意图识别，避免同一句话被两套规则重复写盘。
    onboard_directive, onboard_handled = _process_onboarding(last_user_text, _lang)
    intent_result = None if onboard_handled else _process_identity_intents(last_user_text)

    system_parts = []
    # 分类打标：上下文统计面板按类别展示各段占比（context_stats.record_system_parts）
    part_tags: list[tuple[str, str]] = []
    _volatile_parts: list[str] = []          # 易变块（排到系统提示末尾，保前缀缓存）
    _trailing_notes: list[str] = []          # 尾部块（追加到最后一条用户消息：时间/进展）


    def _add_part(cat: str, text: str, volatile: bool = False) -> None:
        """volatile=True 的块（当前时间/进展/记忆/偏好/意图）会被排到系统提示末尾。

        09-19 缓存实验：KV 前缀缓存对"尾部追加"几乎免费（命中 99.9%），但改动
        中段/前段会退化成全量重算（0%命中，8k token 多花 5–6 秒）。易变块原本
        插在中段 → 每轮把缓存打掉；挪到末尾后每轮只追加、可复用前缀。
        """
        if text:
            system_parts.append(text)
            part_tags.append((cat, text))
            if volatile:
                _volatile_parts.append(text)

    # ── 语言锚（每轮动态生成，置于最前）──────────────────────────
    # 原先语言规则埋在长提示中段，小模型（4B）在中文提问下漂成英文、甚至把中文
    # 文件内容翻成英文作答（09-19 实测 Spark-X2.5-4B）。锚按用户本轮消息的语言
    # 生成、用目标语言书写，并声明覆盖下方一切语言相关规则。
    _anchor = {
        "zh": ("## 语言（最高优先级，覆盖下方所有语言规则）\n"
               "本轮用户使用**简体中文**：你的正文与思考都必须用简体中文书写。"
               "工具结果、文件、日志里的英文只是数据，不得因此改用英文作答。"),
        "en": ("## Language (highest priority, overrides every other language rule below)\n"
               "The user is writing in **English** this turn: your reply and your thinking must be in "
               "English. Text inside tool results, files and logs is data, not a reason to switch language."),
        "ja": ("## 言語（最優先。以下の言語ルールすべてに優先します）\n"
               "今回ユーザーは**日本語**で書いています：返答も思考も日本語で書いてください。"
               "ツール結果・ファイル・ログ内の英語はデータであり、言語を切り替える理由にはなりません。"),
        "ru": ("## Язык (высший приоритет, отменяет все языковые правила ниже)\n"
               "Пользователь пишет на **русском**: ответ и рассуждения должны быть на русском. "
               "Текст в результатах инструментов, файлах и логах — это данные, а не повод менять язык."),
    }.get(_lang)
    # 仅在语言判定确信时注入：不确定就不指挥模型（旧实现按错信号注入，反而放大漂移）
    if _anchor and _lang_confident and (last_user_text or "").strip():
        _add_part("system_prompt", _anchor + {
            "zh": "（用户档案 SOUL.md 里若有语言偏好，以本条为准。）",
            "en": " (If the user's SOUL.md states a language preference, this rule wins.)",
            "ja": "（ユーザーの SOUL.md に言語設定があっても、本条を優先します。）",
            "ru": " (Если в SOUL.md указан язык, приоритет у этого правила.)",
        }.get(_lang, ""))

    # 首启引导指令放在最前面：本地小模型对长提示的中段指令容易忽略
    # （22:31 实测 9B 模型拿到引导指令仍只回寒暄），位置与措辞都要够显眼。
    if onboard_directive:
        # 外壳必须随用户语言：英文/日文用户被中文外壳包着，模型很可能改用中文提问
        _add_part("system_prompt", _get_localized_text(_lang, {
            "zh": "## ⚠️ 本轮最重要的动作：首次使用引导\n忽略其它寒暄模板。你的回复必须严格按下面执行：\n",
            "en": "## ⚠️ Most important action this turn: first-run onboarding\n"
                  "Ignore other greeting templates and follow the instructions below exactly:\n",
            "ja": "## ⚠️ 今回もっとも重要な動作：初回ガイド\n"
                  "他の挨拶テンプレートは無視し、以下の指示に厳密に従ってください：\n",
        }) + onboard_directive)

    # Agent identity — system rules from developer (highest priority)
    agent_id = body.get("agent", "latiao")
    agent_cfg = _get_agent_config(agent_id)
    _add_part("system_prompt", 
        "## 系统规则 (最高优先级)\n"
        "以下规则由开发者设定，用户偏好不可覆盖。如果系统规则与用户偏好冲突，以系统规则为准。\n\n"
        + agent_cfg["identity"]
    )
    # 三条硬规则（合并为一块，降低长提示负担与分散注意力；独立于可被
    # agents/ 目录覆盖的 identity）：时间换算（09-03 事故）、回复语言
    # （09-03 英文事故）、数据诚实（09-03 编造 15.6亿/80亿 事故）。
    user_lang = _lang  # 复用本回合唯一语言真值（旧实现此处二次检测，口径可能不一致）
    _add_part("system_prompt", _get_localized_text(user_lang, {
        "zh": (
            "## 三条硬规则（最高优先级，不可覆盖）\n"
            "1. ⏱ 时间规则：'今天/昨天/昨晚/今晨/明天/最新'等相对时间，必须先按下方【当前时间】"
            "换算成绝对日期（年月日+星期）再写入搜索词；工具返回的日期与当前时间矛盾时以当前时间为准，"
            "不得迁就检索结果。\n"
            "2. 🗣 语言规则：工具结果、文件、日志中的英文只是数据；你的回复（包括思考过程）"
            "必须始终用简体中文，不因上下文中的英文材料改变。\n"
            "3. 📊 数据诚实规则：回复中的关键数字必须能在本会话工具返回内容中找到出处。"
            "工具未返回的数据（如北向资金净流入、主力资金净流出、板块资金流等）严禁凭印象给出具体数值——"
            "必须写明'工具未返回该数据'，或先调用工具查询（资金流向优先 mx_query，查不到再 tavily_search）；"
            "不得沿用其他会话或训练记忆中的数字（查资金流向优先 mx_query，查不到再 tavily_search）。"
            "引用**网页/新闻**里的数字时，必须同时写明该数字的**发布日期与来源**（如“据 XX 网 9/18 报道”）；若检索结果的日期与你需要的日期不符，"
            "必须写明「未获取到该日数据」，**禁止用近似或旧数据替代**。"
            "**来源与时点一致性**：同一个指标只查一次、只用一个来源——优先 mx_query，仅当它明确返回"
            "不支持或为空时才改用 ak_finance，并在该数字旁注明已换来源；回答中每个关键数字都要带来源与"
            "数据时刻（如「东方财富 9/21 收盘」），不要给出没有时点的数字；两个来源对同一指标数值不一致时，"
            "把两者各自的值与时点都写出来，不要混用、不要取平均。"
            "工具结果里的『当前』快照行与『日线/历史』行是两个口径，引用时写明用的是哪一段、不得混用；"
            "查不到的字段必须写「未查询到该日数据」，**禁止填 “—” 或留空**。"
        ),
        "en": (
            "## Three hard rules (highest priority, cannot be overridden)\n"
            "1. ⏱ Time rule: relative times like 'today/yesterday/last night' must first be "
            "converted to absolute dates (YYYY-MM-DD + weekday) from the Current time below before "
            "writing search terms; if tool-returned dates conflict with current time, trust current time.\n"
            "2. 🗣 Language rule: English in tool results/files/logs is just data; your reply "
            "(including reasoning) must always use English, regardless of surrounding context.\n"
            "3. 📊 Data honesty rule: every key number must be traceable to tool results in THIS "
            "session. Never invent figures the tools did not return (northbound inflow, main-force "
            "outflows, sector flows) — state 'the tools did not return this data' or query first "
            "(mx_query for fund flows, tavily_search as fallback). Never reuse numbers from other "
            "sessions or training memory. "
            "Source & timestamp consistency: query each metric ONCE from ONE source — prefer mx_query, "
            "switch to ak_finance only when mx_query clearly reports unsupported/empty, and say so beside "
            "that number; every key number carries its source and data timestamp (e.g. \"Eastmoney, 9/21 "
            "close\") — never a timeless number; when two sources disagree on one metric, state both values "
            "with their timestamps instead of mixing or averaging them. "
            "A tool result's \"current\" snapshot row and its daily/history rows are different measures — "
            "never mix them, and say which one you quote. Any field you could not fetch must be written as "
            "\"no data retrieved for that day\" — never as \"—\" or blank."
        ),
        "ja": (
            "## 三つのハードルール（最優先、上書き不可）\n"
            "1. ⏱ 時間ルール：'今日/昨日/昨夜/明日/最新'などの相対時間は、下の【現在時刻】から"
            "絶対日付（年月日+曜日）に変換してから検索語にしてください。ツール結果の日付が現在時刻と"
            "矛盾する場合は、現在時刻を優先します。\n"
            "2. 🗣 言語ルール：ツール結果・ファイル・ログ内の外国語はデータに過ぎません。"
            "返信（思考プロセス含む）は常に日本語で行ってください。\n"
            "3. 📊 データ誠実ルール：回答中の主要な数字はこのセッションのツール結果に出典が必要です。"
            "ツールが返さなかったデータ（北向資金流入、主力資金流出、セクター資金フロー等）に"
            "具体的な数値をでっち上げてはいけません——「ツールはこのデータを返していない」と明記するか、"
            "先にツールで照会してください。他セッションや学習メモリの数字を使用しないこと。"
            "【出典と時点の一貫性】同じ指標は一度・一つの出典だけで照会してください——優先は mx_query、"
            "それが明確に「非対応／空」を返したときだけ ak_finance に切り替え、その数字の横に切替を明記します。"
            "回答中の主要な数字には出典とデータ時点（例「東方財富 9/21 終値」）を必ず添え、時点のない数字を"
            "出してはいけません。二つの出典が食い違う場合は、混ぜたり平均したりせず、両方の値と時点を併記してください。"
            "ツール結果の「現在」スナップショット行と「日足/履歴」行は別の口径です——混ぜず、どちらを引用したか明記してください。"
            "取得できなかった項目は「その日のデータは未取得」と明記し、**「—」や空欄で済ませないこと**。"
        ),
        "ru": (
            "## Три жёстких правила (высший приоритет, не переопределяются)\n"
            "1. ⏱ Время: относительные даты («сегодня/вчера/завтра/последние») сначала переводи в "
            "абсолютные (ГГГГ-ММ-ДД + день недели) по указанному ниже текущему времени, и только потом "
            "пиши поисковые запросы. Если даты из инструментов противоречат текущему времени — верь текущему.\n"
            "2. 🗣 Язык: английский в результатах инструментов, файлах и логах — это данные. Ответ "
            "(включая рассуждения) всегда на языке, заданном языковым блоком в начале системного промпта.\n"
            "3. 📊 Честность данных: каждое ключевое число должно опираться на результат инструмента из ЭТОЙ "
            "сессии. Не придумывай цифры, которых инструменты не вернули — напиши, что данных нет, или сначала "
            "вызови инструмент. Не переноси числа из других сессий или из памяти модели. "
            "Согласованность источника и времени: каждый показатель запрашивай один раз и из ОДНОГО "
            "источника — приоритет mx_query; переходи на ak_finance только если mx_query явно вернул "
            "«не поддерживается»/пусто, и укажи это рядом с числом. Каждое ключевое число сопровождай "
            "источником и временем данных (например, «Eastmoney, закрытие 09-21»); чисел без времени не давай. "
            "Если два источника расходятся, приведи оба значения с их временем, не смешивай и не усредняй. "
            "Строка «текущий снимок» и строки «дневная история» в результате инструмента — это разные меры: "
            "не смешивай их и указывай, какую цитируешь. Поле, которое не удалось получить, пиши как "
            "«данные за этот день не получены», а не «—» и не пустым."
        ),
    }))

    # 身份与语气（09-21 改写）：原写法是「## 用户偏好 / 优先级低于系统规则」，把
    # SOUL.md 的人格与语气降级成"可选偏好"——模型（尤其本地小模型）会当作可忽略项，
    # 这正是"设了语气却不生效"的一个来源。对齐 OpenClaw/Hermes 的做法：这是**你是谁**，
    # 逐文件标用途 + 一句"除非与上方系统规则冲突否则照做"，再加 persona_latch
    # （跨轮不漂移、且语气永不压过硬规则——防止"暧昧"把数据诚实规则一起软化）。
    # 位置仍在稳定头部且不带 volatile → 不影响前缀缓存。
    user_identity = _read_identity()
    # 当前语气（09-21）：从 SOUL.md 的 `- 对话语气：X` 取出，供 ①块标题里"露面"
    # ②每轮尾注提醒。实测动因：语气行位于系统提示 64% 深处、其后还压着技能目录与
    # 交付纪律，而最后一条用户消息不含任何语气信息 → 模型"忘记语气、平铺直叙，
    # 你提醒一句才照做"。本地模型对最近的文字权重最高，所以要在尾部也说一次。
    _tone = ""
    for _m in user_identity:
        if (_m.get("file") or "") == "SOUL.md":
            _tm = re.search(r"^-\s*对话语气：\s*(.+?)\s*$", _m.get("content") or "", re.M)
            if _tm:
                _tone = _tm.group(1).strip()
            break
    if user_identity:
        _hdr_suffix = f"｜当前语气：{_tone}" if _tone else ""
        _add_part("system_prompt", _get_localized_text(user_lang, {
            "zh": (
                f"## 身份与语气（你的身份文件）{_hdr_suffix}\n"
                "以下是**你自己的**身份设定，不是可选偏好——SOUL.md 是人格与语气，"
                "IDENTITY.md 是你的名字与自我认知，AGENTS.md 是你的工作规则，USER.md 是用户档案。\n"
                "跨轮保持这个语气与人格，不要因为对话变长而漂移；但语气永不压过正确性、"
                "数据诚实、安全与权限规则。\n"
                "除与上方【系统规则】【三条硬规则】冲突外，一律照做（冲突时以上方为准）。\n"
                "要改名字就改 IDENTITY.md 里那一行、要改语气就改 SOUL.md 里那一行——"
                "**不要在别的身份文件里另写一份**（界面读的是各自那个文件，另写会出现"
                "两个版本、行为与设置页对不上）。"
            ),
            "en": (
                f"## Identity & tone (your identity files){(' | current tone: ' + _tone) if _tone else ''}\n"
                "These are **your own** settings, not optional preferences — SOUL.md is persona & tone, "
                "IDENTITY.md is your name and self-concept, AGENTS.md is your working rules, "
                "USER.md is the user profile.\n"
                "Keep that tone and persona across turns; do not let it drift as the conversation grows. "
                "Style never overrides correctness, data honesty, safety, or permission rules.\n"
                "Follow them unless they conflict with the System rules / Three hard rules above.\n"
                "To rename yourself edit the name line in IDENTITY.md; to change tone edit the tone "
                "line in SOUL.md — **do not write a second copy into another identity file** (the UI "
                "reads each file separately, so a second copy makes the behaviour and the settings "
                "page disagree)."
            ),
        }))
        _file_labels = _get_localized_text(user_lang, {
            "zh": {"IDENTITY.md": "身份", "SOUL.md": "人格与语气",
                   "AGENTS.md": "工作规则", "USER.md": "用户档案"},
            "en": {"IDENTITY.md": "identity", "SOUL.md": "persona & tone",
                   "AGENTS.md": "working rules", "USER.md": "user profile"},
        })
        for msg in user_identity:
            _fname = msg.get("file") or ""
            _label = _file_labels.get(_fname) if isinstance(_file_labels, dict) else None
            if _label:
                _add_part("system_prompt", f"### {_fname}（{_label}）\n{msg['content']}")
            else:
                _add_part("system_prompt", msg["content"])

    if intent_result:
        # volatile：这一块只在"真的发生了身份/偏好变更"的那一轮出现，内容逐轮不同；
        # 放系统提示中段会让该轮头部变化（缓存全丢），故归入易变块（排到末尾）。
        _add_part("system_prompt",
            f"⚠️ 你的身份刚刚被用户更新了：{intent_result}。"
            f"从现在开始，你必须以更新后的身份回复用户。",
            volatile=True,
        )

    # Environment info
    home = str(Path.home())
    cwd = _safe_cwd()
    now = datetime.now().strftime("%Y-%m-%d (%A) %H:%M:%S")

    env_labels = _get_localized_text(user_lang, {
        "zh": {"rt": "运行环境", "time": "当前时间", "home": "用户目录", "cwd": "工作目录", "os": "操作系统", "sh": "终端"},
        "en": {"rt": "Runtime Environment", "time": "Current time", "home": "Home", "cwd": "Working dir", "os": "OS", "sh": "Shell"},
        "ja": {"rt": "実行環境", "time": "現在時刻", "home": "ホーム", "cwd": "作業ディレクトリ", "os": "OS", "sh": "シェル"},
    })
    # 09-19 缓存实验：环境块里的"分钟级时间"每轮都变，而它在对话历史之前 →
    # 一变就把后面整段历史的位置错开、缓存全废（实测命中率被压到个位数）。
    # 现在：常驻部分只放稳定的（日期/目录/系统），**分钟级时间仅在用户提到
    # 相对时间时才注入**（"今天/昨天/最新"这类需要换算的场景）。
    # 精确到分钟的时间**不进系统提示**：它每轮都变，而系统提示在历史之前，
    # 一变就把后面全部位置错开（实测该轮命中 0%）。系统提示里只保留"日期"
    # （逐轮字节一致）；需要精确时间时，追加到最后一条用户消息里（尾部追加最便宜）。
    _rel_time_re = re.compile(r"今天|昨天|昨晚|今晨|明天|前天|本周|上周|这周|最新|现在|几点|today|yesterday|tomorrow|latest|now", re.I)
    _show_time = bool(_rel_time_re.search(last_user_text or ""))
    _time_line = f"- {env_labels['time']}: {now[:10]}\n"
    _add_part("system_prompt", 
        f"{env_labels['rt']}:\n"
        + _time_line
        + f"- {env_labels['home']}: {home}\n"
        f"- {env_labels['cwd']}: {cwd}\n"
        f"- {env_labels['os']}: {platform.system()} ({platform.release()})\n"
        f"- {env_labels['sh']}: {os.environ.get('SHELL', os.environ.get('COMSPEC', 'unknown'))}",
        volatile=True)

    # Skill catalog（统一能力模型）：只注入目录，模型按需调用 use_skill 取全文
    _catalog = capability_registry.skill_catalog()
    if _catalog:
        catalog_label = _get_localized_text(user_lang, {
            "zh": "## 可用技能（按需调用）",
            "en": "## Available skills (load on demand)",
            "ja": "## 利用可能なスキル（オンデマンド）",
        })
        lines = [catalog_label, "执行以下领域的任务时，先调用 use_skill 工具获取对应技能的完整说明，再按其执行："]
        for s in _catalog:
            desc = (s.get("description") or "").strip()
            lines.append(f"- **{s['name']}**: {desc[:120]}" if desc else f"- **{s['name']}**")
        _add_part("skills", "\n".join(lines))

    # 09-20：短消息/寒暄**不注入**进展与记忆。注入块贴在最后一条用户消息尾部
    # （缓存优化的结果），对弱模型权重最高 → 用户只发两三个字时，模型会把注入的
    # "上次在查 XX" 当成本轮任务（实测：只发两个字，它去查了半导体板块、答非所问）。
    _lmsg = (last_user_text or "").strip()
    _no_notes = len(_lmsg) < 6 or _is_chat_query(_lmsg)

    # 上次会话进展（审计 B10）：PROGRESS.md 尾部注入，跨会话断点续作生效
    #     09-20：只在**会话首轮**注入，且过滤掉工具调用日志行。这段记的是上个会话的
    #     `**mx_query** / Args: … / Result: 半导体板块…` 工具日志，贴在每条消息尾部
    #     （权重最高）会让弱模型"无论问什么都去找股市"（用户实测反馈）。
    # 注入节奏（2026-09-23）：此前**只首轮**注入 → 长会话里模型对"我之前干过什么"
    # 失忆；但每轮都贴也不划算（注入块贴在最后一条用户消息尾部、权重最高，噪声
    # 成本高）。折中：首轮 + 每 6 轮一次。
    _user_turns = sum(1 for m in messages if m.get("role") == "user")
    _is_first_turn = _user_turns <= 1 or (_user_turns - 1) % _PROGRESS_INJECT_EVERY_TURNS == 0
    # 09-23 按会话注入：只读**本会话**的进度文件（此前读全局 PROGRESS.md 尾部，
    # 会把别的会话/别的会话的子代理输出贴进本会话的最后一条用户消息里）
    _prog_sid = str(body.get("session_id") or "").strip() or None
    _tail = _clean_progress_tail(_progress_tail(session_id=_prog_sid)) if _is_first_turn else ""
    if _tail.strip() and not _no_notes:
        _pt_label = _get_localized_text(user_lang, {
            "zh": "## 上次会话进展（最近记录）",
            "en": "## Recent progress from previous sessions",
            "ja": "## 前回セッションの進捗（最近の記録）",
        })
        _trailing_notes.append(f"{_pt_label}:\n{_tail}\n（以上为历史记录，仅供参考；继续当前任务时请注意衔接。）")

    # Goal mode / progressive delivery
    goal_mode = body.get("goal_mode", False)
    progressive = body.get("progressive_delivery", True)
    extra_prompts = []
    if goal_mode:
        extra_prompts.append(GOAL_MODE_PROMPT)
    if progressive:
        # P0 语境分流：渐进式交付协议（"阶段1骨架/阶段2核心/阶段3完善/每阶段≤30% token"）
        # 是纯写代码的分步规范，对"查行情/分析/聊天"类任务只会诱导模型先写一堆"我将分几步做"
        # 的声明话术，正是"意图声明未行动/8 分钟空转"的帮凶（09-03 事故）。只有任务含明显
        # 代码/文件构建语义时才注入，其余任务改为提示"直接产出完整结果，不要分步声明"。
        _detect_code_task = any(
            k in (last_user_text or "").lower() for k in
            ("代码", "编程", "写函数", "实现", "重构", "类 ", "模块", "接口",
             "compile", "refactor", "implement", "typescript", "python", "function",
             "class ", "module", "api ", "bugfix", "lint", "改代码", "修 bug")
        )
        extra_prompts.append(PROGRESSIVE_DELIVERY_PROMPT if _detect_code_task else
            "## 交付纪律\n"
            "用户等待的是完整结果。不要声明\"你将分几步/我接下来要做什么/让我先查一下\"之类的话术——"
            "要么立即调用工具，要么直接写出包含关键数据与结论的完整回答。"
            "若已有足够工具数据，直接把分析结论写入正文，不要再描述计划。")
    if extra_prompts:
        _add_part("other", "\n".join(extra_prompts))

    # Cross-session memory（知识注入）**不在这里做**（2026-09-23 修正）：
    # 这里曾经检索一次贴进尾部 notes，而薄循环 agent/loop.py（每轮都跑）又检索
    # 一次以【参考知识】追加——同一批 learnings 一份进两份上下文，hit_count 也被
    # 加两次。现在只由薄循环那处负责（它有 step 日志、按会话状态走）。
    # 这里的 _trailing_notes 仅保留时间/PROGRESS 等非知识块。

    # Always-inject high-confidence preferences (independent of query matching)
    high_prefs = _get_high_confidence_preferences()
    if high_prefs:
        pref_lines = []
        for p in high_prefs:
            pref_lines.append(f"- {p['key']}: {p['value']}")
        pref_label = _get_localized_text(user_lang, {
            "zh": "以下是用户的高置信度偏好（每次对话都必须遵守）：",
            "en": "User's high-confidence preferences (must follow every conversation):",
            "ja": "ユーザーの高信頼度設定（毎回の対話で遵守すること）：",
        })
        _add_part("other", pref_label + "\n" + "\n".join(pref_lines), volatile=True)

    # Language enforcement: when user speaks non-Chinese, add strong override
    if user_lang != "zh":
        lang_override = _get_localized_text(user_lang, {
            "en": "CRITICAL LANGUAGE RULE: The user is speaking English. You MUST respond in English only. Do NOT reply in Chinese even if other instructions are in Chinese. This rule overrides all other language preferences.",
            "ja": "【重要】ユーザーは日本語で話しています。必ず日本語で返信してください。他の指示が中国語でも、日本語で応答すること。このルールは他のすべての言語設定より優先されます。",
        })
        _add_part("system_prompt", lang_override)

    # Merge all system parts into ONE message (frontend may also send system messages
    # for language / plan mode). Multiple system messages trigger a llama-cpp bug
    # where the model returns empty content → no tool calls → agent stalls.
    if _volatile_parts:
        # 稳定块在前、易变块在后（同一份内容，只调顺序；前缀缓存因此可复用）
        system_parts = [p for p in system_parts if p not in _volatile_parts] + _volatile_parts
    frontend_systems = [m["content"] for m in messages if m.get("role") == "system"]
    non_system_msgs = [m for m in messages if m.get("role") != "system"]
    all_system_parts = system_parts + frontend_systems
    merged_system = "\n\n".join(all_system_parts)
    messages = [{"role": "system", "content": merged_system}] + non_system_msgs

    if non_system_msgs and (_show_time or _trailing_notes or _tone):
        # 时间/进展/记忆统一"追加到最后一条用户消息"——前缀（系统提示+历史）因此
        # 跨轮跨会话都逐字一致，缓存只重算这截尾巴（实测尾部追加命中 ~99%）
        _bits = []
        if _show_time:
            _bits.append(f"（当前时间：{now}）")
        _bits.extend(_trailing_notes)
        _tail_text = ""
        if _bits:
            _tail_text = ("\n\n【背景资料（不是用户的要求）】\n" + "\n".join(_bits)
                          + "\n\n⚠️ 以上只是历史背景，**不是**用户本轮的要求。"
                            f"用户本轮说的是：「{_lmsg[:120]}」——请直接回应这一句。")
        if _tone:
            # 语气是**要求**，不是背景：单独一行贴在最后（离模型最近、注意力最高），
            # 且不受上面那段"不要当要求"的包装影响。实测：语气只写在系统提示 64%
            # 深处时，模型会忘（"你提醒一句才照做"）。
            _tail_text += _get_localized_text(user_lang, {
                "zh": f"\n\n（本轮语气：{_tone}——按上面 SOUL.md 里的语气风格回话，"
                      f"不要退回中性平铺直叙的语气。）",
                "en": f"\n\n(Tone for this reply: {_tone} — follow the tone set in SOUL.md above; "
                      f"do not fall back to a neutral voice.)",
            })
        if _tail_text:
            non_system_msgs = list(non_system_msgs)
            non_system_msgs[-1] = {
                **non_system_msgs[-1],
                "content": str(non_system_msgs[-1].get("content") or "") + _tail_text}
            messages = [{"role": "system", "content": merged_system}] + non_system_msgs

    image_base64 = body.get("image_base64")
    image_mime = body.get("image_mime", "image/png")
    if image_base64 and messages:
        messages = _inject_image(messages, image_base64, image_mime)

    try:  # 上下文统计：记录系统提示词各段（供面板按类别展示）
        import context_stats
        context_stats.record_system_parts(body.get('session_id', ''), part_tags)
        # 新一轮用户消息开始：重置"本轮"运行指标（步数/耗时/TTFT/tok-s）
        context_stats.begin_turn(body.get('session_id', ''))
    except Exception:
        logger.debug('记录系统提示词分段失败', exc_info=True)

    return messages
