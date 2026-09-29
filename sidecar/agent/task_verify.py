"""任务级验证器（gap 清单 P1，2026-09-29）——「这一轮的任务算不算完成」的机械定义。

判据**只来自本轮自己的工具账本**（`note_tool` 记录），**不解析用户意图**：不猜任务
类型，就不会把开放问答误判成"未完成任务"。白名单之外（本轮没有产出型工具）一律
返回 `None`，行为零变化。

三类可机检终点：
1. **写文件**：本轮 `write_file` 的产物必须存在、非空；结构化格式（.json/.xlsx/
   .docx/.pptx/.pdf）还要过结构下限（能解析 / 是带 `[Content_Types].xml` 的 ZIP /
   有 `%PDF` 头尾）——"写了但写成半个文件"不算交付。
2. **跑测试**：本轮最后一次测试类命令的退出码/失败数。正文若**宣称完成**而它是红的
   → 未达标（"测试全绿才算完"的辣条对等物）。
3. **正文声称的产物**：`已保存到 /…/x.xlsx` 这类句式里的路径必须真实存在——
   说到没做到、或凭记忆编了路径，都当场露馅。

反作弊（被验对象完整性，对应文章的"目标误设"失败模式）：
- 本轮**删掉/清空**测试文件 → `hard`（立即交回用户，不给重采机会）；
- 本轮**改写**测试文件且同轮先红后绿 → `soft`（可能真是测试写错了，允许解释一次）。

两级生效（本模块只给判定，策略在调用方 `agent/loop.py`）：
第一次未达标 → 尾部消息要求补齐或如实说明（重采一次）；第二次仍不过 → 交回用户。
`hard` 直接交回。

本模块**只读账本、不写任何计数器**（施工约束 1：闸门不许吞记账）。
"""
import json
import logging
import re
import threading
import zipfile
from pathlib import Path

logger = logging.getLogger("latiao-sidecar")

# 产出型工具：本轮出现过这些工具才启用验证器（白名单闸，开放问答零行为变化）
_OUTPUT_TOOLS = {"write_file", "run_cmd"}

# 测试类命令（"修复类任务"的机械终点）
_TEST_CMD_RE = re.compile(
    r"\b(pytest|py\.test|unittest|jest|vitest|mocha|tox|nox"
    r"|npm\s+(run\s+)?test|pnpm\s+(run\s+)?test|yarn\s+(run\s+)?test"
    r"|cargo\s+test|go\s+test|make\s+test|dotnet\s+test|mvn\s+test)\b")

# 测试文件路径（反作弊的"被验对象"）
_TEST_PATH_RE = re.compile(
    r"(^|/)(tests?/|conftest\.py$|test_[^/]*\.[a-z]+$|[^/]*_test\.[a-z]+$"
    r"|[^/]*\.(test|spec)\.[jt]sx?$|[^/]*\.feature$)")

# 删除/清空测试文件
_RM_RE = re.compile(r"(^|[;&|]\s*)(rm|del|trash|mv)\s")

# 正文里的"完成"宣称（只在"测试是红的"这一条上用作对照）
_CLAIM_DONE_RE = re.compile(
    r"已完成|已修复|修复完成|已解决|已搞定|搞定|全部通过|测试(?:已|全部)?通过|全绿|已通过"
    r"|all (?:the )?tests? pass|tests? pass(?:ed)?|passing|fixed|resolved|completed successfully",
    re.I)

# 正文里的"产物已落地"句式（路径存在性检查）
_CLAIM_PATH_RE = re.compile(
    r"(?:已(?:保存|生成|写入|导出|创建|存为|输出)|保存在|生成于|已落盘|文件已生成)"
    r"[到至为在于：:\s]*\s*(~?/[^\s，。；、）)】\]\"'<>|]+)")

_STRUCT_SUFFIXES = {".json", ".xlsx", ".docx", ".pptx", ".pdf"}

_LEDGER_MAX = 200          # 单会话账本上限（防长任务无界增长）
_LEDGER: dict[str, list] = {}
_LOCK = threading.RLock()


def begin_turn(session_id: str) -> None:
    """本轮开始：清空账本（由 ThinAgentLoop.__init__ 调用）。"""
    with _LOCK:
        _LEDGER[str(session_id or "")] = []


def forget(session_id: str) -> None:
    with _LOCK:
        _LEDGER.pop(str(session_id or ""), None)


def note_tool(session_id: str, tool_name: str, args, result) -> None:
    """记一笔工具调用（`agent/tool_exec._handle_tool_execution` 单点调用）。

    args 可能是 JSON 字符串（native 调用）或 dict（围栏调用）——统一成 dict；
    解析失败的参数记为 {}（只影响"能不能机检"，不影响任何已有行为）。
    """
    if not session_id:
        return
    if isinstance(args, str):
        try:
            args = json.loads(args or "{}")
        except Exception:
            args = {}
    if not isinstance(args, dict):
        args = {}
    with _LOCK:
        led = _LEDGER.setdefault(str(session_id), [])
        led.append({"tool": str(tool_name or ""), "args": args, "result": str(result or "")})
        if len(led) > _LEDGER_MAX:
            del led[: len(led) - _LEDGER_MAX]


def _arg_path(args: dict) -> str:
    return str(args.get("path") or args.get("file") or args.get("directory") or "")


def _cmd_of(args: dict) -> str:
    return str(args.get("command") or args.get("cmd") or args.get("script") or "")


def _is_test_path(p: str) -> bool:
    return bool(_TEST_PATH_RE.search(str(p or "").replace("\\", "/")))


def _exit_code(result: str) -> int | None:
    m = re.search(r"退出码[:：]\s*(\d+)", result) or re.search(
        r"exit(?:\s*code)?[:：]?\s*(\d+)", result, re.I)
    return int(m.group(1)) if m else None


def _failed_count(result: str) -> int:
    m = re.search(r"(\d+)\s+failed", result)
    return int(m.group(1)) if m else 0


def _run_is_red(result: str) -> bool:
    if "超时" in result[:200] or "timeout" in result[:200].lower():
        return True
    code = _exit_code(result)
    if code is not None:
        return code != 0
    return _failed_count(result) > 0


def _structural_problem(fp: Path) -> str:
    """结构化产物的下限检查（返回问题码，'' = 没问题）。"""
    suf = fp.suffix.lower()
    if suf not in _STRUCT_SUFFIXES:
        return ""
    try:
        if suf == ".json":
            json.loads(fp.read_text(encoding="utf-8"))
            return ""
        if suf == ".pdf":
            raw = fp.read_bytes()
            if not raw.startswith(b"%PDF"):
                return "not a PDF (missing %PDF header)"
            if b"%%EOF" not in raw[-2048:]:
                return "truncated PDF (missing %%EOF)"
            return ""
        # OOXML：docx/xlsx/pptx 都是 ZIP，且必须有 [Content_Types].xml
        if not zipfile.is_zipfile(fp):
            return "not a valid OOXML package (not a zip)"
        with zipfile.ZipFile(fp) as z:
            if "[Content_Types].xml" not in z.namelist():
                return "not a valid OOXML package (no [Content_Types].xml)"
    except Exception as e:
        return f"unreadable ({type(e).__name__})"
    return ""


def _verdict(ok: bool, kind: str, detail: str, hard: bool = False) -> dict:
    return {"ok": ok, "kind": kind, "detail": detail, "hard": hard}


def verify(session_id: str, body_text: str) -> dict | None:
    """本轮的机械验收。返回 None = 本轮没有验证器（开放问答）；否则 verdict dict。"""
    with _LOCK:
        led = list(_LEDGER.get(str(session_id or ""), []))
    if not led:
        return None
    if not ({e["tool"] for e in led} & _OUTPUT_TOOLS):
        return None        # 白名单闸：没有产出型工具 → 不判定（零行为变化）

    writes = [( _arg_path(e["args"]), str(e["args"].get("content") or ""))
              for e in led if e["tool"] == "write_file" and _arg_path(e["args"])]
    runs = [e for e in led if e["tool"] == "run_cmd" and _TEST_CMD_RE.search(_cmd_of(e["args"]))]

    # ① 反作弊（hard）：本轮删掉/清空被验对象
    for p, content in writes:
        if _is_test_path(p) and not content.strip():
            return _verdict(False, "test_wiped", f"{p} emptied this turn", hard=True)
    for e in led:
        cmd = _cmd_of(e["args"])
        if e["tool"] == "run_cmd" and _RM_RE.search(cmd):
            for tok in re.split(r"[\s\"']+", cmd):
                if tok and _is_test_path(tok):
                    return _verdict(False, "test_deleted", f"{tok} deleted this turn", hard=True)

    # ② 产物存在 + 结构下限
    for p, _content in writes:
        fp = Path(p).expanduser()
        if not fp.exists():
            return _verdict(False, "artifact_missing", f"written artifact not found: {p}")
        if fp.is_dir():
            return _verdict(False, "artifact_missing", f"written path is a directory: {p}")
        try:
            if fp.stat().st_size == 0:
                return _verdict(False, "artifact_empty", f"written artifact is empty: {p}")
        except OSError:
            continue        # 权限/竞态：交给下游，别把验证器本身变成故障源
        prob = _structural_problem(fp)
        if prob:
            return _verdict(False, "artifact_broken", f"{p} — {prob}")

    # ③ 正文声称生成的产物必须真实存在（"已保存到 …"）
    for m in _CLAIM_PATH_RE.finditer(body_text or ""):
        claimed = m.group(1).rstrip("。.,，；;")
        if not Path(claimed).suffix:
            continue        # 目录/无扩展名的说法不查（避免把提示句当交付物）
        if not Path(claimed).expanduser().exists():
            return _verdict(False, "claimed_missing", f"answer claims an artifact that does not exist: {claimed}")

    # ④ 测试仍是红的，而正文宣称完成（"幻觉成功"的机械对照）
    if runs and _CLAIM_DONE_RE.search(body_text or ""):
        last = runs[-1]
        if _run_is_red(last["result"]):
            code = _exit_code(last["result"])
            detail = (f"last test run exit {code}" if code is not None
                      else f"last test run has {_failed_count(last['result'])} failed")
            return _verdict(False, "test_red", detail)

    # ⑤ 反作弊（soft）：本轮改了测试文件、且同轮先红后绿（可能是放宽断言换绿）
    if len(runs) >= 2:
        _mod_tests = [p for p, _c in writes if _is_test_path(p)]
        if _mod_tests and any(_run_is_red(e["result"]) for e in runs[:-1]) \
                and not _run_is_red(runs[-1]["result"]):
            return _verdict(False, "test_rewritten",
                            f"test file modified this turn while tests went red → green: {_mod_tests[0]}")

    return _verdict(True, "", "")
