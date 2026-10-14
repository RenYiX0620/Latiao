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
from pathlib import Path

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
    # AppleScript 侧门（实证放行过）：osascript -e 'do shell script "..."' 里是
    # 完整 shell，黑名单全绕过。同族还有 JXA、open -a Terminal。
    r"\bosascript\b",
    r"\bdo\s+shell\s+script\b",
    r"\bsystem\s+events\b",
    r"\bopen\s+(-a\s+)?(Terminal|Console)\b",
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

# 辣条数据目录下"改了等于改安全开关"的注册表文件：写一次 permissions.json 就能把
# run_cmd 降成 safe、确认门被持久关掉；agents.json / skills.json 可植入提示注入。
# 写入工具路径（sensitive_write_block）与命令路径（_protected_write_target）共用这一份，
# 避免两处清单各自漂移。
_APP_REGISTRY_FILES = ("permissions.json", "agents.json", "skills.json")

# 身份文件（identity.IDENTITY_FILES 的镜像）：全文会进系统提示且带"一律照做"语义，
# 改它 = 跨会话改写人格/工作规则（2026-10-11 深审 High）。与注册表同级保护，
# 修改只走设置页 / 身份意图通道。两份写路径共用，避免漂移。
IDENTITY_FILE_NAMES = ("identity.md", "soul.md", "agents.md", "user.md")


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

# ── 登录即执行 / 下次启动即加载的路径（2026-10-11 第三轮审计修复）────
# 此前 write_file 能写穿 ~/.zshrc（实测）、~/Library/LaunchAgents/*.plist（实测）、
# crontab、git hooks——写入一次 = 下次登录/下次提交即执行，是比 extensions 更直接的
# 持久化 RCE 通道。写侧（sensitive_write_block）与读侧（sensitive_read_block）
# 共用这一份清单，避免两处漂移（2026-09-24 的老教训）。
_SHELL_INIT_BASENAMES = frozenset({
    ".zshrc", ".bashrc", ".profile", ".zprofile", ".zshenv", ".bash_profile",
    ".bash_login", ".bash_logout", ".zlogin", ".zlogout", ".inputrc", ".cshrc", ".tcshrc",
})
_SHELL_INIT_DIRS = ("/etc/zsh", "/etc/bash", "/etc/profile.d", "/etc/profile")
_AUTOEXEC_DIR_SUBSTRINGS = (
    "/Library/LaunchAgents", "/Library/LaunchDaemons",
    "/.config/autostart",            # Linux 桌面自启
    "/.config/systemd/user",         # systemd 用户单元
    "/etc/cron.d", "/etc/cron.daily", "/etc/cron.hourly",
    "/etc/cron.weekly", "/etc/cron.monthly", "/var/at/tabs",
)
_AUTOEXEC_BASENAME_SUFFIXES = (".desktop", ".service")

def _is_autoload_path(rp: str) -> str | None:
    """登录/自启即加载的路径 → 拒绝原因（None = 不在名单）。三个消费者共用。"""
    norm = rp.replace(os.sep, "/")
    base = os.path.basename(rp).lower()
    if base in _SHELL_INIT_BASENAMES:
        return "shell 启动脚本（下次开终端即执行）"
    for d in _SHELL_INIT_DIRS:
        if norm == d or norm.startswith(d + "/"):
            return "系统级 shell 初始化目录（所有终端会话都会加载）"
    for sub in _AUTOEXEC_DIR_SUBSTRINGS:
        if sub in norm:
            return "开机/登录自启目录（下次登录即执行）"
    if base.endswith(_AUTOEXEC_BASENAME_SUFFIXES) and (
            "/.config/" in norm or "/LaunchAgents" in norm):
        return "自启项文件（下次登录即执行）"
    if norm.endswith("/.git/hooks") or "/.git/hooks/" in norm:
        return "git 钩子（下次 git 操作即执行）"
    return None


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
    辣条自身目录下的 `config.json`（含 .bak 备份，里面有云端与 Tavily key）、
    以及 shell 启动脚本/自启目录（2026-10-11 补：那里常有 export 的密钥，
    且是"读出来就能外传"的高价值目标）。
    """
    if not path:
        return None
    _rp = os.path.realpath(str(path))
    if any(s in _rp for s in _BLOCKED_DIR_SUBSTRINGS):
        return f"⛔ 不允许访问敏感目录 - {_rp}"
    _auto = _is_autoload_path(_rp)
    if _auto:
        return f"⛔ 不允许读取{_auto} - {_rp}"
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


def sensitive_write_name_block(path: str) -> str | None:
    """写入路径的**文件名**判定：密钥/凭据名 + `.env` 家族（模板除外）。

    写入侧此前是插件里 5 项的本地拷贝，与 fallback 的 9 项漂移 —— `~/.netrc`、
    `.git-credentials`、`.env.local` 在插件路径（实际生效的那条）放行。
    这里与读侧共用同一份清单和同一条 .env 家族规则（2026-09-24 复查修复）。
    """
    if not path:
        return None
    low = os.path.basename(str(path)).lower()
    if not low:
        return None
    if low in _BLOCKED_FILE_NAMES:
        return f"⛔ 不允许写入敏感文件（密钥/凭据）- {path}"
    if _ENV_FAMILY_RE.match(low) and not low.endswith(_ENV_TEMPLATE_SUFFIXES):
        return f"⛔ 不允许写入 .env 家族文件（可能含密钥）- {path}"
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


# ── 进程环境读取拦截（2026-09-23 审计，与 token 移出环境配套）────────
# 实测：`ps eww -p <pid>` / `ps -E -p <pid>` 能把同用户进程**启动时**的环境整份
# 打出来（运行时 os.environ.pop() 无效——ps 读的是 exec 快照）；sysctl
# kern.procargs2 不含环境（0 命中）、launchctl procinfo 需 root。
# 这条是纵深防御：token 已经改走 stdin、妙想 key 也搬出启动环境，但只要还有
# 别的进程把凭据放进环境，模型一条 ps 就能读到。只拦"带 e/E 标志的 ps"，普通
# `ps aux` / `ps -p <pid>`（看进程是否在跑）保持可用。
_PS_ENV_FLAG_RE = re.compile(r"^(?:[a-zA-Z]{0,5}[eE][a-zA-Z]{0,5})$")


def reject_process_env_read(cmd: str) -> str | None:
    """读其他进程环境变量的命令 → 拒绝（返回文案），否则 None。"""
    import shlex
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        tokens = cmd.split()
    if not tokens:
        return None
    if os.path.basename(tokens[0]).lower() in ("ps", "busybox"):
        for tok in tokens[1:]:
            flag = tok.lstrip("-")
            if not flag or flag[0].isdigit():
                continue
            if os.sep in flag or flag.startswith("/"):
                continue
            if _PS_ENV_FLAG_RE.match(flag):
                return (f"⛔ 不允许读取其他进程的环境变量（ps 的 e/E 标志会把启动环境"
                        f"整份打出来，里面有凭据）: {cmd[:80]}")
    # Linux 的 /proc/<pid>/environ 同理
    if "/proc/" in cmd and "environ" in cmd:
        return f"⛔ 不允许读取 /proc/*/environ（进程环境含凭据）: {cmd[:80]}"
    return None


# ── 敏感路径出现在命令里 → 拒绝（2026-09-23 审计，读/写/命令三层同源同心）──
# 实测暴露的两个洞：(a) 读拦截只认 cat/head/tail/find 这几个**动词**，换 grep 就
# 放行——`grep -o 'tvly[^"]*' ~/.local-ai-os/config.json` 能直接把 key 打出来；
# (b) `cp ~/.ssh/id_rsa /tmp/k.txt` 放行，把私钥拷到可读位置再 cat。所以判定要按
# **路径**做：命令里出现敏感路径（复用 sensitive_read_block 的同一份判定）就拒，
# 只放行"只看存在性/元数据"的动词（ls/stat/file/test/wc…），内容读取一律不给。
_METADATA_ONLY_VERBS = frozenset({
    "ls", "stat", "file", "test", "du", "wc", "readlink", "realpath", "dirname",
    "basename", "which", "type", "echo", "printf", "true", "false",
})


def reject_sensitive_path(cmd: str) -> str | None:
    """命令的任何参数命中敏感路径 → 拒绝（元数据类动词除外）。"""
    import shlex
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        tokens = cmd.split()
    if not tokens:
        return None
    verb = os.path.basename(tokens[0]).lower()
    if verb in _METADATA_ONLY_VERBS:
        return None
    for tok in tokens[1:]:
        if not tok or tok.startswith("-") or "=" in tok:
            continue
        try:
            rp = os.path.realpath(os.path.abspath(os.path.expanduser(tok)))
        except OSError:
            continue
        if sensitive_read_block(rp):
            return (f"⛔ 命令里出现敏感路径（密钥/凭据/配置），已拒绝：{rp}")
    return None




# ── 引号感知的 shell 操作符检测（2026-09-24：python -c "...;..." 误杀）──
_SHELL_OPS = ("&&", "||", "$(", ";", "|", ">", "<")






def shell_op_error(op: str) -> str:
    return (
        f"⛔ 不支持 shell 操作符 '{op}'：本工具以 shell=False 直接执行。\n"
        f"已支持：尾部 `> 文件`、尾部 `2>&1` / `2>/dev/null`、单管道 `A | B`"
        f"（如 head f | grep x）。\n"
        f"仍不支持：`&&` `;` `||` `$()` `<`。复合命令请拆成多次调用。\n"
        f"读文件建议直接用 read_file，不必 head/cat。"
    )


def split_pipeline(cmd: str) -> tuple[str, str] | None:
    """拆一条**引号外**的单管道 `A | B`；多管道/复杂形态返回 None。

    shell=False 下管道可等价实现为 Popen1.stdout → Popen2.stdin，
    两侧仍各自 shlex.split，绝不经 shell。
    """
    if split_stdout_redirect(cmd):
        return None  # 管道 + 重定向同时出现：不解析，交给拒绝
    in_single = in_double = False
    pipe_at = None
    i, n = 0, len(cmd)
    while i < n:
        c = cmd[i]
        if c == "\\" and in_double and i + 1 < n:
            i += 2
            continue
        if c == "'" and not in_double:
            in_single = not in_single
        elif c == '"' and not in_single:
            in_double = not in_double
        elif not (in_single or in_double):
            if cmd.startswith("||", i):
                return None
            if c == "|":
                if pipe_at is not None:
                    return None
                pipe_at = i
        i += 1
    if pipe_at is None:
        return None
    left, right = cmd[:pipe_at].strip(), cmd[pipe_at + 1:].strip()
    if not left or not right:
        return None
    return left, right


def _stdout_redirect_target(cmd: str) -> tuple[str, str, bool] | None:
    """扫描 `> path` / `>> path`（引号外、单一目标）→ (head, target, append)。

    **只做解析、不做安全判定**（2026-10-11 重构）：此前安全判定混在
    split_stdout_redirect 的返回值里（被拒 = 返回 None），而 reject_sensitive_write
    要靠解析结果生成用户可见的拒绝文案——两个消费者拿同一个 None 却想要不同语义，
    修 ① 时因此出现"split 拦住了但 check_cmd 放行且没有任何提示"的自相矛盾。
    现在解析归解析、判定归判定，两边各查各的。
    """
    in_single = in_double = False
    hits: list[tuple[int, str]] = []
    i, n = 0, len(cmd)
    while i < n:
        c = cmd[i]
        if c == "\\" and in_double and i + 1 < n:
            i += 2
            continue
        if c == "'" and not in_double:
            in_single = not in_single
        elif c == '"' and not in_single:
            in_double = not in_double
        elif not (in_single or in_double):
            if cmd.startswith(">>", i) or cmd.startswith(">", i):
                # fd 前缀重定向（2> / 1>> / &>）没有等价实现：只剥掉 `>` 会把前面的
                # 数字/& 留在命令里 → 命令被静默错执行（实测 `grep x 2>/dev/null`
                # 解析成 `grep x 2` + 输出进 /dev/null，工具还报"已写入"）。
                # 只在操作符处判前缀 —— 写成对每个位置判会误伤路径（`…/00000gn/`）。
                if i > 0 and (cmd[i - 1].isdigit() or cmd[i - 1] == "&"):
                    return None
            if cmd.startswith(">>", i):
                hits.append((i, ">>"))
                i += 2
                continue
            if cmd.startswith(">", i):
                hits.append((i, ">"))
                i += 1
                continue
        i += 1
    if len(hits) != 1:
        return None
    idx, op = hits[0]
    head = cmd[:idx].rstrip()
    target = cmd[idx + len(op):].strip()
    if not head or not target:
        return None
    # 目标必须是单一简单路径（无空白/元字符/引号不配对）
    if any(ch in target for ch in " \t\n<|;&$`\'\")"):
        return None
    if len(target) > 512:
        return None
    if head.count('"') % 2 or head.count("'") % 2:
        return None
    return head, target, op == ">>"


def split_stdout_redirect(cmd: str) -> tuple[str, str, bool] | None:
    """解析**末尾**的 `> path` / `>> path`（引号外、单一目标）。

    shell=False 没有 shell 重定向，但 stdout 写文件可以等价实现，
    不必整条拒绝。返回 (命令体, 目标路径, append)；无法安全解析则 None
    （含**目标受保护**的情况——上层按「含 >」拒绝并给提示）。
    """
    parsed = _stdout_redirect_target(cmd)
    if parsed is None:
        return None
    # 敏感写路径不许经重定向落盘（判定只在这里做一次；解析见上方独立函数）
    if _protected_write_target(parsed[1]):
        return None
    return parsed


def _strip_one_stderr_tail(cmd: str) -> tuple[str, str]:
    """剥离**一个**尾部 stderr 重定向 → (命令体, "merge"|"discard"|"")。

    只认 split_stderr_redirect 说明里的两种等价形态；其它一律返回 ("", "")。
    """
    in_single = in_double = False
    pos = -1
    i, n = 0, len(cmd)
    while i < n:
        c = cmd[i]
        if c == "\\" and in_double and i + 1 < n:
            i += 2
            continue
        if c == "'" and not in_double:
            in_single = not in_single
        elif c == '"' and not in_single:
            in_double = not in_double
        elif not (in_single or in_double) and cmd.startswith("2>", i):
            # 与 stdout 重定向同款位置判定：必须是独立 token（行首或前面是空白），
            # 否则 `…x2>&1` 这种粘连形态会被错剥。只在操作符位置判前缀——写成
            # 对每个位置判会误伤路径（`…/00000gn/`，09-24 实测）。
            if i == 0 or cmd[i - 1].isspace():
                pos = i
        i += 1
    if pos < 0:
        return cmd, ""
    rest = cmd[pos + 2:].strip()
    if rest == "&1":
        mode = "merge"
    elif rest.lower() in ("/dev/null", "nul"):
        mode = "discard"
    else:
        return cmd, ""
    head = cmd[:pos].rstrip()
    if not head:
        return cmd, ""
    return head, mode


def split_stderr_redirect(cmd: str) -> tuple[str, str]:
    """剥离**尾部** stderr 重定向（可连续多个）→ (命令体, 最后生效的模式)。

    模式：`2>&1` → "merge"（错误信息并入正常输出）；`2>/dev/null`（Windows 写法
    `2>nul`）→ "discard"（丢弃错误信息）；"" = 没有。

    为什么从"一律拒绝"改为支持（2026-10-08 用户实测）：这两个有**精确等价**——
    `stderr=STDOUT` / `stderr=DEVNULL`，不像 `&&` 那样无法翻译；而模型习惯性写
    这两句（同一条命令被挡了三次，白耗轮次）。**其它 fd 形态继续拒绝**：
    `2> 文件`、`1>`、`2>>`、`&>` 会写到用户路径且无法等价，硬剥会把数字/& 留在
    命令里（2026-09-24 静默错执行事故）。
    """
    mode = ""
    body = cmd
    while True:
        # 从**右往左**剥（_strip_one_stderr_tail 取最后一个 token）——shell 里
        # 后写的生效，所以第一次剥到的模式就是最终模式，不能被更早的覆盖
        # （`… 2>&1 2>/dev/null` 等价于丢弃，实测反了才发现）。
        new, m = _strip_one_stderr_tail(body)
        if not m:
            return body, mode
        body = new
        if not mode:
            mode = m


def stderr_target(mode: str):
    """stderr 模式 → subprocess 的 stderr 目标（两条执行路径共用一份定义）。"""
    import subprocess
    return {"merge": subprocess.STDOUT,
            "discard": subprocess.DEVNULL}.get(mode, subprocess.PIPE)


def unsupported_op_message(op: str) -> str:
    """不支持的操作符统一文案（插件 / fallback / 内嵌 seed 共用一份）。

    此前两份实现的说法互相矛盾：插件写"重定向和管道都不支持"，fallback 写
    "尾部 `> 文件` 已支持"——同一个工具、两条路径给模型的指引不同（2026-09-24
    用户实测撞上）。实际支持面：末尾 `> 文件` / `>> 文件`、末尾 `2>&1` /
    `2>/dev/null`（错误信息并入/丢弃）、单管道 `A | B`、引号内的 ; | >；
    其余一律拒绝。
    """
    return (
        f"⛔ 不支持 shell 操作符 '{op}'：本工具以 shell=False 直接执行，"
        f"无法解释 {op}，否则会把它当参数传给程序（静默失败）。\n"
        "已支持：末尾 `> 文件` / `>> 文件`、末尾 `2>&1`（错误并入输出）/ "
        "`2>/dev/null`（丢弃错误）、单管道 `A | B`、引号内的 ; | > 。\n"
        "不支持：`&&`、`||`、`;`、`<`、多管道、`2> 文件`（写文件的 fd 重定向）。\n"
        "请拆成多次调用，每次只运行一条命令；需要看错误信息时用 `2>&1`。"
    )


def find_unsupported_shell_op(cmd: str) -> str | None:
    """返回引号外的 shell 操作符；引号内（解释器脚本里的 ; | >）不拦。

    shell=False 不解释引号内的元字符——`python3 -c "a; b"` 里的 `;` 是
    Python 语句分隔符，拦它属于误杀。
    """
    # 尾部有等价实现的形态先剥离，再对残余扫描：stderr 重定向（2>&1 / 2>/dev/null）、
    # stdout 重定向（> file）、单管道（A | B，两侧各自递归）
    body = cmd
    try:
        body, _mode = split_stderr_redirect(body)
        sp = split_stdout_redirect(body)
        if sp:
            body = sp[0]
        else:
            _pl = split_pipeline(body)
            if _pl:
                # 管道两侧再各查一次（嵌套的 ; && 等）
                left, right = _pl
                for side in (left, right):
                    bad = find_unsupported_shell_op(side)
                    if bad and bad != "|":
                        return bad
                return None
    except Exception:
        body = cmd
    in_single = in_double = False
    i, n = 0, len(body)
    while i < n:
        c = body[i]
        if c == "\\" and in_double and i + 1 < n:
            i += 2
            continue
        if c == "'" and not in_double:
            in_single = not in_single
        elif c == '"' and not in_single:
            in_double = not in_double
        elif not (in_single or in_double):
            for op in _SHELL_OPS:
                if body.startswith(op, i):
                    return op
        i += 1
    return None


_INLINE_LANG = (
    r"(?:python(?:3(?:\.\d+)*)?|node|nodejs|deno|bun|ruby|perl|php|lua|nu"
    r"|pwsh|powershell|ba(?:sh)?|zsh|ksh|fish)"
)
_INTERP_INLINE_PATTERN = (
    r"\b(python(?:3(?:\.\d+)*)?|node|nodejs|deno|bun|ruby|perl|php|lua|nu"
    r"|pwsh|powershell)\s+(-[a-zA-Z]*[ce]\b|--command\b|--eval\b)"
)


def extract_inline_code(cmd: str) -> str | None:
    """取出解释器 -c/-e/--command/--eval 的内联源码；非内联解释器命令返回 None。"""
    import shlex
    try:
        toks = shlex.split(cmd)
    except ValueError:
        return None
    for i, tok in enumerate(toks[:-1]):
        base = Path(tok).name.lower() if "/" in tok or "\\" in tok else tok.lower()
        if base not in ("python", "python3", "node", "nodejs", "deno", "bun",
                        "ruby", "perl", "php", "lua", "nu", "pwsh", "powershell",
                        "bash", "zsh", "ksh", "sh", "fish"):
            # python3.11 等
            if not (base.startswith("python") and base[6:].replace(".", "").isdigit()):
                continue
        flag = toks[i + 1]
        if flag in ("-c", "-e", "--command", "--eval") or (
            flag.startswith("-") and not flag.startswith("--")
            and re.fullmatch(r"-[a-zA-Z]*[ce]", flag)
        ):
            return toks[i + 2] if i + 2 < len(toks) else ""
    return None


def _looks_like_path_str(s: str) -> bool:
    """字符串是否值得按"路径"审查（过滤噪声：`"a/b"`、`"1+1"` 这类不查）。"""
    if not s or len(s) > 512:
        return False
    s = s.strip()
    if not s or "\n" in s:
        return False
    if s.startswith(("/", "~")):
        return True
    if "/" in s:
        return True
    if s in _SHELL_INIT_BASENAMES or s in _BLOCKED_FILE_NAMES:
        return True
    return False


def _inline_path_audit(code: str) -> str | None:
    """内联代码/脚本的**路径审查**（2026-10-11 第四轮修复）。

    背景：解释器内联代码此前只过"形状黑名单 + 禁止调用名单"——它看代码长什么
    样，不看代码碰什么路径。实测 `python3 -c "open('/x/config.json').read()"`
    与拼字符串变体全部放行，而同一条路径的传统形态（cat/cp）被拦——同一件事
    两种写法两种结局，且解释器能把路径在运行期拼出来，黑名单永远追不上。
    修法：AST 抽字符串字面量 → 逐个过**已有**的读/写封印判据
    （sensitive_read_block / sensitive_write_block），路径形态与内联形态同判据。
    非法语法/非 Python 代码返回 None（交给上层既有黑名单处理，不重复兜底）。
    """
    import ast
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return None
    lits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            lits.append(node.value)
        elif isinstance(node, ast.JoinedStr):          # f-string 的静态片段
            for v in node.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    lits.append(v.value)
    seps = ("/", os.sep, "\\")
    for s in lits:
        if not _looks_like_path_str(s):
            continue
        cands = [s]
        # 相对路径按"有分隔符"与"当前工作目录"两种口径解析，两种都查
        if s.startswith("~"):
            cands.append(os.path.expanduser(s))
        elif not s.startswith("/"):
            if any(k in s for k in seps):
                cands.append(os.path.join(os.getcwd(), s))
            cands.append(os.path.expanduser("~/" + s))
        for cand in cands:
            hit = sensitive_read_block(cand) or sensitive_write_block(cand)
            if hit:
                return (f"⛔ Blocked: 代码中引用了受保护的路径（内联/脚本路径审查）: {cand}")
    return None


def check_inline_code(code: str) -> str | None:
    """审查解释器内联源码（黑名单 + Python AST 禁调用 + 路径审查）。"""
    if not code or not code.strip():
        return "⛔ Blocked: 内联代码为空"
    if len(code) > 8000:
        return "⛔ Blocked: 内联代码过长，拒绝执行"
    low = code.lower()
    for pattern in DESTRUCTIVE_PATTERNS:
        if re.search(pattern, low):
            return "⛔ Blocked: 内联代码命中破坏性模式，拒绝执行"
    for pattern in OBFUSCATION_PATTERNS:
        # 「解释器 -c」自身那条不对内联体生效（否则永远拦自己）
        if "python" in pattern and "[ce]" in pattern:
            continue
        if re.search(pattern, low):
            return "⛔ Blocked: 内联代码命中危险模式，拒绝执行"
    # 看起来像 Python 时过 AST 禁调用（与 plugin_creator 同一份）
    if re.search(r"\b(import |def |print\(|from \w+ import)", code):
        try:
            from plugin_creator import validate_plugin_code
            v = validate_plugin_code(code)
            sec = [e for e in (v.get("errors") or []) if "禁止的调用" in e]
            if sec:
                return f"⛔ Blocked: 内联代码含禁止调用，拒绝执行: {'; '.join(sec)}"
        except Exception:
            return "⛔ Blocked: 内联代码无法静态解析，拒绝执行"
    # 路径审查（2026-10-11）：代码里引用的路径过读/写封印判据——拼字符串
    # 变体（'.../x/'+'config.json'）也能被 AST 字面量捕获
    return _inline_path_audit(code)


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
        from safety.rules import Verdict, analyze, decide
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
            # 解释器 -c/-e：一刀切会误杀 python3 -c "print(1); print(2)"——
            # 改为审查内联源码（与脚本文件同一黑名单 + AST 禁调用）
            if "[ce]" in pattern and "python" in pattern:
                inl = extract_inline_code(cmd)
                if inl is not None:
                    denied = check_inline_code(inl)
                    if denied:
                        return denied
                    continue
            return f"⛔ Blocked potentially unsafe command: {cmd}"
    return (reject_sensitive_read(cmd) or reject_sensitive_write(cmd)
            or reject_process_env_read(cmd) or reject_sensitive_path(cmd))

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
    # 登录/自启即执行（2026-10-11 第三轮审计修复）：~/.zshrc、LaunchAgents、
    # crontab、git hooks 此前全部可写穿——比 extensions 更直接的持久化 RCE
    # 通道（实测 write_file 写穿 .zshrc 与 plist）。
    _auto = _is_autoload_path(_rp)
    if _auto:
        return (f"⛔ 不允许写入{_auto}: {_rp}。"
                "如需修改，请自己编辑或把内容写到普通文件后手动应用")
    for d in _app_data_dirs():
        for sub in ("extensions", "skills"):
            _dir = f"{d}{os.sep}{sub}"
            if _rp == d + os.sep + sub or _rp.startswith(_dir + os.sep):
                return (f"⛔ 不允许写入自动加载目录（{sub}）—— 写进去的代码会在下次启动/"
                        f"热重载时执行: {_rp}")
    # 权限/代理/技能注册表：写一次 permissions.json 就能把 run_cmd 降成 safe，
    # 确认门被持久关掉（P0）。agents.json/skills.json 同理可植入提示注入。
    _base = os.path.basename(_rp).lower()
    if _base in _APP_REGISTRY_FILES:
        for d in _app_data_dirs():
            if _rp.startswith(d + os.sep):
                return f"⛔ 不允许改写安全配置（{_base}）—— 走设置界面: {_rp}"
    # 身份文件直通系统提示（prompt_build 会以"除与上方系统规则冲突外一律照做"
    # 注入 IDENTITY/SOUL/AGENTS/USER.md 全文）——2026-10-11 深审 High：它们不在
    # 任何密封名单里，一次被批准的 write_file 即可跨会话改写"你是谁/工作规则"。
    # 修改走设置页 / 身份意图通道（identity._apply_*），不走工具与命令。
    if _base in IDENTITY_FILE_NAMES:
        for d in _app_data_dirs():
            if _rp.startswith(d + os.sep):
                return (f"⛔ 不允许改写身份文件（{_base}）—— 它会进系统提示、跨会话生效。"
                        f"如需修改请走设置界面或直接说「改名叫X/语气改成Y」: {_rp}")
    # 应用配置含凭据（云端 key / Tavily key），且改它等于改安全开关；走设置界面
    if _base.startswith("config.json"):
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
    """单个 argv token 是否是受保护的写入目标（自动加载目录 / 注册表文件 / config.json）。"""
    if not tok or tok.startswith("-"):
        return None
    if "=" in tok:                      # dd of=/path、--output=/path、-o=/path
        tok = tok.split("=", 1)[1]
    if not tok or tok.startswith("-"):
        return None
    rp = os.path.realpath(os.path.abspath(os.path.expanduser(tok)))
    # 登录/自启即执行的路径（2026-10-11 第三轮审计修复）：此前 `.zshrc`、
    # LaunchAgents、crontab、git hooks 全部可经命令写穿（`cp`/`tee`/`echo >`）。
    if _is_autoload_path(rp):
        return rp
    for d in _app_data_dirs():
        for sub in ("extensions", "skills"):
            if rp == f"{d}{os.sep}{sub}" or rp.startswith(f"{d}{os.sep}{sub}{os.sep}"):
                return rp
        _base = os.path.basename(rp).lower()
        # 注册表文件与 config.json 同级保护：命令路径此前漏了 permissions/agents/skills.json，
        # `cp` 一次就能把确认门持久关掉（2026-09-24 复查实测 check_cmd 放行）。
        if _base in _APP_REGISTRY_FILES and rp.startswith(d + os.sep):
            return rp
        # 身份文件：与工具路径同一份名单（2026-10-11）——`cp`/`tee`/`echo >` 不得
        # 改写 IDENTITY/SOUL/AGENTS/USER.md，它们直通系统提示。
        if _base in IDENTITY_FILE_NAMES and rp.startswith(d + os.sep):
            return rp
        if _base.startswith("config.json") and rp.startswith(d + os.sep):
            return rp
    return None


def reject_sensitive_write(cmd: str) -> str | None:
    """命令写入自动加载目录/安全配置时返回拒绝文案，否则 None。

    2026-10-11 安全审计修复（实测成立的两个缺陷叠加）：
    ① **只读动词白名单短路**——echo/printf 等为"看目录"而设的白名单在函数开头
       `return None`，重定向形态下目标根本没被检查（cp/tee/sed -i 因不在名单反而
       拦得住，同一路径两种结局）；
    ② **裸路径不着检查**——`reject_sensitive_write(目标路径)` 是 split_stdout_redirect
       的内部调用形态，而裸路径经 shlex.split 是**单个 token**，循环从 tokens[1:]
       开始 → 目标自己永远不被检查（这条让 ① 的修复在上层重定向路径里失效）。
    修法：先解析重定向目标检查；动词是路径形态时也检查 tokens[0]；再做白名单短路。
    """
    import shlex
    # ① 重定向目标先查（不管动词是什么）——echo/printf 写受保护路径必须拦。
    #    用**纯解析**函数（不查安全），命中就给拒绝文案；split_stdout_redirect
    #    自己的封印判定只负责让上层按「含 >」拒绝，两者语义不再混用。
    try:
        _parsed = _stdout_redirect_target(cmd)
    except Exception:
        _parsed = None
    if _parsed is not None:
        hit = _protected_write_target(_parsed[1])
        if hit:
            return (f"⛔ 不允许用命令写入自动加载目录/配置（写进去的代码会被执行、"
                    f"配置含 API 密钥）: {hit}")
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        tokens = cmd.split()
    if not tokens:
        return None
    verb = os.path.basename(tokens[0]).lower()
    # ②b awk 的写目标是它自己的语法（print > "file"），不是路径 token——
    # 2026-10-11 实测 `awk 'BEGIN{print "x" > "~/.local-ai-os/permissions.json"}'`
    # 放行（同一目标用 echo/printf 写就被拦）。这里解析 awk 程序里的重定向目标。
    if verb in ("awk", "gawk", "mawk", "nawk"):
        for tok in tokens[1:]:
            if tok.startswith("-"):
                continue
            for m in re.finditer(r">>?\s*\"([^\"]{1,512})\"", tok):
                hit = _protected_write_target(os.path.expanduser(m.group(1)))
                if hit:
                    return (f"⛔ 不允许用命令写入自动加载目录/配置（写进去的代码会被执行、"
                            f"配置含 API 密钥）: {hit}")
    # ② 裸路径形态（split_stdout_redirect 的内部调用）：tokens[0] 本身就是目标
    if "/" in tokens[0] or tokens[0].startswith("~"):
        hit = _protected_write_target(tokens[0])
        if hit:
            return (f"⛔ 不允许用命令写入自动加载目录/配置（写进去的代码会被执行、"
                    f"配置含 API 密钥）: {hit}")
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


# ── ⑨ 解释器脚本内容审查（堵 write_file → run_cmd 组合逃逸）────────
# 命令黑名单只看 argv，拦不住 "python3 /tmp/pwn.py"——脚本体可为任意代码。
# 显式脚本仍放行（合法工作流），但**内容**必须先过同一套混淆/破坏性规则。
_SCRIPTY_CMD_RE = re.compile(
    r"^(?:.*/)?(python|python3|python3\.\d+[0-9]*|node|nodejs|deno|bun|ruby|perl|php|lua|nu|pwsh|powershell|ba|z|k|d?sh|bash|zsh|dash|fish)\s+\S+\.(py|js|mjs|cjs|ts|rb|pl|php|lua|ps1|sh|bash|zsh|fish)\b",
    re.IGNORECASE,
)
_SCRIPT_MAX_BYTES = 256 * 1024


def check_script_content(script_path: str) -> str | None:
    """解释器执行外部脚本前的内容审查。返回拒绝文案或 None（放行）。

    只审"会被解释器执行的脚本文件"；大小超限 fail-closed（不放行巨文件赌运气）。
    """
    try:
        rp = os.path.realpath(os.path.expanduser(script_path))
    except Exception:
        return "⛔ Blocked: 脚本路径无效"
    if not os.path.isfile(rp):
        return None  # 不存在由执行层报错
    try:
        if os.path.getsize(rp) > _SCRIPT_MAX_BYTES:
            return f"⛔ Blocked: 脚本过大，拒绝执行: {rp}"
        with open(rp, "r", encoding="utf-8", errors="ignore") as f:
            body = f.read(_SCRIPT_MAX_BYTES)
    except Exception as e:
        return f"⛔ Blocked: 无法读取脚本内容，拒绝执行: {e}"
    # 逐行/全文过混淆与破坏性规则（与命令侧同一份黑名单）
    low = body.lower()
    for pattern in OBFUSCATION_PATTERNS:
        if re.search(pattern, low):
            return f"⛔ Blocked: 脚本内容命中危险模式，拒绝执行: {rp}"
    for pattern in DESTRUCTIVE_PATTERNS:
        if re.search(pattern, low):
            return f"⛔ Blocked: 脚本内容命中破坏性模式，拒绝执行: {rp}"
    # Python 脚本再过 AST 禁调用（eval/exec/os.system 等）
    if rp.endswith(".py"):
        try:
            from plugin_creator import validate_plugin_code
            v = validate_plugin_code(body)
            sec = [e for e in (v.get("errors") or []) if "禁止的调用" in e]
            if sec:
                return f"⛔ Blocked: 脚本含禁止调用，拒绝执行: {'; '.join(sec)}"
        except Exception:
            # AST 不可用/语法失败 → fail-closed（内容审查是唯一防线时不能放行）
            return f"⛔ Blocked: 脚本无法静态解析，拒绝执行: {rp}"
        # 路径审查（2026-10-11）：脚本里引用的路径同样过读/写封印判据
        path_denied = _inline_path_audit(body)
        if path_denied:
            return path_denied
    return None


def check_cmd_with_script(cmd: str) -> str | None:
    """命令规则 + 解释器脚本内容审查（run_cmd / launch_bg 共用）。"""
    denied = check_cmd(cmd)
    if denied:
        return denied
    m = _SCRIPTY_CMD_RE.match(cmd.strip())
    if not m:
        return None
    # 取脚本参数（第一个以脚本扩展名结尾的 token）
    import shlex
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return None
    for tok in tokens[1:]:
        if tok.startswith("-"):
            continue
        if re.search(r"\.(py|js|mjs|cjs|ts|rb|pl|php|lua|ps1|sh|bash|zsh|fish)$", tok, re.I):
            return check_script_content(tok)
    return None
