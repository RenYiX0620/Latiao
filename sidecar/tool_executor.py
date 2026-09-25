"""Tool Executor — fallback tool implementations, dispatch table, permission resolution.

Split from main.py (Section 4: Tool Fallbacks + permission helpers). Code is a
verbatim move from main.py — only imports were adjusted for the module split.
"""
import fnmatch
import json
import logging
import os
import platform
import re
import shlex
import subprocess
from pathlib import Path

import httpx

# 命令安全不变量单点定义（审计 P0）：插件、本 fallback、插件 seed 共用，
# 消除三处漂移——fallback 此前缺解释器内联拦截（python3 -c / node -e）。
from cmd_safety import child_env, reject_sensitive_read

logger = logging.getLogger("latiao-sidecar")


def __getattr__(name: str):
    """兼容层：扩展包从本模块导入 `execute_tool`（实际定义在 agent_loop）。

    09-21 实测：官方 finance-pack 的 market_insight 工具写的是
    `from tool_executor import execute_tool`，而该函数从来只存在于 agent_loop.py
    → 每次调用都报 `cannot import name 'execute_tool' from 'tool_executor'`，
    模型只能退回手动多次查询（用户侧表现为"数据来源/时点全乱"）。第三方的聚合型
    工具包很可能照抄同一个写法，所以在**调用点**做惰性转发，而不是让每个包各自改。
    用模块级 __getattr__ 而不是顶层 import：agent_loop 会 import 本模块，
    顶层转发会形成循环导入。
    """
    if name == "execute_tool":
        from agent_loop import execute_tool as _execute_tool
        return _execute_tool
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# ═══════════════════════════════════════════════════════
#  Harness: 工具权限分级 + 状态持久化
# ═══════════════════════════════════════════════════════

# Fallback definitions used when no plugins are found
_FALLBACK_PERMISSIONS = {
    "read_file": "safe",
    "list_dir": "safe",
    "search_files": "safe",
    "write_file": "confirm",
    "run_cmd": "confirm",
    "open_app": "confirm",
    "open_folder": "confirm",
    "delegate_task": "safe",
    "tavily_search": "safe",
    "web_search": "safe",
}


def _resolve_permission(tool_name: str, args: dict) -> str:
    """
    Resolve permission level for a tool call.
    Priority: path_pattern rules (permissions.json) → capability 表（统一模型）
    → legacy 无路径规则 → TOOL_PERMISSIONS 插件默认。
    use_skill 特殊处理：解析目标技能的安全等级（技能权限在 capabilities 表）。
    """
    # 状态由 main.py 门面持有（测试通过 main._custom_permissions 重绑定，
    # 这里必须在调用时读 main 的实时槽位）→ 函数内 lazy import 避免循环依赖
    from main import TOOL_PERMISSIONS, _custom_permissions
    for rule in _custom_permissions:
        if rule.get("tool") != tool_name:
            continue
        path_pattern = rule.get("path_pattern")
        if path_pattern:
            for val in args.values():
                if isinstance(val, str) and (
                    fnmatch.fnmatch(val, path_pattern) or
                    fnmatch.fnmatch(os.path.expanduser(val), os.path.expanduser(path_pattern))
                ):
                    return rule.get("permission", "confirm")
        else:
            return rule.get("permission", "confirm")
    # 统一能力模型：use_skill 的技能安全等级 / 工具的表中权限
    try:
        import capability_registry
        if tool_name == "use_skill":
            skill_name = str(args.get("skill_name") or "")
            if skill_name:
                skill = capability_registry.get_skill_content(skill_name)
                return skill["permission"] if skill else "safe"
        table_perm = capability_registry.get_permission(tool_name)
        if table_perm:
            return table_perm
    except Exception:
        logger.debug("capability registry lookup failed for %s", tool_name, exc_info=True)
    # 未知工具 fail-close（审计 P0）：此前默认 "safe"——任何拼写错误/未注册
    # 工具都免确认执行。未知应降级为 confirm，由用户把关。
    return TOOL_PERMISSIONS.get(tool_name, "confirm")


#  工具执行函数
# ═══════════════════════════════════════════════════════

MAX_READ_SIZE = 50000  # chars before truncation (~1500 lines)

# ╔══════════════════════════════════════════════════════╗
# ║  SECTION 4: Tool Fallbacks                           ║
# ║  Fallback impls used when plugins/ is empty           ║
# ╚══════════════════════════════════════════════════════╝

def read_file(path: str, offset: int = 0, limit: int = 0) -> str:
    """Read file contents. Supports offset/limit for large files.
    - offset: start reading from this line (1-indexed)
    - limit: max lines to return (0 = up to MAX_READ_SIZE chars)"""
    # Block path traversal
    if ".." in path.split("/") or ".." in path.split("\\"):
        return "⛔ Blocked: path traversal not allowed"
    # 敏感路径判定与插件/命令路径同源（审计 P1：此 fallback 此前连黑名单都没有）
    try:
        from cmd_safety import sensitive_read_block
        _blk = sensitive_read_block(path)
        if _blk:
            return _blk
    except Exception:
        logger.debug("敏感路径判定不可用（fallback read_file）", exc_info=True)
    try:
        with open(path, "r", encoding="utf-8") as f:
            if offset > 1:
                for _ in range(offset - 1):
                    if not f.readline():
                        return f"错误：偏移超出文件范围（第 {offset} 行不存在）"
            if limit > 0:
                lines = []
                for _ in range(limit):
                    line = f.readline()
                    if not line:
                        break
                    lines.append(line)
                content = "".join(lines)
                if len(lines) == limit and f.readline():
                    content += f"\n... (继续读取请使用 offset={offset + limit})"
                return content or "(空)"
            content = f.read(MAX_READ_SIZE + 1)
        if len(content) > MAX_READ_SIZE:
            est_lines = content.count("\n")
            return (
                content[:MAX_READ_SIZE]
                + f"\n\n... (文件过长，已截断。约 {est_lines}+ 行，仅显示前 {MAX_READ_SIZE} 字符。"
                + f"分段读取：read_file(path=\"{path}\", offset={est_lines + 1})"
            )
        return content
    except FileNotFoundError:
        return f"错误：文件不存在 - {path}"
    except Exception as e:
        return f"错误：{e}"


def write_file(path: str, content: str) -> str:
    # Block path traversal
    if ".." in path.split("/") or ".." in path.split("\\"):
        return "⛔ Blocked: path traversal not allowed"
    if len(content) > 10 * 1024 * 1024:  # 10 MB limit
        return f"⛔ File too large ({len(content)} bytes, max 10 MB)"
    # 与插件版 write_file.py 同级封印（P0）：此前 fallback 只查 `..`，
    # 插件目录被删/seed 前走本路径时可写 extensions/*/plugin.py → 下次启动
    # exec_module 即 RCE。系统目录/敏感目录/敏感文件名/自动加载目录全拒。
    try:
        _rp = os.path.realpath(os.path.expanduser(path))
    except Exception:
        return "⛔ Blocked: 路径无效"
    _blocked_dirs = ("/etc", "/System", "/usr", "/bin", "/sbin", "/var", "/private/etc")
    if any(_rp == d or _rp.startswith(d + os.sep) for d in _blocked_dirs):
        return f"⛔ Blocked: 不允许写入系统目录 - {_rp}"
    # 敏感目录/文件名清单统一走 cmd_safety 单点（此前这里是另一份内联拷贝，
    # 与插件侧的 5 项清单漂移 —— 2026-09-24 复查修复）
    from cmd_safety import (
        _BLOCKED_DIR_SUBSTRINGS, sensitive_write_block, sensitive_write_name_block,
    )
    if any(s in _rp for s in _BLOCKED_DIR_SUBSTRINGS):
        return f"⛔ Blocked: 不允许访问敏感目录 - {_rp}"
    _fn_block = sensitive_write_name_block(_rp)
    if _fn_block:
        return _fn_block
    try:
        _blk = sensitive_write_block(_rp)
        if _blk:
            return _blk
    except Exception:
        return "⛔ Blocked: 写入封印不可用，已拒绝"
    try:
        os.makedirs(os.path.dirname(_rp) or ".", exist_ok=True)
        with open(_rp, "w", encoding="utf-8") as f:
            f.write(content)
        return f"✅ 已写入：{_rp}（{len(content)} 字符）"
    except Exception as e:
        return f"错误：{e}"


def list_dir(path: str) -> str:
    # Block path traversal
    if ".." in path.split("/") or ".." in path.split("\\"):
        return "⛔ Blocked: path traversal not allowed"
    try:
        entries = os.listdir(path)
        lines = [f"  {'📁' if os.path.isdir(os.path.join(path, e)) else '📄'} {e}"
                 for e in sorted(entries)]
        return "目录内容:\n" + "\n".join(lines)
    except Exception as e:
        return f"错误：{e}"


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


def run_cmd(cmd: str) -> str:
    # Strip shell comment lines (models sometimes prepend "# comment\n")
    cmd = "\n".join(line for line in cmd.split("\n") if not line.strip().startswith("#")).strip()
    if not cmd:
        return "错误：命令为空（可能只包含注释行）"
    # 引号外的 shell 操作符才拦（引号内是脚本内容，shell=False 不会解释）
    from cmd_safety import (
        find_unsupported_shell_op, split_pipeline, split_stdout_redirect,
        unsupported_op_message,
    )
    _redir = split_stdout_redirect(cmd)
    if _redir:
        cmd, _target, _append = _redir
    _pipe = None if _redir else split_pipeline(cmd)
    if _pipe:
        op = find_unsupported_shell_op(cmd)  # 两侧已查
    else:
        op = find_unsupported_shell_op(cmd)
    if op:
        return unsupported_op_message(op)
    # 统一安全检查（cmd_safety 单点定义——插件/fallback/seed 共用，含脚本内容审查）
    from cmd_safety import check_cmd_with_script
    denied = check_cmd_with_script(cmd)
    if denied:
        return denied
    if len(cmd) > 1000:
        return f"⛔ Command too long ({len(cmd)} chars, max 1000)"
    # Redirect: if the model is trying to do web search via Python code, tell it to use the tool
    cmd_lower = cmd.lower().strip()
    if re.search(r'(tavily|requests\.|urllib|httpx|aiohttp)', cmd_lower) and re.search(r'(search|api|get|post)', cmd_lower):
        return (
            "⛔ 不要用 Python 代码做网络搜索或 API 请求！\n"
            "请使用 web_search 工具来做网络搜索，例如：\n"
            "  web_search({query: \"你的搜索词\"})\n"
            "对于文件操作，使用 read_file、list_dir、write_file 等工具。"
        )
    if _pipe:
        return _run_pipeline(_pipe[0], _pipe[1], 300)
    try:
        try:
            tokens = shlex.split(cmd)
        except ValueError as e:
            return f"命令格式错误: {e}"
        r = subprocess.run(tokens, shell=False, capture_output=True, text=True, timeout=300,
                           # ④ 兜底执行器同样过白名单：命令由模型给出，子进程不该拿到
                           # sidecar token 与云模型密钥（审查时靠 test_spawn_env_guard 抓到）
                           env=child_env())
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
        return f"超时: {cmd}"
    except Exception as e:
        return f"错误：{e}"


def open_folder(path: str) -> str:
    try:
        if platform.system() == "Darwin":
            subprocess.Popen(["open", path])
        elif platform.system() == "Windows":
            os.startfile(path)
        else:
            subprocess.Popen(["xdg-open", path])
        return f"✅ 已打开：{path}"
    except Exception:
        logger.debug("open_folder failed, falling back to list_dir", exc_info=True)
        return list_dir(path)


_APP_ALIASES = {
    "照片": "Photos", "相册": "Photos", "photo": "Photos",
    "音乐": "Music", "music": "Music",
    "浏览器": "Safari", "safari": "Safari",
    "邮件": "Mail", "mail": "Mail",
    "日历": "Calendar", "calendar": "Calendar",
    "备忘录": "Notes", "notes": "Notes",
    "提醒": "Reminders", "reminders": "Reminders",
    "计算器": "Calculator", "calculator": "Calculator",
    "终端": "Terminal", "terminal": "Terminal",
    "设置": "System Settings", "系统设置": "System Settings", "偏好设置": "System Settings",
    "App Store": "App Store", "app store": "App Store",
    "地图": "Maps", "maps": "Maps",
    "天气": "Weather", "weather": "Weather",
    "时钟": "Clock", "clock": "Clock",
    "查找": "Find My", "find my": "Find My",
}


def open_app(name: str) -> str:
    """Open a macOS application by name. Supports both English and Chinese names."""
    # Resolve Chinese aliases
    resolved = _APP_ALIASES.get(name, name)
    try:
        subprocess.Popen(["open", "-a", resolved])
        return f"✅ 已打开应用：{resolved}"
    except Exception as e:
        return f"无法打开应用 {resolved}: {e}"


# 有界扫描（09-19 事故）：原实现用 glob(recursive=True) 把匹配全量物化后再截断显示，
# 模型给 `**/tavily_*` + 家目录时递归扫过 100GB+ 模型文件 → 整轮卡死 21 分钟。
# 改为带剪枝的有界遍历：跳过已知大目录、限制深度与结果数，并在结果里说明已截断。
_SEARCH_EXCLUDE_DIRS = frozenset({
    ".lmstudio", "Library", "node_modules", ".git", ".cache", "__pycache__",
    "site-packages", ".venv", "venv", ".Trash", ".npm", ".cargo", ".rustup",
    "Applications", ".zcode", ".cursor", ".vscode", ".docker", ".gradle", ".m2",
    ".ollama", ".cache", "Photos Library.photoslibrary", "Movies", "Music",
})
_SEARCH_MAX_RESULTS = int(os.environ.get("LATIAO_SEARCH_MAX_RESULTS", "200") or 200)
_SEARCH_MAX_DEPTH = int(os.environ.get("LATIAO_SEARCH_MAX_DEPTH", "6") or 6)


def search_files(directory: str, pattern: str) -> str:
    """Search for files matching a glob pattern in a directory (bounded)."""
    if ".." in directory.split("/"):
        return "⛔ Blocked: path traversal not allowed"
    import fnmatch
    try:
        base = os.path.expanduser(directory)
        if not os.path.isdir(base):
            return f"No such directory: {directory}"
        pat = pattern.strip()
        if pat.startswith("**/"):
            pat = pat[3:]
        base_depth = base.rstrip("/").count("/")
        matches, skipped, truncated = [], [], False
        for root, dirs, files in os.walk(base, topdown=True):
            kept = []
            for d in dirs:
                if d in _SEARCH_EXCLUDE_DIRS or d.endswith(".photoslibrary"):
                    skipped.append(d)
                else:
                    kept.append(d)
            dirs[:] = kept
            if root.count("/") - base_depth >= _SEARCH_MAX_DEPTH:
                dirs[:] = []
            for name in list(files) + list(dirs):
                full = os.path.join(root, name)
                rel = os.path.relpath(full, base)
                if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(rel, pat):
                    matches.append(full)
                    if len(matches) >= _SEARCH_MAX_RESULTS:
                        truncated = True
                        break
            if truncated:
                break
        if not matches:
            note = (f"\n（已跳过 {len(set(skipped))} 类大目录，如 {', '.join(sorted(set(skipped))[:4])}）"
                    if skipped else "")
            return f"No files matching '{pattern}' found in {directory}{note}"
        lines = [f"  {'📁' if os.path.isdir(m) else '📄'} {m}" for m in sorted(matches)[:50]]
        result = f"Search results for '{pattern}' in {directory}:\n" + "\n".join(lines)
        if len(matches) > 50:
            result += f"\n  ... and {len(matches) - 50} more results"
        if truncated:
            result += (f"\n⚠️ 结果已达上限 {_SEARCH_MAX_RESULTS} 条并截断（或深度超过 {_SEARCH_MAX_DEPTH} 层），"
                       "请缩小目录或把模式写得更具体。")
        if skipped:
            result += f"\n（已跳过 {', '.join(sorted(set(skipped))[:5])} 等大目录）"
        return result
    except Exception as e:
        return f"Error searching files: {e}"


async def tavily_search(args: dict) -> str:
    """Search the web using Tavily API."""

    # 常量由 main.py 门面持有 → 函数内 lazy import 避免循环依赖
    from main import TAVILY_API_URL
    config_file = Path.home() / ".local-ai-os" / "config.json"
    # Priority: env var → macOS Keychain → config.json (legacy)
    api_key = os.environ.get("TAVILY_API_KEY")

    if not api_key:
        # Try macOS Keychain via security CLI
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-s", "com.latiao.desktop", "-a", "tavily_api_key", "-w"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                api_key = result.stdout.strip()
        except Exception:
            logger.debug("Tavily keychain read failed", exc_info=True)

    if not api_key:
        try:
            if config_file.exists():
                cfg = json.loads(config_file.read_text(encoding="utf-8"))
                api_key = cfg.get("tavily_api_key")
        except Exception:
            logger.warning("Failed to read Tavily key from config.json", exc_info=True)

    if not api_key:
        return (
            "⚠️ Tavily API Key 未配置。\n"
            "请在应用的「技能」界面中找到 Web Search (Tavily)，填写 API Key。\n"
            "免费注册：https://tavily.com"
        )

    query = args["query"]
    search_depth = args.get("search_depth", "basic")
    # 模型常给 "high"/"deep" 等非法值 → 映射为 advanced，避免 HTTP 400 整轮失败
    if search_depth not in ("basic", "advanced"):
        search_depth = "advanced"
    max_results = min(args.get("max_results", 5), 10)

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30)) as client:
            resp = await client.post(
                TAVILY_API_URL,
                json={
                    "api_key": api_key,
                    "query": query,
                    "search_depth": search_depth,
                    "max_results": max_results,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        results = data.get("results", [])
        answer = data.get("answer", "")

        if not results and not answer:
            return f"🔍 Tavily 搜索: {query}\n\n未找到相关结果。"

        lines = [f"🔍 Tavily 搜索: {query}\n"]

        if answer:
            lines.append(f"📝 {answer}\n")

        if results:
            lines.append(f"📎 共 {len(results)} 条结果:\n")
            for i, r in enumerate(results, 1):
                title = r.get("title", "No title")
                url = r.get("url", "")
                content = r.get("content", "")
                if len(content) > 300:
                    content = content[:300] + "..."
                lines.append(f"{i}. **{title}**")
                lines.append(f"   {url}")
                lines.append(f"   {content}\n")

        return "\n".join(lines)

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            return "⚠️ Tavily API Key 无效或已过期。请在技能设置中更新 API Key。"
        return f"⚠️ Tavily 搜索失败: HTTP {e.response.status_code}"
    except httpx.ConnectError:
        return "⚠️ 无法连接 Tavily API (api.tavily.com)。请检查网络连接。"
    except Exception as e:
        return f"⚠️ Tavily 搜索异常: {e}"


# ═══════════════════════════════════════════════════════
#  Sub-Agent System: delegate_task spawns specialist sub-agents
# ═══════════════════════════════════════════════════════

# 子智能体可免确认执行的只读命令白名单（ZCode 式 Explore）。
# 审计 P0：此前只校验首 token 且含 env——"env curl ..." 可执行任意命令、
# "cat ~/.ssh/id_rsa" 可读私钥。现在校验完整 token 序列：命令名必须在
# 白名单内、参数只能是简单选项/路径形态、敏感路径拒绝。
_READONLY_CMD_WORDS = {
    "ls", "cat", "head", "tail", "find", "grep", "rg", "wc", "file",
    "stat", "du", "df", "ps", "which", "whoami", "uname", "pwd",
    "echo", "date",
}

_READONLY_ARG_RE = re.compile(r"-{1,2}[A-Za-z0-9][A-Za-z0-9_-]*|[\w./@+~:-]+")


def _is_readonly_cmd(cmd: str) -> bool:
    """判断子智能体请求的命令是否命中只读白名单（仅限单条简单命令）。

    阶段 3 优先走语义层（safety.rules.readonly_safe）：AST 展开嵌套/前缀，
    `env cat`、`bash -c 'cat x'`、$() 全部现形；语义层不可用时回退本函数
    原有的 token 形态校验（升级不改变旧环境行为）。
    """
    cmd = (cmd or "").strip()
    if not cmd:
        return False
    try:
        from safety.rules import analyze, readonly_safe
        if analyze(cmd).ts_available:
            return readonly_safe(cmd)
    except Exception:
        pass  # 语义层异常 → 回退旧路径
    if re.search(r"[;&|><`$]", cmd):
        return False
    if re.match(r"git\s+(log|status|diff|show|branch|remote|tag|blame)\b", cmd):
        return True
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return False
    if not tokens:
        return False
    first = tokens[0].rsplit("/", 1)[-1]
    if first not in _READONLY_CMD_WORDS:
        return False
    # 参数必须是简单选项/路径形态——拦截一切嵌套命令与引用花活
    if not all(_READONLY_ARG_RE.fullmatch(t) for t in tokens[1:]):
        return False
    # 读取类命令的敏感路径拒绝（与 read_file 插件黑名单对齐）
    return reject_sensitive_read(" ".join(tokens)) is None


# ── 后台子任务注册表（ZCode 式：fire-and-forget + 进度事件 + 结果查询） ──

# delegate 机制已迁至 agent/subagent.py（Stage 5 子代理一等公民）；
# 这里保留兼容导入（api_routes/tests 的既有引用不变）。
from agent.subagent import (  # noqa: F401
    _SUBTASKS, _SUBTASK_EVENTS, _SUBTASK_SEQ, _SUBTASK_TASKS,
    _SUBAGENT_TOOLS, _prune_subtasks, _delegate_task,
    _delegate_task_bg, _delegate_task_fg, _run_subtask_bg,
    _subtask_snapshot,)

# ── Fallback OpenAI Function Calling tool definitions ──

_FALLBACK_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file at the given path. Supports offset and limit for large files. File truncated at 50000 chars — use offset to continue reading.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path to the file."},
                    "offset": {"type": "integer", "description": "Line number to start reading from (1-indexed). Use this with limit to read large files in chunks."},
                    "limit": {"type": "integer", "description": "Maximum number of lines to return. Use with offset for chunked reading."}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write text content to a file. Creates parent directories if needed. ⚠️ Requires user confirmation before executing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path where the file should be written."},
                    "content": {"type": "string", "description": "The text content to write to the file."}
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List the contents of a directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path to the directory to list."}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_cmd",
            "description": "Run ONE single command and return its output (no shell). ⚠️ Requires user confirmation. Dangerous commands (rm -rf, sudo, etc.) are always blocked. Do NOT use shell operators (&& | ; > <) — call once per command instead.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cmd": {"type": "string", "description": "A single command with arguments (no shell operators like && | ; > <)."}
                },
                "required": ["cmd"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "open_folder",
            "description": "Open a folder in Finder (macOS only).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path to the folder to open."}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "open_app",
            "description": "Open a macOS application by name. Use this when the user asks to open an app. Supports both English names (Photos, Safari, Mail) and Chinese names (照片/相册, 浏览器, 邮件).",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "App name in English or Chinese (e.g., 'Photos', 'Safari', '照片', '浏览器')."}
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search for files matching a glob pattern in a directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {"type": "string", "description": "Absolute path to the directory to search in."},
                    "pattern": {"type": "string", "description": "Glob pattern to match (e.g., '*.py', '**/*.md')."}
                },
                "required": ["directory", "pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "搜索互联网获取实时信息（tavily_search 的旧名别名，二者等价，指向同一个 Tavily 搜索）。优先使用 tavily_search；仅当工具列表中没有 tavily_search 时才用本工具，不要同时调用两者。返回标题、URL和内容摘要。不要用 run_cmd 或手写代码来做网络搜索。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query. Be specific and use keywords."},
                    "search_depth": {"type": "string", "enum": ["basic", "advanced"], "description": "Search depth: 'basic' (faster) or 'advanced' (thorough). Default: basic."},
                    "max_results": {"type": "integer", "description": "Max results to return (1-10). Default: 5."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delegate_task",
            "description": "Delegate a sub-task to a specialist sub-agent. Sub-agents run independently with limited tools and return results. Use this to parallelize work — spawn multiple sub-agents for independent sub-tasks. Available agents: explore (read-only deep exploration: search codebase, run read-only commands like ls/grep, web research), code-reviewer (read-only code review), doc-generator (documentation), debugger (bug analysis), translator (translation).",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent": {
                        "type": "string",
                        "enum": ["explore", "code-reviewer", "doc-generator", "debugger", "translator"],
                        "description": "The specialist agent type to delegate to."
                    },
                    "task": {
                        "type": "string",
                        "description": "The specific task for the sub-agent. Be clear and concise — the sub-agent only sees this task description."
                    }
                },
                "required": ["agent", "task"]
            }
        }
    },
]

_FALLBACK_DISPATCH = {
    "read_file": lambda args: read_file(args["path"]),
    "write_file": lambda args: write_file(args["path"], args["content"]),
    "list_dir": lambda args: list_dir(args["path"]),
    "run_cmd": lambda args: run_cmd(args["cmd"]),
    "open_folder": lambda args: open_folder(args["path"]),
    "open_app": lambda args: open_app(args["name"]),
    "search_files": lambda args: search_files(args["directory"], args["pattern"]),
    "tavily_search": lambda args: tavily_search(args),
    "web_search": lambda args: tavily_search(args),
    "delegate_task": lambda args: _delegate_task(args.get("agent", "code-reviewer"), args.get("task", "")),
}
