"""引擎稳定性回归测试：重载防重入 / 状态持久化 / 忙引擎保护。

背景：内存 95% 尖峰 = ensure_engine_healthy 的重载线程不带守卫，与
get_api_url 的自动重载并发 → 双 start_model 串行执行，第二个杀掉第一个
刚加载完的 26GB 再加载一遍。
"""
import unittest


class TestRequestReloadGuard(unittest.TestCase):
    def test_second_request_skipped(self):
        from local_llm import LocalLLMEngine
        eng = LocalLLMEngine.__new__(LocalLLMEngine)  # 不跑 __init__（避免拉起下载等）
        eng._auto_reloading = True  # 模拟已有重载进行中
        eng.current_model_id = "test-model"
        # 直接调用内部请求逻辑应返回 False（跳过）
        ok = LocalLLMEngine._request_reload(eng, "test-model")
        self.assertFalse(ok)

    def test_first_request_starts_thread(self):
        import threading
        from local_llm import LocalLLMEngine
        eng = LocalLLMEngine.__new__(LocalLLMEngine)
        eng._auto_reloading = False
        started = threading.Event()

        def fake_reload(mid):
            started.set()
            eng._auto_reloading = False

        eng._auto_reload = fake_reload
        ok = LocalLLMEngine._request_reload(eng, "test-model")
        self.assertTrue(ok)
        self.assertTrue(started.wait(timeout=2))


class TestEngineStatePersistence(unittest.TestCase):
    def _eng(self):
        from local_llm import LocalLLMEngine
        eng = LocalLLMEngine.__new__(LocalLLMEngine)
        eng.current_model_id = ""
        eng.current_model_name = ""
        eng._active_backend = ""
        eng.server_port = 1235
        return eng

    def test_save_and_restore_roundtrip(self):
        from local_llm import LocalLLMEngine
        eng = self._eng()
        eng.current_model_id = "/models/test-35b"
        eng.current_model_name = "test-35b"
        eng._active_backend = "mlx"
        LocalLLMEngine._save_engine_state(eng)
        try:
            eng2 = self._eng()
            LocalLLMEngine._restore_engine_state(eng2)
            self.assertEqual(eng2.current_model_id, "/models/test-35b")
            self.assertEqual(eng2.current_model_name, "test-35b")
            self.assertEqual(eng2._active_backend, "mlx")
        finally:
            LocalLLMEngine._clear_engine_state(eng)

    def test_clear_removes_file(self):
        from local_llm import LocalLLMEngine
        eng = self._eng()
        eng.current_model_id = "m"
        LocalLLMEngine._save_engine_state(eng)
        LocalLLMEngine._clear_engine_state(eng)
        import os
        from local_llm import LocalLLMEngine as L
        self.assertFalse(L._engine_state_file.exists())


class TestBusyEngineProtection(unittest.TestCase):
    def test_busy_flag_blocks_and_expires(self):
        from local_llm import LocalLLMEngine
        eng = LocalLLMEngine.__new__(LocalLLMEngine)
        LocalLLMEngine._engine_busy_until = 0.0

        eng.mark_engine_busy(grace_sec=5)
        self.assertTrue(eng._engine_busy())

        # 到期后自动失效
        LocalLLMEngine._engine_busy_until = 0.0
        self.assertFalse(eng._engine_busy())

    def test_idle_clears_flag(self):
        from local_llm import LocalLLMEngine
        eng = LocalLLMEngine.__new__(LocalLLMEngine)
        eng.mark_engine_busy(grace_sec=60)
        eng.mark_engine_idle()
        self.assertFalse(eng._engine_busy())


if __name__ == "__main__":
    unittest.main(verbosity=2)
