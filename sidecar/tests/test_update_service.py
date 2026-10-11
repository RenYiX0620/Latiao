"""更新预下载服务（update_service）测试：清单生成/版本比较/状态机。

网络隔离：mock urllib（清单拉取与下载），不真实下载。
"""
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
        # 模块级下载线程引用必须每例复位（2026-10-09 实测）：前序用例把
        # _download_thread 留成 mock 后，.is_alive() 恒真 → start_prepare 会在
        # 最开头"已在跑"处直接返回，后面的状态机分支一条都进不去
        # （单独跑绿、全量红）。
        self._old_thread = us._download_thread
        us._download_thread = None
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
        us._download_thread = self._old_thread
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
    def test_resume_selfheals_when_package_complete(self, mock_thread):
        """下载残留下、目标包其实已完整 → 自愈为 done，不再白续 324MB（2026-10-09）。

        现场：GitHub 下载第 1 次中断（11%）后进程退出，状态残留 downloading；
        目标包已由官方渠道补齐（大小 == state.total）。旧逻辑下次启动会从 11%
        续传——白下一整个 324MB。"""
        mock_thread.side_effect = lambda target=None, daemon=None: (target(), mock.MagicMock())[1]
        us.UPDATE_DIR.mkdir(parents=True, exist_ok=True)
        pkg = us.UPDATE_DIR / "Latiao_0.3.6_x64-setup.exe"
        pkg.write_bytes(b"x" * 100)                      # 完整包（大小 == total）
        (us.UPDATE_DIR / "Latiao_0.3.6_x64-setup.exe.part").write_bytes(b"partial" * 5)
        us._save_state({"status": "downloading", "version": "0.3.6", "total": 100,
                        "url": "https://x/Latiao_0.3.6_x64-setup.exe"})
        st = us.start_prepare("0.3.4")
        self.assertEqual(st["status"], "done", "完整包在位时必须自愈为 done")
        self.assertFalse((us.UPDATE_DIR / "Latiao_0.3.6_x64-setup.exe.part").exists(),
                         ".part 残片应被清掉")

    @mock.patch("threading.Thread")
    @mock.patch.object(us, "fetch_remote_manifest", return_value=None)
    def test_prepare_done_survives_manifest_failure(self, mock_fetch, mock_thread):
        """包已下完、但远端清单拉不到 → 状态必须保持 done（2026-10-09 用户实测）。

        早先 done 分支拉不到远端时不 return、落到 worker；worker 开头把状态改成
        checking，于是"保留既有 done"的判断恒假 → 包下完了却被写成 failed，
        界面报"更新失败"（用户更新 v0.3.73 时正是这个链路）。"""
        mock_thread.side_effect = lambda target=None, daemon=None: (
            target(), mock.MagicMock())[1]
        us._save_state({"status": "done", "version": "0.3.6",
                        "url": "https://x/a.tar.gz", "signature": "s"})
        st = us.start_prepare("0.3.4")
        self.assertEqual(st["status"], "done",
                         "拉不到远端清单时必须保留 done，不得改写成 failed")

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


class TestDigestVerification(TestUpdateService):
    """更新包校验（2026-10-11 审计修复）：镜像可换包，下载/自愈都必须验。

    继承 TestUpdateService 的 setUp（UPDATE_DIR/STATE_FILE 重定向 + 落盘 mock）
    ——**这是我上一版犯过的错**：独立写一个不隔离的测试类，直接落盘到真机
    state.json，把假状态写进用户更新目录（用户随后看到"下载中 天文数字%"）。
    """


    def test_sha256_file_matches_hashlib(self):
        import hashlib, tempfile
        from pathlib import Path
        import update_service as us
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "x.bin"
            f.write_bytes(b"hello world")
            want = "sha256:" + hashlib.sha256(b"hello world").hexdigest()
            self.assertEqual(us._sha256_file(f), want)

    def test_fetch_digest_missing_version_returns_empty(self):
        import update_service as us
        self.assertEqual(us._fetch_expected_digest("", "a.tar.gz"), "")

    def test_download_verifies_and_rejects_tampered_part(self):
        """下载完成时校验不过 → failed + integrity=mismatch + 坏包删除。"""
        import tempfile
        from pathlib import Path
        import update_service as us

        class _Resp:
            def __init__(self, body): self._b = body; self._done = False
            def read(self, n=-1):
                if self._done or not self._b:
                    return b""          # 读完必须返回空，否则下载循环永不结束
                self._done = True
                return self._b
            def getheader(self, k, d=None): return str(len(self._b))
            def __enter__(self): return self
            def __exit__(self, *a): return False

        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "pkg.tar.gz"
            part = dest.with_suffix(dest.suffix + ".part")
            calls = {"n": 0}
            def _open(req, timeout=None, context=None):
                calls["n"] += 1
                if calls["n"] == 1:
                    return _Resp(b"tampered-bytes")     # 第一次喂坏内容
                raise RuntimeError("stop")               # 之后立即失败
            orig_open = us.urllib.request.urlopen
            orig_sleep = us.time.sleep
            us.urllib.request.urlopen = _open
            us.time.sleep = lambda s: None               # 跳过退避，测试不空转 36s
            us._state.clear()
            us._state.update({"expected_digest": "sha256:" + "0" * 64})
            try:
                us._download_worker("https://x/pkg.tar.gz", dest, "0.3.9")
            finally:
                us.urllib.request.urlopen = orig_open
                us.time.sleep = orig_sleep
            self.assertEqual(us._state.get("status"), "failed")
            self.assertEqual(us._state.get("integrity"), "mismatch")
            self.assertFalse(part.exists(), "坏包必须删除")
            self.assertFalse(dest.exists())


class TestCleanupOldPackages(TestUpdateService):
    """旧包清理（2026-10-11）：实测堆了 43 个包 13.8GB 且无清理机制。

    只清**可再生的下载缓存**：更早版本的整包与孤儿 .part。必须保住：
    状态引用的包（安装要用）、版本最高者（状态丢失时兜底）、签名文件、
    backup/ 之类的非缓存目录。
    """

    def _make(self, name, size=100):
        f = us.UPDATE_DIR / name
        f.write_bytes(b"x" * size)
        return f

    def test_removes_older_keeps_newest_and_backup(self):
        us.UPDATE_DIR.mkdir(parents=True, exist_ok=True)
        self._make("Latiao_0.3.10_aarch64.app.tar.gz")
        self._make("Latiao_0.3.50_aarch64.app.tar.gz")
        newest = self._make("Latiao_0.3.77_aarch64.app.tar.gz")
        (us.UPDATE_DIR / "Latiao_0.3.77_aarch64.app.tar.gz.sig").write_text("sig")
        (us.UPDATE_DIR / "backup").mkdir(exist_ok=True)
        (us.UPDATE_DIR / "backup" / "user-snapshot").mkdir(exist_ok=True)
        us._state.clear()
        us._state.update({"status": "done",
                          "url": "https://x/v0.3.77/Latiao_0.3.77_aarch64.app.tar.gz"})
        r = us.cleanup_old_packages()
        left = {p.name for p in us.UPDATE_DIR.iterdir()}
        assert newest.name in left, "最新包必须保留"
        assert "Latiao_0.3.10_aarch64.app.tar.gz" not in left
        assert "Latiao_0.3.50_aarch64.app.tar.gz" not in left
        assert (us.UPDATE_DIR / "Latiao_0.3.77_aarch64.app.tar.gz.sig").exists()
        assert (us.UPDATE_DIR / "backup" / "user-snapshot").exists(), "用户文件目录不得动"
        assert r["freed_bytes"] >= 200

    def test_keeps_referenced_package_even_if_not_newest(self):
        """状态引用的是旧包（装着没装完的场景）→ 也必须保留。"""
        us.UPDATE_DIR.mkdir(parents=True, exist_ok=True)
        ref = self._make("Latiao_0.3.70_aarch64.app.tar.gz")
        self._make("Latiao_0.3.77_aarch64.app.tar.gz")
        us._state.clear()
        us._state.update({"status": "downloading",
                          "url": "https://x/v0.3.70/Latiao_0.3.70_aarch64.app.tar.gz"})
        us.cleanup_old_packages()
        left = {p.name for p in us.UPDATE_DIR.iterdir()}
        assert ref.name in left, "状态引用的包必须保留"

    def test_state_lost_keeps_newest_by_version(self):
        """状态复位过（无 url）→ 用版本号兜底，别把最新包删了。"""
        us.UPDATE_DIR.mkdir(parents=True, exist_ok=True)
        self._make("Latiao_0.3.10_aarch64.app.tar.gz")
        newest = self._make("Latiao_0.3.77_aarch64.app.tar.gz")
        us._state.clear()
        r = us.cleanup_old_packages()
        left = {p.name for p in us.UPDATE_DIR.iterdir()}
        assert newest.name in left, f"无状态时必须保最新: {r}"

    def test_removes_orphan_part_keeps_live_one(self):
        us.UPDATE_DIR.mkdir(parents=True, exist_ok=True)
        live = self._make("Latiao_0.3.77_aarch64.app.tar.gz.part")
        orphan = self._make("Latiao_0.3.50_aarch64.app.tar.gz.part")
        us._state.clear()
        us._state.update({"status": "downloading",
                          "url": "https://x/v0.3.77/Latiao_0.3.77_aarch64.app.tar.gz",
                          "part_path": str(live)})
        us.cleanup_old_packages()
        left = {p.name for p in us.UPDATE_DIR.iterdir()}
        assert live.name in left, "正在续传的 .part 必须保留"
        # 孤儿（正式包不在、状态也不引用）应被清掉
        assert orphan.name not in left


if __name__ == "__main__":
    unittest.main()
