"""敏感路径读取拦截 + 日志/落盘脱敏（09-23，审计 P1）。

真机复现的漏洞：同一份 `~/.local-ai-os/config.json`，`cat` 走 run_cmd 的
SENSITIVE_READ_RE 被拦，而 `read_file`（safe 档、免确认）一路读通 → 明文
Tavily key 进模型上下文、进日志（0644 的 sidecar.log）、进 838 字的 cron
报告、进 cron_state.json 与 localStorage。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cmd_safety import (  # noqa: E402
    redact_secrets,
    sensitive_read_block,
    tool_log_preview,
)


def _blk(p: str) -> bool:
    return bool(sensitive_read_block(os.path.expanduser(p)))


# ── 必须拦（密钥/凭据/辣条自身配置）──────────────────────────────────

@pytest.mark.parametrize("path", [
    "~/.local-ai-os/config.json",                # 云端 key + Tavily key 明文
    "~/.local-ai-os/config.json.bak-0922-1934",  # 备份里同样是明文 key
    "~/.netrc",
    "~/.env.local",
    "~/.env.production",
    "~/.env",
    "~/.ssh/id_rsa",
    "~/.ssh/config",
    "~/.aws/credentials",
    "~/.git-credentials",
])
def test_blocks_sensitive_paths(path):
    assert _blk(path), f"该拦下：{path}"


# ── 必须放行（别把正常开发拦死）───────────────────────────────────

@pytest.mark.parametrize("path", [
    "/Users/x/proj/tsconfig.json",       # 子串含 config.json，但属于用户项目
    "/Users/x/proj/config.json",
    "/Users/x/proj/.env.example",        # 模板无密钥
    "/Users/x/proj/.env.sample",
    "/tmp/report.txt",
    "~/.local-ai-os/SOUL.md",            # 身份文件模型要能读
    "~/.local-ai-os/PROGRESS.md",
])
def test_allows_normal_paths(path):
    assert not _blk(path), f"不该拦：{path}"


def test_read_file_plugin_uses_shared_check():
    """插件路径与命令路径同源：插件里不再有自己一套不一致的黑名单。"""
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "rf_under_test", Path(__file__).resolve().parent.parent / "plugins" / "read_file.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = mod.execute({"path": os.path.expanduser("~/.local-ai-os/config.json")})
    assert "⛔" in out and "config" in out.lower()
    # 正常文件仍可读
    tmp = Path(__file__).resolve().parent / "_tmp_readable.txt"
    tmp.write_text("hello", encoding="utf-8")
    try:
        assert mod.execute({"path": str(tmp)}) == "hello"
    finally:
        tmp.unlink()


def test_tool_executor_fallback_also_blocks():
    """tool_executor 里的 fallback read_file 此前连黑名单都没有，现在同源。"""
    import tool_executor
    out = tool_executor.read_file(os.path.expanduser("~/.local-ai-os/config.json"))
    assert "⛔" in out


# ── 脱敏 ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    '{"tavily_api_key": "tvly-dev-3xjAA9-DOgHqGx855ZJNZMaLo5PINhwdukUK"}',
    "Authorization: Bearer sk-proj-abcdefghijklmnopqrstuvwxyz123456",
    "token=ghp_abcdefghijklmnopqrstuvwxyz0123",
    "xai-abcdefghijklmnopqrstuvwx",
])
def test_redacts_secret_shapes(text):
    assert "***" in redact_secrets(text)


def test_redaction_keeps_normal_text():
    """正常内容不该被动（否则日志/报告会失真）。"""
    for t in ["配置包含四项：cloud_models、custom_engine、tts、tavily_api_key。",
              "板块资金：半导体 +1.2%，主力净额 3.4 亿",
              "sk 是 stock 的缩写，风险提示：不构成投资建议"]:
        assert redact_secrets(t) == t, f"误伤：{t}"


def test_raw_content_tools_log_only_length():
    """read_file 这类整份内容：日志只记长度（内容才是泄露载体）。"""
    out = tool_log_preview("read_file", '{"tavily_api_key": "tvly-dev-abcdefghijkl"}')
    assert "tvly" not in out and "字内容已省略" in out
    # 检索/行情类保留脱敏预览（09-01 靠这几行排查工具参数问题）
    keep = tool_log_preview("mx_query", "板块资金：半导体 +1.2%")
    assert "半导体" in keep


def test_report_and_progress_writes_are_redacted():
    """接线点检查：进度文件与记忆库写入前都过脱敏。"""
    import inspect
    import agent_loop
    import agent.tool_exec
    # 工具执行簇 2026-09-23 拆到 agent/tool_exec.py：日志/进度/DB 脱敏点在那边
    src = inspect.getsource(agent_loop) + inspect.getsource(agent.tool_exec)
    assert "redact_secrets(json.dumps(args" in src, "进度/参数写入未脱敏"
    assert "_record_tool_call_db(session_id, tool_name, args, redact_secrets(result))" in src, \
        "记忆库写入未脱敏"
    assert "tool_log_preview(tool_name, result)" in src, "工具结果日志未脱敏"
    import cron
    cron_src = inspect.getsource(cron)
    assert "tool_log_preview(tool_name, result)" in cron_src, "cron 工具结果日志未脱敏"
    assert "redact_secrets(str(tool_args))" in cron_src, "cron 工具参数日志未脱敏"
