"""Run a shell command and return its output. ⚠️ Requires user confirmation."""
import re
import shlex
import subprocess

# 安全不变量单点定义（破坏/混淆/白名单/敏感路径），
# fallback 与 seed 共用同一模块，消除三处漂移（审计 P0）。
from cmd_safety import (
    DESTRUCTIVE_PATTERNS,
    OBFUSCATION_PATTERNS,
    SAFE_CMD_RE,
    check_cmd,
)

# 保留旧名字导出：test_security 等引用这些名字
_DESTRUCTIVE_PATTERNS = DESTRUCTIVE_PATTERNS
_OBFUSCATION_PATTERNS = OBFUSCATION_PATTERNS
_ALWAYS_ALLOWED = SAFE_CMD_RE

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

    # ── Reject unsupported shell syntax FIRST (before whitelist shortcut) ──
    rejected = _reject_shell_operators(cmd)
    if rejected:
        return rejected

    # ── Whitelist fast path for simple safe commands ──
    # 整条命令必须完全匹配白名单形态，且命中后仍走完整安全检查——
    # 此前只校验首 token 且命中即整条直行，"env curl ..." 可绕过全部
    # 黑名单执行任意命令（P0）。env/printenv 已从白名单移除。
    if SAFE_CMD_RE.match(cmd) and len(cmd) < 200:
        denied = check_cmd(cmd)
        if denied:
            return denied
        try:
            r = subprocess.run(shlex.split(cmd), shell=False, capture_output=True, text=True, timeout=10)
            return r.stdout.strip() or r.stderr.strip() or "(无输出)"
        except subprocess.TimeoutExpired:
            return f"超时: {cmd}"
        except Exception as e:
            return f"错误：{e}"

    # ── Full safety check for everything else ──
    denied = check_cmd(cmd)
    if denied:
        return denied

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
