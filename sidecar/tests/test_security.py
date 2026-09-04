"""Security regression tests (audit H2/H3: fail-open auth, implicit privilege, run_cmd bypass)."""
import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "plugins"))


class TestNormalizeAccess(unittest.TestCase):
    """未知权限值不得静默升格为 full（审计 H2）。"""

    def test_unknown_value_falls_to_confirm(self):
        from agent_loop import _normalize_access
        self.assertEqual(_normalize_access(""), "confirm")
        self.assertEqual(_normalize_access("root"), "confirm")
        self.assertEqual(_normalize_access("管理员"), "confirm")

    def test_known_values_preserved(self):
        from agent_loop import _normalize_access
        for v in ("read_only", "confirm", "auto_edit", "plan", "full"):
            self.assertEqual(_normalize_access(v), v)
        self.assertEqual(_normalize_access("workspace"), "auto_edit")


class TestRunCmdInlineBlocked(unittest.TestCase):
    """解释器内联代码（python3 -c / node -e / bash -c）不得通过黑名单（审计 H3）。"""

    def _blocked(self, cmd):
        from run_cmd import _DESTRUCTIVE_PATTERNS, _OBFUSCATION_PATTERNS
        low = cmd.lower()
        return any(re.search(p, low) for p in _OBFUSCATION_PATTERNS + _DESTRUCTIVE_PATTERNS)

    def test_inline_interpreters_blocked(self):
        for cmd in (
            "python3 -c 'print(1)'",
            "node -e 'x'",
            "bash -c 'rm -rf /tmp/x'",
            "perl -e 'x'",
            "deno eval 'x'",
            "fish -c 'x'",
        ):
            self.assertTrue(self._blocked(cmd), cmd)

    def test_script_and_normal_cmds_allowed(self):
        for cmd in ("python3 script.py", "node app.js", "git status", "ls -la", "npm test"):
            self.assertFalse(self._blocked(cmd), cmd)


class _Headers(dict):
    pass


class _StubUrl:
    def __init__(self, path):
        self.path = path


class _Stub:
    def __init__(self, path, headers=None):
        self.url = _StubUrl(path)
        self.headers = _Headers(headers or {})


class TestAuthFailClosed(unittest.TestCase):
    """无 AUTH_TOKEN 时拒绝非豁免端点（审计 H2 fail-open 修复）。"""

    def setUp(self):
        import main as m
        self._orig = m.AUTH_TOKEN
        m.AUTH_TOKEN = ""  # 模拟手动启动（Rust 未注入）
        self.main = m

    def tearDown(self):
        self.main.AUTH_TOKEN = self._orig

    def test_no_token_raises_on_protected_endpoint(self):
        with self.assertRaises(self.main._UnauthorizedError):
            self.main._check_auth(_Stub("/v1/chat/completions"))

    def test_no_token_raises_even_on_health(self):
        # fail-closed：无 token 连 /health 也拒（前端在应用启动路径有 token，不受影响）
        with self.assertRaises(self.main._UnauthorizedError):
            self.main._check_auth(_Stub("/health"))

    def test_wrong_token_raises(self):
        self.main.AUTH_TOKEN = "secret-token"
        with self.assertRaises(self.main._UnauthorizedError):
            self.main._check_auth(_Stub("/v1/chat/completions", {"x-latiao-token": "wrong"}))

    def test_correct_token_passes(self):
        self.main.AUTH_TOKEN = "secret-token"
        self.assertIsNone(self.main._check_auth(
            _Stub("/v1/chat/completions", {"x-latiao-token": "secret-token"})))


if __name__ == "__main__":
    unittest.main()
