"""命令安全语义层——规则引擎（阶段 3，移植 Codex execpolicy 概念：语义解析 + 规则判定）。

相对 cmd_safety.py 正则层的升级（对比报告"模块 3"的结论：正则启发式的天花板）：
- 用 tree-sitter-bash 解析命令，提取**所有**命令调用与重定向目标——不受
  `&&`、`|`、`$()`、`env <cmd>` 前缀、`exec env` 嵌套影响（README 里
  Codex 用 tree-sitter 拿"命令结构"而不是"字符串形状"）；
- 规则文件 `rules.yaml`（可审可改，用户可覆盖）；解析失败 fail-loud 回退
  内置默认规则并告警（不静默降级）；
- decide() 三态：allow / prompt / forbidden。未知命令 → prompt（不 fail-open，
  与审计 F4"未知工具默认 safe"的教训相反）。
- cmd_safety.check_cmd 与之双层叠加：语义层先判，正则层兜底（纵深防御）。

模块边界：本模块只做"判定"，不执行、不落地策略（策略仍在工具/权限层）。
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

try:  # tree-sitter 为可选依赖：缺失时语义层不可用，调用方会记录并回退正则层
    from tree_sitter import Language, Parser
    import tree_sitter_bash

    _LANGUAGE = Language(tree_sitter_bash.language())
    _TS_AVAILABLE = True
except Exception:  # pragma: no cover — 依赖缺失路径
    _LANGUAGE = None
    _TS_AVAILABLE = False

# P2-1 修复：py-tree-sitter 明确 Parser 非线程安全；check_cmd/readonly_safe
# 可能被工具线程调用（同步工具经线程池）与事件循环并发——线程局部实例化。
_TS_LOCAL = threading.local()


def _parser() -> "Parser | None":
    if not _TS_AVAILABLE:
        return None
    parser = getattr(_TS_LOCAL, "parser", None)
    if parser is None:
        parser = Parser(_LANGUAGE)
        _TS_LOCAL.parser = parser
    return parser


class Verdict(str, Enum):
    ALLOW = "allow"
    PROMPT = "prompt"
    FORBIDDEN = "forbidden"


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str = ""
    matched_rule: str = ""

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ALLOW


@dataclass(frozen=True)
class Invocation:
    """一次命令调用（AST 提取）。words 为去除引号后的参数词。"""

    name: str            # 可执行名（basename）
    words: tuple[str, ...]  # 全部词（args 含 flags/path）
    text: str            # 原始文本（诊断用）

    @property
    def args(self) -> tuple[str, ...]:
        return self.words[1:]


@dataclass
class Analysis:
    invocations: list[Invocation] = field(default_factory=list)
    redirects: list[str] = field(default_factory=list)   # 重定向目标路径（裸文本）
    parse_error: str | None = None
    ts_available: bool = False   # tree-sitter 依赖可用否（区分"不可用"与"解析失败"）


# ── 内置默认规则（rules.yaml 覆盖/追加；字段见模块头注释）───────────────
_DEFAULT_RULES = {
    # 破坏性命令：禁止
    "forbidden_commands": {
        "rm": r"^-[A-Za-z]*[rf][A-Za-z]*$|^--(recursive|force)$",
        "dd": r"^if=",
        "mkfs": None, "mkswap": None, "wipefs": None, "chattr": r"^[+-].*i",
        "sudo": None, "doas": None, "su": r"^[0-9]",
        "shutdown": None, "reboot": None, "halt": None, "poweroff": None,
        "init": r"^[0-6]$",
        "systemctl": r"^(shutdown|reboot|halt|poweroff|suspend|stop|disable|mask|kill)$",
        "launchctl": r"^(unload|remove|bootout)$",
        "iptables": r"^-F$", "pfctl": r"^-d$",
        "chmod": r"^[0-7]*7[0-7]*$", "chown": r"^-R$",
    },
    # 混淆/内联执行：禁止（shell=False 也拦不住内联脚本，语义层按命令名判）
    "forbidden_commands_any_arg": {
        "eval": None, "base64": r"^(-d|--decode)$", "xxd": r"^-r$",
        "perl": r"^-e$", "python": r"^-[a-zA-Z]*c$|^--command$", "python3": r"^-[a-zA-Z]*c$|^--command$",
        "node": r"^-[a-zA-Z]*e$|^--eval$", "deno": r"^-[a-zA-Z]*e$", "bun": r"^-[a-zA-Z]*e$",
        "bash": r"^-[a-zA-Z]*c$|^--command$", "sh": r"^-[a-zA-Z]*c$", "zsh": r"^-[a-zA-Z]*c$",
        "fish": r"^-(c|--command)$", "pwsh": r"^-[a-zA-Z]*c$", "powershell": r"^-[a-zA-Z]*c$",
    },
    # 管道投毒：curl|sh 等（rhs 为 shell、lhs 为下载/回显类）
    "pipe_shell_lhs": ("curl", "wget", "echo", "cat", "base64", "openssl"),
    "pipe_shell_rhs": ("sh", "bash", "zsh", "fish", "dash"),
    # 敏感路径（与 read_file 黑名单对齐）
    "sensitive_read": (
        r"\.ssh", r"\.aws", r"\.gnupg", r"\.kube", r"\.docker",
        r"id_rsa", r"id_ed25519", r"id_ecdsa", r"known_hosts",
        r"\.netrc", r"git-credentials", r"credentials", r"keychain",
        r"config\.json", r"\.env", r"\.local-ai-os",
    ),
    "sensitive_readers": ("cat", "head", "tail", "type", "find", "findstr", "strings"),
    # 白名单：整条命令（每词）匹配简单形态才放行；否则交 prompt 判定
    "allow_commands": (
        "ls", "pwd", "echo", "cat", "head", "tail", "wc", "file", "which",
        "whoami", "uname", "date", "dir", "where", "ver", "find", "findstr",
    ),
    "allow_word_shape": r"^(-\s?-?[A-Za-z0-9][\w-]*|[\w./@+~:\\-]+)$",
}


# ── 可执行名提取（语义层核心：env/exec 前缀与嵌套结构都展开）────────────
def _command_name(node) -> str | None:
    """command 节点里找 command_name（env/exec 亦被解析为 command_name）。"""
    for child in node.children:
        if child.type == "command_name":
            return child.text.decode("utf-8", errors="replace").strip()
    return None


def _command_words(node) -> list[str]:
    out: list[str] = []
    for child in node.children:
        if child.type in ("word", "string", "concatenation", "number"):
            out.append(child.text.decode("utf-8", errors="replace").strip().strip('"\'`'))
        elif child.type in ("simple_expansion", "command_substitution"):
            out.append(child.text.decode("utf-8", errors="replace").strip())
    return out


# 命令包装器：语法层会把 "env curl ..." 解析成单个 command（env 名 + 参数），
# 内层真实命令需要语义展开（上限 8 层，同 Codex is_dangerous_command 的嵌套限）。
_WRAPPER_COMMANDS = frozenset({"env", "exec", "nice", "nohup", "time", "command", "builtin"})
_WRAPPER_DEPTH = 8


def _unwrap_wrappers(analysis: Analysis) -> None:
    additions: list[Invocation] = []
    for inv in analysis.invocations:
        depth = 0
        cur = inv
        while cur.name in _WRAPPER_COMMANDS and cur.args and depth < _WRAPPER_DEPTH:
            inner = Invocation(
                name=cur.args[0].rsplit("/", 1)[-1],
                words=(cur.args[0], *cur.args[1:]),
                text=inv.text,
            )
            additions.append(inner)
            cur = inner
            depth += 1
    analysis.invocations.extend(additions)


def _walk(node, analysis: Analysis) -> None:
    for child in node.children:
        if child.type == "command":
            name = _command_name(child)
            if name:
                # words 含命令名本身（args = words[1:]）；命令名缺失时已用 name 兜底
                words = (name, *_command_words(child)) or (name,)
                analysis.invocations.append(Invocation(
                    name=name, words=tuple(words),
                    text=child.text.decode("utf-8", errors="replace")[:200],
                ))
            # 命令体内可能嵌 $()/重定向——递归进子树（命令名不是终结结构）
            _walk(child, analysis)
        elif child.type in ("redirected_statement", "file_redirect", "redirection",
                            "command_substitution", "subshell", "pipeline", "list",
                            "case_item", "declaration", "postfix", "function_definition"):
            _walk(child, analysis)
        elif child.child_count > 0:
            _walk(child, analysis)
    # 重定向目标（file_redirect 子节点文本作保守匹配）
    if node.type == "file_redirect":
        analysis.redirects.append(node.text.decode("utf-8", errors="replace"))


def analyze(cmd: str) -> Analysis:
    """tree-sitter 解析 → 全命令提取。失败区分两种：依赖缺失（ts_available=False）
    与语法解析异常（parse_error）——前者由调用方回退旧行为，后者 fail-closed。"""
    analysis = Analysis(ts_available=_TS_AVAILABLE)
    if not _TS_AVAILABLE:
        return analysis
    try:
        tree = _parser().parse(cmd.encode("utf-8", errors="replace"))  # type: ignore[union-attr]
        _walk(tree.root_node, analysis)
        _unwrap_wrappers(analysis)
    except Exception as exc:  # pragma: no cover — 解析器异常不应击穿调用方
        analysis.parse_error = str(exc)
    return analysis


def is_safe_shape(words: tuple[str, ...], shape_re: re.Pattern) -> bool:
    """白名单形状：每个词都是简单选项/路径形态（对齐 cmd_safety.SAFE_CMD_RE）。"""
    if not words:
        return False
    return all(shape_re.fullmatch(w) for w in words)


def decide(cmd: str, rules: dict[str, Any] | None = None) -> Decision:
    """规则判定（语义层）。verdict 语义：
        allow    —— 白名单整条匹配且无危险
        forbidden—— 命中禁止规则（返回禁用原因）
        prompt   —— 未知/未匹配（不 fail-open；收件者按访问档位转确认或放行）
    """
    r = rules or _DEFAULT_RULES
    a = analyze(cmd)
    if a.parse_error:
        return Decision(Verdict.PROMPT, reason=f"命令无法语义解析（{a.parse_error}）", matched_rule="parse-error")

    # 1) 禁止规则（按调用语义，对每个提取到的调用判定）
    for inv in a.invocations:
        reason = _match_forbidden(inv, r)
        if reason:
            return Decision(Verdict.FORBIDDEN, reason=reason, matched_rule=inv.name)

    # 2) 管道投毒：推断 rhs 命令名（按调用的文本序取样；失败则跳过的保守丢弃）
    #    —— 精确关联交给正则层兜底，这里只抓"尾部为 shell 且头部为下载类"的典型形态
    if len(a.invocations) >= 2:
        names = [i.name for i in a.invocations]
        if names[-1] in r["pipe_shell_rhs"] and names[0] in r["pipe_shell_lhs"]:
            return Decision(Verdict.FORBIDDEN, reason=f"管道投毒: {names[0]} | {names[-1]}", matched_rule="pipe-shell")

    # 3) 白名单整条匹配（形状 + 无敏感读）
    if _is_allowlisted(a, r):
        return Decision(Verdict.ALLOW, reason="whitelist matched", matched_rule="allow")

    # 4) 未知 → prompt（不 fail-open）
    return Decision(Verdict.PROMPT, reason=f"未命中的命令: {a.invocations[0].name if a.invocations else cmd}")


def _match_forbidden(inv: Invocation, rules: dict[str, Any]) -> str | None:
    for table_key in ("forbidden_commands", "forbidden_commands_any_arg"):
        table: dict[str, Any] = rules.get(table_key, {})
        pattern = table.get(inv.name)
        if pattern is None and inv.name in table:
            return f"禁止命令: {inv.name}"
        if pattern is not None and inv.args and _any_arg_match(inv.args, pattern):
            return f"禁止用法: {inv.name} {inv.args[0]}"
    # 敏感读（保留正则层同款对齐；语义层提前拦）
    if inv.name in rules.get("sensitive_readers", ()):
        for arg in inv.args:
            if _sensitive_path(arg, rules):
                return f"敏感路径读取被拒绝: {arg}"
    return None


def _any_arg_match(args: tuple[str, ...], pattern: str) -> bool:
    pat = re.compile(pattern, re.IGNORECASE)
    return any(pat.fullmatch(a) or pat.search(a) for a in args)


def _sensitive_path(arg: str, rules: dict[str, Any]) -> bool:
    low = arg.lower()
    return any(re.search(p, low) for p in rules["sensitive_read"])


def _is_allowlisted(a: Analysis, rules: dict[str, Any]) -> bool:
    allow = set(rules.get("allow_commands", ()))
    shape = re.compile(rules.get("allow_word_shape", ""))
    if not a.invocations:
        return False
    for inv in a.invocations:
        if inv.name not in allow:
            return False
        if not is_safe_shape(inv.words, shape):
            return False
    return not a.redirects or all(
        not any(re.search(p, t.lower()) for p in rules["sensitive_read"]) for t in a.redirects
    )


# ── 只读语义判定（子智能体 readonly 白名单的语义版）──────────────────────
# 对齐 tool_executor._READONLY_CMD_WORDS：全部提取到的调用都必须命中只读集
# 且参数为简单形态、无敏感路径——`env cat`、`bash -c 'cat x'`、$() 嵌套在
# AST 层直接现形（旧实现对首 token 形态校验，嵌套漏网）。
_READONLY_COMMANDS = frozenset({
    "ls", "cat", "head", "tail", "find", "grep", "rg", "wc", "file",
    "stat", "du", "df", "ps", "which", "whoami", "uname", "pwd",
    "echo", "date", "dir", "where", "ver", "type", "findstr", "strings",
})
_GIT_READONLY = re.compile(r"^(log|status|diff|show|branch|remote|tag|blame)$")


def readonly_safe(cmd: str) -> bool:
    """全部调用都是单条简单只读命令才放行（否则 False，不 fail-open）。

    契约对齐 tool_executor._is_readonly_cmd 的既有白名单语义：shell 操作符
    （| ; & 重定向）一律拒绝——白名单只覆盖"单条简单命令"。语义层在此比
    token 形态更强：嵌套 $() 会导致多调用而直接拒绝。
    """
    a = analyze(cmd)
    if a.parse_error or not a.invocations:
        return False
    if len(a.invocations) != 1 or a.redirects:
        return False  # 管道/分号/重定向/嵌套子命令 → 非"单条简单命令"
    rules = load_rules()
    shape = re.compile(rules.get("allow_word_shape", ""))
    inv = a.invocations[0]
    if inv.name == "git":
        return bool(inv.args and _GIT_READONLY.fullmatch(inv.args[0]))
    if inv.name not in _READONLY_COMMANDS:
        return False
    if not is_safe_shape(inv.words, shape):
        return False
    if inv.name in rules.get("sensitive_readers", ()):
        for arg in inv.args:
            if _sensitive_path(arg, rules):
                return False
    # 全文级敏感串兜底（防参数被拼接/引用花活绕过逐词判定：宁可误拦不漏拦）
    if inv.name in rules.get("sensitive_readers", ()):
        if any(re.search(p, cmd.lower()) for p in rules["sensitive_read"]):
            return False
    return True


# ── 规则文件加载（rules.yaml；解析失败 fail-loud 回退默认并告警）────────
DEFAULT_RULES_PATH = Path(__file__).resolve().parent / "rules.yaml"
_loaded: dict[str, Any] | None = None


def load_rules(path: Path | None = None) -> dict[str, Any]:
    """加载规则文件（合并到默认规则之上）。幂等缓存；失败回退默认。

    文件损坏时 fail-loud：记录 ERROR 并以默认规则继续（安全优先于功能，
    但绝不静默——用户必须知道自定义规则没生效，同 Codex execpolicy
    "parse error" 的可见性约定）。
    """
    global _loaded
    if _loaded is not None:
        return _loaded
    merged = {k: (dict(v) if isinstance(v, dict) else v)
              for k, v in _DEFAULT_RULES.items()}
    yaml_path = path or DEFAULT_RULES_PATH
    try:
        import yaml
        if yaml_path.exists():
            with open(yaml_path, encoding="utf-8") as f:
                user = yaml.safe_load(f) or {}
            for key, value in user.items():
                if key in merged:
                    if isinstance(merged[key], (list, tuple)):
                        merged[key] = tuple(value)
                    else:
                        merged[key] = {**merged[key], **value}
        _loaded = merged
    except Exception:
        logger.error("rules.yaml 解析失败，使用内置默认规则: %s", yaml_path, exc_info=True)
        _loaded = merged
    return _loaded


def reset_rules_cache() -> None:
    """测试用：清除规则文件缓存。"""
    global _loaded
    _loaded = None
