"""第三轮审计 P1 四条修复的回归测试（2026-10-11）。

审计原文四条，均本机实测成立：
1. `write_file` 能写穿 `~/.zshrc`（实测）与 `~/Library/LaunchAgents/*.plist`（实测）
   ——写入一次 = 下次登录即执行；
2. `/v1/files/browse` 不限根目录（resolve 后直接 iterdir）；
3. `/v1/upload_local` 无敏感读保护（读 config.json 即泄露 key）；
4. `/v1/file/preview` 黑名单漏 `.env` 家族、无扩展名身份文件、shell 启动脚本。

修法共用一份判据（cmd_safety 的 sensitive_read_block / sensitive_write_block /
_is_autoload_path），这里钉住行为与"真机文件未被触碰"。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LATIAO_AUTH_TOKEN", "sec3-token")

HOME = os.path.expanduser("~")


# ── 1. write_file 写穿 dotfile / 自启目录 ──────────────────────────

def test_write_file_blocked_on_shell_rc_and_launchagents(tmp_path):
    """真机 dotfile 与自启目录必须被拒——且真文件不受任何影响。"""
    from tool_executor import write_file
    rc = Path(HOME) / ".zshrc"
    before = rc.read_bytes() if rc.exists() else None
    for target in (str(rc),
                   f"{HOME}/.bashrc",
                   f"{HOME}/Library/LaunchAgents/evil-probe.plist",
                   f"{HOME}/.config/autostart/evil.desktop",
                   "/etc/cron.d/evil",
                   f"{HOME}/.zprofile"):
        out = write_file(target, "pwned")
        assert "⛔" in str(out), f"应被拒: {target} → {out}"
    after = rc.read_bytes() if rc.exists() else None
    assert before == after, "真机 .zshrc 内容必须零改动"
    assert not Path(f"{HOME}/Library/LaunchAgents/evil-probe.plist").exists()


def test_write_file_normal_paths_still_work(tmp_path):
    """对照：普通路径照常可写（别修成谁都拦）。"""
    from tool_executor import write_file
    ok = tmp_path / "ok.txt"
    out = write_file(str(ok), "hello")
    assert "已写入" in str(out) and ok.read_text() == "hello"


def test_write_file_git_hooks_blocked(tmp_path):
    from tool_executor import write_file
    hook = tmp_path / "repo" / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True)
    out = write_file(str(hook), "#!/bin/sh\nrm -rf ~")
    assert "⛔" in str(out)
    assert not hook.exists()


# ── 2. 命令路径同名单（cp/echo 写 .zshrc 等）──────────────────────

@pytest.mark.parametrize("tpl", ["cp /tmp/x {t}", "echo x > {t}", "tee {t}",
                                 "sed -i s/a/b/ {t}"])
def test_command_write_to_autoload_paths_blocked(tpl):
    from cmd_safety import check_cmd
    for t in (f"{HOME}/.zshrc", f"{HOME}/Library/LaunchAgents/e.plist",
              f"{HOME}/.zprofile", "/etc/cron.d/e"):
        cmd = tpl.format(t=t)
        assert check_cmd(cmd), f"应被拦: {cmd}"


# ── 3. 读取侧：shell 启动文件 / .env / 无扩展名身份文件 ──────────────

@pytest.mark.parametrize("name", [".zshrc", ".zprofile", ".bash_profile", ".profile",
                                  ".env", ".env.local", ".netrc", "_netrc"])
def test_read_block_blocks_shell_and_env(name):
    from cmd_safety import sensitive_read_block
    assert sensitive_read_block(f"{HOME}/{name}"), name


def test_read_block_allows_env_templates_and_normal_files():
    from cmd_safety import sensitive_read_block
    assert sensitive_read_block(f"{HOME}/.env.example") is None
    assert sensitive_read_block("/tmp/notes.md") is None
    assert sensitive_read_block("/tmp/project/config.json") is None  # 不误伤用户项目


# ── 4. /v1/upload_local 敏感读保护 ────────────────────────────────

@pytest.fixture()
def media_client(tmp_path, monkeypatch):
    monkeypatch.setenv("LATIAO_TEST_PROGRESS_DIR", str(tmp_path / "prog"))
    import importlib

    import config as cfg_mod
    importlib.reload(cfg_mod)
    import main as m
    m.AUTH_TOKEN = "sec3-token"
    from fastapi.testclient import TestClient
    return TestClient(m.app, raise_server_exceptions=False)


_H = {"X-Latiao-Token": "sec3-token"}


def test_upload_local_rejects_sensitive_paths(media_client, tmp_path):
    # 辣条自身 config.json（含 key）
    import config as cfg_mod
    appcfg = Path(str(cfg_mod.CONFIG_FILE))
    appcfg.parent.mkdir(parents=True, exist_ok=True)
    appcfg.write_text('{"cloud_models":[{"key":"sk-real"}]}', encoding="utf-8")
    r = media_client.post("/v1/upload_local", json={"path": str(appcfg)}, headers=_H)
    assert r.status_code == 403 and r.json()["status"] == "error"

    # .env 与 shell 启动文件
    for name, body in ((".env", "SECRET=1"), (".zshrc", "export K=1")):
        f = tmp_path / name
        f.write_text(body, encoding="utf-8")
        r = media_client.post("/v1/upload_local", json={"path": str(f)}, headers=_H)
        assert r.status_code == 403, name


def test_upload_local_allows_normal_files(media_client, tmp_path):
    f = tmp_path / "note.md"
    f.write_text("hello world", encoding="utf-8")
    r = media_client.post("/v1/upload_local", json={"path": str(f)}, headers=_H)
    assert r.json()["status"] in ("ok", "success")


# ── 5. /v1/files/browse 范围限制 ──────────────────────────────────

def test_browse_rejects_out_of_range(media_client):
    """家目录之外（非模型根）的目录不得可列。

    断言只认"被拒"（4xx），不锁死具体码：路径不存在时先命中 400（不是目录），
    存在且越界才是 403（不在可浏览范围内）——两者都证明"拿不到列表"，而
    **平台差异**（macOS 有 /private/etc、CI 的 Linux 没有）曾让锁死 403 的
    版本在 CI 假红（实测 1 failed, 1502 passed）。
    """
    for probe in ("/private/etc", "/usr", "/etc"):
        r = media_client.get("/v1/files/browse", params={"path": probe}, headers=_H)
        assert r.status_code >= 400, f"{probe} 必须被拒，实际 {r.status_code}"
        body = r.json()
        # 两类拒绝形态都算通过：HTTPException → {"detail": ...}；业务错误 → {"status":"error"}
        assert body.get("status") == "error" or body.get("detail")
        assert not body.get("entries"), "被拒时绝不能带回目录列表"


def test_browse_allows_home_and_model_roots(media_client):
    r = media_client.get("/v1/files/browse", params={"path": HOME}, headers=_H)
    assert r.json()["status"] == "ok"
    r = media_client.get("/v1/files/browse", headers=_H)   # 默认根
    assert r.json()["status"] == "ok"


def test_browse_secret_dir_skipped():
    """浏览家目录时不泄漏敏感目录内容（.ssh 等直接按受保护拒绝）。"""
    resp = None
    try:
        from cmd_safety import sensitive_read_block
        resp = sensitive_read_block(f"{HOME}/.ssh")
    except Exception:
        pass
    assert resp, "sensitive_read_block 应拦 .ssh"


# ── 6. 内联代码/脚本的路径审查（2026-10-11 第四轮）──────────────────
# 背景：解释器内联代码此前只过"形状黑名单+禁止调用名单"——看代码长什么样、
# 不看代码碰什么路径。实测 `python3 -c "open('.../config.json').read()"` 与
# 变量变体全部放行，同一条路径的传统形态（cat/cp）却被拦：同一件事两种写法
# 两种结局。现在 AST 抽字符串字面量 → 过同一份读/写封印判据。

@pytest.mark.parametrize("code", [
    "print(open('/Users/langzuxiang/.local-ai-os/config.json').read())",
    "p='/Users/langzuxiang/.local-ai-os/config.json'; print(open(p).read())",
    "open('/Users/langzuxiang/.zshrc','a').write('evil')",
    "print(open('/Users/langzuxiang/.ssh/id_rsa').read())",
    "print(open('/Users/langzuxiang/proj/.env').read())",
])
def test_inline_python_sensitive_paths_blocked(code):
    from cmd_safety import check_inline_code
    denied = check_inline_code(code)
    assert denied and "⛔" in denied, f"内联代码引用受保护路径必须拦: {code}"


@pytest.mark.parametrize("code", [
    "print(1+1)",
    "print(open('README.md').read())",
    "print('a'+'b')",
    "open('/tmp/out.txt','w').write('x')",
    "print(open('/tmp/notes.txt').read())",
])
def test_inline_python_normal_code_allowed(code):
    from cmd_safety import check_inline_code
    assert check_inline_code(code) is None, f"正常代码不得误伤: {code}"


def test_cmd_level_inline_audit_wired():
    """走完整 check_cmd 链路（模型实际路径）：读 config 必须拦。"""
    from cmd_safety import check_cmd
    c = "python3 -c \"print(open('/Users/langzuxiang/.local-ai-os/config.json').read())\""
    assert check_cmd(c), "check_cmd 必须拦下读 config 的内联代码"


def test_script_content_path_audit(tmp_path):
    """脚本文件路径审查：写受保护文件的脚本必须拦。"""
    from cmd_safety import check_script_content
    s = tmp_path / "evil.py"
    s.write_text("open('/Users/langzuxiang/.zshrc','a').write('evil')\n", encoding="utf-8")
    assert check_script_content(str(s)), "脚本写 .zshrc 必须拦"
    ok = tmp_path / "ok.py"
    ok.write_text("print('hello')\n", encoding="utf-8")
    assert check_script_content(str(ok)) is None, "正常脚本不得误伤"


def test_awk_redirection_target_blocked():
    """awk 的写目标是它自己的语法（print > \"file\"），与 echo/printf 同判据。"""
    from cmd_safety import check_cmd
    home = os.path.expanduser("~")
    assert check_cmd(f"awk 'BEGIN{{print \"x\" > \"{home}/.local-ai-os/permissions.json\"}}'")
    assert check_cmd(f"awk 'BEGIN{{print \"\" > \"{home}/.bashrc\"}}'")
    assert check_cmd("awk 'BEGIN{print \"x\" > \"/tmp/out.txt\"}'") is None, "普通文件不误伤"
