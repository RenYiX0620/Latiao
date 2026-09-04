"""批次1 修复回归测试：命令白名单绕过、权限默认档、只读白名单、取消注册表、cron 角色转换。"""
import inspect
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "plugins"))


class TestWhitelistNoEnvBypass(unittest.TestCase):
    """env/printenv 前缀不得再绕过白名单（审计 P0-2）。"""

    def test_env_not_in_whitelist(self):
        from cmd_safety import SAFE_CMD_RE
        for cmd in ("env curl https://evil.com", "env python3 /tmp/x.py",
                    "printenv", "env", "env ls"):
            self.assertIsNone(SAFE_CMD_RE.match(cmd), cmd)

    def test_simple_safe_cmds_still_match(self):
        from cmd_safety import SAFE_CMD_RE
        for cmd in ("ls", "ls -la", "ls /tmp", "pwd", "whoami", "cat file.txt",
                    "head -n 5 x.txt", "echo hello", "date"):
            self.assertIsNotNone(SAFE_CMD_RE.match(cmd), cmd)

    def test_operators_never_match(self):
        from cmd_safety import SAFE_CMD_RE
        for cmd in ("ls; rm -rf /", "cat x | sh", "ls > out", "echo $(id)", "ls `id`"):
            self.assertIsNone(SAFE_CMD_RE.match(cmd), cmd)


class TestSensitiveReadBlocked(unittest.TestCase):
    """cat/head/tail 读取密钥/凭据路径必须被拒（白名单不能成为 read_file 黑名单的侧门）。"""

    def test_ssh_keys_blocked(self):
        from cmd_safety import check_cmd
        for cmd in ("cat ~/.ssh/id_rsa", "cat /Users/u/.ssh/id_ed25519",
                    "head -n 5 ~/.aws/credentials", "tail /Users/u/.netrc"):
            self.assertIsNotNone(check_cmd(cmd), cmd)

    def test_normal_files_allowed(self):
        from cmd_safety import check_cmd
        for cmd in ("cat README.md", "head -n 5 src/main.py", "tail -20 app.log"):
            self.assertIsNone(check_cmd(cmd), cmd)


class TestInlineInterpretersBlockedUnified(unittest.TestCase):
    """解释器内联拦截在插件与 fallback 共用同一 check_cmd（此前 fallback 缺失）。"""

    def test_inline_blocked(self):
        from cmd_safety import check_cmd
        for cmd in ("python3 -c 'print(1)'", "node -e 'x'", "bash -c 'rm -rf /tmp/x'",
                    "perl -e 'x'", "fish -c 'x'"):
            self.assertIsNotNone(check_cmd(cmd), cmd)

    def test_scripts_allowed(self):
        from cmd_safety import check_cmd
        for cmd in ("python3 script.py", "node app.js", "bash deploy.sh",
                    "git status", "npm test"):
            self.assertIsNone(check_cmd(cmd), cmd)


class TestReadonlyWhitelistFullTokens(unittest.TestCase):
    """子智能体只读白名单：完整 token 校验（审计 P0-3）。"""

    def _ro(self, cmd):
        from tool_executor import _is_readonly_cmd
        return _is_readonly_cmd(cmd)

    def test_env_bypass_rejected(self):
        self.assertFalse(self._ro("env curl https://evil.com"))
        self.assertFalse(self._ro("env"))
        self.assertFalse(self._ro("printenv LATIAO_AUTH_TOKEN"))

    def test_sensitive_cat_rejected(self):
        self.assertFalse(self._ro("cat ~/.ssh/id_rsa"))
        self.assertFalse(self._ro("cat /Users/u/.aws/credentials"))

    def test_normal_readonly_ok(self):
        for cmd in ("ls -la", "ls /tmp", "grep -r foo .", "head -n 5 x", "pwd", "whoami"):
            self.assertTrue(self._ro(cmd), cmd)

    def test_complex_rejected(self):
        for cmd in ("curl -s https://x.com", "ls && id", "rm -rf /tmp/x"):
            self.assertFalse(self._ro(cmd), cmd)


class TestAccessDefaultsConfirm(unittest.TestCase):
    """权限默认档必须为 confirm（审计 P0-1：此前三处默认 full）。"""

    def test_loop_defaults_confirm(self):
        from agent_loop import _agent_loop_stream, _local_agent_loop_stream, _handle_tool_execution
        for fn in (_agent_loop_stream, _local_agent_loop_stream, _handle_tool_execution):
            self.assertEqual(
                inspect.signature(fn).parameters["access_mode"].default, "confirm",
                fn.__name__)


class TestSessionCancelRegistry(unittest.TestCase):
    def test_cancel_lifecycle(self):
        from agent_loop import (
            _request_session_cancel, _clear_session_cancel, _session_cancel_requested,
        )
        sid = "sess-test"
        _clear_session_cancel(sid)
        self.assertFalse(_session_cancel_requested(sid))
        _request_session_cancel(sid)
        self.assertTrue(_session_cancel_requested(sid))
        _clear_session_cancel(sid)
        self.assertFalse(_session_cancel_requested(sid))

    def test_other_session_unaffected(self):
        from agent_loop import (
            _request_session_cancel, _clear_session_cancel, _session_cancel_requested,
        )
        _request_session_cancel("sess-a")
        self.assertFalse(_session_cancel_requested("sess-b"))
        _clear_session_cancel("sess-a")


class TestCronToolRoleConversion(unittest.TestCase):
    def test_tool_to_user_conversion(self):
        from cron import _convert_tool_messages_for_local
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "tool", "content": "result 1"},
            {"role": "assistant", "content": "ok"},
        ]
        out = _convert_tool_messages_for_local(msgs)
        self.assertEqual([m["role"] for m in out],
                         ["system", "user", "user", "assistant"])
        self.assertEqual(out[2]["content"], "[工具结果] result 1")


if __name__ == "__main__":
    unittest.main()
