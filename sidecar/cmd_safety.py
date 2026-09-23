"""命令安全不变量——run_cmd 插件、fallback、seed 三处共享的单点定义。

此前三份实现各自维护黑名单（插件版有解释器内联拦截，fallback/seed 没有），
插件目录被删或重建时安全逻辑整体回退。本模块是唯一事实源（审计 P0）：
- 破坏性模式（rm/sudo/dd 等）
- 混淆/内联执行模式（python3 -c / node -e / curl|sh 等）
- 白名单快捷路径：整条命令必须完全匹配简单形态，且命中后仍走完整检查
- 敏感路径读取拦截（cat .ssh/id_rsa 等，与 read_file 插件黑名单对齐）
"""
import logging
import os
import platform
import re

logger = logging.getLogger("latiao-sidecar")

# 语义层缺失告警只打一次（升级后未装 tree-sitter 的环境不刷屏）
_WARNED_TS_MISSING = False

# ── Blocked patterns (checked case-insensitive) ──
DESTRUCTIVE_PATTERNS = [
    # File system destruction
    r"rm\s+(-[a-z]*[rf]|--recursive|--force)",
    r">\s*/dev/(sd|nvme|hd|disk|dm-)",
    r"dd\s+if=",
    r"mkfs",
    r"mkswap",
    r"wipefs",
    r"chattr\s+[+-]=*i",
    r"mv\s+/.*\s*/dev/null",
    # Privilege escalation / system control
    r"\bsudo\b", r"\bdoas\b", r"\bsu\s",
    r"\bshutdown\b", r"\breboot\b", r"\bhalt\b", r"\bpoweroff\b",
    r"\binit\s+[0-6]",
    r"\bsystemctl\s+(shutdown|reboot|halt|poweroff|suspend|stop|disable|mask|kill)",
    r"\blaunchctl\s+(unload|remove|bootout)",
    # Fork bomb / resource exhaustion
    r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;",
    r":\(\)\s*\{", r":\|:&",
    # Network dangerous
    r"\biptables\s+-F\b", r"\bpfctl\s+-d\b",
    # Write to critical paths
    r">\s*/etc/", r">>\s*/etc/",
    r">\s*/System/", r">>\s*/System/",
    r"chmod\s+[0-7]*7[0-7]*\s+/",
    r"chown\s+-R\s+",
]

OBFUSCATION_PATTERNS = [
    r"\beval\s", r"\bbase64\s+(-d|--decode)", r"\bxxd\s+-r",
    r"`[^`]+`",  # backtick subshell
    r"\$\([^)]+\)",  # $() subshell
    r"\\x[0-9a-fA-F]{2}",  # hex-encoded chars in command
    r"\bcurl\b.*\|\s*(ba)?sh\b", r"\bwget\b.*\|\s*(ba)?sh\b",  # curl | sh
    r"echo.*\b\|\s*(ba)?sh\b", r"cat.*\b\|\s*(ba)?sh\b",
    r"\bbase64.*\|\b.*sh", r"openssl.*\|\b.*sh",
    r"xargs\s+rm", r"xargs\s+kill",
    r"nohup.*rm\s", r"nohup.*kill\s",
    r"\bperl\s+-e\b",  # often used for obfuscation
    # 解释器内联代码（审计 H3）：黑名单可被 python3 -c / node -e / bash -c
    # 绕过——shell=False 不拦这些，内联脚本可执行任意操作。
    # 显式命令/脚本文件仍放行（python3 script.py、node app.js、bash deploy.sh）。
    r"\b(python|python3|python3\.\d+[0-9]*|node|nodejs|deno|bun|ruby|perl|php|lua|nu|pwsh|powershell)\s+(-[a-zA-Z]*[ce]\b|--command\b|--eval\b)",
    r"\b(ba|z|k|d)?sh\s+(-[a-zA-Z]*[ce]\b|--command\b)",
    r"\bfish\s+(-c\b|--command\b)",
]

_WIN_CMDS = r"|dir|cd|where|ver|type|find|findstr" if platform.system() == "Windows" else ""

# 白名单只放"裸命令 + 简单选项/路径"形态——整条命令必须完全匹配该形态。
# 命中后仍走 check_cmd 完整检查（防御纵深）。env/printenv 曾被模型用作
# 任意命令前缀（"env curl ..." 绕过全部黑名单，P0），已从白名单移除。
SAFE_CMD_RE = re.compile(
    r"^(ls|pwd|echo|cat|head|tail|wc|file|which|whoami|uname|date"
    + _WIN_CMDS
    + r")(\s+-{1,2}[A-Za-z0-9][A-Za-z0-9_-]*|\s+[\w./@+~:-]+)*$"
)

# 敏感路径读取拦截：白名单快捷路径不能成为 read_file 黑名单的侧门。
SENSITIVE_READ_RE = re.compile(
    r"(\.ssh|\.aws|\.gnupg|\.kube|\.docker|id_rsa|id_ed25519|id_ecdsa|"
    r"known_hosts|\.netrc|git-credentials|credentials|keychain|config\.json|\.env)",
    re.IGNORECASE,
)

_SENSITIVE_READERS = ("cat", "head", "tail", "type", "find", "findstr")


def reject_sensitive_read(cmd: str) -> str | None:
    """读取类命令命中敏感路径时返回拒绝文案，否则 None。"""
    low = cmd.lower()
    first = low.split()[0] if low.split() else ""
    if first in _SENSITIVE_READERS and SENSITIVE_READ_RE.search(low):
        return f"⛔ 敏感路径读取被拒绝（密钥/凭据/配置）: {cmd[:80]}"
    return None


# ── 敏感路径读取：**工具路径**版（09-23，审计 P1）────────────────────
# 命令路径（run_cmd 白名单）一直有 SENSITIVE_READ_RE 守着，工具路径（read_file
# 插件、tool_executor 的 fallback）却没有：同一份 config.json，`cat` 被拦、
# `read_file` 免确认读通。真机复现（09-23 11:16）：模型读 config.json → 明文
# Tavily key 进模型上下文 → 进日志、进报告、进 cron 事件与会话。
# 这里给"路径"形态一份判定，与命令版**同源同心**（密钥/凭据/配置一律不可读），
# 但**不**直接套用 SENSITIVE_READ_RE：它按子串匹配 `config.json`，会把项目的
# `tsconfig.json` 一起拦掉（子串命中），那是能力回归。
_BLOCKED_DIR_SUBSTRINGS = ("/.ssh", "/.aws", "/.gnupg", "/Library/Keychains",
                           "/.kube", "/.docker")
_BLOCKED_FILE_NAMES = (".env", "id_rsa", "id_ed25519", "id_ecdsa", "known_hosts",
                       ".netrc", ".git-credentials", "credentials", "_netrc")
# .env 家族（.env.local / .env.production…）全拦；模板类放行（无密钥、开发要看）
_ENV_FAMILY_RE = re.compile(r"^\.env(\.[\w.-]+)?$", re.IGNORECASE)
_ENV_TEMPLATE_SUFFIXES = (".example", ".sample", ".template", ".dist", ".defaults", ".md")


def _app_data_dir() -> str:
    """辣条自身的数据目录（config.json 所在处）。取不到时退回默认位置。"""
    try:
        from config import PROGRESS_DIR
        return os.path.realpath(str(PROGRESS_DIR))
    except Exception:
        return os.path.realpath(os.path.expanduser("~/.local-ai-os"))


def _app_data_dirs() -> tuple[str, ...]:
    """辣条自身数据目录（config.json 所在处）的候选集合。

    同时认**运行时 PROGRESS_DIR** 和**默认位置** `~/.local-ai-os`：只认前者时，
    数据目录一旦被重定向（测试环境/自定义位置），真实 config.json 就从判定里漏过
    ——写测试时实测到的漏洞（`_blk('~/.local-ai-os/config.json')` 返回 False）。
    """
    dirs: list[str] = []
    try:
        from config import PROGRESS_DIR
        dirs.append(os.path.realpath(str(PROGRESS_DIR)))
    except Exception:
        pass
    dirs.append(os.path.realpath(os.path.expanduser("~/.local-ai-os")))
    out: list[str] = []
    for d in dirs:
        if d and d not in out:
            out.append(d)
    return tuple(out)


def sensitive_read_block(path: str) -> str | None:
    """工具路径读文件前的敏感判定：返回拒绝文案或 None（放行）。

    拦截范围：密钥/凭据目录、密钥文件名、`.env` 家族（模板除外）、
    辣条自身目录下的 `config.json`（含 .bak 备份，里面有云端与 Tavily key）。
    """
    if not path:
        return None
    _rp = os.path.realpath(str(path))
    if any(s in _rp for s in _BLOCKED_DIR_SUBSTRINGS):
        return f"⛔ 不允许访问敏感目录 - {_rp}"
    base = os.path.basename(_rp)
    low = base.lower()
    if low in _BLOCKED_FILE_NAMES:
        return f"⛔ 不允许读取敏感文件（密钥/凭据）- {_rp}"
    if _ENV_FAMILY_RE.match(low) and not low.endswith(_ENV_TEMPLATE_SUFFIXES):
        return f"⛔ 不允许读取 .env 家族文件（可能含密钥）- {_rp}"
    if low.startswith("config.json"):
        # 只在辣条自己的数据目录里拦，不误伤用户项目的 config.json / tsconfig.json
        _under = any(_rp.startswith(d + os.sep) for d in _app_data_dirs())
        if _under or "/.local-ai-os/config.json" in _rp.replace(os.sep, "/"):
            return (f"⛔ 不允许读取辣条自身配置（含 API 密钥）- {_rp}。"
                    "需要改配置请到设置界面操作")
    return None


# ── 日志/落盘脱敏（09-23，审计 P1 附带）──────────────────────────
# 真机复现：工具结果原样进日志（`Tool result: read_file → { "tavily_api_key":
# "tvly-dev-…"`），而日志是 0644 且常被贴进工单。这里统一打码，供日志预览、
# 进度文件、记忆库写入三处共用。
_SECRET_PATTERNS = re.compile(
    r"(?:tvly|sk|gsk|xai|hf|ghp|gho|glpat|github_pat)[-_][A-Za-z0-9_\-]{12,}"
    r"|AIza[A-Za-z0-9_\-]{20,}"
    r"|eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"      # JWT
    r"|(?:Bearer|token|api[_-]?key)\s*[\"']?\s*[:=]?\s*[\"']?[A-Za-z0-9_\-\.]{16,}",
    re.IGNORECASE,
)
# 整份文件/目录内容类工具：日志里只记长度，不记内容（内容是本类泄露的主要载体）
_RAW_CONTENT_TOOLS = frozenset({"read_file", "headless_read", "list_dir"})


def redact_secrets(text: str) -> str:
    """把文本里像密钥的片段打码（日志、进度文件、记忆库写入前都要用）。"""
    try:
        return _SECRET_PATTERNS.sub("***", text or "")
    except Exception:
        return text or ""


def tool_log_preview(tool_name: str, result: str, limit: int = 80) -> str:
    """工具结果的日志预览：密钥一律打码；整份文件内容这一类只记长度。

    read_file 的内容几乎没诊断价值（路径已经单独打过），而它正是密钥泄露的载体；
    mx_query/搜索类保留脱敏后的预览——09-01 排查工具参数问题就靠这几行。
    """
    txt = result or ""
    if tool_name in _RAW_CONTENT_TOOLS:
        return f"[{len(txt)} 字内容已省略]"
    return redact_secrets(txt)[:limit].replace("\n", " ")


def check_cmd(cmd: str) -> str | None:
    """整条命令的安全检查（阶段 3 双层）。返回拒绝文案或 None（放行）。

    第 1 层语义层（safety/rules.py）：tree-sitter 解析全部命令调用/重定向/
    嵌套子命令——`env curl`、`exec bash -c`、`$(rm -rf)` 等前缀/嵌套绕过
    在 AST 层直接暴露（正则层的结构损失不复存在）。
    第 2 层正则兜底：保留既有 DESTRUCTIVE/OBFUSCATION 黑名单作为纵深防御
    （语义层拿到的是结构，正则层兜的是形状——两层漏判互为镜像，不留单点）。
    语义层不可用（tree-sitter 缺失）时回退正则层旧行为——升级不能瘫痪现网；
    语义层可用但命令语法解析失败时 fail-closed 拦截。
    """
    global _WARNED_TS_MISSING
    try:
        from safety.rules import analyze, decide, Verdict
        a = analyze(cmd)
        if a.ts_available:
            decision = decide(cmd)
            if decision.verdict is Verdict.FORBIDDEN:
                return f"⛔ Blocked command: {cmd}（{decision.reason}）"
            if a.parse_error:
                # 语法解析失败不静默放行（fail-closed；不是依赖缺失路径）
                return f"⛔ 命令语义解析失败，已拦截（请检查语法）: {cmd[:80]}"
        elif not _WARNED_TS_MISSING:
            _WARNED_TS_MISSING = True
            logger.warning("命令语义层不可用（tree-sitter 未安装），使用正则层")
    except Exception:
        logger.warning("命令语义层异常，回退正则层", exc_info=True)
    # 正则兜底层（纵深防御；语义层 fail-open 时的唯一防线）
    low = cmd.lower()
    for pattern in DESTRUCTIVE_PATTERNS:
        if re.search(pattern, low):
            return f"⛔ Blocked destructive command: {cmd}"
    for pattern in OBFUSCATION_PATTERNS:
        if re.search(pattern, low):
            return f"⛔ Blocked potentially unsafe command: {cmd}"
    return reject_sensitive_read(cmd) or reject_sensitive_write(cmd)

# ── ⑧ 自动加载目录的写入封印（2026-09-23 审计 P1）────────────────────
# ~/.local-ai-os/extensions/<name>/<ver>/plugin.py 会被 tool_system 用
# exec_module 加载（下次启动或热重载即执行）。write_file 此前只封了
# sidecar/plugins/，模型（或被批准的一次写入）能把代码塞进扩展目录 → 与
# sidecar 同权限的代码执行。
def sensitive_write_block(path: str) -> str | None:
    """写入自动加载目录/凭据文件的拒绝文案；允许时返回 None。"""
    if not path:
        return None
    _rp = os.path.realpath(str(path))
    _norm = _rp.replace(os.sep, "/")
    for d in _app_data_dirs():
        for sub in ("extensions", "skills"):
            _dir = f"{d}{os.sep}{sub}"
            if _rp == d + os.sep + sub or _rp.startswith(_dir + os.sep):
                return (f"⛔ 不允许写入自动加载目录（{sub}）—— 写进去的代码会在下次启动/"
                        f"热重载时执行: {_rp}")
    # 应用配置含凭据（云端 key / Tavily key），且改它等于改安全开关；走设置界面
    if os.path.basename(_rp).lower().startswith("config.json"):
        for d in _app_data_dirs():
            if _rp.startswith(d + os.sep):
                return f"⛔ 不允许改写辣条自身配置（含 API 密钥）: {_rp}"
    if "/.local-ai-os/config.json" in _norm:
        return f"⛔ 不允许改写辣条自身配置（含 API 密钥）: {_rp}"
    return None


# ── 命令路径的写入封印（2026-09-23 审查补）─────────────────────────
# 上面那层只挡住了 write_file 工具。审查实测：同一份 config.json，
# `cp /tmp/x <config>`、`tee <config>`、`sed -i s/a/b/ <config>`、
# `cp /tmp/evil.py ~/.local-ai-os/extensions/x/1.0/plugin.py` **全部放行**
# ——封印被绕过的路子和读的那半一模一样（工具被封、命令放行）。
# 判定按"路径"而不是"动词"：命令里出现受保护的写入目标就拒，只读动词放行
# （ls/stat/grep 看扩展目录是正常操作，不能误伤）。
_READ_ONLY_VERBS = frozenset({
    "ls", "stat", "file", "head", "tail", "wc", "du", "tree", "find", "grep", "rg",
    "diff", "md5", "md5sum", "shasum", "sha256sum", "cat", "less", "more", "bat",
    "xxd", "strings", "echo", "pwd", "which", "type", "basename", "dirname",
})


def _protected_write_target(tok: str) -> str | None:
    """单个 argv token 是否是受保护的写入目标（自动加载目录 / config.json）。"""
    if not tok or tok.startswith("-"):
        return None
    if "=" in tok:                      # dd of=/path、--output=/path、-o=/path
        tok = tok.split("=", 1)[1]
    if not tok or tok.startswith("-"):
        return None
    rp = os.path.realpath(os.path.abspath(os.path.expanduser(tok)))
    for d in _app_data_dirs():
        for sub in ("extensions", "skills"):
            if rp == f"{d}{os.sep}{sub}" or rp.startswith(f"{d}{os.sep}{sub}{os.sep}"):
                return rp
        if os.path.basename(rp).lower().startswith("config.json") and rp.startswith(d + os.sep):
            return rp
    return None


def reject_sensitive_write(cmd: str) -> str | None:
    """命令写入自动加载目录/配置时返回拒绝文案，否则 None。"""
    import shlex
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        tokens = cmd.split()
    if not tokens:
        return None
    verb = os.path.basename(tokens[0]).lower()
    if verb in _READ_ONLY_VERBS:
        return None
    for tok in tokens[1:]:
        hit = _protected_write_target(tok)
        if hit:
            return (f"⛔ 不允许用命令写入自动加载目录/配置（写进去的代码会被执行、"
                    f"配置含 API 密钥）: {hit}")
    return None


# ── ④ 子进程环境白名单（2026-09-23 审计 P1）──────────────────────────
# 此前四处子进程全量继承 os.environ：LATIAO_AUTH_TOKEN（侧车全权令牌）与
# 各家云端 key 对任何被 spawn 的进程可见——模型让 run_cmd 执行 `env` 就能读
# 回来，MCP server（第三方代码）同样看得到。改为白名单：只透传运行必需项。
_ENV_ALLOW = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "PWD", "OLDPWD",
    "TMPDIR", "TEMP", "TMP", "LANG", "TZ", "DISPLAY",
    # 代理：功能必需（很多用户靠它联网），且通常不含项目凭据
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    # SSH agent 转发（git over ssh 需要）
    "SSH_AUTH_SOCK",
    # Windows 基本项
    "SystemRoot", "SYSTEMROOT", "windir", "WINDIR", "ComSpec", "COMSPEC",
    "PATHEXT", "APPDATA", "LOCALAPPDATA", "USERPROFILE", "ProgramData",
    "ProgramFiles", "ProgramFiles(x86)", "PSModulePath",
    "NUMBER_OF_PROCESSORS", "OS", "PROCESSOR_ARCHITECTURE",
})


def child_env(extra: dict | None = None, allow_prefixes: tuple = ()) -> dict:
    """白名单化的子进程环境变量。

    extra 覆盖/追加（如 MCP server 自己声明的变量）；allow_prefixes 供引擎类
    子进程透传运行库变量（DYLD_/MTL_/GGML_ 等）——**仍不会放进凭据类变量**。
    """
    env = {k: v for k, v in os.environ.items() if k in _ENV_ALLOW}
    if allow_prefixes:
        env.update({k: v for k, v in os.environ.items() if k.startswith(allow_prefixes)})
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env
