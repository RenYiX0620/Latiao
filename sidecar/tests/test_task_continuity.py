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

    def _kill_port(self, port):
        self.killed_port = port

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
        eng.server_status = "stopped"  # 非加载中——加载中探活失败不处置
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

    def test_loading_engine_not_disposed(self):
        """加载中的引擎探活失败（模型未就绪 404）是常态：不处置、不误杀
        （14:38 事故回归——误杀正在加载的引擎会再拉一轮重载）。"""
        from local_llm import LocalLLMEngine
        eng = LocalLLMEngine.__new__(LocalLLMEngine)
        eng.server_port = 1235
        eng.current_model_id = "m1"
        eng.server_status = "starting"
        eng._health_ok = False
        eng._health_verified_at = 0.0
        eng._health_first_fail_at = 0.0
        eng._health_fail_count = 0
        eng._engine_busy = lambda: False
        eng._dispose_dead_engine = lambda: setattr(eng, "disposed", True)
        eng.verify_engine_health = lambda timeout=20: False
        LocalLLMEngine._wait_before_recheck = staticmethod(lambda s: None)
        try:
            ok = LocalLLMEngine.ensure_engine_healthy(eng)
        finally:
            LocalLLMEngine._wait_before_recheck = staticmethod(time.sleep)
        self.assertTrue(ok)  # 暂不处置
        self.assertFalse(getattr(eng, "disposed", False))

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

    def _run_stream_error(self, engine, api_url="http://127.0.0.1:1235/v1/chat/completions",
                          client=None):
        import asyncio
        import httpx
        import agent_loop
        import local_llm
        original = getattr(local_llm, "_engine", None)
        local_llm._engine = engine
        # 排队等待的 sleep 立即返回（只验控制流，不真等 6 分钟）
        if client is None:
            class _BrokenClient:
                def stream(self, *a, **kw):
                    raise httpx.ConnectError("refused")
            client = _BrokenClient()
        try:
            async def _fast_sleep(*a, **kw):
                return None
            _orig_sleep = agent_loop.asyncio.sleep
            agent_loop.asyncio.sleep = _fast_sleep

            async def main():
                try:
                    async with agent_loop._local_llm_stream(
                            client, api_url, {}, {}) as r:
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

    def test_read_error_also_queues_reload(self):
        """kill -9 在预填充期抛 ReadError（非 ConnectError），同样必须触发重载
        排队而非秒死（E2E 测试 B 回归：TransportError 基类捕获）。"""
        import httpx
        eng = _FakeEngine(current_model_id="m1", server_status="running")

        class _ReadErrorClient:
            def stream(self, *a, **kw):
                raise httpx.ReadError("connection reset by peer")

        kind, err = self._run_stream_error(eng, client=_ReadErrorClient())
        self.assertNotIn("内部", str(err))
        self.assertEqual(eng.reload_calls, ["m1"])

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

    def test_reload_failed_fails_fast(self):
        """重载已请求且已结束仍连不上（状态 error）：快速失败，不等 6 分钟。"""
        import httpx
        eng = _FakeEngine(current_model_id="m1", _auto_reloading=True,
                          server_status="stopped")
        eng._request_reload = lambda mid: (_ for _ in ()).throw(
            AssertionError("重载已进行过，不应再次触发"))

        class _ReloadFailsClient:
            def __init__(self, engine):
                self.engine = engine
                self.calls = 0

            def stream(self, *a, **kw):
                self.calls += 1
                if self.calls >= 2:
                    # 模拟重载线程结束并失败
                    self.engine._auto_reloading = False
                    self.engine.server_status = "error"
                    self.engine.status_message = "模型文件不存在"
                raise httpx.ConnectError("refused")

        kind, err = self._run_stream_error(eng, client=_ReloadFailsClient(eng))
        self.assertIn("自动重载失败", str(err))


class TestDeadProcessKeepsModel(unittest.TestCase):
    """get_status 轮询发现引擎子进程死亡：必须保留模型记录并自动重载，
    不得清空 current_model_id（否则排队中的请求因"无模型"被秒死）。"""

    def _eng(self):
        from local_llm import LocalLLMEngine
        eng = LocalLLMEngine.__new__(LocalLLMEngine)
        eng._process = type("_DeadProc", (), {"returncode": -9, "poll": lambda s: -9})()
        eng.server_status = "running"
        eng.current_model_id = "m1"
        eng._explicit_stop = False
        eng._auto_reloading = False
        return eng

    def test_dead_process_keeps_model_and_reloads(self):
        from local_llm import LocalLLMEngine
        eng = self._eng()
        reloads = []
        eng._request_reload = lambda mid: reloads.append(mid)
        LocalLLMEngine._handle_dead_process(eng)
        self.assertEqual(reloads, ["m1"])              # 自动重载已触发
        self.assertEqual(eng.current_model_id, "m1")   # 模型记录保留

    def test_dead_process_no_model_no_reload(self):
        from local_llm import LocalLLMEngine
        eng = self._eng()
        eng.current_model_id = ""
        reloads = []
        eng._request_reload = lambda mid: reloads.append(mid)
        LocalLLMEngine._handle_dead_process(eng)
        self.assertEqual(reloads, [])


class TestHealthProbeUsesRealModel(unittest.TestCase):
    """健康检查必须用真实模型 id 而非假名 "health-check"：
    假名会让 mlx_lm.server 去 HuggingFace Hub 按名解析 → 镜像 SSL 校验失败 →
    健康检查对健康引擎也报死，挂起检测整条链失明（14:22 事故根因之一）。"""

    def test_probe_sends_real_model_id(self):
        import json
        import urllib.request
        from local_llm import LocalLLMEngine
        eng = LocalLLMEngine.__new__(LocalLLMEngine)
        eng.server_port = 1235
        eng.current_model_id = "/models/real-gguf"
        eng.current_model_name = "real"
        captured = {}

        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(
                    {"choices": [{"message": {"content": "ok"}}]}).encode()

        def fake_urlopen(req, timeout, context=None):
            captured["body"] = json.loads(req.data.decode())
            return _FakeResp()

        eng._probe_port = lambda port, timeout=1: True
        original = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        try:
            ok = LocalLLMEngine.verify_engine_health(eng)
        finally:
            urllib.request.urlopen = original
        self.assertTrue(ok)
        self.assertEqual(captured["body"]["model"], "/models/real-gguf")
        self.assertNotEqual(captured["body"]["model"], "health-check")


class TestAutoReloadClearsExplicitStop(unittest.TestCase):
    """自动重载失败 ≠ 用户手动停止：_auto_reload 失败后必须复位 _explicit_stop，
    否则排队请求收到误导性的"已被手动停止"（14:41 事故回归）。"""

    def test_auto_reload_failure_resets_explicit_stop(self):
        from local_llm import LocalLLMEngine
        eng = LocalLLMEngine.__new__(LocalLLMEngine)
        eng._explicit_stop = False
        eng._auto_reloading = True
        eng.server_status = "running"

        def fail_start(mid):
            eng._explicit_stop = True  # 模拟失败路径经 stop_model 置位
            eng.server_status = "error"

        eng.start_model = fail_start
        LocalLLMEngine._auto_reload(eng, "m1")
        self.assertFalse(eng._auto_reloading)
        self.assertFalse(eng._explicit_stop)  # 已复位
        self.assertEqual(eng.server_status, "error")


class TestMergeSystemMessages(unittest.TestCase):
    """mlx_lm.server v0.31 只接受一个 system 且必须在最前面，任何位置的
    第二个 system 都 404（"System message must be at the beginning"）。
    nudge 轮在尾部追加 system 是任务迭代 2+ 全 404 的真凶（16:16 实测
    roles=[system,user,assistant,system]）——所有 system 必须合并。"""

    def test_merges_leading_systems(self):
        from agent_loop import _merge_system_messages
        msgs = [
            {"role": "system", "content": "A"},
            {"role": "system", "content": "B"},
            {"role": "user", "content": "hi"},
        ]
        out = _merge_system_messages(msgs)
        self.assertEqual(out[0], {"role": "system", "content": "A\n\nB"})
        self.assertEqual([m.get("role") for m in out], ["system", "user"])

    def test_merges_trailing_system(self):
        """nudge 轮的真实形态：尾部 system 必须并入开头（16:16 事故）。"""
        from agent_loop import _merge_system_messages
        msgs = [
            {"role": "system", "content": "A"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "x"},
            {"role": "system", "content": "不要写执行计划，直接行动。"},
        ]
        out = _merge_system_messages(msgs)
        self.assertEqual([m.get("role") for m in out], ["system", "user", "assistant"])
        self.assertIn("A", out[0]["content"])
        self.assertIn("直接行动", out[0]["content"])

    def test_single_system_unchanged(self):
        from agent_loop import _merge_system_messages
        msgs = [{"role": "system", "content": "A"}, {"role": "user", "content": "hi"}]
        self.assertEqual(_merge_system_messages(msgs), msgs)

    def test_no_system_unchanged(self):
        from agent_loop import _merge_system_messages
        msgs = [{"role": "user", "content": "hi"}]
        self.assertEqual(_merge_system_messages(msgs), msgs)


class TestGetApiUrlNeverEmpty(unittest.TestCase):
    """修复 1：端口活但引擎不健康时，get_api_url 不得返回空串。"""

    def _eng(self):
        from local_llm import LocalLLMEngine
        eng = LocalLLMEngine.__new__(LocalLLMEngine)
        eng.server_port = 1235
        eng._external_engine = ""
        eng._external_url = ""
        eng.is_running = lambda: False
        return eng

    def test_unhealthy_engine_returns_url(self):
        from local_llm import LocalLLMEngine
        eng = self._eng()
        eng._probe_port = lambda port, timeout=1: True
        eng.ensure_engine_healthy = lambda force=False: False  # 已处置完（内部已杀+重载）
        url = LocalLLMEngine.get_api_url(eng)
        self.assertEqual(url, "http://127.0.0.1:1235/v1")
        self.assertTrue(url.startswith("http://"))  # 有协议，不再 UnsupportedProtocol

    def test_healthy_engine_returns_url(self):
        from local_llm import LocalLLMEngine
        eng = self._eng()
        eng._probe_port = lambda port, timeout=1: True
        eng.ensure_engine_healthy = lambda force=False: True
        eng.server_status = "stopped"
        eng._active_backend = "mlx"
        eng.status_message = ""
        url = LocalLLMEngine.get_api_url(eng)
        self.assertEqual(url, "http://127.0.0.1:1235/v1")


class TestEngineStateRestoreValidation(unittest.TestCase):
    """修复 6：状态文件里的假路径必须被丢弃（test-35b 污染事件）。"""

    def setUp(self):
        import tempfile
        from local_llm import LocalLLMEngine
        self._tmp = tempfile.TemporaryDirectory()
        LocalLLMEngine._engine_state_file = (
            __import__("pathlib").Path(self._tmp.name) / ".engine_state.json")
        self.addCleanup(self._tmp.cleanup)

    def _eng(self):
        from local_llm import LocalLLMEngine
        eng = LocalLLMEngine.__new__(LocalLLMEngine)
        eng.current_model_id = ""
        eng.current_model_name = ""
        eng._active_backend = ""
        eng.server_port = 1235
        return eng

    def test_fake_slash_path_discarded(self):
        from local_llm import LocalLLMEngine
        eng = self._eng()
        eng.current_model_id = "/models/test-35b"
        eng.current_model_name = "test"
        eng._active_backend = "mlx"
        LocalLLMEngine._save_engine_state(eng)
        eng2 = self._eng()
        LocalLLMEngine._restore_engine_state(eng2)
        self.assertEqual(eng2.current_model_id, "")
        # 状态文件也应被清掉
        self.assertFalse(LocalLLMEngine._engine_state_file.exists())

    def test_real_path_restored(self):
        import os
        from local_llm import LocalLLMEngine
        real = os.path.join(self._tmp.name, "m.gguf")
        open(real, "w").close()
        eng = self._eng()
        eng.current_model_id = real
        eng.current_model_name = "real"
        eng._active_backend = "llama-cpp"
        LocalLLMEngine._save_engine_state(eng)
        eng2 = self._eng()
        LocalLLMEngine._restore_engine_state(eng2)
        self.assertEqual(eng2.current_model_id, real)

