"""命令安全不变量——run_cmd 插件、fallback、seed 三处共享的单点定义。

此前三份实现各自维护黑名单（插件版有解释器内联拦截，fallback/seed 没有），
插件目录被删或重建时安全逻辑整体回退。本模块是唯一事实源（审计 P0）：
- 破坏性模式（rm/sudo/dd 等）
- 混淆/内联执行模式（python3 -c / node -e / curl|sh 等）
- 白名单快捷路径：整条命令必须完全匹配简单形态，且命中后仍走完整检查
- 敏感路径读取拦截（cat .ssh/id_rsa 等，与 read_file 插件黑名单对齐）
"""
import platform
import re

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


def check_cmd(cmd: str) -> str | None:
    """整条命令的破坏/混淆/敏感读取检查。返回拒绝文案或 None（放行）。"""
    low = cmd.lower()
    for pattern in DESTRUCTIVE_PATTERNS:
        if re.search(pattern, low):
            return f"⛔ Blocked destructive command: {cmd}"
    for pattern in OBFUSCATION_PATTERNS:
        if re.search(pattern, low):
            return f"⛔ Blocked potentially unsafe command: {cmd}"
    return reject_sensitive_read(cmd)
