"""run_cmd 操作符判定：fd 重定向不许被静默错解析 + 被拒/超时不许贴成功（2026-09-24）。

背景（用户实测）：
- `grep … 2>/dev/null` 曾被"放行"并解析成 `grep … 2` + 输出丢进 /dev/null，工具还
  回"已写入"——把 `>` 剥掉却把前面的数字留在命令里 = 静默错执行；
- 被拒（⛔ 不支持 shell 操作符…）与超时（超时: …）的结果被后处理贴上
  `✅ Exit code: 0 (success)`，模型把"被拒绝"读成"跑过了、只是没输出"。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LATIAO_AUTH_TOKEN", "runop-token")

# fd 前缀重定向：没有等价实现，必须整条拒绝（不能只剥 `>`）
FD_FORMS = [
    "grep -rl x /tmp 2>/dev/null",
    "grep -rl x /tmp 1>/tmp/o",
    "grep -rl x /tmp 2>>/tmp/e",
    "grep -rl x /tmp &>/tmp/o",
]


def test_fd_redirect_is_not_silently_reparsed():
    from cmd_safety import find_unsupported_shell_op, split_stdout_redirect
    for cmd in FD_FORMS:
        assert split_stdout_redirect(cmd) is None, f"fd 重定向不该被解析: {cmd}"
        assert find_unsupported_shell_op(cmd) == ">", f"应当按 '>' 拒绝: {cmd}"


def test_fd_redirect_rejected_by_plugin_not_misexecuted(tmp_path):
    import plugins.run_cmd as rc
    out = rc.execute({"cmd": f"grep -rl x {tmp_path} 2>/dev/null"})
    assert "不支持 shell 操作符" in out
    # 关键：不能出现"已写入 …（N 字符）"这种静默错执行的痕迹
    assert "已写入" not in out


def test_supported_forms_still_pass():
    from cmd_safety import find_unsupported_shell_op
    assert find_unsupported_shell_op("echo hi > /tmp/x") is None
    assert find_unsupported_shell_op("echo hi >> /tmp/x") is None
    assert find_unsupported_shell_op("echo hi | tr a-z A-Z") is None
    # 引号内的 > 不受限
    assert find_unsupported_shell_op('echo "a > b"') is None


def test_plugin_and_fallback_reach_same_verdict(tmp_path):
    """两条命令路径同一输入必须同一结论（这个项目栽过"一份改了另一份没改"）。"""
    import plugins.run_cmd as rc
    import tool_executor as te
    for cmd in FD_FORMS + ["cd /tmp && ls", "echo hi; rm -rf /", "echo a > b > c"]:
        plugin_out = rc.execute({"cmd": cmd.replace("/tmp", str(tmp_path))})
        fallback_out = te.run_cmd(cmd.replace("/tmp", str(tmp_path)))
        assert ("不支持 shell 操作符" in plugin_out) == (
            "不支持" in fallback_out
        ), f"两条路径结论不一致: {cmd}"


def test_blocked_and_timeout_results_are_not_stamped_success():
    from agent.tool_exec import _append_run_outcome

    refused = "⛔ 不支持 shell 操作符 '>'：本工具以 shell=False 直接执行，无法解释 > 。"
    timed_out = "超时: 命令已运行 5 分钟被截断。长任务请拆分为多步执行。"
    for result in (refused, timed_out):
        out = _append_run_outcome("run_cmd", result, {})
        assert "Exit code: 0 (success)" not in out, f"被拒/超时不该贴成功: {result[:20]}"


def test_real_run_results_keep_the_success_stamp():
    from agent.tool_exec import _append_run_outcome

    ok = _append_run_outcome("run_cmd", "hello-latiao", {})
    assert "Exit code: 0 (success)" in ok
    # 非零退出码：工具已自带 (退出码: N)，不贴成功
    failed = _append_run_outcome("run_cmd", "boom\n(退出码: 1)", {})
    assert "Exit code: 0 (success)" not in failed
