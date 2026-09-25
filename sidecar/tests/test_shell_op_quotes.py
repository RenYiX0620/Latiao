"""引号内 ; | > 不误杀 + python -c 内联审查（2026-09-24）。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LATIAO_AUTH_TOKEN", "shellop-token")


def test_quoted_semicolon_not_shell_op():
    from cmd_safety import find_unsupported_shell_op
    assert find_unsupported_shell_op('python3 -c "from PIL import Image; print(1)"') is None
    assert find_unsupported_shell_op("python3 -c 'a=1; b=2'") is None
    assert find_unsupported_shell_op('python3 -c "x|y"') is None
    assert find_unsupported_shell_op('echo "a > b"') is None


def test_unquoted_ops_still_blocked():
    from cmd_safety import find_unsupported_shell_op
    assert find_unsupported_shell_op("cd /tmp && ls") == "&&"
    # 单管道已支持（等价 Popen A|B），多管道仍拦
    assert find_unsupported_shell_op("pip3 list | grep docx") is None
    assert find_unsupported_shell_op("a | b | c") is not None
    assert find_unsupported_shell_op("echo hi; rm -rf /") == ";"
    # 尾部 `> file` 已支持（等价重定向），不再算 shell 操作符
    assert find_unsupported_shell_op("echo hi > /tmp/x") is None


def test_python_c_inline_clean_passes():
    from cmd_safety import check_cmd_with_script
    assert check_cmd_with_script('python3 -c "import docx"') is None
    assert check_cmd_with_script('python3 -c "from PIL import Image; print(\'PIL OK\')"') is None


def test_python_c_inline_destructive_still_blocked():
    from cmd_safety import check_cmd_with_script
    denied = check_cmd_with_script('python3 -c "import os; os.system(\'rm -rf /\')"')
    assert denied and "⛔" in denied


def test_run_cmd_plugin_quoted_semicolon():
    import plugins.run_cmd as rc
    out = rc.execute({"cmd": 'python3 -c "from PIL import Image; print(1)"'})
    # 不应再出现「不支持 shell 操作符」——可能因环境缺 PIL 报别的错，都算过了拦截层
    assert "不支持 shell 操作符" not in out


def test_run_cmd_plugin_safe_pipe_works():
    import plugins.run_cmd as rc
    out = rc.execute({"cmd": "echo abc | tr a-z A-Z"})
    assert "不支持 shell 操作符" not in out
    assert "ABC" in out


def test_trailing_stdout_redirect_supported(tmp_path):
    import plugins.run_cmd as rc
    out_file = tmp_path / "hello.txt"
    out = rc.execute({"cmd": f"echo hello-latiao > {out_file}"})
    assert "不支持 shell 操作符" not in out
    assert out_file.read_text(encoding="utf-8").strip() == "hello-latiao"
    assert "已写入" in out


def test_append_redirect(tmp_path):
    import plugins.run_cmd as rc
    f = tmp_path / "a.txt"
    rc.execute({"cmd": f"echo line1 > {f}"})
    rc.execute({"cmd": f"echo line2 >> {f}"})
    lines = f.read_text(encoding="utf-8").strip().splitlines()
    assert lines == ["line1", "line2"]


def test_pipe_still_blocked_and_multi_redirect_blocked():
    from cmd_safety import find_unsupported_shell_op, split_stdout_redirect
    assert find_unsupported_shell_op("a | b") is None  # 单管道已支持
    assert split_stdout_redirect("a > b > c") is None
    assert find_unsupported_shell_op("a > b > c") is not None


def test_safe_pipeline_supported():
    import plugins.run_cmd as rc
    out = rc.execute({"cmd": "echo hello-latiao | tr a-z A-Z"})
    assert "不支持 shell 操作符" not in out
    assert "HELLO-LATIAO" in out


def test_pipeline_split_helper():
    from cmd_safety import find_unsupported_shell_op, split_pipeline
    assert split_pipeline("head -n 10 f.txt | grep x") == ("head -n 10 f.txt", "grep x")
    assert find_unsupported_shell_op("head -n 10 f.txt | grep x") is None
    # 多管道 / && 仍拦
    assert find_unsupported_shell_op("a | b | c") is not None
    assert find_unsupported_shell_op("a && b") == "&&"


def test_head_file_alone_allowed():
    import plugins.run_cmd as rc
    import tempfile, os
    fd, path = tempfile.mkstemp(suffix=".txt")
    os.write(fd, b"line1\nline2\nline3\n")
    os.close(fd)
    try:
        out = rc.execute({"cmd": f"head -n 2 {path}"})
        assert "不支持" not in out
        assert "line1" in out
    finally:
        os.unlink(path)
