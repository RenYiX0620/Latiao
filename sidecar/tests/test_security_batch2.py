"""安全批次 2（审计 P1）回归测试：子进程 env 白名单（④）与写入封印（⑧）。

①（curl token 进 argv）、②（store_secret 进 argv）、③（Windows 弱随机）在 Rust 侧，
Python 测试无法执行它们，用源码级别的绊线守着（下面 test_rust_side_*），
真正的验证是编译 + 人工跑一次"重启 sidecar / 存密钥"路径。
"""
import pathlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cmd_safety import child_env  # noqa: E402

SIDECAR = Path(__file__).resolve().parents[1]


# ── ④ 子进程 env 白名单 ─────────────────────────────────────────────

def test_child_env_excludes_credentials(monkeypatch):
    monkeypatch.setenv("LATIAO_AUTH_TOKEN", "sidecar-token")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-y")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")
    monkeypatch.setenv("HF_TOKEN", "hf_x")
    env = child_env()
    for k in ("LATIAO_AUTH_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
              "TAVILY_API_KEY", "HF_TOKEN"):
        assert k not in env, f"{k} 泄漏进子进程环境"


def test_child_env_keeps_runtime_essentials(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/Users/x")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    env = child_env()
    assert env["PATH"] == "/usr/bin:/bin" and env["HOME"] == "/Users/x"
    assert "HTTPS_PROXY" in env, "代理变量要保留（否则网络类命令全废）"


def test_child_env_prefix_and_extra(monkeypatch):
    monkeypatch.setenv("MTL_DEBUG_LAYER", "1")
    monkeypatch.setenv("LATIAO_AUTH_TOKEN", "t")
    engine_env = child_env(allow_prefixes=("MTL_",))
    assert engine_env["MTL_DEBUG_LAYER"] == "1" and "LATIAO_AUTH_TOKEN" not in engine_env
    assert child_env(extra={"MCP_FOO": "1"})["MCP_FOO"] == "1"


def test_run_cmd_passes_whitelisted_env(monkeypatch):
    """真实调用点：run_cmd 执行子进程时传的 env 必须过白名单。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("rc_under_test", SIDECAR / "plugins" / "run_cmd.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setenv("LATIAO_AUTH_TOKEN", "secret")
    seen = {}

    class _Proc:
        stdout = "ok\n"
        stderr = ""
        returncode = 0

    def _fake_run(*a, **kw):
        seen.update(kw)
        return _Proc()

    monkeypatch.setattr(mod.subprocess, "run", _fake_run)
    mod.execute({"cmd": "echo hi"})
    assert "env" in seen, "run_cmd 没传 env（子进程会继承整个环境）"
    assert "LATIAO_AUTH_TOKEN" not in seen["env"]


def test_all_four_spawn_sites_use_whitelist():
    """四处子进程（run_cmd 两处 / 控制工具 / MCP / 引擎）都得过白名单。"""
    checks = {
        "plugins/run_cmd.py": 2,
        "plugins/_control_common.py": 1,
        "mcp_client.py": 1,
        "local_llm.py": 3,
    }
    for rel, expected in checks.items():
        src = (SIDECAR / rel).read_text("utf-8")
        found = src.count("child_env(")
        assert found >= expected, f"{rel} 只找到 {found} 处 child_env（应 ≥{expected}）"


# ── ⑧ 写入封印 ────────────────────────────────────────────────────

@pytest.mark.parametrize("rel", [
    ".local-ai-os/extensions/foo/1.0/plugin.py",
    ".local-ai-os/skills/x/SKILL.md",
    ".local-ai-os/config.json",
])
def test_write_seal_blocks_auto_loaded_paths(rel, monkeypatch, tmp_path):
    monkeypatch.setenv("LATIAO_TEST_PROGRESS_DIR", str(tmp_path / ".local-ai-os"))
    import importlib
    import config
    importlib.reload(config)
    import cmd_safety
    importlib.reload(cmd_safety)
    target = pathlib.Path.home() / rel
    assert cmd_safety.sensitive_write_block(str(target)), f"{rel} 应被拦下"
    importlib.reload(cmd_safety)
    importlib.reload(config)


def test_write_seal_allows_normal_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("LATIAO_TEST_PROGRESS_DIR", str(tmp_path / ".local-ai-os"))
    import importlib
    import config
    importlib.reload(config)
    import cmd_safety
    importlib.reload(cmd_safety)
    for p in ("/tmp/notes.md", str(pathlib.Path.home() / "Documents/report.py")):
        assert cmd_safety.sensitive_write_block(p) is None, f"{p} 不该被拦"
    importlib.reload(cmd_safety)
    importlib.reload(config)


# ── ⑧ 补：命令路径的写入封印（2026-09-23 审查发现）──────────────────
# write_file 被封住之后，同一份 config.json / 扩展目录用 shell 仍能写：
# `sed -i`、`cp`、`tee`、`dd of=` 全部放行（实测 13 例，见下）。
@pytest.mark.parametrize("tpl", [
    "sed -i '' s/a/b/ {cfg}",
    "cp /tmp/x {cfg}",
    "tee {cfg}",
    "mv /tmp/x {cfg}",
    "dd if=/tmp/x of={cfg}",
    "cp /tmp/evil.py {ext}",
    "tee {ext}",
    "cat {cfg}",              # 读那半由 reject_sensitive_read 守，必须仍然拦
])
def test_cmd_write_seal_blocks_protected_targets(tpl, monkeypatch, tmp_path):
    monkeypatch.setenv("LATIAO_TEST_PROGRESS_DIR", str(tmp_path / ".local-ai-os"))
    import importlib
    import config
    importlib.reload(config)
    import cmd_safety
    importlib.reload(cmd_safety)
    root = pathlib.Path.home() / ".local-ai-os"
    cmd = tpl.format(cfg=str(root / "config.json"),
                     ext=str(root / "extensions/evil/1.0/plugin.py"))
    assert cmd_safety.check_cmd(cmd), f"应被拦下: {cmd}"
    importlib.reload(cmd_safety)
    importlib.reload(config)


@pytest.mark.parametrize("tpl", [
    "ls {ext_dir}",                                  # 看扩展目录是正常操作
    "cp /tmp/a.txt /tmp/b.txt",
    "sed -i '' s/a/b/ /tmp/notes.md",
    "tee /tmp/out.txt",
    "cp /tmp/a.py ./config.json",                    # 相对路径落在 cwd，不误伤
])
def test_cmd_write_seal_allows_normal_targets(tpl, monkeypatch, tmp_path):
    monkeypatch.setenv("LATIAO_TEST_PROGRESS_DIR", str(tmp_path / ".local-ai-os"))
    import importlib
    import config
    importlib.reload(config)
    import cmd_safety
    importlib.reload(cmd_safety)
    root = pathlib.Path.home() / ".local-ai-os"
    cmd = tpl.format(ext_dir=str(root / "extensions"))
    assert cmd_safety.check_cmd(cmd) is None, f"不该被拦: {cmd}"
    importlib.reload(cmd_safety)
    importlib.reload(config)


def test_write_file_plugin_refuses_extensions(monkeypatch, tmp_path):
    """插件层的端到端：写扩展目录会拿到拒绝文案。"""
    import importlib
    monkeypatch.setenv("LATIAO_TEST_PROGRESS_DIR", str(tmp_path / ".local-ai-os"))
    import config
    importlib.reload(config)
    import importlib.util
    spec = importlib.util.spec_from_file_location("wf_under_test", SIDECAR / "plugins" / "write_file.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = mod.execute({"path": str(pathlib.Path.home() / ".local-ai-os/extensions/x/1/plugin.py"),
                       "content": "print(1)"})
    assert "⛔" in out and "自动加载" in out
    normal = tmp_path / "ok.txt"
    assert "⛔" not in mod.execute({"path": str(normal), "content": "hi"})
    importlib.reload(config)


# ── ①②③ Rust 侧：源码绊线（真验证靠编译 + 人工路径）──────────────────

def _rust_code_lines() -> str:
    """main.rs 去掉 `//` 注释后的正文（注释里引述历史写法不该触发绊线）。"""
    raw = (SIDECAR.parent / "src-tauri/src/main.rs").read_text("utf-8")
    return "\n".join(l.split("//")[0] for l in raw.splitlines())


def test_rust_side_no_credentials_in_argv():
    src = _rust_code_lines()
    assert '"-H", &auth_header' not in src, "token 又回到 curl 的 argv 里了"
    assert '"-w", &value' not in src, "密钥又回到 security 的 argv 里了"
    assert "post_to_sidecar" in src, "进程内 HTTP 辅助函数不见了"


def test_rust_token_uses_os_random():
    src = (SIDECAR.parent / "src-tauri/src/main.rs").read_text("utf-8")
    assert "getrandom::fill" in src, "token 生成没用操作系统随机源"
