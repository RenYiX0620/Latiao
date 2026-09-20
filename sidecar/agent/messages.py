"""用户可见的运行时提示（多语）——循环/交付路径上的告警与说明。

背景（09-19 审计）：这些提示此前是中文写死的，英文/日文/俄文用户会在聊天里直接
看到中文句子（"⚠️ 已达安全步数上限…"之类）。这里集中成一张表，四语齐备；
未知语言一律回落英文（比回落中文更可能被读懂）。

用法：msg("tool_timeout", lang, name="tavily_search", limit=45)
语言来源：循环内用 loop.user_lang；没有 loop 的地方用 lang_of(messages) 现算。
"""
from agent.context import detect_language_decision

_LANGS = ("zh", "en", "ja", "ru")

MESSAGES: dict[str, dict[str, str]] = {
    # ── 检索预算（措辞须与"工具是否可用"一致）──
    "search_budget": {
        "zh": "\n\n## 检索预算\n本轮检索已用 {used}/{total} 次（还剩 {remain} 次）。若已有足够数据，请直接作答；若仍需补数据，请用更具体的查询，避免重复之前的检索。",
        "en": "\n\n## Search budget\n{used}/{total} searches used this turn ({remain} left). If you already have enough, answer now; if you still need data, use a more specific query and avoid repeating earlier searches.",
        "ja": "\n\n## 検索予算\n今回の検索は {used}/{total} 回（残り {remain} 回）。十分なデータがあれば直接回答し、不足ならより具体的なクエリで重複を避けてください。",
        "ru": "\n\n## Бюджет поиска\nИспользовано {used}/{total} поисков (осталось {remain}). Если данных достаточно — отвечайте; если нет, уточните запрос и не повторяйте прежние поиски.",
    },
    "search_budget_closed": {
        "zh": "\n\n## 检索预算（本轮不再检索）\n数据已够：请直接写出最终答案（含关键数字与结论），不要再调用检索类工具；若确实缺关键项，在答案里写明缺什么。",
        "en": "\n\n## Search budget (no more searching this turn)\nYou have enough: write the final answer now with the key figures and conclusions, do not call search tools again; if something essential is missing, name it.",
        "ja": "\n\n## 検索予算（今回これ以上検索しない）\nデータは十分です。重要数値と結論を含む最終回答を直接書いてください。検索系ツールはもう呼ばないでください。欠けている重要項目は明記してください。",
        "ru": "\n\n## Бюджет поиска (больше не искать в этом ходу)\nДанных достаточно: напишите итоговый ответ с ключевыми цифрами и выводами, не вызывайте поисковые инструменты; если чего-то важного не хватает — укажите это.",
    },
    # 收口轮尾部指令：追加在请求末尾（不改提示头 → 缓存可复用，见 agent/loop.py）
    "finalize_tail": {
        "zh": "📣 收尾：数据已足够，请立刻用简体中文写出完整分析正文（含关键数字与结论），不要再调用任何工具，不要输出任何工具调用格式，写完即停。",
        "en": "📣 Wrap up: enough data — write the complete analysis now in English (with the key figures and conclusions), do not call any tools, do not output tool-call markup, and stop when done.",
        "ja": "📣 まとめ：データは十分です。今すぐ日本語で完全な分析本文（重要数値を含む）を書いてください。ツールは呼ばず、ツール呼び出し形式も出力せず、書き終えたら停止してください。",
        "ru": "📣 Завершение: данных достаточно — напишите сейчас полный анализ на русском (с ключевыми цифрами и выводами), не вызывайте инструменты, не выводите формат вызова инструментов и остановитесь, когда закончите.",
    },
    # ── 收口轮提问 ──
    "finalize_ask": {
        "zh": "请根据以上数据写出完整分析正文（{lang_name}，包含关键数字与结论）。",
        "en": "Using the data above, write the complete analysis ({lang_name}, including the key figures and conclusions).",
        "ja": "上記のデータをもとに完全な分析本文を書いてください（{lang_name}、重要数値と結論を含む）。",
        "ru": "На основе данных выше напишите полный анализ ({lang_name}, с ключевыми цифрами и выводами).",
    },
    # 空转闸门：只声明计划不行动（09-20：27B 只输出"我来帮你分析…先调取数据"就收尾）
    "plan_nudge": {
        "zh": "⛔ 你刚才只说明了打算做什么，并没有真正调用工具。不要复述计划：现在直接调用工具，或直接写出包含关键数据的完整答案。",
        "en": "⛔ You only announced what you were going to do and did not actually call a tool. Do not restate the plan: call the tool now, or write the complete answer with the key data.",
        "ja": "⛔ 何をするつもりかを述べただけで、実際にはツールを呼んでいません。計画を繰り返さず、今すぐツールを呼ぶか、重要な数値を含む完全な回答を書いてください。",
        "ru": "⛔ Вы только объявили о намерении и не вызвали инструмент. Не повторяйте план: вызовите инструмент сейчас или напишите полный ответ с ключевыми данными.",
    },
    # ── 交互控制 ──
    "task_stopped": {
        "zh": "⏹️ 任务已停止。", "en": "⏹️ Task stopped.",
        "ja": "⏹️ タスクを停止しました。", "ru": "⏹️ Задача остановлена.",
    },
    "plan_rejected": {
        "zh": "⏹️ 计划已被拒绝，任务未执行。你可以调整要求后重新发起。",
        "en": "⏹️ The plan was rejected and nothing was executed. Adjust your request and try again.",
        "ja": "⏹️ 計画が拒否されたため実行していません。要件を調整して再実行してください。",
        "ru": "⏹️ План отклонён, ничего не выполнено. Измените запрос и повторите.",
    },
    "plan_timeout": {
        "zh": "⚠️ 计划等待确认超时（5 分钟无人操作），任务已暂停未执行。可重新发起任务。",
        "en": "⚠️ Timed out waiting for plan approval (5 minutes). The task was paused and not executed; start it again when ready.",
        "ja": "⚠️ 計画の確認待ちがタイムアウトしました（5 分間操作なし）。タスクは実行せず保留です。再実行してください。",
        "ru": "⚠️ Истекло время ожидания подтверждения плана (5 минут). Задача приостановлена и не выполнялась.",
    },
    # ── 工具 ──
    "tool_timeout": {
        "zh": "⏱ 工具超时：{name} 超过 {limit:.0f} 秒未返回，已中止。请缩小范围（更具体的目录/模式/更少的条数）后重试，或改用其他工具；若已有足够数据，请直接作答。",
        "en": "⏱ Tool timeout: {name} did not return within {limit:.0f}s and was aborted. Retry with a narrower scope (a more specific directory/pattern, fewer results) or use another tool; if you already have enough data, answer now.",
        "ja": "⏱ ツールタイムアウト：{name} が {limit:.0f} 秒以内に返らなかったため中止しました。範囲を狭めて（より具体的なディレクトリ/パターン、件数を減らす）再試行するか、別のツールを使ってください。十分なデータがあれば直接回答してください。",
        "ru": "⏱ Таймаут инструмента: {name} не ответил за {limit:.0f} с, вызов прерван. Сузьте область (конкретнее каталог/шаблон, меньше результатов) или используйте другой инструмент; если данных достаточно — сразу отвечайте.",
    },
    # ── 代码轮的过程叙述折叠 ──
    "narration_folded": {
        "zh": "…（本轮为过程说明，已折叠 {n} 字）",
        "en": "… (process notes for this round; {n} characters collapsed)",
        "ja": "…（このラウンドの過程メモ。{n} 文字を折りたたみました）",
        "ru": "… (промежуточные заметки этого раунда; скрыто {n} символов)",
    },
    # ── 循环自保 ──
    "max_steps": {
        "zh": "⚠️ 已达安全步数上限（{n}）。请发送新消息继续。",
        "en": "⚠️ Safety step limit reached ({n}). Send a new message to continue.",
        "ja": "⚠️ 安全ステップ上限（{n}）に達しました。新しいメッセージで続行してください。",
        "ru": "⚠️ Достигнут предохранительный лимит шагов ({n}). Отправьте новое сообщение, чтобы продолжить.",
    },
    "repeat_calls": {
        "zh": "⚠️ 模型反复重复相同调用，已基于已收集数据尽力生成未果。任务已收口。请重试或换用云端模型。",
        "en": "⚠️ The model kept repeating the same call, so the task was closed with the data already collected and no usable answer. Retry, or use a cloud model.",
        "ja": "⚠️ 同じ呼び出しを繰り返したため、収集済みデータで打ち切りました。再試行するか、クラウドモデルをご利用ください。",
        "ru": "⚠️ Модель повторяла один и тот же вызов; задача закрыта на уже собранных данных без готового ответа. Повторите или используйте облачную модель.",
    },
    # ── 交付诊断 ──
    "thinking_only": {
        "zh": "⚠️ 本地模型本轮只输出了思考过程。请回复「继续」重试，或换用其他模型。",
        "en": "⚠️ The local model produced only its reasoning this turn. Reply “continue” to retry, or switch models.",
        "ja": "⚠️ 今回は思考過程のみが出力されました。「続けて」と送って再試行するか、別のモデルをお試しください。",
        "ru": "⚠️ Локальная модель выдала только рассуждения. Ответьте «продолжай», чтобы повторить, или смените модель.",
    },
    "empty_response": {
        "zh": "⚠️ 模型返回了空响应。可能原因：上下文超限被截断、模型不支持当前请求格式。建议换用更大的模型或重试。",
        "en": "⚠️ The model returned an empty response — likely the context was truncated or the request format is unsupported. Try a larger model or retry.",
        "ja": "⚠️ 空の応答が返りました。コンテキスト超過か、リクエスト形式が非対応の可能性があります。より大きなモデルか再試行をお試しください。",
        "ru": "⚠️ Модель вернула пустой ответ: вероятно, контекст обрезан или формат запроса не поддерживается. Попробуйте модель больше или повторите.",
    },
    "no_final_answer": {
        "zh": "⚠️ 模型未生成最终分析，任务已收口。",
        "en": "⚠️ The model did not produce a final analysis; the task was closed.",
        "ja": "⚠️ 最終的な分析が生成されなかったため、タスクを打ち切りました。",
        "ru": "⚠️ Финальный анализ не был сгенерирован; задача закрыта.",
    },
    "data_only": {
        "zh": "⚠️ 模型未能生成分析。以下为已收集的数据，",
        "en": "⚠️ The model could not produce an analysis. Below is the data collected so far; ",
        "ja": "⚠️ 分析を生成できませんでした。以下は収集済みのデータです：",
        "ru": "⚠️ Модель не смогла подготовить анализ. Ниже — собранные данные; ",
    },
    "retry_hint": {
        "zh": "，请重试或检查模型服务。",
        "en": "; retry or check the model service.",
        "ja": "。再試行するか、モデルサービスを確認してください。",
        "ru": "; повторите попытку или проверьте сервис модели.",
    },
    "http_error": {
        "zh": "⚠️ 模型服务返回错误 HTTP {status}，请稍后重试。",
        "en": "⚠️ The model service returned HTTP {status}; retry later.",
        "ja": "⚠️ モデルサービスが HTTP {status} を返しました。しばらくして再試行してください。",
        "ru": "⚠️ Сервис модели вернул HTTP {status}; повторите позже.",
    },
}


def msg(key: str, lang: str = "zh", **kw) -> str:
    """取一条本地化提示（未知语言回落英文，缺 key 回落中文表）。"""
    table = MESSAGES.get(key) or MESSAGES["retry_hint"]
    template = table.get(lang) or table.get("en") or table.get("zh", "")
    try:
        return template.format(**kw) if kw else template
    except (KeyError, IndexError, ValueError):
        return template


def lang_of(messages: list) -> str:
    """从消息列表推断回复语言（取最后一条 user 文本；无则 zh）。"""
    texts = [m.get("content") for m in (messages or [])
             if m.get("role") == "user" and isinstance(m.get("content"), str)]
    if not texts:
        return "zh"
    return detect_language_decision(texts[-1], texts[:-1])[0]
