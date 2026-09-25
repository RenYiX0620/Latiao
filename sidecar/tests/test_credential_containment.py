"""审计后补的凭据保护（2026-09-23）：token 走 stdin、妙想 key 内存持有、
进程环境读取拦截、敏感路径出现在命令里即拒。

为什么这批重要：macOS 的 `ps eww` / `ps -E` 读的是进程 **exec 时**的环境快照——
实测运行时 `os.environ.pop()` 之后本进程 os.environ 已无该键、ps 仍显示它（命中
1 次）。所以"把凭据放进环境、运行时删掉"是自欺；要么一开始就不进环境（token 走
stdin），要么放 0600 配置里读进内存、按需显式注入（妙想 key）。而模型自己就能跑
命令，这几条是它拿到凭据的三条路：ps 读环境、grep/cp 读拷贝敏感文件、子进程继承。
"""
import io
import json
import os
import pathlib
import sys

import pytest

import cmd_safety
import runtime_secrets


# ── 进程环境读取拦截 ─────────────────────────────────────────────────

@pytest.mark.parametrize("cmd", [
    "ps eww -p 1234", "ps -E -p 1234", "ps eww", "ps auxeww", "ps -ef",
    "cat /proc/1234/environ",
])
def test_process_env_readers_blocked(cmd):
    assert cmd_safety.check_cmd(cmd), f"应被拦下: {cmd}"


@pytest.mark.parametrize("cmd", [
    "ps aux", "ps -p 1234", "ps -o pid,command", "pgrep -fl llama-server",
    "lsof -nP -iTCP:1235 -sTCP:LISTEN",
])
def test_normal_process_inspection_allowed(cmd):
    assert cmd_safety.check_cmd(cmd) is None, f"不该被拦: {cmd}"


# ── 敏感路径出现在命令里 ─────────────────────────────────────────────

@pytest.mark.parametrize("tpl", [
    "grep tavily {cfg}",
    "grep -o 'tvly[^\"]*' {cfg}",
    "awk /tavily/ {cfg}",
    "cp {cfg} /tmp/c.json",
    "cp {ssh} /tmp/k.txt",
    "mv {netrc} /tmp/n",
    "sed -n 1,5p {aws}",
])
def test_sensitive_path_in_command_blocked(tpl):
    home = pathlib.Path.home()
    cmd = tpl.format(cfg=home / ".local-ai-os/config.json",
                     ssh=home / ".ssh/id_rsa",
                     netrc=home / ".netrc",
                     aws=home / ".aws/credentials")
    assert cmd_safety.check_cmd(cmd), f"应被拦下: {cmd}"


@pytest.mark.parametrize("cmd", [
    "ls ~/.ssh",
    "stat ~/.ssh/id_rsa",
    "grep TODO /tmp/notes.md",
    "grep -r TODO ./src",
    "cp /tmp/a.txt /tmp/b.txt",
    # 项目自己的 config.json（不是辣条数据目录里的）不受影响——误伤过一次就难查
    "grep name ./config.json",
    "sed -i '' s/a/b/ ./tsconfig.json",
])
def test_metadata_only_and_normal_paths_allowed(cmd):
    assert cmd_safety.check_cmd(cmd) is None, f"不该被拦: {cmd}"


@pytest.mark.parametrize("cmd", [
    "sed -i '' s/A/B/ ./.env",
    "cp ./.env /tmp/e",
    "awk 1 $HOME/.local-ai-os/.env",
])
def test_env_family_blocked_on_purpose(cmd):
    """`.env` 家族在任何位置都被拦（含项目里的）——这是**有意的**过度拦截：
    密钥/凭据文件不该经模型的手（读也一样，read_file 早就拦了）。若哪天确实
    需要让模型改某个项目的 .env，应改这条规则而不是绕过它。"""
    assert cmd_safety.check_cmd(cmd), f"应被拦下: {cmd}"


# ── 妙想 key：内存持有 + 显式注入 ─────────────────────────────────────

def test_adopt_moves_secret_out_of_env(monkeypatch):
    runtime_secrets.reset_for_tests()
    monkeypatch.setenv("MX_APIKEY", "mx-abc")
    assert runtime_secrets.adopt("MX_APIKEY") is True
    assert runtime_secrets.get("MX_APIKEY") == "mx-abc"
    assert "MX_APIKEY" not in os.environ, "收进内存后必须从环境移除（否则 ps 可见）"
    runtime_secrets.reset_for_tests()


def test_load_from_config_file(monkeypatch, tmp_path):
    runtime_secrets.reset_for_tests()
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"mx_api_key": "mx-from-cfg", "tavily_api_key": "t"}), "utf-8")
    assert runtime_secrets.load_from_config_file(cfg) == ["MX_APIKEY"]
    assert runtime_secrets.get("MX_APIKEY") == "mx-from-cfg"
    assert runtime_secrets.load_from_config_file(tmp_path / "missing.json") == []
    runtime_secrets.reset_for_tests()


def test_mx_query_child_env_injects_key_without_token(monkeypatch):
    """mx_query 给子进程的 env：带妙想 key、不带 sidecar token。"""
    import importlib.util
    monkeypatch.setenv("LATIAO_AUTH_TOKEN", "sidecar-secret-token")
    spec = importlib.util.spec_from_file_location(
        "mxq_under_test", pathlib.Path(cmd_safety.__file__).parent / "plugins" / "mx_query.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    runtime_secrets.reset_for_tests()
    runtime_secrets.put("MX_APIKEY", "mx-mem-key")
    env = mod._mx_child_env()
    assert env.get("MX_APIKEY") == "mx-mem-key"
    assert "LATIAO_AUTH_TOKEN" not in env
    assert "PATH" in env
    runtime_secrets.reset_for_tests()


def test_mx_data_prefers_memory_over_env(monkeypatch):
    """进程内的妙想技能（模型的 use_skill 走这条，不走 mx_query 子进程）拿得到 key。

    首版这个测试只断言"模块里有 MX* 名字"，等于什么都没验——改成真的构造对象、
    检查 api_key 的来源优先级。"""
    import importlib.util
    runtime_secrets.reset_for_tests()
    runtime_secrets.put("MX_APIKEY", "mx-memory")
    monkeypatch.setenv("MX_APIKEY", "mx-env")
    spec = importlib.util.spec_from_file_location(
        "mxdata_under_test",
        pathlib.Path(cmd_safety.__file__).parent / "skills" / "mx_data" / "mx_data.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.MXData().api_key == "mx-memory", "内存里的 key 优先于环境变量"
    runtime_secrets.reset_for_tests()
    assert mod.MXData().api_key == "mx-env", "内存为空时回退环境变量（独立运行场景）"
    # 迁移后的真机形态：环境里没有 MX_APIKEY，只有内存里有
    monkeypatch.delenv("MX_APIKEY", raising=False)
    runtime_secrets.put("MX_APIKEY", "mx-only-memory")
    assert mod.MXData().api_key == "mx-only-memory"
    runtime_secrets.reset_for_tests()


# ── token 走 stdin ───────────────────────────────────────────────────

def test_read_token_from_stdin(monkeypatch):
    import main
    token = "a" * 64

    # 真管道（与 Rust 侧 spawn 后的形态一致）：select() 需要真实 fileno，
    # StringIO 没有 fileno 会走到兜底分支——那是另一条断言（见下）。
    r, w = os.pipe()
    os.write(w, (token + "\n").encode())
    os.close(w)
    with os.fdopen(r, "r") as f:
        monkeypatch.setattr(sys, "stdin", f)
        assert main._read_token_from_stdin(timeout=0.5) == token

    # 管道里是别的东西 → 形状不符不认（防误当凭据）
    r2, w2 = os.pipe()
    os.write(w2, b"not a token!!\n")
    os.close(w2)
    with os.fdopen(r2, "r") as f2:
        monkeypatch.setattr(sys, "stdin", f2)
        assert main._read_token_from_stdin(timeout=0.5) == ""

    # tty（手动启动）：不读，直接返回空
    class _Tty(io.StringIO):
        def isatty(self):
            return True
    monkeypatch.setattr(sys, "stdin", _Tty(token + "\n"))
    assert main._read_token_from_stdin(timeout=0.5) == ""

    # 没有 fileno 的 stdin（嵌入式/异常环境）→ 兜底返回空，不抛异常
    monkeypatch.setattr(sys, "stdin", io.StringIO(token + "\n"))
    assert main._read_token_from_stdin(timeout=0.5) == ""


def test_auth_token_not_in_environment_source():
    """Rust 侧不再把 token 放进环境（读源码钉住，防回退）。"""
    rs = (pathlib.Path(cmd_safety.__file__).parent.parent
          / "src-tauri" / "src" / "main.rs").read_text("utf-8")
    assert '.env_remove("LATIAO_AUTH_TOKEN")' in rs
    assert '.env_remove("MX_APIKEY")' in rs
    assert '.stdin(std::process::Stdio::piped())' in rs
    assert '.env("LATIAO_AUTH_TOKEN"' not in rs
