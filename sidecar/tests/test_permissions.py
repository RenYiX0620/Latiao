"""Tests for the tool permission system (main._resolve_permission)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import main


class TestResolvePermission(unittest.TestCase):
    """Unit tests for main._resolve_permission.

    _resolve_permission checks custom rules (with optional path_pattern
    matching) first, then falls back to the TOOL_PERMISSIONS default table.
    """

    def setUp(self):
        self._saved_rules = list(main._custom_permissions)
        self._saved_table = dict(main.TOOL_PERMISSIONS)
        main._custom_permissions = []
        main.TOOL_PERMISSIONS.clear()

    def tearDown(self):
        main._custom_permissions = self._saved_rules
        main.TOOL_PERMISSIONS.clear()
        main.TOOL_PERMISSIONS.update(self._saved_table)

    # ── default permission table (TOOL_PERMISSIONS) ──
    def test_unknown_tool_defaults_to_safe(self):
        self.assertEqual(main._resolve_permission("no_such_tool", {}), "safe")

    def test_tool_permissions_table_honored(self):
        main.TOOL_PERMISSIONS["mock_editor"] = "confirm"
        main.TOOL_PERMISSIONS["mock_deleter"] = "danger"
        self.assertEqual(main._resolve_permission("mock_editor", {}), "confirm")
        self.assertEqual(main._resolve_permission("mock_deleter", {}), "danger")
        self.assertEqual(main._resolve_permission("mock_unknown", {}), "safe")

    # ── custom rule matching (safe / confirm / danger) ──
    def test_custom_rule_safe(self):
        main._custom_permissions = [{"tool": "read_file", "permission": "safe"}]
        self.assertEqual(main._resolve_permission("read_file", {}), "safe")

    def test_custom_rule_confirm(self):
        main._custom_permissions = [{"tool": "write_file", "permission": "confirm"}]
        self.assertEqual(
            main._resolve_permission("write_file", {"path": "/tmp/x.txt"}), "confirm"
        )

    def test_custom_rule_danger(self):
        main._custom_permissions = [{"tool": "run_cmd", "permission": "danger"}]
        self.assertEqual(
            main._resolve_permission("run_cmd", {"command": "rm -rf /"}), "danger"
        )

    def test_custom_rule_overrides_table(self):
        main.TOOL_PERMISSIONS["read_file"] = "danger"
        main._custom_permissions = [{"tool": "read_file", "permission": "safe"}]
        self.assertEqual(main._resolve_permission("read_file", {}), "safe")

    def test_custom_rule_for_other_tool_skipped(self):
        main.TOOL_PERMISSIONS["read_file"] = "danger"
        main._custom_permissions = [{"tool": "write_file", "permission": "safe"}]
        self.assertEqual(main._resolve_permission("read_file", {}), "danger")

    # ── path_pattern matching ──
    def test_path_pattern_match(self):
        main._custom_permissions = [
            {"tool": "read_file", "path_pattern": "/tmp/*", "permission": "safe"}
        ]
        self.assertEqual(
            main._resolve_permission("read_file", {"path": "/tmp/data.txt"}), "safe"
        )

    def test_path_pattern_no_match_falls_back(self):
        main.TOOL_PERMISSIONS["read_file"] = "confirm"
        main._custom_permissions = [
            {"tool": "read_file", "path_pattern": "/tmp/*", "permission": "safe"}
        ]
        self.assertEqual(
            main._resolve_permission("read_file", {"path": "/etc/hosts"}), "confirm"
        )

    def test_path_pattern_with_expanduser(self):
        main._custom_permissions = [
            {"tool": "read_file", "path_pattern": "~/notes/*", "permission": "safe"}
        ]
        self.assertEqual(
            main._resolve_permission("read_file", {"path": "~/notes/todo.md"}), "safe"
        )

    def test_path_pattern_non_string_args_ignored(self):
        main.TOOL_PERMISSIONS["read_file"] = "danger"
        main._custom_permissions = [
            {"tool": "read_file", "path_pattern": "/tmp/*", "permission": "safe"}
        ]
        self.assertEqual(main._resolve_permission("read_file", {"path": 12345}), "danger")

    # ── invalid / missing permission value degradation ──
    def test_rule_missing_permission_degrades_to_confirm(self):
        main.TOOL_PERMISSIONS["read_file"] = "safe"
        main._custom_permissions = [{"tool": "read_file"}]
        # 自定义规则缺少 permission 字段时降级为 "confirm"
        self.assertEqual(main._resolve_permission("read_file", {}), "confirm")

    def test_path_rule_missing_permission_degrades_to_confirm(self):
        main.TOOL_PERMISSIONS["read_file"] = "safe"
        main._custom_permissions = [{"tool": "read_file", "path_pattern": "/tmp/*"}]
        # path 命中但 permission 缺失同样降级为 "confirm"
        self.assertEqual(
            main._resolve_permission("read_file", {"path": "/tmp/x.txt"}), "confirm"
        )


if __name__ == "__main__":
    unittest.main()
