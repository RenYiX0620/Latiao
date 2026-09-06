"""文本质量守卫：复读检测/截尾、去重、元收尾与工具幻觉识别、think 围栏过滤。"""
import re
import logging

logger = logging.getLogger("latiao-sidecar")


def _deduplicate_response(text: str) -> str:
    """Remove repeated identity introductions. Keeps only the first complete one."""
    if not text:
        return text
    # 性能护栏（09-21 实测 4.2M 字符累积流 → 锚定正则 O(n) 每次×每 40 delta
    # = CPU 爆炸 100% 挂死）：介绍去重只关心开头，超长文本截到 2 万字符。
    if len(text) > 20_000:
        text = text[:20_000]
    # Pattern: text starts with "我是辣条...", then finds "我是辣条" again
    import re
    for prefix in ["我是辣条", "我是 LaTiao", "我是LaTiao", "我是Latiao", "我叫辣条", "我是拉条"]:
        pattern = re.escape(prefix)
        m = re.search(f'^({pattern}.*?){pattern}', text, re.DOTALL)
        if m:
            return m.group(1).strip()
        m = re.search(f'^(你好[，！、\\s]*{pattern}.*?)(?:你好[，！、\\s]*)?{pattern}', text, re.DOTALL)
        if m:
            return m.group(1).strip()
    return text


def _detect_text_loop(text: str) -> bool:
    """检测流式输出陷入复读循环（同一片段连续重复）。

    本地小模型长生成时偶发：最后两三句话无限重复直到 max_tokens。
    判定：取尾部窗口，若存在长度 ≥12 字符的片段在尾部连续重复 ≥3 次
    （或尾部 200 字符内同一片段出现 ≥4 次），判为循环。
    09-04 扩充：27B 模型的"元认知死循环"重复段落约 180 字（自我对话
    "打破循环→但用户未指定→让我查询"整段循环），旧上限 100 字符成
    盲区——片段长度上限扩到 400 字符。
    性能：只在流式累积每 ~2KB 时调用一次，非逐 token。"""
    if not text or len(text) < 120:
        return False
    tail = text[-1200:]
    n = len(tail)
    # 尝试所有可能的循环片段长度（12 ~ 400 字符）
    for plen in range(12, min(400, n // 3) + 1):
        piece = tail[-plen:]
        if piece.strip() != piece or len(piece.strip()) < 12:
            continue
        # 该片段在尾部连续出现次数
        count = 0
        pos = n
        while pos - plen >= 0 and tail[pos - plen:pos] == piece:
            count += 1
            pos -= plen
        if count >= 3:
            return True
    # 兜底：短/中片段高频重复（如"好的，"×8，或 180 字段落×5）。注意 \1
    # 是反向引用——此前误写成字面 \x01 控制字符，兜底永远不匹配（P1-10）
    import re as _re
    m = _re.search(r"(.{6,400}?)\1{3,}$", tail, _re.DOTALL)
    if m:
        return True
    return False


class _GenerationLoopError(Exception):
    """生成复读循环（检测器命中）：本轮生成本地截断，不炸整个会话。

    此前直接 raise TimeoutError 会一路穿到 api_routes 把流收掉——
    闸门/翻译/终答提取全被跳过，用户只拿到一句"已截断"（18:01 事故）。
    改为在流式解析层捕获后 break，已产出文本（裁掉重复尾）交给
    后续工具解析/收尾闸门继续处理。"""


def _strip_repeat_tail(text: str) -> str:
    """复读触发后裁掉尾部重复段，保留首次出现的部分。

    优先用与检测器同款的正则定位重复单元，尾部只留一份（外科手术式）；
    匹配不到时退化为粗裁 200 字符。
    性能护栏（同 dedup）：只处理尾部 2 万字符（该函数只操作尾部）。"""
    if len(text) > 20_000:
        text = text[-20_000:]
    for _ in range(10):
        if not _detect_text_loop(text):
            break
        m = re.search(r"(.{6,60}?)\1{2,}$", text, re.DOTALL)
        if m:
            text = text[: m.start()] + m.group(1)
        else:
            text = text[: max(0, len(text) - 200)]
    return text


def _extract_magnitude_numbers(text: str) -> set[str]:
    """提取"量级数字"（用于数据真伪核对）：带 亿/万亿 后缀的数 + ≥10 万裸数。

    归一化：'15.6亿元' → '亿:15.6'；'242742.51'（百万单位成交额等）→ 'raw:242742.51'。
    指数点位（3942.09）、建议类小数字（3900 点位、止损 5%）不进入校验，防误伤。"""
    out: set[str] = set()
    for u in ("万亿", "亿"):
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*" + u, text):
            out.add(f"{u}:{round(float(m.group(1)), 2)}")
    for m in re.finditer(r"(?<![\d.])(\d{6,}(?:\.\d+)?)(?![\d.])", text):
        out.add(f"raw:{float(m.group(1))}")
    return out


def _find_unsourced_numbers(reply: str, current_msgs: list) -> list[str]:
    """回复中的量级数字减去本会话全部工具结果 + 用户消息内联数据的量级数字并集
    → 无来源数字。

    19:05 事故：模型编造'北向净流入15.6亿/主力净流出80亿'，全库工具结果
    从未返回过——会被此校验命中。工具结果截断（3000 字符）只影响少量
    尾部数字的漏报，不影响本机制防编造的目的。
    用户消息也算合法来源（09-05 13:06 事故："分析这个文件"+内联表格数据，
    报价量 7557.9105t 等本就在用户消息里，不算编造）。"""
    if not reply:
        return []
    src_ctx = " ".join(
        str(m.get("content") or "") for m in current_msgs
        if m.get("role") in ("tool", "user"))
    unsourced = _extract_magnitude_numbers(reply) - _extract_magnitude_numbers(src_ctx)
    if not unsourced:
        return []
    readable = []
    for tag in sorted(unsourced):
        unit, _, val = tag.partition(":")
        if unit == "raw":
            readable.append(f"{float(val):,.0f}")
        else:
            readable.append(f"{float(val):g}{unit}")
    return readable


def _is_meta_wrapup(text: str) -> bool:
    """判断文本是否为“元评论式收尾”而非实质回答。

    思考类模型（Ornith/Qwen3.8）常把完整分析写在 CoT 里，正文只输出
    “上面的分析已经覆盖了…任务完成”之类的收尾话——用户什么都读不到
    （19:38 事故）。此类文本特征是：含任务完成声明短语、且长度不够
    承载真正的分析（<800 字符）。
    """
    if not text or len(text.strip()) >= 800:
        return False
    lowered = text.lower()
    markers = (
        "任务完成", "已完成", "已经完成", "上面的分析", "已经覆盖",
        "已覆盖", "分析已", "已为你", "任务已", "以上是", "以上内容",
        "task complete", "task is done", "i've already", "i have already",
        "the analysis", "already provided", "已经给出", "已给出",
    )
    hits = sum(1 for m in markers if m in lowered)
    return hits >= 2


def _looks_like_tool_fantasy(text: str) -> bool:
    """判断文本是否为"工具/脚本角色扮演"——模型在思考里幻想自己执行了
    命令、看到了"工具输出"，但实际从未调用任何工具（09-05 13:29 事故：
    思考全文在 openpyxl 讨论 + 'timeout 20 python3 -c' 悬空脚本中结束，
    被收尾回退当作最终答案发给用户）。此类内容禁止作为答案交付。"""
    if not text:
        return False
    low = text.lower()
    markers = (
        "python3", "run_cmd", "openpyxl", "subprocess", "pip install",
        "timeout ", "import ", "print(", "sys.argv", "traceback",
        "bash", "shell", "命令超时", "脚本卡住", "工具输出显示",
        "让我查看工具结果", "让我用更简单的方法",
        "```", "$ ", "2>&1", "head -",
    )
    return sum(1 for m in markers if m in low) >= 2


_THINK_FENCE_RE = re.compile(r"```think\s*[<>]")


def _strip_think_fences(text: str) -> str:
    return _THINK_FENCE_RE.sub("", text)


class _ThinkFenceFilter:
    """逐 delta 清洗 ```think 围栏，能捕获被 tokenizer 拆分的标记。

    LLM 流式输出常把 ```think> 拆成多个 delta（如 ``` / think / >），
    对单个 delta 做正则永远匹配不到。本过滤器在累积缓存中识别拆分中的
    标记：尾部可能拼成 ```think 前缀时暂存等待下一 delta，拼完整后整体
    剥掉；确认是普通代码块（```json 等）再放行（最多延迟 1-2 个 delta）。
    """

    _MAX_PENDING = 64

    def __init__(self) -> None:
        self._pending = ""

    def feed(self, text: str) -> str:
        """输入一个流式增量，返回可安全发送给前端的内容（可为空串）。"""
        if not text:
            return ""
        buf = _THINK_FENCE_RE.sub("", self._pending + text)
        # 尾部可能是拆分中的 ```think 前缀（``` 本身、反引号、或 ``` 后跟字母）
        # → 暂存等待下一 delta，拼完整后由上面的正则整体剥掉
        if re.search(r"```[a-zA-Z]*$", buf) or buf.endswith("`"):
            self._pending = buf
            return ""
        if len(buf) > self._MAX_PENDING:  # 防极端场景卡死，强制放行
            self._pending = ""
            return buf
        self._pending = ""
        return buf

    def finalize(self) -> str:
        """流结束时取回缓存。think 标记中间态剥掉；纯反引号残片保留
        （可能是普通代码块的闭合围栏，丢了会导致代码块不闭合）。"""
        pending, self._pending = self._pending, ""
        if not pending:
            return ""
        if "```think" in pending:
            return _THINK_FENCE_RE.sub(r"```+", "", pending)
        return pending


# 未执行的"规划意图"词形：模型说了要做但整轮没调用工具（21:42 事故的
# 扩充词表——"让我用/让我先/先看看"等声明式话术不可当实质完成收尾；
# 注意避免误伤完整回答开头（"让我来整理一下关键数据…"是实质回答）
def _extract_think_body(text: str) -> str:
    """提取思考段内容：```think>…</think> / ```think>…```think< / <think>…</think>。

    27B Q4 模型常把完整分析写进思考段、正文只留半截声明（16:45/19:38/21:13
    事故）——正文不足时用思考段内容兜底交付。
    """
    m = re.search(
        r"(?:```think\s*[>]|<think>)([\s\S]*?)(?:</think>|```think\s*[<]|```)",
        text,
        flags=re.DOTALL,
    )
    return m.group(1).strip() if m else ""


# Common commands in bash blocks
