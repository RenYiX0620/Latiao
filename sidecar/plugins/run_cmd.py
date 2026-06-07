"""Run a shell command and return its output. ⚠️ Requires user confirmation."""
import re
import shlex
import subprocess

NAME = "run_cmd"
PERMISSION = "confirm"

DEFINITION = {
    "type": "function",
    "function": {
        "name": "run_cmd",
        "description": "Run a shell command and return its output. ⚠️ Requires user confirmation. Destructive commands are always blocked.",
        "parameters": {
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "description": "The shell command to execute."}
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
]

# Commands that are always allowed (whitelist override for common dev tools)
_ALWAYS_ALLOWED = re.compile(
    r"^(ls|pwd|echo|cat|head|tail|wc|file|which|whoami|uname|date|env|printenv)$"
)


def execute(args: dict) -> str:
    cmd = (args.get("cmd") or args.get("command", "")).strip()
    cmd_lower = cmd.lower()

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
    try:
        r = subprocess.run(shlex.split(cmd), shell=False, capture_output=True, text=True, timeout=30)
        out = r.stdout.strip()
        if r.returncode != 0:
            out += f"\n(退出码: {r.returncode})"
            if r.stderr.strip():
                out += f"\n{r.stderr.strip()}"
        return out or "(无输出)"
    except subprocess.TimeoutExpired:
        return f"超时: {cmd}"
    except Exception as e:
        return f"错误：{e}"
