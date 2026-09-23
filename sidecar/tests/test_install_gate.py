"""⑤⑥⑦ 三条审计项的守卫（2026-09-23）。

⑤ config.json 0600：写入口统一 + 启动补齐已有文件权限
⑥ 安装摘要必填：网络/GitHub 安装路径此前把包打成本地临时文件，绕过了
   "网络来源必须 sha256"的闸门；现在摘要由服务端算、确认时必须回传
⑦ 服务端确认闸门：扩展是"下次启动就执行的代码"，安装必须经用户确认 →
   stage（预览，不写盘）→ confirm（回传摘要）两阶段；install_extension 默认拒装
"""
import hashlib
import io
import json
import os
import stat
import zipfile
from pathlib import Path

import pytest

import extension_manager as em


def _make_zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


MANIFEST = """name: gate-ext
version: 1.0.0
description: 测试扩展
author:
  name: test
permissions:
  - network
"""

PLUGIN_PY = "def execute(args):\n    return 'ok'\n"


@pytest.fixture()
def ext_env(monkeypatch, tmp_path):
    """把扩展目录/待确认区指到 tmp，避免碰真机 ~/.local-ai-os。"""
    monkeypatch.setattr(em, "EXTENSIONS_DIR", tmp_path / "extensions", raising=False)
    monkeypatch.setattr(em, "INSTALLED_FILE", tmp_path / "extensions" / ".installed.json",
                        raising=False)
    monkeypatch.setattr(em, "_PENDING_DIR", tmp_path / "pending", raising=False)
    monkeypatch.setattr(em, "_PENDING", {}, raising=False)
    return tmp_path


def _pkg(source_dir: Path, name: str = "pkg.latiaoext") -> Path:
    p = source_dir / name
    p.write_bytes(_make_zip({"manifest.yaml": MANIFEST, "plugin.py": PLUGIN_PY}))
    return p


# ── ⑤ config.json 0600 ────────────────────────────────────────────────

def test_save_config_writes_0600(monkeypatch, tmp_path):
    import importlib
    monkeypatch.setenv("LATIAO_TEST_PROGRESS_DIR", str(tmp_path / ".local-ai-os"))
    import config
    importlib.reload(config)
    target = config.save_config({"tavily_api_key": "tvly-secret"})
    mode = stat.S_IMODE(os.stat(target).st_mode)
    assert mode == 0o600, f"config.json 权限应为 0600，实际 {oct(mode)}"
    assert json.loads(target.read_text("utf-8"))["tavily_api_key"] == "tvly-secret"
    importlib.reload(config)


def test_harden_config_perms_tightens_existing_files(monkeypatch, tmp_path):
    """老版本装出来的 0644（含 .bak 备份）在启动时被收紧。"""
    import importlib
    root = tmp_path / ".local-ai-os"
    root.mkdir(parents=True)
    monkeypatch.setenv("LATIAO_TEST_PROGRESS_DIR", str(root))
    import config
    importlib.reload(config)
    for nm in ("config.json", "config.json.bak-0922", "notes.md"):
        f = root / nm
        f.write_text("{}", encoding="utf-8")
        os.chmod(f, 0o644)
    changed = config.harden_config_perms()
    assert sorted(changed) == ["config.json", "config.json.bak-0922"], changed
    assert stat.S_IMODE(os.stat(root / "config.json").st_mode) == 0o600
    assert stat.S_IMODE(os.stat(root / "config.json.bak-0922").st_mode) == 0o600
    # 无关文件不动
    assert stat.S_IMODE(os.stat(root / "notes.md").st_mode) == 0o644
    # 已经是 0600 的不重复报
    assert config.harden_config_perms() == []
    importlib.reload(config)


# ── ⑦ install_extension 默认拒装 ────────────────────────────────────────

def test_install_extension_refuses_without_confirmation(ext_env):
    zp = _pkg(ext_env)
    res = em.install_extension(str(zp))
    assert res["status"] == "error"
    assert "需要用户确认" in res["message"]
    assert not (ext_env / "extensions" / "gate-ext").exists(), "拒装时不该落盘"


# ── ⑥⑦ stage → confirm ────────────────────────────────────────────────

def test_stage_returns_preview_and_does_not_install(ext_env):
    zp = _pkg(ext_env)
    res = em.stage_install(str(zp))
    assert res["status"] == "pending", res
    pv = res["preview"]
    assert pv["name"] == "gate-ext" and pv["version"] == "1.0.0"
    assert pv["permissions"] == ["network"]
    assert pv["file_count"] == 2 and "plugin.py" in pv["files"]
    assert pv["digest"] == hashlib.sha256(zp.read_bytes()).hexdigest()
    assert not (ext_env / "extensions" / "gate-ext").exists(), "预览阶段不该落盘"
    # 待确认区存在且是 0600
    pend = list((ext_env / "pending").glob("*.latiaoext"))
    assert len(pend) == 1
    assert stat.S_IMODE(os.stat(pend[0]).st_mode) == 0o600


def test_confirm_installs_and_records_digest(ext_env):
    zp = _pkg(ext_env)
    staged = em.stage_install(str(zp))
    digest = staged["preview"]["digest"]
    res = em.confirm_install(staged["pending_id"], digest)
    assert res["status"] == "ok", res
    assert res["sha256"] == digest
    installed = ext_env / "extensions" / "gate-ext" / "1.0.0" / "plugin.py"
    assert installed.exists()
    record = json.loads((ext_env / "extensions" / ".installed.json").read_text("utf-8"))
    rec = record["extensions"][0]
    assert rec["sha256"] == digest and rec["name"] == "gate-ext"
    # 确认后待确认条目被消费（同一个 pending_id 不能用第二次）
    assert em.confirm_install(staged["pending_id"], digest)["status"] == "error"


def test_confirm_requires_digest(ext_env):
    """⑥：空摘要必须拒绝（不能"确认一个不知道内容的东西"）。"""
    zp = _pkg(ext_env)
    staged = em.stage_install(str(zp))
    res = em.confirm_install(staged["pending_id"], "")
    assert res["status"] == "error" and "sha256" in res["message"]
    res2 = em.confirm_install("", staged["preview"]["digest"])
    assert res2["status"] == "error"


def test_confirm_rejects_wrong_digest_and_installs_nothing(ext_env):
    zp = _pkg(ext_env)
    staged = em.stage_install(str(zp))
    res = em.confirm_install(staged["pending_id"], "0" * 64)
    assert res["status"] == "error" and "不匹配" in res["message"]
    assert not (ext_env / "extensions" / "gate-ext").exists()


def test_confirm_unknown_pending_id(ext_env):
    res = em.confirm_install("deadbeef", "a" * 64)
    assert res["status"] == "error" and "不存在或已过期" in res["message"]


def test_stage_rejects_declared_digest_mismatch(ext_env):
    """⑥：调用方（市场清单）声明的摘要与实际内容不符 → 当场拒。"""
    zp = _pkg(ext_env)
    res = em.stage_install(str(zp), sha256="b" * 64)
    assert res["status"] == "error" and "sha256 校验失败" in res["message"]


def test_stage_accepts_matching_declared_digest(ext_env):
    """市场清单带 sha256（官方清单每个包都有）→ 摘要一致则正常进入待确认。"""
    zp = _pkg(ext_env)
    good = hashlib.sha256(zp.read_bytes()).hexdigest()
    res = em.stage_install(str(zp), sha256=good)
    assert res["status"] == "pending" and res["preview"]["digest"] == good


def test_stage_rejects_network_source_without_sha256(ext_env, monkeypatch):
    monkeypatch.setattr(em, "_load_source_bytes",
                        lambda src: (b"whatever", "https://example.com/x.latiaoext", None),
                        raising=False)
    res = em.stage_install("https://example.com/x.latiaoext")
    assert res["status"] == "error" and "必须提供 sha256" in res["message"]


def test_github_install_stages_not_installs(ext_env, monkeypatch):
    """⑥ 的核心：GitHub 下载的内容也必须走 stage（不再打包成本地文件直接装）。"""
    import adapters
    payload = _make_zip({"manifest.yaml": MANIFEST, "plugin.py": PLUGIN_PY})
    monkeypatch.setattr(adapters, "install_openclaw_skill",
                        lambda repo, path: payload, raising=False)
    res = em.install_github_item("someone/some-skill", "skills/demo", "openclaw-skill")
    assert res["status"] == "pending", res
    assert res["preview"]["digest"] == hashlib.sha256(payload).hexdigest()
    assert not (ext_env / "extensions" / "gate-ext").exists()
    # 声明摘要不符 → 拒
    bad = em.install_github_item("someone/some-skill", "skills/demo", "openclaw-skill",
                                 expect_sha256="c" * 64)
    assert bad["status"] == "error" and "sha256 校验失败" in bad["message"]


def test_create_skill_tool_is_confirm_level():
    """⑦ 的第二道：模型侧唯一能装扩展的工具必须是 confirm 级。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "cs_under_test", Path(em.__file__).parent / "plugins" / "create_skill.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.PERMISSION == "confirm"
