"""引擎启动参数（local_llm）：并发槽位显式化的验收测试。

09-23 前：`-c 64000` 且无 `--parallel` → 该构建按 auto（n_parallel=4 且 kv_unified）
处理，4 个槽**共用**一个 64000 池，多会话同跑时每路只分到 1/4 窗口。
09-23 起：显式 `--parallel N` + `-c = N × 每会话窗口`，每槽独占完整窗口。

这里不真起进程：mock subprocess.Popen 捕获命令行，并让健康检查立即失败。
"""
import json
import unittest.mock as mock
from pathlib import Path

import local_llm_engine


class _StubEngine:
    """只提供 _start_llama_native 需要的最小属性/方法。"""

    def __init__(self, slots=2, ctx=64000, healthy=True):
        self.parallel_slots = slots
        self.model_token_limit = ctx
        self.n_gpu_layers = -1
        self.current_model_name = "stub-model"
        self.current_model_id = "stub-model"
        self.server_status = "stopped"
        self.status_message = ""
        self.server_port = 1234
        self._launched_slots = 1
        self._process = None
        self._explicit_stop = False
        self._auto_reloading = False
        self._cancel_load = None
        self._active_backend = ""
        self._healthy = healthy

    # 默认"启动成功"（避免真等 900s）；healthy=False 时走失败路径
    def _wait_for_http(self, *a, **k):
        return self._healthy

    def _guess_chat_format(self, *a, **k):
        return ""

    def stop_model(self):
        pass

    def get_status(self):
        return {"status": self.server_status, "message": self.status_message}


def _capture(engine, model_path="/tmp/models/Test-Model.gguf", **kw):
    """跑一次 _start_llama_native，返回捕获到的命令行。"""
    captured = {}

    class _Proc:
        stderr = None

        def poll(self):
            return None

    def _popen(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        # 加载中的状态文案（用户能看到的那句）
        captured["status_at_spawn"] = engine.status_message
        return _Proc()

    with mock.patch("subprocess.Popen", side_effect=_popen):
        local_llm_engine.EngineProcess._start_llama_native(
            engine, model_path, 1234, exe="/tmp/fake-llama-server", **kw)
    return captured.get("cmd", [])


def _flag(cmd, name):
    return cmd[cmd.index(name) + 1] if name in cmd else None


def test_native_2_slots_splits_context():
    """自带引擎 + 2 槽：-c = 2 × 窗口，且显式 --parallel 2。"""
    eng = _StubEngine(slots=2, ctx=64000)
    cmd = _capture(eng)
    assert _flag(cmd, "--parallel") == "2"
    assert _flag(cmd, "-c") == str(64000 * 2)
    assert eng._launched_slots == 2


def test_native_1_slot_keeps_old_behavior():
    """slots=1：不加 --parallel，-c 就是窗口本身（与老版本一致）。"""
    eng = _StubEngine(slots=1, ctx=64000)
    cmd = _capture(eng)
    assert "--parallel" not in cmd
    assert _flag(cmd, "-c") == "64000"
    assert eng._launched_slots == 1


def test_slots_clamped_to_4():
    """配置里写再大也钳到 4（KV 内存按槽位线性增长）。"""
    assert local_llm_engine.EngineProcess._resolve_parallel_slots(
        type("E", (), {"model_token_limit": 1})()) in (1, 2, 3, 4)


def test_resolve_slots_env_wins(monkeypatch):
    """环境变量 LATIAO_LLM_SLOTS 优先于 config.json；非法值回退默认 2。"""
    eng = _StubEngine()
    monkeypatch.setenv("LATIAO_LLM_SLOTS", "3")
    assert local_llm_engine.EngineProcess._resolve_parallel_slots(eng) == 3
    monkeypatch.setenv("LATIAO_LLM_SLOTS", "99")
    assert local_llm_engine.EngineProcess._resolve_parallel_slots(eng) == 4
    monkeypatch.setenv("LATIAO_LLM_SLOTS", "abc")
    assert local_llm_engine.EngineProcess._resolve_parallel_slots(eng) == 2
    monkeypatch.delenv("LATIAO_LLM_SLOTS")


def test_resolve_slots_from_config(monkeypatch):
    """没设环境变量时读 config.json 的 local_llm.slots。"""
    eng = _StubEngine()
    monkeypatch.delenv("LATIAO_LLM_SLOTS", raising=False)
    monkeypatch.setattr(local_llm_engine, "_read_config", lambda: {"local_llm": {"slots": 3}})
    assert local_llm_engine.EngineProcess._resolve_parallel_slots(eng) == 3
    monkeypatch.setattr(local_llm_engine, "_read_config", lambda: {"local_llm": {"slots": "x"}})
    assert local_llm_engine.EngineProcess._resolve_parallel_slots(eng) == 2


def test_custom_engine_never_gets_parallel_flag():
    """第三方 fork：不代加 --parallel（fork 可能不认 → 启动即被拒），容量退化 1。"""
    eng = _StubEngine(slots=2, ctx=64000)
    cmd = _capture(eng, backend="llama-cpp-custom", extra_args=["-fa", "on"])
    assert "--parallel" not in cmd
    assert _flag(cmd, "-c") == "64000"      # 总量不放大
    assert eng._launched_slots == 1
    assert _flag(cmd, "-fa") == "on"        # 用户自己的参数照旧追加


def test_custom_engine_honors_explicit_parallel():
    """用户自己在 custom_engine.args 里写了 --parallel N：照用，容量跟上。"""
    eng = _StubEngine(slots=2, ctx=64000)
    cmd = _capture(eng, backend="llama-cpp-custom",
                   extra_args=["--parallel", "3"])
    assert _flag(cmd, "--parallel") == "3"
    assert eng._launched_slots == 3


def test_slots_reset_on_failed_launch():
    """启动失败 → 容量复位 1（引擎没起来，别让 transport 以为有多个槽）。"""
    eng = _StubEngine(slots=2, ctx=64000, healthy=False)
    _capture(eng)                       # _wait_for_http 返回 False，即失败路径
    assert eng._launched_slots == 1


def test_loading_status_shows_slots():
    """加载中的状态文案要告诉用户"几并发 × 多大上下文"（用户看的就是这句）。"""
    eng = _StubEngine(slots=2, ctx=64000)
    captured = {}

    class _Proc:
        stderr = None

        def poll(self):
            return None

    def _popen(cmd, **kwargs):
        captured["status"] = eng.status_message
        return _Proc()

    with mock.patch("subprocess.Popen", side_effect=_popen):
        local_llm_engine.EngineProcess._start_llama_native(
            eng, "/tmp/models/M.gguf", 1234, exe="/tmp/fake-llama-server")
    assert "2 并发" in captured["status"] and "64000" in captured["status"]

def test_kv_cache_flags_preserved():
    """并发改造不许动 KV 量化（q4_0 是显存/内存的关键）。"""
    eng = _StubEngine(slots=2)
    cmd = _capture(eng)
    assert _flag(cmd, "--cache-type-k") in ("q4_0", "q8_0", "f16")
    assert _flag(cmd, "--cache-type-v") in ("q4_0", "q8_0", "f16")
    assert _flag(cmd, "-ngl") == "999"      # -1 映射为 999（09-08 兼容修复）


def test_no_dangling_flags_in_cmd():
    """命令行基本形态：模型/端口/主机齐备，没有重复的 -c。"""
    eng = _StubEngine(slots=2)
    cmd = _capture(eng)
    assert cmd[0].endswith("fake-llama-server")
    assert _flag(cmd, "-m").endswith(".gguf")
    assert cmd.count("-c") == 1
    assert json.dumps(cmd)      # 全部参数可序列化（无对象混进 argv）
    assert Path("/tmp/fake-llama-server").name in cmd[0]
