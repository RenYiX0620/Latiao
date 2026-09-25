"""测试共享配置（conftest）。

关键：场景测试会真实执行 agent 循环并写入进度/DB/memory 等文件——
pytest_configure 在收集/导入任何测试模块之前把 PROGRESS_DIR 重定向到
临时目录（config.py 支持 LATIAO_TEST_PROGRESS_DIR 覆盖），避免污染真机
~/.local-ai-os（此前 PROGRESS.md 被场景测试越写越大，见阶段 2b 基线提交）。
"""
import os
import tempfile


def pytest_configure(config):
    if not os.environ.get("LATIAO_TEST_PROGRESS_DIR"):
        os.environ["LATIAO_TEST_PROGRESS_DIR"] = tempfile.mkdtemp(
            prefix="latiao-test-progress-")

# ── cron 测试共享夹具（2026-09-23 审计：夹具原先定义在 test_cron_final_round.py、
#    另一个测试文件靠 import 复用 → ruff F811（参数名遮蔽导入名）。夹具放 conftest
#    是 pytest 的正规做法：所有测试模块自动可见，无需 import。──
import asyncio
import json

import httpx
import pytest


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("POST", "http://127.0.0.1:9999/v1/chat/completions"),
                response=httpx.Response(self.status_code))


class _FakeClient:
    """记录每次请求体，按脚本返回响应（脚本按引用共享，见 set_script）。"""

    def __init__(self, script, seen):
        self._script = script
        self._seen = seen

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None, **kw):
        self._seen.append(json)
        item = self._script.pop(0) if self._script else {"choices": [{"message": {"content": "x"}}]}
        if isinstance(item, Exception):
            raise item
        # 裸 dict 也要包成响应对象（cron 会调 .status_code/.raise_for_status()）
        return item if hasattr(item, "raise_for_status") else _Resp(item)


def _tool_call_msg(name="list_dir", args=None, cid="c1"):
    return {"choices": [{"message": {
        "role": "assistant", "content": "",
        "tool_calls": [{"id": cid, "type": "function",
                        "function": {"name": name,
                                     "arguments": json.dumps(args or {"path": "."})}}]}}]}


# 一段"够长、不像过渡句"的可交付正文（≥120 字，供收口用例使用）
_LONG_FINAL = (
    "该配置包含四项顶层键：cloud_models 用于云端模型接入，custom_engine 用于自定义引擎，"
    "tts 控制本地语音合成，tavily_api_key 供联网搜索使用。其中 cloud_models 与 "
    "custom_engine 是数组结构，tts 是对象结构，tavily_api_key 为字符串。")


def _text_msg(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


@pytest.fixture()
def cron_env(monkeypatch, tmp_path):
    """把 cron 变成"本地引擎在跑 + 假 HTTP"的可测环境。"""
    import agent_loop
    import cron                      # 夹具内导入：模块级导入会早于 PROGRESS_DIR 重定向
    import local_llm

    seen: list[dict] = []
    recorded: list[tuple] = []
    script_holder: list = []          # 就地替换脚本（保持 _FakeClient 的引用）

    class _Engine:
        current_model_id = "stub-model"
        current_model_name = "stub-model"
        _launched_slots = 2

        def is_running(self):
            return True

    class _LLM:
        _engine = _Engine()

        def get_api_url(self):
            return "http://127.0.0.1:9999/v1/chat/completions"

    # 只换 AsyncClient：cron 的 except 子句要读 httpx.TransportError/HTTPStatusError，
    # 整个模块换掉会 AttributeError（首版踩过）
    import httpx as _real_httpx
    monkeypatch.setattr(_real_httpx, "AsyncClient",
                        lambda **kw: _FakeClient(script_holder, seen))
    monkeypatch.setattr(local_llm, "_engine", _Engine(), raising=False)

    async def _resolve_target(cloud):
        # ("protocol", "api_url", "headers", "is_local")
        return ("openai", "http://127.0.0.1:9999/v1/chat/completions", {}, True)
    monkeypatch.setattr(agent_loop, "_resolve_api_target", _resolve_target, raising=False)

    async def _fake_exec(tool_name, args):
        return f"[stub {tool_name}]"
    monkeypatch.setattr(agent_loop, "execute_tool", _fake_exec, raising=False)
    monkeypatch.setattr(cron, "execute_tool", _fake_exec, raising=False)

    monkeypatch.setattr(cron, "_record_cron_result",
                        lambda job, status, content: recorded.append((status, content)), raising=False)

    async def _no_sleep(_s):
        return None
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    def _set_script(items):
        script_holder[:] = list(items)

    return {"seen": seen, "recorded": recorded, "set_script": _set_script}


def _job():
    return {"id": "job1", "task": "看看目录", "schedule": "0 9 * * *"}


