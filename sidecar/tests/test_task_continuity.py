"""「任务执行一半停止」回归测试（2026-08-29 轮）。

背景（当天日志实证）：
- 09:39 引擎判死瞬间 get_api_url 返回空串 -> UnsupportedProtocol 秒死
- 11:03 引擎被杀与重载置位之间的竞态窗口，请求走快速失败死亡
  （6 秒后引擎已自动恢复）
- 11:03 空闲引擎挂起，首次探活失败还要等 60s 窗口才处置
- 11:04 搜索成功但模型只回 192 字符收场（短回答重试只在"工具失败"时触发）
"""
import unittest
import time


class _FakeEngine:
    """最小 LocalLLMEngine 替身：只带被测字段/方法。"""

    def __init__(self, **kw):
        self._auto_reloading = False
        self._explicit_stop = False
        self.current_model_id = ""
        self.server_port = 1235
        self.server_status = "stopped"
        self.status_message = ""
        self._active_backend = ""
        self._external_engine = ""
        self._external_url = ""
        self._engine_busy_until = 0.0
        self._active_local_streams = 0
        self.reload_calls = []
        for k, v in kw.items():
            setattr(self, k, v)

    # LocalLLMEngine 的忙保护方法（stream 包装器会调用）
    def mark_engine_busy(self, grace_sec=180.0):
        self._engine_busy_until = __import__("time").time() + grace_sec

    def mark_engine_idle(self):
        time.time()

    def mark_stream_enter(self):
        self._active_local_streams += 1

    def mark_stream_exit(self):
        self._active_local_streams = max(0, self._active_local_streams - 1)

    def _engine_busy(self):
        return self._active_local_streams > 0

    def _auto_reload(self, mid):
        self._auto_reloading = False

    def _request_reload(self, model_id):
        self.reload_calls.append(model_id)
        self._auto_reloading = True
        return True


def _install_fake_engine(monkey_engine):
    import local_llm
    original = getattr(local_llm, "_engine", None)
    local_llm._engine = monkey_engine
    return original


class TestIdleEngineFastDispose(unittest.TestCase):
    """修复 3：空闲引擎探活失败 -> 3s 复验 -> 仍失败立即处置（不等 60s）。"""

    def test_idle_engine_disposed_fast(self):
        import time
        from local_llm import LocalLLMEngine
        eng = LocalLLMEngine.__new__(LocalLLMEngine)
        eng.server_port = 1235
        eng.current_model_id = "m1"
        eng._health_ok = False
        eng._health_verified_at = 0.0
        eng._health_first_fail_at = 0.0
        eng._health_fail_count = 0
        eng._engine_busy = lambda: False
        eng._dispose_dead_engine = lambda: setattr(eng, "disposed", True)
        calls = {"n": 0}

        def always_fail(timeout=20):
            calls["n"] += 1
            return False

        eng.verify_engine_health = always_fail
        slept = []
        LocalLLMEngine._wait_before_recheck = staticmethod(slept.append)
        try:
            ok = LocalLLMEngine.ensure_engine_healthy(eng)
        finally:
            LocalLLMEngine._wait_before_recheck = staticmethod(time.sleep)
        self.assertFalse(ok)
        self.assertTrue(getattr(eng, "disposed", True))
        self.assertEqual(len(slept), 1)  # 3s 复验等待过一次
        self.assertEqual(calls["n"], 2)  # 首验 + 复验

    def test_busy_engine_keeps_slow_window(self):
        """忙引擎：首次失败仍走 60s 慢窗口，不立即处置（防误杀长生成）。"""
        from local_llm import LocalLLMEngine
        eng = LocalLLMEngine.__new__(LocalLLMEngine)
        eng.server_port = 1235
        eng._health_ok = False
        eng._health_verified_at = 0.0
        eng._health_first_fail_at = 0.0
        eng._health_fail_count = 0
        eng._engine_busy = lambda: True
        eng._dispose_dead_engine = lambda: setattr(eng, "disposed", True)
        eng.verify_engine_health = lambda timeout=20: False
        LocalLLMEngine._wait_before_recheck = staticmethod(lambda s: None)
        try:
            ok = LocalLLMEngine.ensure_engine_healthy(eng)
        finally:
            LocalLLMEngine._wait_before_recheck = staticmethod(time.sleep)
        self.assertTrue(ok)  # 首次失败=暂不处置
        self.assertFalse(getattr(eng, "disposed", False))


class TestConnectErrorRecoveryRace(unittest.TestCase):
    """修复 2a：ConnectError 快速失败只在"无可恢复资源"时发生。"""

    def _run_stream_error(self, engine, api_url="http://127.0.0.1:1235/v1/chat/completions"):
        import asyncio
        import httpx
        import agent_loop
        import local_llm
        original = getattr(local_llm, "_engine", None)
        local_llm._engine = engine
        # 排队等待的 sleep 立即返回（只验控制流，不真等 6 分钟）
        original_engine = local_llm._engine
        if hasattr(engine, "_engine_busy_until"):
            pass
        try:
            class _BrokenClient:
                def stream(self, *a, **kw):
                    raise httpx.ConnectError("refused")

            async def _fast_sleep(*a, **kw):
                return None
            _orig_sleep = agent_loop.asyncio.sleep
            agent_loop.asyncio.sleep = _fast_sleep

            async def main():
                try:
                    async with agent_loop._local_llm_stream(
                            _BrokenClient(), api_url, {}, {}) as r:
                        pass
                except httpx.ConnectError as e:
                    return ("connect", e)
                except Exception as e:
                    return (type(e).__name__, e)
                return (None, None)
            try:
                return asyncio.run(main())
            finally:
                agent_loop.asyncio.sleep = _orig_sleep
        finally:
            local_llm._engine = original

    def test_model_present_queues_reload(self):
        """有模型记录且非用户停止：触发重载（排队等待），不再秒死。"""
        eng = _FakeEngine(current_model_id="m1", server_status="stopped")
        kind, err = self._run_stream_error(eng)
        # 不是快速失败的 ConnectError（会带"请到模型页加载"文案）
        self.assertNotIn("请到模型页加载模型", str(err))
        # 但重载必须已被触发（等恢复资源就绪）
        self.assertEqual(eng.reload_calls, ["m1"])
        self.assertTrue(eng._auto_reloading)

    def test_explicit_stop_fails_fast_with_hint(self):
        """用户主动停止：立即失败并给出明确指引。"""
        eng = _FakeEngine(current_model_id="m1", _explicit_stop=True,
                          server_status="stopped")
        eng._request_reload = lambda mid: (_ for _ in ()).throw(AssertionError("不应重载"))
        kind, err = self._run_stream_error(eng)
        self.assertIn("模型页重新加载模型", str(err))

    def test_no_model_ever_loaded_fails_fast(self):
        """从未加载模型：快速失败。"""
        eng = _FakeEngine(current_model_id="", server_status="stopped")
        eng._request_reload = lambda mid: (_ for _ in ()).throw(AssertionError("不应重载"))
        kind, err = self._run_stream_error(eng)
        self.assertIn("请到模型页加载模型", str(err))

    def test_external_engine_fails_fast(self):
        """外部引擎（LM Studio/Ollama 端口）：不自动重载，快速失败。"""
        eng = _FakeEngine(current_model_id="m1", server_status="stopped")
        eng._request_reload = lambda mid: (_ for _ in ()).throw(AssertionError("不应重载"))
        kind, err = self._run_stream_error(
            eng, api_url="http://127.0.0.1:1234/v1/chat/completions")
        self.assertIn("外部", str(err))

