"""五控工具测试：进程/鼠标/屏幕/流程控制插件。

隔离：mock subprocess/ctypes，不真实注入事件（避免误操作真实机器）。
只验证参数校验、权限级别、防护逻辑与返回值形态。
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

from plugins import _control_common as cc
from plugins import _control_mouse_common as mc


class _FakeProc:
    def __init__(self, pid=1234):
        self.pid = pid


class TestProcessCommon(unittest.TestCase):
    def test_kill_rejects_system_and_self(self):
        self.assertIn("系统关键进程", cc.kill_pid(1))
        import os
        self.assertIn("自身进程", cc.kill_pid(os.getpid()))

    def test_kill_rejects_invalid(self):
        self.assertIn("系统关键进程", cc.kill_pid(0))
        self.assertIn("系统关键进程", cc.kill_pid(-1))

    @mock.patch("subprocess.run")
    def test_ps_table_parse(self, mock_run):
        mock_run.return_value = mock.MagicMock(stdout="123 1.5 2.3 /usr/bin/ls\n456 0.1 0.2 /usr/bin/python3\n")
        rows = cc.ps_table()
        self.assertEqual(len(rows), 2)
        self.assertIn("pid=123", rows[0])

    @mock.patch("subprocess.run")
    def test_ps_table_pattern_filter(self, mock_run):
        mock_run.return_value = mock.MagicMock(stdout="123 1.5 2.3 /usr/bin/ls\n456 0.1 0.2 /usr/bin/python3\n")
        rows = cc.ps_table("python")
        self.assertEqual(len(rows), 1)
        self.assertIn("pid=456", rows[0])

    @mock.patch("subprocess.Popen")
    def test_launch_bg_returns_pid(self, mock_popen):
        mock_popen.return_value = _FakeProc(9999)
        out = cc.launch_bg("sleep 5")
        self.assertIn("pid=9999", out)
        self.assertIn("输出文件", out)

    def test_launch_rejects_empty(self):
        self.assertIn("不能为空", cc.launch_bg(""))
        self.assertIn("不能为空", cc.launch_bg("x" * 2001))

    @mock.patch("subprocess.run")
    def test_kill_by_name(self, mock_run):
        mock_run.return_value = mock.MagicMock(stdout="123 1.5 2.3 /usr/bin/notepad\n")
        out = cc.kill_by_name("notepad")
        self.assertIn("已终止", out)

    def test_kill_by_name_no_match(self):
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(stdout="123 /usr/bin/ls\n")
            out = cc.kill_by_name("no-such-proc-xyz")
        self.assertIn("没有找到", out)


class TestMouseCommon(unittest.TestCase):
    def test_validate_coord_negative(self):
        self.assertIsNotNone(mc.validate_coord(-1, 10))
        self.assertIsNotNone(mc.validate_coord(10, -5))

    def test_validate_coord_positive(self):
        # mock 分辨率 1920x1080
        with mock.patch("plugins._control_mouse_common.display_bounds", return_value=(1920, 1080)):
            self.assertIsNone(mc.validate_coord(0, 0))
            self.assertIsNone(mc.validate_coord(1920, 1080))
            self.assertIsNotNone(mc.validate_coord(2000, 100))

    def test_keyboard_type_rejects_empty(self):
        with mock.patch("plugins._control_mouse_common.is_macos", return_value=True), \
             mock.patch("plugins._control_mouse_common.tcc_ax_trusted", return_value=True):
            out = mc.keyboard_type("")
            self.assertIn("不能为空", out)

    def test_keyboard_type_rejects_long(self):
        with mock.patch("plugins._control_mouse_common.is_macos", return_value=True), \
             mock.patch("plugins._control_mouse_common.tcc_ax_trusted", return_value=True):
            out = mc.keyboard_type("x" * 501)
            self.assertIn("过长", out)

    def test_ax_not_trusted_guide(self):
        with mock.patch("plugins._control_mouse_common.is_macos", return_value=True), \
             mock.patch("plugins._control_mouse_common.tcc_ax_trusted", return_value=False):
            out = mc.mouse_move(10, 10)
            self.assertIn("辅助功能", out)

    def test_non_macos_unsupported(self):
        with mock.patch("plugins._control_mouse_common.is_macos", return_value=False):
            self.assertIn("仅支持 macOS", mc.mouse_move(10, 10))
            self.assertIn("仅支持 macOS", mc.screen_capture())

    def test_keyboard_press_unknown_combo(self):
        with mock.patch("plugins._control_mouse_common.is_macos", return_value=True), \
             mock.patch("plugins._control_mouse_common.tcc_ax_trusted", return_value=True):
            self.assertIn("无法识别", mc.keyboard_press("zzz"))
            self.assertIn("无法识别", mc.keyboard_press("cmd"))
            self.assertIn("不能为空", mc.keyboard_press(""))


class TestFlowTools(unittest.TestCase):
    def test_wait_clamps(self):
        import asyncio
        spec = importlib_util("plugins/control_wait.py")
        with mock.patch("asyncio.sleep") as m:
            r = asyncio.run(spec.execute({"seconds": 999}))
            self.assertIn("已等待 120.0", r)
        with mock.patch("asyncio.sleep") as m:
            asyncio.run(spec.execute({"seconds": -5}))
            self.assertEqual(m.call_args[0][0], 0.0)

    def test_wait_invalid(self):
        import asyncio
        spec = importlib_util("plugins/control_wait.py")
        r = asyncio.run(spec.execute({"seconds": "abc"}))
        self.assertIn("必须是数字", r)

    @mock.patch("db._get_db")
    def test_audit_queries(self, mock_db):
        spec = importlib_util("plugins/control_audit.py")
        fake_conn = mock.MagicMock()
        fake_conn.execute.return_value.fetchall.return_value = [
            ("id1", "sess", "write_file", "{}", "ok", "2026-01-01")
        ]
        mock_db.return_value = fake_conn
        out = spec.execute({"limit": 5})
        self.assertIn("write_file", out)
        self.assertIn("工具调用历史", out)

    def test_audit_empty(self):
        spec = importlib_util("plugins/control_audit.py")
        with mock.patch("db._get_db") as mock_db:
            fake_conn = mock.MagicMock()
            fake_conn.execute.return_value.fetchall.return_value = []
            mock_db.return_value = fake_conn
            out = spec.execute({})
        self.assertIn("暂无", out)


def importlib_util(path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(f"t_{abs(hash(path))}", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


if __name__ == "__main__":
    unittest.main()


class TestConfirmFlowIntegration(unittest.TestCase):
    """确认流集成：confirm 级控制工具 ∈ _handle_tool_execution 的批准/拒绝路径。"""

    def test_confirm_control_tool_approve_executes(self):
        import asyncio
        import agent_loop
        from plugins import control_launch

        async def run():
            msgs = []
            orig = agent_loop._await_tool_confirmation

            async def fake_approve(call_id, tool_name, args):
                return True, []

            agent_loop._await_tool_confirmation = fake_approve
            try:
                tc = {"id": "c1", "function": {"name": "control_launch", "arguments": '{"command":"sleep 3"}'}}
                failed, _ = await agent_loop._handle_tool_execution(tc, msgs, "s", "latiao", "confirm")
                # 批准后工具真实执行
                return failed, msgs[-1].get("content", "") if msgs else ""
            finally:
                agent_loop._await_tool_confirmation = orig

        failed, content = asyncio.run(run())
        self.assertFalse(failed)
        self.assertIn("已启动", content)

    def test_confirm_control_tool_deny_blocks(self):
        import asyncio
        import agent_loop

        async def run():
            msgs = []
            orig = agent_loop._await_tool_confirmation

            async def fake_deny(call_id, tool_name, args):
                return False, []

            agent_loop._await_tool_confirmation = fake_deny
            try:
                tc = {"id": "c2", "function": {"name": "control_launch", "arguments": '{"command":"sleep 3"}'}}
                failed, _ = await agent_loop._handle_tool_execution(tc, msgs, "s", "latiao", "confirm")
                return failed, msgs[-1].get("content", "") if msgs else ""
            finally:
                agent_loop._await_tool_confirmation = orig

        failed, content = asyncio.run(run())
        self.assertTrue(failed)
        self.assertIn("denied", content.lower())


if __name__ == "__main__":
    unittest.main()
