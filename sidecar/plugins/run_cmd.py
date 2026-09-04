"""Run a shell command and return its output. ⚠️ Requires user confirmation."""
import platform
import re
import shlex
import subprocess

_IS_WINDOWS = platform.system() == "Windows"
_WIN_CMDS = r"|dir|cd|where|ver|type|find|findstr" if _IS_WINDOWS else ""

NAME = "run_cmd"
PERMISSION = "confirm"

DEFINITION = {
    "type": "function",
    "function": {
        "name": "run_cmd",
        "description": "Run ONE single command and return its output (no shell). ⚠️ Requires user confirmation. Destructive commands are always blocked. Do NOT use shell operators like && | ; > < — they are not supported; call this tool once per command instead.",
        "parameters": {
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "description": "A single command with arguments, e.g. 'python3 -m pytest -v'. No shell operators (&& | ; >)."}
            },
            "required": ["cmd"]
        }
    }
}

# ── Blocked patterns (checked case-insensitive) ──
_DESTRUCTIVE_PATTERNS = [
    # File system destruction
    r"rm\s+(-[a-z]*[rf]|--recursive|--force)",
    r">\s*/dev/(sd|nvme|hd|disk|dm-)",
    r"dd\s+if=",
    r"mkfs",
    r"mkswap",
    r"wipefs",
    # Privilege escalation / system control
    r"\bsudo\b", r"\bdoas\b", r"\bsu\s",
    r"\bshutdown\b", r"\breboot\b", r"\bhalt\b", r"\bpoweroff\b",
    r"\binit\s+[0-6]",
    r"\bsystemctl\s+(shutdown|reboot|halt|poweroff|suspend)",
    r"\blaunchctl\s+(unload|remove)",
    # Fork bomb / resource exhaustion
    r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;",
    r"\bperl\s+-e\b",  # often used for obfuscation
    # Network dangerous
    r"\biptables\s+-F\b", r"\bpfctl\s+-d\b",
    # Write to critical paths
    r">\s*/etc/", r">>\s*/etc/",
    r">\s*/System/", r">>\s*/System/",
    r"chmod\s+[0-7]*7[0-7]*\s+/",
    r"chown\s+-R\s+",
]

_OBFUSCATION_PATTERNS = [
    r"\beval\s", r"\bbase64\s+(-d|--decode)", r"\bxxd\s+-r",
    r"`[^`]+`",  # backtick subshell
    r"\$\([^)]+\)",  # $() subshell
    r"\\x[0-9a-fA-F]{2}",  # hex-encoded chars in command
    r"\bcurl\b.*\|\s*(ba)?sh\b", r"\bwget\b.*\|\s*(ba)?sh\b",  # curl | sh
    # 解释器内联代码（审计 H3：黑名单可被 python3 -c / node -e / bash -c 绕过——
    # shell=False 不拦这些，内联脚本可执行任意操作）。显式命令/脚本文件仍放行
    # （如 python3 script.py、node app.js、bash deploy.sh）。
    r"\b(python|python3|python3\.\d+[0-9]*|node|nodejs|deno|bun|ruby|perl|php|lua|nu|pwsh|powershell)\s+(-[a-zA-Z]*[ce]\b|--command\b|--eval\b)",
    r"\b(ba|z|k|d)?sh\s+(-[a-zA-Z]*[ce]\b|--command\b)",
    r"\bfish\s+(-c\b|--command\b)",
]

# Commands that are always allowed (whitelist override for common dev tools)
_ALWAYS_ALLOWED = re.compile(
    r"^(ls|pwd|echo|cat|head|tail|wc|file|which|whoami|uname|date|env|printenv"
    + _WIN_CMDS
    + r")$"
)

# Shell operators that are NOT supported because we run with shell=False.
# If these slip through, shlex.split() passes them as literal arguments and the
# command silently misbehaves (e.g. "cd x && pytest" only runs `cd`, exits 0,
# prints nothing — the agent then hallucinates success). Detect and reject loudly.
_SHELL_OPERATORS = re.compile(r"(&&|\|\||\||;|\$\(|>|<)")


def _reject_shell_operators(cmd: str) -> str | None:
    """Return an error string if cmd uses shell syntax we can't execute, else None."""
    m = _SHELL_OPERATORS.search(cmd)
    if not m:
        return None
    return (
        f"⛔ 不支持 shell 操作符 '{m.group(1)}'：本工具以 shell=False 直接执行，"
        f"无法解释 {m.group(1)}，否则只会运行第一个命令并把其余当参数（静默失败）。\n"
        "请拆成多次调用，每次只运行一条命令，例如：\n"
        "  ❌ cd /tmp/x && python3 -m pytest -v\n"
        "  ✅ 第1次 run_cmd: cd /tmp/x    第2次 run_cmd: python3 -m pytest -v\n"
        "重定向(>)和管道(|)同样不支持；需要输出时请让命令直接打印到 stdout。"
    )


def execute(args: dict) -> str:
    cmd = (args.get("cmd") or args.get("command", "")).strip()
    cmd_lower = cmd.lower()

    # ── Reject unsupported shell syntax FIRST (before whitelist shortcut) ──
    rejected = _reject_shell_operators(cmd)
    if rejected:
        return rejected

    # ── Whitelist check for simple safe commands ──
    base_cmd = cmd_lower.split()[0] if cmd_lower.split() else ""
    if _ALWAYS_ALLOWED.match(base_cmd) and len(cmd) < 200:
        try:
            r = subprocess.run(shlex.split(cmd), shell=False, capture_output=True, text=True, timeout=10)
            return r.stdout.strip() or r.stderr.strip() or "(无输出)"
        except subprocess.TimeoutExpired:
            return f"超时: {cmd}"
        except Exception as e:
            return f"错误：{e}"

    # ── Block destructive patterns ──
    for pattern in _DESTRUCTIVE_PATTERNS:
        if re.search(pattern, cmd_lower):
            return f"⛔ Blocked destructive command: {cmd}"

    # ── Block obfuscation attempts ──
    for pattern in _OBFUSCATION_PATTERNS:
        if re.search(pattern, cmd_lower):
            return f"⛔ Blocked potentially unsafe command: {cmd}"

    # ── Length limit ──
    if len(cmd) > 1000:
        return f"⛔ Command too long ({len(cmd)} chars, max 1000)"

    # ── Execute ──
    # 30s 会截断 npm install/构建类长任务——放宽到 300s（P2-15）
    try:
        r = subprocess.run(shlex.split(cmd), shell=False, capture_output=True, text=True, timeout=300)
        out = r.stdout.strip()
        if r.returncode != 0:
            out += f"\n(退出码: {r.returncode})"
            if r.stderr.strip():
                out += f"\n{r.stderr.strip()}"
        return out or "(无输出)"
    except subprocess.TimeoutExpired:
        return (f"超时: 命令已运行 5 分钟被截断。长任务请拆分为多步执行，"
                f"或改用后台方式（nohup ... &）。\n命令: {cmd}")
    except Exception as e:
        return f"错误：{e}"
