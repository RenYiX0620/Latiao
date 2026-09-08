"""上下文与工具选配：用户文本提取、语言检测、工具筛选/裁剪/权限映射、原生模式判定。"""
import json
import os
import re

import local_llm


def _extract_last_user_text(messages: list) -> str:
    """Extract text content from the last user message in the messages array."""
    for m in reversed(messages):
        if m.get("role") == "user":
            c = m.get("content", "")
            if isinstance(c, list):
                for part in c:
                    if part.get("type") == "text":
                        return part.get("text", "")
                return ""
            return c
    return ""


# ═══════════════════════════════════════════════════════
#  Dynamic Tool Filtering: intent-based tool selection
# ═══════════════════════════════════════════════════════

TOOL_CATEGORIES = {
    "scheduling": ["create_cron"],
    "file_read": ["read_file", "list_dir", "search_files"],
    "file_write": ["write_file"],
    "command": ["run_cmd"],
    "app": ["open_app", "open_folder"],
    "web": ["tavily_search", "web_search", "bing_search", "headless_read", "dokobot_search"],
    "financial": ["mx_query", "ak_finance"],
    # 五控：进程/鼠标/屏幕/流程
    "control": [
        "screen_capture", "control_list_processes", "control_process_log",
        "control_audit", "control_wait", "control_launch",
        "control_mouse_move", "control_mouse_click",
        "control_keyboard_type", "control_keyboard_press", "control_kill_process",
    ],
}
# 控制类工具在意图匹配不明确时也应保留（避免被 _filter_tools 滤掉）
CONTROL_TOOL_NAMES = set(TOOL_CATEGORIES["control"])

INTENT_PATTERNS = [
    # 定时任务意图：放最前——"每10分钟分析大盘"同时命中 financial，
    # 但用户首要诉求是建定时任务，先给 create_cron（任务内容由 cron 执行时
    # 独立跑 agent 循环处理，不受本次过滤影响）
    (re.compile(r"定时|每\s*\d+\s*(分钟|小时|天|周|秒)|每天|每小时|每周|每分钟|每个小时|cron|计划任务|日程|提醒|周期性", re.IGNORECASE),
     ["scheduling"]),
    (re.compile(r"读|看|查看|检查|搜索|找|列出|显示|看看|分析|审查|review|check|read|find|list|show|cat|head|tail|grep|ls|dir", re.IGNORECASE),
     ["file_read"]),
    (re.compile(r"写|创建|修改|改|删|新建|保存|生成|write|create|modify|update|delete|save|generate|make", re.IGNORECASE),
     ["file_read", "file_write"]),
    (re.compile(r"运行|执行|跑|命令|安装|测试|构建|编译|build|run|test|exec|install|npm|pip|git|docker|tsc|vite|cargo", re.IGNORECASE),
     ["file_read", "command"]),
    (re.compile(r"打开|启动|open|launch|start|应用|app|程序|finder", re.IGNORECASE),
     ["file_read", "app"]),
    (re.compile(r"大盘|A股|港股|股票|个股|股价|行情|涨停|跌停|板块|上证|深证|创业板|科创板|沪深|指数|基金|财报|财务|营收|净利润|上市公司|分红|PE|PB|ROE|股息|龙头|K线|成交量|换手率|资金流向|北向资金|龙虎榜|券商研报", re.IGNORECASE),
     ["file_read", "financial"]),
    (re.compile(r"上网|联网|搜索网络|搜一下|搜一搜|查一下|查询|查一查|了解一下|最新的|最新消息|新闻|热搜|汇率|天气|资料|网页|网址|链接|页面|http|url|search|web|online|latest|news|weather|trending", re.IGNORECASE),
     ["file_read", "web"]),
    # 信息询问型问题（“X 是什么/有哪些/对比/评测”）：给出搜索工具，模型按需调用
    (re.compile(r"是什么|什么是|有哪些|有什么|为什么|如何|怎么|怎么样|怎么回事|介绍一下|介绍下|原理|机制|评测|测评|对比|区别|哪款|哪家|哪个|性价比|值不值得", re.IGNORECASE),
     ["file_read", "web"]),
    # 五控：进程/鼠标/屏幕/流程控制意图
    (re.compile(r"进程|pid|杀|终止|启动|后台运行|后台任务|运行中|列表进程|进程列表|cpu|内存占用|tasklist|kill|process|list_process", re.IGNORECASE),
     ["control"]),
    (re.compile(r"截屏|截图|屏幕|界面|点一下|点击|鼠标|移动鼠标|滚动|双击|右键|键入|输入文字|按键|快捷键|keyboard|mouse|click|screenshot|screen_capture", re.IGNORECASE),
     ["control"]),
]


# 权限模式五档（自主权从低到高）：
#   read_only  只读 —— 只能查询，改不了任何东西
#   confirm    变更前确认 —— 高风险操作每次确认（= 默认行为）
#   auto_edit  自动编辑 —— 文件类工具免确认（write_file/open_folder）
#   plan       计划模式 —— 动工前先出方案确认 + 高危确认
#   full       完全访问 —— 仅高危确认，其余自动
READ_ONLY_TOOLS = {"read_file", "list_dir", "search_files", "tavily_search", "web_search", "bing_search"}
AUTO_EDIT_TOOLS = {"write_file", "open_folder"}


_INLINED_FILE_RE = re.compile(r"上传|内容如下|附件|粘贴")


def _maybe_add_inline_file_note(current_msgs: list, user_text: str) -> None:
    """用户消息含内联文件内容时，注入提示避免模型白费一轮去读文件路径
    （09-21 实测：模型"先看看环境中是否有这个文件"→ 空名/读盘失败诱因）。"""
    if _INLINED_FILE_RE.search(user_text or ""):
        note = ("用户已在消息中提供文件/表格内容，无需调用 read_file/list_dir 等读取工具；"
                "直接基于消息内容分析即可。")
        if not any(m.get("role") == "system" and "无需调用" in str(m.get("content", ""))
                   for m in current_msgs):
            current_msgs.insert(0, {"role": "system", "content": note})

_MARKET_TASK_RE = re.compile(r"大盘|行情|股票|板块|资金|涨|跌|收盘|市场|指数|A股|美股|港股")


def _recover_tool_name(args: dict) -> str:
    """空工具名恢复（09-21 实测：deepseek-v4 流式 tool_calls 名称字段为空串，
    参数却完整——按参数键与工具 JSON Schema 匹配推断名称）。

    09-06 14:34 事故：{"url": …} 同时命中 dokobot_read/headless_read（多候选）
    → 恢复放弃 → 守卫反馈循环 3 连击 → 任务中止。多候选改为确定性择优：
    参数键与工具 schema 完全等同者优先；仍平手取注册表顺序首个——执行后
    工具结果会引导模型，优于中止。"""
    if not isinstance(args, dict) or not args:
        return ""
    from agent_loop import TOOLS  # 惰性导入：注册表在宿主模块装配
    keys = set(args.keys())
    candidates = []
    exact = []
    for t in TOOLS:
        fn = t.get("function", {}) or {}
        params = fn.get("parameters") or {}
        props = params.get("properties") or {}
        pkeys = set(props.keys())
        if pkeys and keys <= pkeys:
            candidates.append(fn.get("name", ""))
            if keys == pkeys:
                exact.append(fn.get("name", ""))
    if not candidates:
        return ""
    if len(set(candidates)) == 1:
        return candidates[0]
    if len(set(exact)) == 1:
        return exact[0]
    return candidates[0]


def _candidate_tool_names(args: dict) -> list:
    """与参数键匹配的全部候选工具名（供守卫消息给出可操作提示）。"""
    if not isinstance(args, dict) or not args:
        return []
    from agent_loop import TOOLS  # 惰性导入：注册表在宿主模块装配
    keys = set(args.keys())
    out = []
    for t in TOOLS:
        fn = t.get("function", {}) or {}
        params = fn.get("parameters") or {}
        pkeys = set((params.get("properties") or {}).keys())
        if pkeys and keys <= pkeys:
            out.append(fn.get("name", ""))
    return out


def _ensure_market_tools(active_tools: list, user_text: str) -> list:
    """行情类问题保底工具：_cap_tools 按序裁剪会丢 mx_query/ak_finance
    （09-21 实测：模型工具列表 8 个无 mx_query）——命中市场关键词时补回。"""
    if _MARKET_TASK_RE.search(user_text or ""):
        from agent_loop import TOOLS  # 惰性导入：注册表在宿主模块装配
        have = {t.get("function", {}).get("name") for t in active_tools}
        out = list(active_tools)
        for t in TOOLS:
            n = t.get("function", {}).get("name")
            if n in ("mx_query", "ak_finance") and n not in have:
                out.append(t)
        return out
    return active_tools


ACCESS_LEVELS = {"read_only", "confirm", "auto_edit", "plan", "full"}
# 旧版本 workspace 档位迁移到 auto_edit（语义对应）
_LEGACY_ACCESS_MAP = {"workspace": "auto_edit"}


def _normalize_access(mode: str) -> str:
    """归一化权限档位。未知值拒绝升格（审计 H2：此前 else "full" 静默
    升权——`confirm` 是产品承诺的默认档：高点操作每次确认）。"""
    if mode in _LEGACY_ACCESS_MAP:
        return _LEGACY_ACCESS_MAP[mode]
    if mode in ACCESS_LEVELS:
        return mode
    return "confirm"


def _filter_tools_by_access(tools: list[dict], access: str) -> list[dict]:
    """按权限模式过滤工具列表（读时过滤 + 执行时拦截双保险）。"""
    access = _normalize_access(access)
    if access != "read_only":
        return tools
    out = [t for t in tools if t.get("function", {}).get("name", "") in READ_ONLY_TOOLS]
    return out or [t for t in tools if t.get("function", {}).get("name") in READ_ONLY_TOOLS]


def _check_access(tool_name: str, access: str) -> str | None:
    """执行时权限拦截：返回拒绝原因或 None（放行）。read_only 档强制只读，其余档不拦截。"""
    access = _normalize_access(access)
    if access == "read_only" and tool_name not in READ_ONLY_TOOLS:
        return f"⛔ 当前为只读模式，工具 {tool_name} 不可用。请切换到自动编辑/计划模式/完全访问后重试。"
    return None


def _filter_tools(user_text: str, all_tools: list[dict], scheduling_shortcut: bool = True) -> list[dict]:
    """Return a filtered tool list based on user intent. Falls back to all tools if uncertain.

    scheduling_shortcut=False：定时任务执行（cron）时禁用"定时意图短路"——
    cron 任务文本自带「定时分析」字样，会被短路成只有 create_cron+read_file，
    金融查询工具全没（09-01 14:00 事故：交易时段无工具可用→空响应放弃）。
    """
    if not user_text or len(user_text) < 3:
        return all_tools
    allowed_categories: set[str] = set()
    for pattern, cats in INTENT_PATTERNS:
        if pattern.search(user_text):
            allowed_categories.update(cats)
    if not allowed_categories:
        return all_tools  # No match = keep all tools
    allowed_tools: set[str] = set()
    # 定时意图优先短路：建任务只需 create_cron + read_file，其余工具（金融/
    # 控制等）对"创建定时任务"是噪音——9B 小模型面对 16 个工具描述会迷失，
    # 反复"我先查清楚"而不调 create_cron（09-01 10:13 事故）
    if scheduling_shortcut and "scheduling" in allowed_categories:
        allowed_tools.update({"create_cron", "read_file"})
        allowed_tools.update({"use_skill", "delegate_task"})
        return [t for t in all_tools if t.get("function", {}).get("name") in allowed_tools] or all_tools
    if "scheduling" in allowed_categories:
        # cron 执行场景：去掉 scheduling 分类本身（含 create_cron），
        # 让金融/文件等真实任务类别决定工具集
        allowed_categories.discard("scheduling")
    for cat in allowed_categories:
        allowed_tools.update(TOOL_CATEGORIES.get(cat, []))
    # Always include read_file as fallback
    allowed_tools.add("read_file")
    # 元工具保底：create_cron/delegate_task/use_skill 不属于任何意图分类，
    # 意图过滤后模型根本看不到它们——"每10分钟分析大盘"被归 financial 后
    # create_cron 被滤掉，模型只能口嗨"我来搭"而无法真正创建定时任务
    # （09-01 事故）。这类跨任务元工具始终保留。
    allowed_tools.update({"create_cron", "delegate_task", "use_skill"})
    # 控制类工具保底：用户意图五花八门（"看看电脑状态"→file_read），
    # 若把控制工具滤掉，模型无法完成进程/鼠标/屏幕操作——有明确控制意图时
    # 保留全部控制工具；无控制意图时仅保留轻量只读控制（list/audit/wait）
    if "control" in allowed_categories:
        allowed_tools.update(CONTROL_TOOL_NAMES)
    else:
        allowed_tools.update({"control_list_processes", "control_audit", "control_wait",
                              "control_process_log", "screen_capture"})
    # 金融意图同时保留 web 工具：美股/港股等境外市场 mx_query 查不到，
    # 需要 tavily 联网搜索——此前 financial 只给 mx_query/ak_finance，
    # 模型想搜行情时工具被白名单过滤、空响应收场（P0-1）
    if "financial" in allowed_categories:
        allowed_tools.update(TOOL_CATEGORIES.get("web", []))
    # Only add web/financial tools when relevant (not unconditionally)
    if "financial" not in allowed_categories and "web" not in allowed_categories:
        allowed_tools.add("tavily_search")
        allowed_tools.add("mx_query")
        allowed_tools.add("bing_search")
        allowed_tools.add("ak_finance")
    filtered = [t for t in all_tools if t.get("function", {}).get("name") in allowed_tools]
    return filtered if filtered else all_tools



def _cap_tools(tools: list[dict], cap: int = 8, keep_first: tuple[str, ...] = ()) -> list[dict]:
    """Cap tool count, keeping essential tools (read_file, write_file, list_dir) first.
    先去重（DeepSeek 等 API 要求工具名唯一，重复名字直接 400）。
    keep_first：额外优先保留的工具名（如 cron 金融任务必须保留 mx_query——
    全局优先级里它排在 read/tavily 之后，cap 5 会被裁掉，任务无金融工具
    可用 → 空响应放弃，09-01 14:00 事故第二层）。"""
    seen: set[str] = set()
    uniq: list[dict] = []
    for t in tools:
        n = t.get("function", {}).get("name")
        if n and n not in seen:
            seen.add(n)
            uniq.append(t)
    essential = {"read_file", "write_file", "list_dir"} | set(keep_first)
    priority = [t for t in uniq if t.get("function", {}).get("name") in essential]
    others = [t for t in uniq if t.get("function", {}).get("name") not in essential]
    return priority + others[:max(0, cap - len(priority))]



def _inject_image(messages: list, image_base64: str, image_mime: str) -> list:
    """Modify the last user message to include an image attachment."""
    msgs = [dict(m) for m in messages]
    for m in reversed(msgs):
        if m.get("role") == "user":
            text = m["content"] if isinstance(m["content"], str) else _extract_last_user_text(msgs)
            m["content"] = [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": f"data:{image_mime};base64,{image_base64}", "detail": "auto"}},
            ]
            break
    return msgs


_FORCED_REASONING = ("deepseek-reasoner",)
_ANTHROPIC_HINTS = ("claude", "anthropic")


def _inject_thinking_disabled(body: dict, model: str, level: str = "high") -> dict:
    """思考强度三档（对应前端 🧠 选择器）：off / high(默认) / max。

    按模型族正确设置（此前 off 只写 Anthropic 字段 `thinking`，DeepSeek/OpenAI
    忽略该字段 → 用户设"关闭思考"无效）：
    - anthropic(claude):  thinking = {type: disabled}（官方关闭方式）
    - openai 推理系(o1/o3/o4/gpt-5): reasoning_effort = "none"（官方关闭方式）
    - deepseek-chat 等非推理: 无需字段（本来就不思考）
    - deepseek-reasoner:  API 层面强制思考、不提供开关 —— 不设无效字段，
      返回 body 并带 _thinking_unsupported 标记，前端据此提示
    """
    m = (model or "").lower()
    body["_thinking_level"] = level
    if level == "off":
        if any(h in m for h in _ANTHROPIC_HINTS):
            body["thinking"] = {"type": "disabled"}
        elif "deepseek-reasoner" in m or m in _FORCED_REASONING:
            body["_thinking_unsupported"] = True  # 无法关闭，提示用户
        elif any(h in m for h in ("o1", "o3", "o4", "gpt-5")) or "reasoner" in m or "r1" in m:
            body["reasoning_effort"] = "none"
        # 其他非推理模型：不设字段（默认不思考）
    elif level == "max":
        # 长推理预算：高于常规推理预算（12288）约 1.5 倍
        body.setdefault("max_tokens", 12288)
        if isinstance(body.get("max_tokens"), int) and body["max_tokens"] < 18432:
            body["max_tokens"] = 18432
        # OpenAI 兼容推理模型支持 reasoning_effort（DeepSeek 不认则该字段忽略）
        if "deepseek" not in m and "o1" not in m and "o3" not in m:
            body["reasoning_effort"] = "high"
    return body


def _sanitize_tool_messages(msgs: list[dict]) -> list[dict]:
    """DeepSeek 等 API 严格校验：assistant 消息带 tool_calls 时，后续必须有
    对应的 tool 结果消息（tool_call_id 一一对应），否则返回 400。
    历史消息可能因工具中断/前端保存丢失 tool 结果 → 自动补空结果消息，避免 400。
    补丁必须紧跟缺失点插入（任何非 tool 消息出现前），保证顺序合法。"""
    out: list[dict] = []
    pending_ids: set[str] = set()
    for msg in msgs:
        role = msg.get("role")
        if role == "assistant" and msg.get("tool_calls"):
            if pending_ids:  # 上一条 assistant 的 tool_calls 未响应，先补空
                for tid in pending_ids:
                    out.append({"role": "tool", "tool_call_id": tid, "content": "[工具结果缺失，已自动补空]"})
            pending_ids = {tc.get("id") for tc in msg["tool_calls"] if tc.get("id")}
            out.append(msg)
            continue
        if role == "tool":
            tid = msg.get("tool_call_id")
            if tid in pending_ids:
                pending_ids.discard(tid)
            out.append(msg)
            continue
        if pending_ids:  # 遇到非 tool 消息时，未响应的 tool_call 先补空
            for tid in pending_ids:
                out.append({"role": "tool", "tool_call_id": tid, "content": "[工具结果缺失，已自动补空]"})
            pending_ids = set()
        out.append(msg)
    if pending_ids:  # 消息末尾仍有未响应的 tool_call
        for tid in pending_ids:
            out.append({"role": "tool", "tool_call_id": tid, "content": "[工具结果缺失，已自动补空]"})
    return out


def _merge_system_messages(messages: list) -> list:
    """把所有 system 消息合并进开头的那一个（内容用空行连接）。

    mlx_lm.server v0.31 只接受一个 system 消息且必须在最前面，任何位置的
    第二个 system 都直接 404（"System message must be at the beginning"）。
    nudge 轮在消息列表尾部追加 system（16:16 实测 roles=
    [system, user, assistant, system] 全 404）——必须全部合并。
    """
    sys_contents = [str(m.get("content", "")) for m in messages if m.get("role") == "system"]
    if not sys_contents:
        return messages
    merged_system = {"role": "system", "content": "\n\n".join(c for c in sys_contents if c)}
    out = []
    inserted = False
    for m in messages:
        if m.get("role") == "system":
            if not inserted:
                out.append(merged_system)
                inserted = True
            continue
        out.append(m)
    if not inserted:
        out.insert(0, merged_system)
    return out


# ══ 本地原生工具调用（09-06：mlx_lm.server 原生 tools 参数，替代 3K token
# 围栏文字扮演——真机实测 27B 工具轮 4.5s、围栏格式分钟级）══
class _NativeToolsUnsupported(Exception):
    """引擎拒绝 tools 参数（HTTP 400，模板不支持工具）→ 回退围栏提示词。"""


# 测试覆盖钩子：None=按引擎自动判定；True/False=强制
_LOCAL_NATIVE_TOOLS_OVERRIDE: bool | None = None
# 回退开关：LATIAO_LOCAL_NATIVE=0 一键还原围栏路径（结构化契约出问题时）
if os.environ.get("LATIAO_LOCAL_NATIVE", "") == "0":
    _LOCAL_NATIVE_TOOLS_OVERRIDE = False

# 任务关键词（与 agent_loop_v2._TASK_KW 同表；v1 原为内联列表，提出来共用）
_TASK_KW = ("运行", "执行", "做", "帮我", "写", "创建", "查", "搜", "找", "分析",
            "修复", "构建", "部署", "安装", "配置", "列出", "读取", "读", "总结",
            "生成", "打开", "查看", "解释", "整理", "统计", "告诉",
            "run", "build", "fix", "create", "search", "analyze", "deploy",
            "list", "read", "summar", "write", "explain", "tell")

# 原生模式下的精简系统提示（工具经 API tools 参数传入，不再文字罗列）
_NATIVE_LEAN_PROMPT = (
    "# 工具\n可用工具已随请求提供（function calling）。需要时直接调用；"
    "不需要工具时直接回复用户。\n"
    "# 输出纪律\n"
    "1. 不要输出执行计划或「我将要…」声明——要么调用工具，要么直接写出完整回答。\n"
    "2. 工具结果返回后，把完整结论（含关键数字）写进回复正文。"
)

_NATIVE_FOLLOWUP_PROMPT = (
    "⚠️ 任务尚未完成：继续调用工具，或如果所有步骤已完成，直接给出最终完整回答。"
)

# 完成确认轮（native 新契约的唯一"继续压力"）：一轮内还没有任何工具调用时，
# 第一条纯文本回复会被追问一次——任务没完成就动手，完成了就重发最终回答。
# 结构化规则（零工具调用计数），不做任何文本内容猜测。
_NATIVE_COMPLETION_CHECK_PROMPT = (
    "⚠️ 完成确认：你上一条回复没有调用任何工具。\n"
    "若任务尚未完成——现在立即调用工具执行，不要再写计划或声明；\n"
    "若你上一条已经是完整最终回答——请原样重发该回答（含结论与关键数据），不要增删。"
)


def _local_native_tools_ok() -> bool:
    """本地引擎是否支持原生 function calling（自管 mlx 引擎；LM Studio/
    llama.cpp 外部引擎不启用——未验证，保持围栏路径）。"""
    if _LOCAL_NATIVE_TOOLS_OVERRIDE is not None:
        return _LOCAL_NATIVE_TOOLS_OVERRIDE
    eng = getattr(local_llm, "_engine", None)
    if eng is None or getattr(eng, "_external_engine", ""):
        return False
    return (getattr(eng, "_active_backend", "") == "mlx"
            or getattr(eng, "backend", "") == "mlx")


def _slim_history_for_local(msgs: list, keep_recent_turns: int = 3) -> list:
    """本地模型历史预算：近 N 轮完整，更早轮次截断（首尾保留）。

    31 条重历史（整文件内容+全部工具结果+历史⛔错误）使首 token 45s+ 且
    模型注意力被稀释（复读/声明式收工，09-06 15:19 事故）。只截 content、
    不动角色结构（tool_calls 与 tool 结果配对安全）。以 user 消息为轮边界。
    """
    if len(msgs) <= 12:
        return msgs
    user_idx = [i for i, m in enumerate(msgs) if m.get("role") == "user"]
    if len(user_idx) <= keep_recent_turns:
        return msgs
    cutoff = user_idx[-keep_recent_turns]

    def _clip(s: str, head: int, tail: int) -> str:
        if len(s) <= head + tail + 20:
            return s
        return s[:head] + f"\n…(历史截断，原文 {len(s)} 字符)…" + s[-tail:]

    out = []
    for i, m in enumerate(msgs):
        if i >= cutoff or m.get("role") == "system":
            out.append(m)
            continue
        c = m.get("content")
        if not isinstance(c, str) or not c:
            out.append(m)
            continue
        if m.get("role") == "tool":
            out.append({**m, "content": _clip(c, 200, 200)})
        elif m.get("role") == "assistant":
            out.append({**m, "content": _clip(c, 400, 200)})
        else:
            out.append({**m, "content": _clip(c, 600, 200)})
    return out


def _is_light_query(text: str, msgs: list) -> bool:
    """闲聊快车道判定：短输入、无任务词、会话至今无工具使用。

    命中后本轮不传工具、不注入工具提示、关闭思考（chat_template_kwargs
    enable_thinking=false，真机实测 27B 闲聊 7.0s → 1.3s）。"""
    t = (text or "").strip()
    if not t or len(t) > 24:
        return False
    if any(kw in t.lower() for kw in _TASK_KW):
        return False
    # 市场类问题（行情/资金/涨跌……）必须保留工具——快车道剥掉工具后
    # 模型只会凭印象编数字（09-08 20:14 实况："今天大盘资金流向怎么样"
    # 19 字未命中任务词 → tools=0 → 编造主力净流出 118 亿、每板块 ±118 亿）
    if _MARKET_TASK_RE.search(t):
        return False
    # 会话里出现过工具结果 → 可能是任务续聊（如"继续"），不走快车道
    if any(m.get("role") == "tool" for m in msgs or []):
        return False
    # 深会话保护（09-07 17:14 事故）：前端回传的历史不含 role:"tool" 消息，
    # 上一条保护形同虚设——任务进行中的会话（24-28 条历史）里，"继续"/
    # "重新去查数据"这类短消息是任务续聊而非闲聊。误走快车道会把工具全剥掉，
    # 模型只能"说要查"却无工具可调（用户看到连续静默停止 + 逼出数据编造）。
    if len(msgs or []) > 8:
        return False
    return True


def _resolve_max_tokens(model: str) -> int:
    """Pick max_tokens by model family.

    Reasoning models (DeepSeek-R1, Qwen3-QwQ, OpenAI o-series, *-think/*-reason)
    emit long <think> blocks that can exhaust a 4096 cap and truncate the
    trailing tool_call JSON. Give them a larger budget so the JSON survives;
    non-reasoning models get a smaller, cheaper budget.
    """
    m = (model or "").lower()
    if any(k in m for k in ("r1", "o1", "o3", "o4", "reason", "qwq", "qwen3", "think", "muse", "glimmer", "deepseek")):
        return 12288
    return 6144


# Session state tracking: session_id → {phase, round, stalled_rounds, last_action}
_CHAT_MARKERS = (
    "你能做什么", "你能干什么", "你可以做什么", "你会什么", "你会哪些",
    "你有什么功能", "有哪些功能", "你有什么能力", "你是谁", "介绍一下",
    "自我介绍一下", "自我介绍", "谢谢", "感谢", "你好", "在吗", "你好呀",
    "hello", "hi", "what can you do", "who are you",
)


def _is_chat_query(text: str) -> bool:
    """闲聊/能力介绍类查询判定（空输入按闲聊处理：无任务可做）。"""
    t = (text or "").strip().lower()
    if not t:
        return True
    return any(m in t for m in _CHAT_MARKERS)


def _strip_transient_reminders(messages: list) -> list:
    """新回合到达时清除上一轮注入的一次性系统提醒。

    这些提醒是 nudge 指令（"这不是用户的新消息""你上一轮的回复是空的"等），
    只对注入的那一轮有效；留在历史里会让模型在新回合产生"用户没发新消息"
    的自我怀疑（09-20 实测：英文思考反复纠结 The user hasn't sent a new
    message yet 后才生成回复）。一次性指令不该作为永久上下文存在。
    """
    return [
        m for m in messages
        if not (isinstance(m, dict) and m.get("role") == "system"
                and any(mk in str(m.get("content", "")) for mk in _TRANSIENT_REMINDER_MARKERS))
    ]


_TRANSIENT_REMINDER_MARKERS = (
    "这不是用户的新消息",
    "这是系统提醒",
    "你上一轮的回复是空的",
    "你刚才收到了工具的执行结果，但只回复了文字",
    "你刚才收到了工具的执行结果，但你的回复里没有给出实质内容",
    "不要写执行计划，直接行动",
    "你之前只回复了文字而没有继续调用工具",
    "你已通过工具获得了实质数据",
    "上一个工具调用失败了",
    "你上一轮只说了计划/声明而没有执行",
    "数据来源校验",
)


def _detect_user_language(text: str) -> str:
    """Detect the language of user input: 'zh', 'en', or 'ja'."""
    if not text:
        return "zh"
    # 剥离 URL/网址再计数：链接里的字母远多于中文消息的汉字数，
    # 不剥离会把"中文+链接"误判为 en，触发强制英文回复规则（09-03 事故）
    text = re.sub(r"https?://\S+|www\.\S+", "", text)
    # Count characters in each language range
    zh = len(re.findall(r'[\u4e00-\u9fff\u3400-\u4dbf]', text))
    ja_kana = len(re.findall(r'[\u3040-\u309f\u30a0-\u30ff]', text))
    en = len(re.findall(r'[a-zA-Z]', text))
    if ja_kana > zh and ja_kana > en:
        return "ja"
    if en > zh + ja_kana:
        return "en"
    return "zh"


def _get_localized_text(lang: str, texts: dict[str, str | dict]) -> str | dict:
    """Get localized text for a given language, falling back to zh."""
    return texts.get(lang) or texts.get("zh", "")


