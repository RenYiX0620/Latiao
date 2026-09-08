"""解析层：SSE delta 行、原生/围栏工具调用解析、kv 参数、循环调试日志。"""
import json
import os
import re
import tempfile
import logging

logger = logging.getLogger("latiao-sidecar")


_NATIVE_TOOL_RE = re.compile(
    # Gemma native format: <|tool_call|>call:name{args}<tool_call|>
    # Tokenizer may strip pipe chars, so be flexible about them
    r"<\s*\|?\s*tool_call\s*\|?\s*>call:(\w+)\{(.*?)\}<\s*\|?\s*tool_call\s*\|?\s*>",
    re.DOTALL | re.IGNORECASE,
)

# Filter native control token wrappers from displayed content.
# The tool execution itself is shown via tool_start/tool_end events,
# so we just need to suppress raw <|tool_call|> / <|channel> / <channel|> markers.
_NATIVE_CONTROL_RE = re.compile(
    r"<\s*\|?\s*(?:tool_call|channel)\s*\|?\s*>",
    re.IGNORECASE,
)

# ╔══════════════════════════════════════════════════════╗
# ║  SECTION 6: Parsing & Formatting                     ║
# ║  _parse_native_tool_calls, _parse_prompt_tool_calls  ║
# ╚══════════════════════════════════════════════════════╝

def _parse_native_tool_calls(text: str) -> list[dict]:
    """Parse Gemma-style native tool calls from streamed text.
    Returns OpenAI-format tool_calls list.

    Handles formats like:
      <|tool_call|>call:list_dir{path:<|\"|>.<|\"|>}<tool_call|>
    """
    tool_calls = []
    for idx, m in enumerate(_NATIVE_TOOL_RE.finditer(text)):
        name = m.group(1)
        args_str = m.group(2).strip()
        # Gemma escapes quotes as <|"|> — restore them
        args_str = args_str.replace("<|\"|>", '"')
        # Convert Gemma's {key:value} or {key:"value"} to JSON {"key": "value"}
        args_str = _gemma_args_to_json(args_str)
        try:
            args = json.loads(args_str)
        except json.JSONDecodeError:
            args = _salvage_tool_args(args_str)
        tool_calls.append({
            "id": f"native_{name}_{idx}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
        })
    return tool_calls


def _gemma_args_to_json(raw: str) -> str:
    """Convert Gemma's {key:value} or {key:\"value\"} format to valid JSON.
    Handles flat key-value pairs only (Gemma tool args are never deeply nested)."""
    raw = raw.strip()
    if not raw.startswith("{"):
        raw = "{" + raw
    if not raw.endswith("}"):
        raw = raw + "}"
    # Quote unquoted keys: key: → "key":
    raw = re.sub(r'(?<!")(\b\w+\b)\s*:', r'"\1":', raw)
    # Quote unquoted string values: : value → : "value"
    # Only quote bare words, not already-quoted strings, not numbers, not objects/arrays
    raw = re.sub(
        r':\s*(?!["{\[\-\d])([a-zA-Z_./~][a-zA-Z0-9_./~*@+\-]*)',
        r': "\1"',
        raw,
    )
    return raw


def _salvage_tool_args(args_str: str) -> dict:
    """Last-resort parse of broken tool call arguments."""
    result: dict[str, object] = {}
    for part in args_str.split(","):
        part = part.strip().strip('"').strip("'")
        if ":" in part:
            key, _, val = part.partition(":")
            key = key.strip().strip('"').strip("'")
            val = val.strip().strip('"').strip("'")
            if key:
                result[key] = val
    return result or {"raw": args_str}


def _strip_native_tool_calls(text: str) -> str:
    """Remove native tool call blocks from text, keeping only the real content."""
    return _NATIVE_TOOL_RE.sub("", text).strip()


# ╔══════════════════════════════════════════════════════╗
# ║  SECTION 7: Cloud Agent Loop                         ║
# ║  OpenAI-compatible function calling                   ║
# ╚══════════════════════════════════════════════════════╝

def _append_loop_log(line: str):
    """追加一行到 latiao-loop.log；超过 5MB 时截断保留尾部一半，避免无界增长。"""
    try:
        log_path = os.path.join(tempfile.gettempdir(), "latiao-loop.log")
        if os.path.exists(log_path) and os.path.getsize(log_path) > 5 * 1024 * 1024:
            with open(log_path, "rb") as f:
                f.seek(-(5 * 1024 * 1024 // 2), os.SEEK_END)
                tail = f.read()
            with open(log_path, "wb") as f:
                f.write(tail)
        with open(log_path, "a") as lf:
            lf.write(line)
    except Exception:
        pass  # 调试日志写失败不影响主流程


def _parse_delta_line(line: str) -> tuple[bool, dict | None]:
    """统一 SSE 行解析（阶段 4 增量收敛点）：云/本地两循环共用唯一实现。

    语义对齐旧的"json.loads + choices[0].get('delta')"块（逐行为等价）：
    - (True, None)          → [DONE]
    - (False, None)         → usage-only / 坏行 → 调用方 continue（旧: continue/pass）
    - (False, {})           → choices 存在但 delta 空 → 调用方照旧计数（旧行为保持）
    - (False, delta_dict)   → 规范化 delta（content/reasoning/tool_calls 同旧形状）
    工具调用增量按旧结构重建（{index,id,function:{name,arguments}}），
    下游 tool_call_bufs 累积逻辑一行不改。
    """
    from model_adapters import OpenAICompatAdapter
    parsed = OpenAICompatAdapter.parse_sse_data_line(line)
    if parsed.done:
        return True, None
    if parsed.parse_error:
        return False, None
    chunks = parsed.chunks
    if not chunks:
        return False, {}
    # finish_reason 透传（09-07 提速/完整性：max-tokens 截断的 tool-call
    # 必须在执行层丢弃——DSH BlockAssembler 同款语义。引擎有时把 finish
    # 放在 usage-only 行，故在 usage 过滤之前提取）
    for c in chunks:
        if c.kind == "finish" and c.finish_reason:
            return False, {"finish_reason": c.finish_reason}
    if all(c.kind == "usage" for c in chunks):
        return False, None  # usage-only chunk（旧: if not choices: continue）
    delta: dict = {}
    texts = [c.text for c in chunks if c.kind == "text" and c.text]
    reasons = [c.reasoning for c in chunks if c.kind == "reasoning" and c.reasoning]
    tcs = [c.tool_call for c in chunks if c.kind == "tool_call" and c.tool_call]
    if texts:
        delta["content"] = "".join(texts)
    if reasons:
        delta["reasoning"] = "".join(reasons)
    if tcs:
        delta["tool_calls"] = [
            {"index": t.index, "id": t.id,
             "function": {"name": t.name, "arguments": t.arguments}}
            for t in tcs
        ]
    return False, delta


_PROMPT_TOOL_FENCE_RE = re.compile(
    r'```tool\s+(\w+)\s*\n(.*?)\n```',
    re.DOTALL | re.IGNORECASE,
)

# OpenAI 风格 json 栅栏：```json {"name": "...", "arguments": {...}} ```
# Qwen3.8/MoziAI 等 27B 级模型实测输出此格式（工具名在 JSON 内部而非栅栏语言位），
# 此前 Fenced/XML/Bare/Inline 四层全不认 → 模型反复正确输出工具调用却被当纯文本，
# nudge 3 次后放弃 → "任务刚开始就停"（09-02 09:14 事故）。
_PROMPT_JSON_FENCE_RE = re.compile(
    r'```json\s*(\{.*?\})\s*```',
    re.DOTALL,
)

# Hermes 风格 XML：Qwen3 系在 prompt-based 模式下常输出
# <tool_call>name<arg_key>k</arg_key><arg_value>v</arg_value></tool_call>
_TOOLCALL_XML_RE = re.compile(
    r'<tool_call>\s*([\w.]+)\s*((?:<arg_key>[^<]*</arg_key>\s*<arg_value>.*?</arg_value>\s*)*)</tool_call>',
    re.DOTALL,
)
_TOOLCALL_KV_RE = re.compile(r'<arg_key>(\w+)</arg_key>\s*<arg_value>(.*?)</arg_value>', re.DOTALL)

_PROMPT_TOOL_RE = re.compile(
    r'(?:\[TOOL:|<tool>|FUNC:)(\w+)\s*(?:\{(.*?)\}|"(.*?)"|(.*?))(?:\]|</tool>|$)',
    re.DOTALL | re.IGNORECASE,
)

# Natural language fallback: matches "web_search \"query\"" or "search \"query\"" etc.
# 09-21 收紧（Nous Hermes 立场：工具执行应由模型结构化调用决定，不做散文推断）：
# 默认关闭；即使开启也只允许只读类工具（可开不可误执行危险性动作）。
_NL_TOOL_FALLBACK_ENABLED = False
_NL_TOOL_READONLY = frozenset({"read_file", "list_dir", "search_files", "headless_read",
                               "web_search", "tavily_search", "bing_search"})
_NL_TOOL_RE = re.compile(
    r'\b(web_search|tavily_search|search|read_file|write_file|list_dir|run_cmd|open_app|open_folder|search_files)\s*[\(\[""]\s*([^\")\]\.]+)\s*[\)\]""]',
    re.IGNORECASE,
)

# Bash/shell code block fallback: ```bash\nls -la /path\n``` → run_cmd
_BASH_BLOCK_RE = re.compile(
    r'```(?:bash|sh|shell|zsh)\s*\n(.*?)\n```',
    re.DOTALL | re.IGNORECASE,
)

# 模型输出的 ```think> / ```think< 围栏（Kimi 风格思维链）：围栏标记会让
# 前端 ReactMarkdown 把它当作未闭合代码块 → 后续正文全部渲染成灰框代码块
_LS_CMD_RE = re.compile(r'^\s*ls\s+(?:-\w+\s+)*["\']?([/\~]\S+|\.\S*)\s*$', re.IGNORECASE)
_CAT_CMD_RE = re.compile(r'^\s*cat\s+["\']?([/\~]\S+)\s*$', re.IGNORECASE)
_FIND_CMD_RE = re.compile(r'^\s*find\s+["\']?([/\~]\S+)\s+(.*)', re.IGNORECASE)


def _parse_prompt_tool_calls(text: str) -> tuple[str, list[dict]]:
    """Parse tool calls from text generated by a local model (prompt-based).
    Returns (cleaned_text, tool_calls_in_openai_format)."""
    tool_calls = []
    used_ranges = []  # track char ranges to strip from text

    # For Qwen models that embed <think>...</think> blocks inside content output:
    # strip think blocks before parsing — reasoning should not contain tool invocations.
    search_text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    if not search_text:
        search_text = text  # fallback if everything was think

    # Priority 1: Fenced format ```tool name\n{json}\n```
    for idx, m in enumerate(_PROMPT_TOOL_FENCE_RE.finditer(search_text)):
        name = m.group(1)
        args_str = m.group(2).strip()
        try:
            args = json.loads(args_str)
        except json.JSONDecodeError:
            args = _salvage_tool_args(args_str)
        tool_calls.append({
            "id": f"local_fence_{name}_{idx}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
        })
        used_ranges.append((m.start(), m.end()))

    # Priority 1.2: ```json {"name": ..., "arguments": {...}} ``` —— OpenAI 风格
    # json 栅栏（Qwen3.8/MoziAI 27B 实测输出格式），此前四层全不认 → 工具调用
    # 被当纯文本，任务"刚开始就停"
    if not tool_calls:
        for idx, jm in enumerate(_PROMPT_JSON_FENCE_RE.finditer(search_text)):
            try:
                obj = json.loads(jm.group(1))
            except json.JSONDecodeError:
                continue
            if not (isinstance(obj, dict) and obj.get("name") and "arguments" in obj):
                continue
            tool_calls.append({
                "id": f"local_json_{obj['name']}_{idx}",
                "type": "function",
                "function": {"name": str(obj["name"]),
                             "arguments": json.dumps(obj["arguments"], ensure_ascii=False)},
            })
            used_ranges.append((jm.start(), jm.end()))

    # Priority 1.4: Hermes 风格 XML —— Qwen3 系 prompt-based 模式常输出
    # <tool_call>name<arg_key>k</arg_key><arg_value>v</arg_value></tool_call>，
    # 此前不解析 → 工具调用原样交付用户、无工具执行（09-01 22:5x 事故）
    if not tool_calls:
        for x_idx, xm in enumerate(_TOOLCALL_XML_RE.finditer(search_text)):
            _xname = xm.group(1)
            _xargs = {}
            for kv in _TOOLCALL_KV_RE.finditer(xm.group(2)):
                _xargs[kv.group(1)] = kv.group(2)
            tool_calls.append({
                "id": f"local_xml_{_xname}_{x_idx}",
                "type": "function",
                "function": {"name": _xname, "arguments": json.dumps(_xargs, ensure_ascii=False)},
            })
            used_ranges.append((xm.start(), xm.end()))

    # Priority 1.5: Bare JSON tool call — 推理模型（Qwen3.8 等）常直接输出
    # "我来查...{"query": "..."}" 的裸 JSON（无 ```tool 栅栏），解析器不认 →
    # JSON 残留在文本里 → 模型复读同样内容 → 复读检测误判循环截断（22:06 事故）。
    # 这里识别"独立 JSON 对象且参数命中文档化工具名"的裸调用，并剥离。
    if not tool_calls:
        _bare_json_re = re.compile(r'\{\s*"(?:query|path|cmd|pattern|url|command)"\s*:\s*"[^"]*"\s*[,}]', re.DOTALL)
        for b_idx, m in enumerate(_bare_json_re.finditer(search_text)):
            # 往前看 30 字符是否有"动/查/读/搜"等动作词（避免误判正文 JSON）
            prefix = search_text[max(0, m.start()-40):m.start()]
            if not re.search(r'[动查读搜取，。]\s*$', prefix) and not re.search(r'[请让我先我再来]', prefix):
                continue
            json_obj = m.group(0)
            try:
                args = json.loads(json_obj)
            except Exception:
                continue
            # 根据参数推工具名：query→mx_query/tavily_search；path→read_file；cmd→run_cmd
            key = next((k for k in args if k in ("query", "path", "cmd", "url")), None)
            if not key:
                continue
            tool_name = ("read_file" if key == "path"
                         else "run_cmd" if key == "cmd"
                         else "tavily_search" if key == "url"
                         else "mx_query")
            tool_calls.append({
                "id": f"local_bare_{tool_name}_{b_idx}",
                "type": "function",
                "function": {"name": tool_name, "arguments": json.dumps(args, ensure_ascii=False)},
            })
            used_ranges.append((m.start(), m.end()))
            break  # 一次只处理一个裸调用（避免误吞多对象）

    # Priority 2: Inline format [TOOL:name key=value ...] or <tool>name{json}</tool> or FUNC:name key=value
    if not tool_calls:
        for idx, m in enumerate(_PROMPT_TOOL_RE.finditer(search_text)):
            name = m.group(1)
            json_str = m.group(2)
            quoted = m.group(3)
            rest = m.group(4)
            if json_str:
                try:
                    args = json.loads("{" + json_str + "}")
                except json.JSONDecodeError:
                    args = _salvage_tool_args(json_str)
            elif quoted:
                args = {"query": quoted} if name in ("web_search", "tavily_search", "search") else {"path": quoted}
            elif rest:
                args = _parse_kv_args(rest.strip())
            else:
                args = {}
            if not args:
                continue
            tool_calls.append({
                "id": f"local_inline_{name}_{idx}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
            })
            used_ranges.append((m.start(), m.end()))

    # Priority 3: Natural language fallback — "web_search \"query\"" etc.
    # 09-21 收紧：默认关闭（_NL_TOOL_FALLBACK_ENABLED），开启时仅限只读工具。
    if not tool_calls and _NL_TOOL_FALLBACK_ENABLED:
        for idx, m in enumerate(_NL_TOOL_RE.finditer(search_text)):
            name = m.group(1).lower()
            if name == "search":
                name = "web_search"
            if name not in _NL_TOOL_READONLY:
                continue
            raw_query = m.group(2).strip()
            if not raw_query:
                continue
            # Build args based on tool type
            if name in ("web_search", "tavily_search"):
                args = {"query": raw_query}
            elif name in ("read_file", "write_file", "open_app", "open_folder"):
                args = {"path": raw_query}
            elif name == "list_dir":
                args = {"path": raw_query}
            elif name == "run_cmd":
                args = {"cmd": raw_query}
            elif name == "search_files":
                args = {"pattern": raw_query}
            else:
                args = {"query": raw_query}
            tool_calls.append({
                "id": f"local_nl_{name}_{idx}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
            })
            used_ranges.append((m.start(), m.end()))

    # Priority 4: Bash/shell code block fallback — models often output ```bash
    # instead of the taught ```tool format; parse ls/cat/find into our tools
    if not tool_calls:
        for idx, m in enumerate(_BASH_BLOCK_RE.finditer(search_text)):
            cmd_text = m.group(1).strip()
            if not cmd_text:
                continue
            tool_name = ""
            args = {}
            ls_match = _LS_CMD_RE.match(cmd_text)
            cat_match = _CAT_CMD_RE.match(cmd_text)
            find_match = _FIND_CMD_RE.match(cmd_text)
            if ls_match:
                tool_name = "list_dir"
                args = {"path": ls_match.group(1) or "."}
            elif cat_match:
                tool_name = "read_file"
                args = {"path": cat_match.group(1)}
            elif find_match:
                tool_name = "search_files"
                args = {"directory": find_match.group(1), "pattern": find_match.group(2).strip()}
            else:
                tool_name = "run_cmd"
                args = {"cmd": cmd_text}
            if tool_name:
                tool_calls.append({
                    "id": f"local_bash_{tool_name}_{idx}",
                    "type": "function",
                    "function": {"name": tool_name, "arguments": json.dumps(args, ensure_ascii=False)},
                })
                used_ranges.append((m.start(), m.end()))

    # Clean text by removing parsed tool call regions
    # 关键：used_ranges 是在 search_text（去 think 块+strip）上计算的，
    # 必须对同一坐标系清洗——此前回放到原始 text 上导致偏移错位：
    # 工具栅栏残留在历史里、think 块被拦腰截断，模型下一轮重复调用。
    clean = search_text
    if used_ranges:
        # Remove from end to start to preserve offsets
        for start, end in sorted(used_ranges, reverse=True):
            clean = clean[:start] + clean[end:]
        clean = clean.strip()

    return clean, tool_calls


def _parse_kv_args(raw: str) -> dict:
    """Parse key=value or key=\"value\" pairs from a raw string."""
    result = {}
    # Match: key="value" or key=value
    for m in re.finditer(r'(\w+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|(\S+))', raw):
        key = m.group(1)
        val = m.group(2) or m.group(3) or m.group(4)
        result[key] = val
    return result


