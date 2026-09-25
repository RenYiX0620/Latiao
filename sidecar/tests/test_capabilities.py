"""统一能力模型（capability registry）测试：工具与技能合并为一张表。

隔离策略：模块级重定向 db._db_conn 到临时库，并把 registry 的技能目录
指向临时目录——绝不触碰真实 ~/.local-ai-os（教训：早期引擎状态测试
曾把假模型 id 写进真实配置）。
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import db as db_mod

_tmp: tempfile.TemporaryDirectory | None = None
_saved_conn = None
_saved_builtin = None
_saved_user = None


def setUpModule():
    global _tmp, _saved_conn, _saved_builtin, _saved_user
    _tmp = tempfile.TemporaryDirectory()
    _saved_conn = db_mod._db_conn
    db_mod._db_conn = sqlite3.connect(
        str(Path(_tmp.name) / "test_memory.db"), check_same_thread=False)
    db_mod._init_db()

    import capability_registry as cap
    _saved_builtin = cap.BUILTIN_SKILLS_DIR
    _saved_user = cap.USER_SKILLS_DIR
    cap.BUILTIN_SKILLS_DIR = Path(_tmp.name) / "builtin_skills"
    cap.USER_SKILLS_DIR = Path(_tmp.name) / "user_skills"
    # 种子内置技能（frontmatter 带 security_level: confirm）
    demo_dir = cap.BUILTIN_SKILLS_DIR / "demo"
    demo_dir.mkdir(parents=True)
    (demo_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: demo 技能\nsecurity_level: confirm\n---\n"
        "# demo body\n第一步：检查环境\n",
        encoding="utf-8")
    # 触发全链路导入（agent_loop 模块级 sync 写入测试库）
    import main  # noqa: F401


def tearDownModule():
    import capability_registry as cap
    cap.BUILTIN_SKILLS_DIR = _saved_builtin
    cap.USER_SKILLS_DIR = _saved_user
    db_mod._db_conn = _saved_conn
    _tmp.cleanup()


class TestCapabilityRegistry(unittest.TestCase):
    def setUp(self):
        import capability_registry as cap
        self.cap = cap
        # 每个用例清空能力表，避免共享测试库的跨用例状态泄漏
        with cap._write_lock:
            db_mod._db_conn.execute("DELETE FROM capabilities")
            db_mod._db_conn.commit()

    def _tools_args(self):
        return (
            [{"type": "function", "function": {
                "name": "t_test", "description": "测试工具", "parameters": {"type": "object"}}}],
            {"t_test": "confirm"},
            {"t_test": lambda a: "ok"},
        )

    def test_sync_tools_rows(self):
        tools, perms, dispatch = self._tools_args()
        self.cap.sync_tools(tools, perms, dispatch)
        row = self.cap.get_capability("t_test")
        self.assertIsNotNone(row)
        self.assertEqual(row["kind"], "tool")
        self.assertEqual(row["permission"], "confirm")
        self.assertEqual(row["source"], "builtin")

    def test_sync_skills_frontmatter_security_level(self):
        self.cap.sync_skills()
        row = self.cap.get_capability("demo-skill")
        self.assertIsNotNone(row)
        self.assertEqual(row["kind"], "skill")
        self.assertEqual(row["permission"], "confirm")
        self.assertIn("# demo body", self.cap.get_skill_content("demo-skill")["content"])

    def test_sync_idempotent(self):
        tools, perms, dispatch = self._tools_args()
        self.cap.sync_tools(tools, perms, dispatch)
        self.cap.sync_tools(tools, perms, dispatch)
        self.cap.sync_skills()
        self.cap.sync_skills()
        rows = self.cap.list_capabilities()
        self.assertEqual(len([r for r in rows if r["name"] == "t_test"]), 1)
        self.assertEqual(len([r for r in rows if r["name"] == "demo-skill"]), 1)

    def test_use_skill_dispatch_content(self):
        import agent_loop
        self.cap.sync_skills()
        out = agent_loop._dispatch_use_skill({"skill_name": "demo-skill"})
        self.assertIn("# demo body", out)
        self.assertIn("安全等级: confirm", out)

    def test_use_skill_unknown_or_disabled(self):
        import agent_loop
        self.cap.sync_skills()
        out = agent_loop._dispatch_use_skill({"skill_name": "no-such"})
        self.assertIn("不存在或已禁用", out)
        self.cap.set_enabled("demo-skill", False)
        out = agent_loop._dispatch_use_skill({"skill_name": "demo-skill"})
        self.assertIn("不存在或已禁用", out)
        self.assertIsNone(self.cap.get_skill_content("demo-skill"))

    def test_use_skill_permission_resolution(self):
        import main
        from tool_executor import _resolve_permission
        self.cap.sync_skills()
        saved_rules = list(main._custom_permissions)
        main._custom_permissions = []
        try:
            self.assertEqual(
                _resolve_permission("use_skill", {"skill_name": "demo-skill"}), "confirm")
            self.assertEqual(
                _resolve_permission("use_skill", {"skill_name": "no-such"}), "safe")
        finally:
            main._custom_permissions = saved_rules

    def test_toggle_enabled_reflects_catalog(self):
        self.cap.sync_skills()
        self.assertTrue(any(s["name"] == "demo-skill" for s in self.cap.skill_catalog()))
        self.cap.set_enabled("demo-skill", False)
        self.assertFalse(any(s["name"] == "demo-skill" for s in self.cap.skill_catalog()))
        self.cap.set_enabled("demo-skill", True)

    def test_create_delete_user_skill(self):
        created = self.cap.create_skill("我的技能", "内容：做某事")
        self.assertIsNotNone(created)
        self.assertEqual(created["kind"], "skill")
        self.assertEqual(created["source"], "user")
        self.assertTrue(Path(created["source_path"]).exists())
        self.assertTrue(self.cap.delete_skill(created["name"]))
        self.assertIsNone(self.cap.get_capability(created["name"]))
        self.assertFalse(Path(created["source_path"]).exists())

    def test_delete_builtin_rejected(self):
        self.cap.sync_skills()
        self.assertFalse(self.cap.delete_skill("demo-skill"))

    def test_set_permission_override(self):
        tools, perms, dispatch = self._tools_args()
        self.cap.sync_tools(tools, perms, dispatch)
        # 无覆盖 → get_permission 返回 None（回落插件默认）
        self.assertIsNone(self.cap.get_permission("t_test"))
        # 用户覆盖 → 返回表中值且标记生效
        self.cap.set_permission("t_test", "danger")
        self.assertEqual(self.cap.get_permission("t_test"), "danger")
        # 再同步插件默认不应覆盖用户设置
        self.cap.sync_tools(tools, perms, dispatch)
        self.assertEqual(self.cap.get_permission("t_test"), "danger")
        self.assertEqual(self.cap.get_capability("t_test")["permission"], "danger")

    def test_bump_usage(self):
        tools, perms, dispatch = self._tools_args()
        self.cap.sync_tools(tools, perms, dispatch)
        self.cap.bump_usage("t_test")
        self.cap.bump_usage("t_test")
        self.assertEqual(self.cap.get_capability("t_test")["usage_count"], 2)

    def test_remove_extension_caps(self):
        tools, perms, dispatch = self._tools_args()
        self.cap.sync_tools(tools, perms, dispatch)
        # 手工造一个扩展源行
        self.cap._upsert_row(name="ext_tool", kind="tool", display_name="ext_tool",
                             description="x", permission="safe", source="extension:demo-ext")
        self.cap.remove_extension_caps("demo-ext")
        self.assertIsNone(self.cap.get_capability("ext_tool"))
        self.assertIsNotNone(self.cap.get_capability("t_test"))

    def test_prune_removes_stale_tools(self):
        tools, perms, dispatch = self._tools_args()
        self.cap.sync_tools(tools, perms, dispatch)
        self.cap._upsert_row(name="gone_tool", kind="tool", display_name="gone_tool",
                             description="x", permission="safe")
        self.cap.sync_tools(tools, perms, dispatch, prune=True)
        self.assertIsNone(self.cap.get_capability("gone_tool"))
        self.assertIsNotNone(self.cap.get_capability("t_test"))


if __name__ == "__main__":
    unittest.main()
