"""手机桥第二凭据（bridge_token 文件）认证测试。

背景（2026-11-14）：官方 OpenClaw 安卓 app 经桥接网关访问辣条，桥进程持有
自己的 token（~/.local-ai-os/bridge_token，0600）。钉住的行为：

- 文件不存在 → 行为与改动前完全一致（只有主 token 合法）
- 文件存在且 0600 → bridge token 作为第二合法凭据，主 token 不受影响
- 权限过宽（组/其他可读写）→ 忽略该文件（0600 纪律）
- 内容无效（<32 字符）→ 忽略
- bridge token 认错 → 401；主 token 认错 → 401
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "plugins"))


class _Headers(dict):
    pass


class _StubUrl:
    def __init__(self, path):
        self.path = path


class _Stub:
    def __init__(self, path, headers=None, method="GET"):
        self.url = _StubUrl(path)
        self.headers = _Headers(headers or {})
        self.method = method


class TestBridgeTokenAuth(unittest.TestCase):
    BRIDGE_TOKEN = "b" * 64
    PRIMARY_TOKEN = "p" * 64

    def setUp(self):
        import main as m
        self.m = m
        self._orig_auth = m.AUTH_TOKEN
        self._orig_path = m._BRIDGE_TOKEN_PATH
        m.AUTH_TOKEN = self.PRIMARY_TOKEN
        self.tmpdir = Path(tempfile.mkdtemp(prefix="latiao_bridge_token_test_"))
        self.bridge_path = self.tmpdir / "bridge_token"
        m._BRIDGE_TOKEN_PATH = self.bridge_path

    def tearDown(self):
        self.m.AUTH_TOKEN = self._orig_auth
        self.m._BRIDGE_TOKEN_PATH = self._orig_path
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write(self, content, mode=0o600):
        self.bridge_path.write_text(content, encoding="utf-8")
        self.bridge_path.chmod(mode)

    def test_no_file_primary_token_still_works(self):
        self.assertIsNone(self.m._check_auth(
            _Stub("/v1/sessions", {"x-latiao-token": self.PRIMARY_TOKEN})))

    def test_no_file_other_token_rejected(self):
        with self.assertRaises(self.m._UnauthorizedError):
            self.m._check_auth(_Stub("/v1/sessions", {"x-latiao-token": "x" * 64}))

    def test_bridge_token_accepted_on_protected_endpoint(self):
        self._write(self.BRIDGE_TOKEN)
        self.assertIsNone(self.m._check_auth(
            _Stub("/v1/chat/completions", {"x-latiao-token": self.BRIDGE_TOKEN})))

    def test_bridge_token_does_not_break_primary_token(self):
        self._write(self.BRIDGE_TOKEN)
        self.assertIsNone(self.m._check_auth(
            _Stub("/v1/sessions", {"x-latiao-token": self.PRIMARY_TOKEN})))

    def test_bridge_token_bearer_header_also_accepted(self):
        self._write(self.BRIDGE_TOKEN)
        self.assertIsNone(self.m._check_auth(
            _Stub("/v1/sessions", {"authorization": f"Bearer {self.BRIDGE_TOKEN}"})))

    def test_bridge_token_wide_perms_rejected(self):
        self._write(self.BRIDGE_TOKEN, mode=0o644)
        with self.assertRaises(self.m._UnauthorizedError):
            self.m._check_auth(_Stub("/v1/sessions", {"x-latiao-token": self.BRIDGE_TOKEN}))

    def test_bridge_token_wrong_value_rejected(self):
        self._write("a" * 64)
        with self.assertRaises(self.m._UnauthorizedError):
            self.m._check_auth(_Stub("/v1/sessions", {"x-latiao-token": self.BRIDGE_TOKEN}))

    def test_bridge_token_short_value_ignored(self):
        self._write("short")
        with self.assertRaises(self.m._UnauthorizedError):
            self.m._check_auth(_Stub("/v1/sessions", {"x-latiao-token": "short"}))

    def test_bridge_token_rotation_picked_up_without_mtime_change(self):
        # 2026-11-14 复审：删缓存后"内容变了但 mtime 不变"也必须立即生效
        self._write(self.BRIDGE_TOKEN)
        self.assertIsNone(self.m._check_auth(
            _Stub("/v1/sessions", {"x-latiao-token": self.BRIDGE_TOKEN})))
        new_token = "c" * 64
        self._write(new_token)  # 不动 mtime
        self.assertIsNone(self.m._check_auth(
            _Stub("/v1/sessions", {"x-latiao-token": new_token})))
        with self.assertRaises(self.m._UnauthorizedError):
            self.m._check_auth(_Stub("/v1/sessions", {"x-latiao-token": self.BRIDGE_TOKEN}))

    def test_bridge_token_invalid_utf8_treated_as_absent(self):
        # 坏字节内容不得让 _check_auth 抛非 401 异常（曾致全端点 500）
        self.bridge_path.write_bytes(b"\xff\xfe" + b"a" * 40)
        with self.assertRaises(self.m._UnauthorizedError):
            self.m._check_auth(_Stub("/v1/sessions", {"x-latiao-token": "x" * 64}))

    def test_bridge_token_symlink_to_wide_target_rejected(self):
        # stat 跟随符号链接：链到权限过宽的目标 → 拒绝（钉住安全语义）
        import os
        real = self.tmpdir / "real_token"
        real.write_text(self.BRIDGE_TOKEN, encoding="utf-8")
        real.chmod(0o644)
        os.symlink(real, self.bridge_path)
        with self.assertRaises(self.m._UnauthorizedError):
            self.m._check_auth(_Stub("/v1/sessions", {"x-latiao-token": self.BRIDGE_TOKEN}))

    def test_fail_closed_unchanged_with_bridge_file_present(self):
        # fail-closed 语义不被第二凭据破坏：AUTH_TOKEN 为空时 bridge token 也放不进去
        self.m.AUTH_TOKEN = ""
        self._write(self.BRIDGE_TOKEN)
        with self.assertRaises(self.m._UnauthorizedError):
            self.m._check_auth(_Stub("/v1/chat/completions", {"x-latiao-token": self.BRIDGE_TOKEN}))


if __name__ == "__main__":
    unittest.main()
