"""更新预下载服务（update_service）测试：清单生成/版本比较/状态机。

网络隔离：mock urllib（清单拉取与下载），不真实下载。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import update_service as us


def _run_worker(target):
    try:
        target()
    except Exception as e:
        import traceback
        print("WORKER_EXC:", repr(e))
        traceback.print_exc()


class TestUpdateService(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_dir = us.UPDATE_DIR
        self._old_state = us.STATE_FILE
        us.UPDATE_DIR = Path(self._tmp.name) / "update"
        us.STATE_FILE = us.UPDATE_DIR / "state.json"
        us._state.clear()
        # 真实文件 IO（mkdir/write/replace）在 CI/tmp 环境偶发挂起 →
        # 状态机测试 mock 落盘层，仅验证内存状态流转
        self._io_patcher = mock.patch.object(us, "_save_state", lambda state=None: us._state.update(state or {}))
        self._load_patcher = mock.patch.object(us, "_load_state",
                                               lambda: dict(us._state) if us._state else {})
        self._io_patcher.start()
        self._load_patcher.start()

    def tearDown(self):
        self._io_patcher.stop()
        self._load_patcher.stop()
        us.UPDATE_DIR = self._old_dir
        us.STATE_FILE = self._old_state
        self._tmp.cleanup()

    def test_version_compare(self):
        self.assertTrue(us._is_newer("0.3.5", "0.3.4"))
        self.assertFalse(us._is_newer("0.3.4", "0.3.4"))
        self.assertFalse(us._is_newer("0.3.4", "0.3.5"))
        self.assertTrue(us._is_newer("0.4.0", "0.3.9"))

    def test_platform_key(self):
        import sys
        key = us._current_platform()
        if sys.platform == "darwin":
            self.assertTrue(key.startswith("darwin-"))
        elif sys.platform.startswith("win"):
            self.assertEqual(key, "windows-x86_64")

    def test_manifest_not_done_returns_none(self):
        # 状态未完成（无可用更新）→ None（端点返回 204），与当前实现契约一致
        us._save_state()
        self.assertIsNone(us.get_tauri_manifest("0.3.4"))

    def test_manifest_done_returns_local_url(self):
        us._save_state({
            "status": "done", "version": "0.3.6",
            "url": "https://github.com/RenYiX0620/Latiao/releases/download/v0.3.6/Latiao_0.3.6_x64-setup.exe",
            "signature": "sig123",
        })
        # 在本地目录放"安装包"文件（get_tauri_manifest 校验文件存在）
        pkg = us.UPDATE_DIR / "Latiao_0.3.6_x64-setup.exe"
        us.UPDATE_DIR.mkdir(parents=True, exist_ok=True)
        pkg.write_bytes(b"installer")
        m = us.get_tauri_manifest("0.3.4")
        self.assertEqual(m["version"], "0.3.6")
        # platforms 键 = 当前设备平台（Mac 上是 darwin-aarch64）
        entry = m["platforms"][us._current_platform()]
        self.assertEqual(entry["url"], "http://127.0.0.1:8765/v1/update-file")
        self.assertEqual(entry["signature"], "sig123")

    def test_manifest_done_but_old_version_returns_none(self):
        us._save_state({
            "status": "done", "version": "0.3.3",
            "url": "https://x/Latiao_0.3.3_x64-setup.exe", "signature": "s",
        })
        us.UPDATE_DIR.mkdir(parents=True, exist_ok=True)
        (us.UPDATE_DIR / "Latiao_0.3.3_x64-setup.exe").write_bytes(b"x")
        # 远端 0.3.3 ≤ 当前 0.3.4 → 无更新，返回 None（端点 204）
        self.assertIsNone(us.get_tauri_manifest("0.3.4"))

    @mock.patch("threading.Thread")
    def test_prepare_skips_when_up_to_date(self, mock_thread):
        # 同步执行 worker（防真实 daemon 线程跨测试存活污染状态）
        mock_thread.side_effect = lambda target=None, daemon=None: (
            target(), mock.MagicMock())[1]
        with mock.patch.object(us, "fetch_remote_manifest", return_value={"version": "0.3.4", "platforms": {}}):
            st = us.start_prepare("0.3.4")  # 当前已是 0.3.4
        self.assertEqual(st["status"], "up_to_date")

    @mock.patch("threading.Thread")
    @mock.patch.object(us, "_download_worker")
    @mock.patch.object(us, "fetch_remote_manifest")
    def test_prepare_starts_download_for_newer(self, mock_manifest, mock_dl, mock_thread):
        # 同步执行 worker（Thread 构造即跑），消除 daemon 线程竞态
        mock_thread.side_effect = lambda target=None, daemon=None: (
            target(), mock.MagicMock())[1]
        mock_manifest.return_value = {
            "version": "0.3.6",
            "platforms": {us._current_platform(): {"url": "https://x/setup.exe", "signature": "s"}},
        }
        st = us.start_prepare("0.3.4")
        self.assertEqual(st["status"], "downloading")  # 已启动后台下载
        self.assertEqual(st["version"], "0.3.6")
        mock_dl.assert_called_once()

    def test_progress_reads_part_file(self):
        us._save_state({"status": "downloading", "version": "0.3.6", "total": 100,
                        "part_path": str(us.UPDATE_DIR / "f.part")})
        us.UPDATE_DIR.mkdir(parents=True, exist_ok=True)
        (us.UPDATE_DIR / "f.part").write_bytes(b"x" * 42)
        p = us.get_progress()
        self.assertEqual(p["downloaded"], 42)
        self.assertEqual(p["total"], 100)


if __name__ == "__main__":
    unittest.main()
