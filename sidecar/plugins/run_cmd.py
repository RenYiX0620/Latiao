"""Run a shell command and return its output. ⚠️ Requires user confirmation."""
import shlex
import subprocess

# 安全不变量单点定义（破坏/混淆/白名单/敏感路径），
# fallback 与 seed 共用同一模块，消除三处漂移（审计 P0）。
from cmd_safety import (
    child_env,
    DESTRUCTIVE_PATTERNS,
    OBFUSCATION_PATTERNS,
    SAFE_CMD_RE,
    check_cmd_with_script,
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
def _reject_shell_operators(cmd: str) -> str | None:
    """引号外的 shell 操作符才拦；引号内（python -c "...;..."）不误杀。"""
    from cmd_safety import find_unsupported_shell_op, unsupported_op_message
    op = find_unsupported_shell_op(cmd)
    if not op:
        return None
    return unsupported_op_message(op)



def _run_pipeline(left: str, right: str, timeout: int) -> str:
    import shlex as _shlex
    import subprocess as _sp
    from cmd_safety import child_env
    p1 = _sp.Popen(_shlex.split(left), shell=False, stdout=_sp.PIPE, stderr=_sp.PIPE,
                   text=True, env=child_env())
    p2 = _sp.Popen(_shlex.split(right), shell=False, stdin=p1.stdout, stdout=_sp.PIPE,
                   stderr=_sp.PIPE, text=True, env=child_env())
    if p1.stdout:
        p1.stdout.close()
    try:
        out, err = p2.communicate(timeout=timeout)
    except _sp.TimeoutExpired:
        p1.kill()
        p2.kill()
        return "超时"
    p1.wait(timeout=5)
    body = (out or "").strip()
    if p2.returncode != 0:
        body += f"\n(退出码: {p2.returncode})"
        if err and err.strip():
            body += f"\n{err.strip()}"
    return body or "(无输出)"


def execute(args: dict) -> str:
    cmd = (args.get("cmd") or args.get("command", "")).strip()

    # ── Reject unsupported shell syntax FIRST (before whitelist shortcut) ──
    rejected = _reject_shell_operators(cmd)
    if rejected:
        return rejected



    # 单管道：Popen A | B（shell=False 两侧各自 exec）
    _pipe = None
    try:
        from cmd_safety import split_pipeline
        _pipe = split_pipeline(cmd)
    except Exception:
        _pipe = None

    # 尾部 stdout 重定向：shell=False 等价实现（写文件）
    _redir = None
    try:
        from cmd_safety import split_stdout_redirect
        _redir = split_stdout_redirect(cmd)
    except Exception:
        _redir = None
    if _redir:
        cmd, _target, _append = _redir

    # ── Whitelist fast path for simple safe commands ──
    # 整条命令必须完全匹配白名单形态，且命中后仍走完整安全检查——
    # 此前只校验首 token 且命中即整条直行，"env curl ..." 可绕过全部
    # 黑名单执行任意命令（P0）。env/printenv 已从白名单移除。
    if SAFE_CMD_RE.match(cmd) and len(cmd) < 200:
        denied = check_cmd_with_script(cmd)
        if denied:
            return denied
        try:
            r = subprocess.run(shlex.split(cmd), shell=False, capture_output=True, text=True, env=child_env(), timeout=10)
            if _redir:
                with open(_target, "a" if _append else "w", encoding="utf-8") as _f:
                    _f.write(r.stdout or "")
                return f"已写入 {_target}（{len(r.stdout or '')} 字符）" + (f"\n{r.stderr.strip()}" if r.stderr.strip() else "")
            return r.stdout.strip() or r.stderr.strip() or "(无输出)"
        except subprocess.TimeoutExpired:
            return f"超时: {cmd}"
        except Exception as e:
            return f"错误：{e}"

    # ── Full safety check for everything else（含脚本内容审查）──
    denied = check_cmd_with_script(cmd)
    if denied:
        return denied

    # ── Length limit ──
    if len(cmd) > 1000:
        return f"⛔ Command too long ({len(cmd)} chars, max 1000)"

    # ── Execute ──
    if _pipe:
        return _run_pipeline(_pipe[0], _pipe[1], 300)
    # 30s 会截断 npm install/构建类长任务——放宽到 300s（P2-15）
    try:
        r = subprocess.run(shlex.split(cmd), shell=False, capture_output=True, text=True, env=child_env(), timeout=300)
        out = r.stdout.strip()
        if r.returncode != 0:
            out += f"\n(退出码: {r.returncode})"
            if r.stderr.strip():
                out += f"\n{r.stderr.strip()}"
        if _redir:
            body = r.stdout or ""
            with open(_target, "a" if _append else "w", encoding="utf-8") as _f:
                _f.write(body)
            return (f"已写入 {_target}（{len(body)} 字符）"
                    + (f"\n退出码: {r.returncode}" if r.returncode else "")
                    + (f"\n{r.stderr.strip()}" if r.stderr and r.stderr.strip() else ""))
        return out or "(无输出)"
    except subprocess.TimeoutExpired:
        return (f"超时: 命令已运行 5 分钟被截断。长任务请拆分为多步执行，"
                f"或改用后台方式（nohup ... &）。\n命令: {cmd}")
    except Exception as e:
        return f"错误：{e}"
