"""技能资产 / allowed-tools / 纯技能 zip 归一化 / market 工具的配套测试（C1-C4）。

隔离策略与 test_capabilities.py 同形：db._db_conn 重定向到临时库，registry 的
技能目录、extension_manager 的安装目录、discovery 的索引文件全部指向临时目录——
绝不触碰真实 ~/.local-ai-os（2026-10-11 事故的家规）。
"""
import io
import json
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import db as db_mod

_tmp: tempfile.TemporaryDirectory | None = None
_saved: dict = {}


def setUpModule():
    global _tmp
    _tmp = tempfile.TemporaryDirectory()
    _saved["conn"] = db_mod._db_conn
    db_mod._db_conn = sqlite3.connect(
        str(Path(_tmp.name) / "test_memory.db"), check_same_thread=False)
    db_mod._init_db()

    import capability_registry as cap
    _saved["builtin"] = cap.BUILTIN_SKILLS_DIR
    _saved["user"] = cap.USER_SKILLS_DIR
    cap.BUILTIN_SKILLS_DIR = Path(_tmp.name) / "builtin_skills"
    cap.USER_SKILLS_DIR = Path(_tmp.name) / "user_skills"

    import extension_manager as em
    _saved["ext_dir"] = em.EXTENSIONS_DIR
    _saved["installed"] = em.INSTALLED_FILE
    _saved["pending_dir"] = em._PENDING_DIR
    em.EXTENSIONS_DIR = Path(_tmp.name) / "extensions"
    em.INSTALLED_FILE = em.EXTENSIONS_DIR / ".installed.json"
    em._PENDING_DIR = Path(_tmp.name) / "pending"

    import discovery as disc
    _saved["index"] = disc.INDEX_FILE
    disc.INDEX_FILE = Path(_tmp.name) / "discovered_index.json"


def tearDownModule():
    import capability_registry as cap
    import extension_manager as em
    import discovery as disc
    cap.BUILTIN_SKILLS_DIR = _saved["builtin"]
    cap.USER_SKILLS_DIR = _saved["user"]
    em.EXTENSIONS_DIR = _saved["ext_dir"]
    em.INSTALLED_FILE = _saved["installed"]
    em._PENDING_DIR = _saved["pending_dir"]
    disc.INDEX_FILE = _saved["index"]
    db_mod._db_conn = _saved["conn"]
    _tmp.cleanup()


def _zip_bytes(files: dict[str, str]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return out.getvalue()


_SKILL_FM = (
    "---\nname: {name}\ndescription: {desc}\nsecurity_level: confirm\n"
    "{extra}---\n正文：运行 scripts/run.py 完成打包\n"
)


class TestSkillAssets(unittest.TestCase):
    """C1：多文件技能资产盘点与 use_skill 转述。"""

    def setUp(self):
        import capability_registry as cap
        self.cap = cap
        with cap._write_lock:
            cap._get_db().execute("DELETE FROM capabilities")
            cap._get_db().commit()

    def test_builtin_dir_assets_listed(self):
        d = self.cap.BUILTIN_SKILLS_DIR / "packer"
        (d / "scripts").mkdir(parents=True)
        (d / "references").mkdir(parents=True)
        (d / "SKILL.md").write_text(
            "---\nname: packer-skill\ndescription: 打包技能\nsecurity_level: confirm\n"
            "version: 2.1.0\n---\n正文\n", encoding="utf-8")
        (d / "scripts" / "run.py").write_text("print('hi')\n", encoding="utf-8")
        (d / "references" / "notes.md").write_text("说明\n", encoding="utf-8")
        self.assertEqual(self.cap.sync_skills(), 1)
        rows = {c["name"]: c for c in self.cap.list_capabilities()}
        row = rows["packer-skill"]
        self.assertTrue(row["has_scripts"])
        self.assertEqual(row["assets_count"], 2)
        self.assertEqual(row["version"], "2.1.0")
        sc = self.cap.get_skill_content("packer-skill")
        self.assertTrue(str(sc["meta"]["assets_dir"]).endswith("packer"))
        self.assertIn("scripts/run.py", sc["meta"]["assets"])
        self.assertIn("references/notes.md", sc["meta"]["assets"])

    def test_flat_user_skill_has_no_assets(self):
        self.cap.USER_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        (self.cap.USER_SKILLS_DIR / "flat.md").write_text(
            "# 平铺技能\n没有资产\n", encoding="utf-8")
        self.cap.sync_skills()
        row = {c["name"]: c for c in self.cap.list_capabilities()}["flat"]
        self.assertFalse(row["has_scripts"])
        self.assertEqual(row["assets_count"], 0)

    def test_user_frontmatter_parsed_and_stripped(self):
        self.cap.USER_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        (self.cap.USER_SKILLS_DIR / "fm-skill.md").write_text(
            "---\nname: FM 显示名\ndescription: 来自 frontmatter\nallowed-tools: run_cmd, read_file\n"
            "---\n正文内容\n", encoding="utf-8")
        self.cap.sync_skills()
        row = {c["name"]: c for c in self.cap.list_capabilities()}["fm-skill"]
        self.assertEqual(row["description"], "来自 frontmatter")
        self.assertEqual(row["allowed_tools"], ["run_cmd", "read_file"])
        self.assertNotIn("---", self.cap.get_skill_content("fm-skill")["content"])

    def test_flat_extension_skill_no_sibling_leak(self):
        """平铺 <skills>/foo.md 的兄弟文件属于别的技能，不得算成它的资产。"""
        import extension_manager as em
        pkg = em.EXTENSIONS_DIR / "extx" / "1.0.0"
        (pkg / "skills").mkdir(parents=True)
        (pkg / "skills" / "foo.md").write_text(
            "---\nname: foo-skill\n---\n正文\n", encoding="utf-8")
        (pkg / "skills" / "bar.md").write_text(
            "---\nname: bar-skill\n---\n正文\n", encoding="utf-8")
        state = {"extensions": [{"name": "extx", "version": "1.0.0", "enabled": True}]}
        em.EXTENSIONS_DIR.mkdir(parents=True, exist_ok=True)
        em._save_installed(state)
        try:
            self.cap.sync_skills()
            row = {c["name"]: c for c in self.cap.list_capabilities()}["foo-skill"]
            self.assertFalse(row["has_scripts"])
            self.assertEqual(row["source"], "extension:extx")
        finally:
            em._save_installed({"extensions": []})


class TestAllowedTools(unittest.TestCase):
    """C2：allowed-tools 解析与建议权限推导。"""

    def setUp(self):
        import capability_registry as cap
        self.cap = cap
        with cap._write_lock:
            cap._get_db().execute("DELETE FROM capabilities")
            cap._get_db().commit()
        # 工具行：run_cmd=danger，write_file=confirm；read_file 不注册（应被忽略）
        self.cap._upsert_row(name="run_cmd", kind="tool", display_name="run_cmd",
                             permission="danger", preserve_state=False)
        self.cap._upsert_row(name="write_file", kind="tool", display_name="write_file",
                             permission="confirm", preserve_state=False)

    def test_list_form_and_suggested_danger(self):
        allowed = self.cap._parse_allowed_tools(
            {"allowed-tools": ["run_cmd", "read_file"]})
        self.assertEqual(allowed, ["run_cmd", "read_file"])
        self.assertEqual(self.cap._suggested_permission(allowed), "danger")

    def test_string_form_and_suggested_confirm(self):
        allowed = self.cap._parse_allowed_tools({"allowed-tools": "write_file, ghost_tool"})
        self.assertEqual(allowed, ["write_file", "ghost_tool"])
        self.assertEqual(self.cap._suggested_permission(allowed), "confirm")

    def test_empty_allowed_tools_no_suggestion(self):
        self.assertEqual(self.cap._parse_allowed_tools({}), [])
        self.assertEqual(self.cap._suggested_permission([]), "safe")


class TestSkillZipNormalize(unittest.TestCase):
    """C3：纯技能 zip 归一化 → 完整 stage/confirm 安装管线。"""

    def test_root_skill_zip(self):
        import extension_manager as em
        z = _zip_bytes({
            "SKILL.md": _SKILL_FM.format(name="my-packer", desc="打包", extra=""),
            "scripts/run.py": "print('x')\n",
            "README.md": "readme\n",
        })
        out = em._normalize_skill_zip(z)
        self.assertIsNotNone(out)
        names = em._zip_names(out)
        self.assertIn("manifest.yaml", names)
        self.assertIn("skills/my-packer/SKILL.md", names)
        self.assertIn("skills/my-packer/scripts/run.py", names)
        # 根级技能：zip 根即技能目录，README 随包
        self.assertIn("skills/my-packer/README.md", names)
        with zipfile.ZipFile(io.BytesIO(out)) as zf:
            mf = json.loads(zf.read("manifest.yaml"))
        self.assertEqual(mf["name"], "my-packer")
        self.assertTrue(mf["version"].startswith("1.0."))
        self.assertEqual(mf["permissions"], ["readonly"])

    def test_wrapped_repo_multi_skill(self):
        import extension_manager as em
        z = _zip_bytes({
            "repo-main/skills/alpha/SKILL.md": _SKILL_FM.format(
                name="alpha-skill", desc="A", extra=""),
            "repo-main/skills/alpha/scripts/a.sh": "echo a\n",
            "repo-main/skills/beta/SKILL.md": _SKILL_FM.format(
                name="beta-skill", desc="B", extra=""),
            "repo-main/README.md": "r\n",
        })
        out = em._normalize_skill_zip(z)
        names = em._zip_names(out)
        self.assertIn("skills/alpha-skill/SKILL.md", names)
        self.assertIn("skills/alpha-skill/scripts/a.sh", names)
        self.assertIn("skills/beta-skill/SKILL.md", names)
        self.assertEqual(names.count("README.md"), 1)

    def test_version_tracks_content(self):
        import extension_manager as em
        z1 = _zip_bytes({"SKILL.md": _SKILL_FM.format(name="v-skill", desc="d", extra="")})
        z2 = _zip_bytes({"SKILL.md": _SKILL_FM.format(name="v-skill", desc="d 改", extra="")})
        v1 = json.loads(zipfile.ZipFile(io.BytesIO(em._normalize_skill_zip(z1))).read("manifest.yaml"))["version"]
        v2 = json.loads(zipfile.ZipFile(io.BytesIO(em._normalize_skill_zip(z2))).read("manifest.yaml"))["version"]
        self.assertNotEqual(v1, v2)
        self.assertEqual(v1, json.loads(zipfile.ZipFile(io.BytesIO(em._normalize_skill_zip(z1))).read("manifest.yaml"))["version"])

    def test_non_skill_inputs_pass_through(self):
        import extension_manager as em
        self.assertIsNone(em._normalize_skill_zip(_zip_bytes({"manifest.yaml": "name: x\nversion: 1.0.0\n"})))
        self.assertIsNone(em._normalize_skill_zip(_zip_bytes({"readme.md": "无技能\n"})))

    def test_zip_bomb_falls_back_without_reading(self):
        """审计修复：解压总量超限时归一化直接放弃（不整读进内存），回落到
        _safe_extract 的正规报错路径。"""
        import extension_manager as em
        bomb = io.BytesIO()
        with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as z:
            # 声明 60MB 解压体积的成员（内容全零，deflate 后很小）
            z.writestr("SKILL.md", _SKILL_FM.format(name="bomb-skill", desc="d", extra=""))
            z.writestr("big.bin", b"\0" * (em._MAX_EXTRACT_BYTES + 1024))
        self.assertIsNone(em._normalize_skill_zip(bomb.getvalue()))

    def test_traversal_segments_dropped(self):
        """审计修复（纵深防御）：含 ".." 段的条目在归一化时丢弃，
        归一化产物自身不携带逃逸路径（_safe_extract 的 resolve 检查仍是最终防线）。"""
        import extension_manager as em
        z = _zip_bytes({
            "SKILL.md": _SKILL_FM.format(name="esc-skill", desc="d", extra=""),
            "scripts/../../escape.py": "print('pwn')\n",
            "ok.py": "print(1)\n",
        })
        out = em._normalize_skill_zip(z)
        self.assertIsNotNone(out)
        names = em._zip_names(out)
        self.assertTrue(all(".." not in n.split("/") for n in names), names)
        self.assertIn("skills/esc-skill/ok.py", names)
        self.assertNotIn("skills/esc-skill/scripts/../../escape.py", names)

    def test_stage_confirm_pipeline_installs_skill(self):
        import capability_registry as cap
        import extension_manager as em
        z = _zip_bytes({
            "SKILL.md": _SKILL_FM.format(
                name="pipe-skill", desc="管线技能", extra="allowed-tools: read_file\n"),
            "scripts/tool.py": "print(1)\n",
        })
        src = Path(_tmp.name) / "pipe-skill.zip"
        src.write_bytes(z)
        staged = em.stage_install(str(src))
        self.assertEqual(staged["status"], "pending")
        preview = staged["preview"]
        self.assertEqual(len(preview["skills"]), 1)
        sk = preview["skills"][0]
        self.assertEqual(sk["name"], "pipe-skill")
        self.assertTrue(sk["has_scripts"])
        self.assertIn("scripts/tool.py", sk["scripts"])
        self.assertEqual(sk["allowed_tools"], ["read_file"])
        result = em.confirm_install(staged["pending_id"], staged["preview"]["digest"])
        self.assertEqual(result["status"], "ok", result)
        cap.sync_skills()
        sc = cap.get_skill_content("pipe-skill")
        self.assertIsNotNone(sc)
        self.assertTrue(sc["meta"]["assets_dir"])          # 资产随包落盘
        self.assertIn("pipe-skill", sc["meta"]["assets_dir"])


class TestMarketTool(unittest.TestCase):
    """C4：market_search 只读分发。"""

    def setUp(self):
        import capability_registry as cap
        with cap._write_lock:
            cap._get_db().execute("DELETE FROM capabilities WHERE kind='skill'")
            cap._get_db().commit()
        cap._upsert_row(name="alpha-skill", kind="skill", display_name="alpha",
                        content="x", source="builtin", preserve_state=False)

    def _dispatch(self, entries, query):
        from agent.tool_exec import _dispatch_market_search
        with mock.patch("discovery.get_discovered_entries", return_value=entries):
            return _dispatch_market_search({"query": query})

    def test_match_and_installed_flag(self):
        entries = [
            {"name": "alpha-skill", "description": "A 技能", "category": "claude",
             "source_url": "https://github.com/x/alpha"},
            {"name": "beta-tool", "description": "B 技能", "category": "github-discovered",
             "source_url": "https://github.com/x/beta"},
        ]
        out = self._dispatch(entries, "alpha")
        self.assertIn("alpha-skill（已安装）", out)
        self.assertNotIn("beta-tool", out)
        self.assertIn("只读", out)

    def test_empty_index_hint(self):
        out = self._dispatch([], "")
        self.assertIn("市场索引当前为空", out)
        self.assertIn("发现", out)

    def test_query_filters_by_description(self):
        out = self._dispatch(
            [{"name": "weird-name", "description": "股票资金流工具",
              "source_url": "u", "category": "c"}], "资金流")
        self.assertIn("weird-name", out)


if __name__ == "__main__":
    unittest.main()
