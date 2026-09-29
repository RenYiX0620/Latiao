"""路由级集成：走真 `/v1/chat/completions` 流式路由，轮末真落一行 turn_metrics。

**为什么单独有这个文件**：单测只证明 `record_turn()` 会写表，证明不了"接线真的通"——
本轮我自己就埋过一个只有这条路才能抓到的作用域错（`api_routes` 调用点用了内层
`_run_agent` 的形参名 `_model`/`_is_local`，外层并不存在 → 首轮真请求才 NameError，
整套单测全绿）。这里按 smoke 测的写法起真 app（TestClient）+ 桩引擎，把
"路由 → 循环 → 记账 → 表里真的多一行"整条链钉住。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LATIAO_AUTH_TOKEN", "tm-route-token")

from fastapi.testclient import TestClient  # noqa: E402

from tests.fake_engine import FakeEngine, _sse  # noqa: E402


@pytest.fixture(scope="module")
def client():
    import main as m
    m.AUTH_TOKEN = "tm-route-token"
    return TestClient(m.app, raise_server_exceptions=False)


def _cloud_endpoint(engine_url: str) -> str:
    """cloud_config.endpoint 要 base URL——侧车自己会拼 /chat/completions。"""
    return engine_url.rsplit("/chat/completions", 1)[0]


def _usage_round(prompt: int, completion: int) -> list[str]:
    return [
        _sse({"choices": [{"delta": {"role": "assistant", "content": "你好，这是一句正常回答。"},
                           "finish_reason": "stop", "index": 0}],
              "usage": {"prompt_tokens": prompt, "completion_tokens": completion}}),
        "data: [DONE]\n\n",
    ]


def test_streaming_turn_writes_turn_metrics_row(client):
    import db
    import local_llm
    import turn_metrics as tm
    db._init_db()
    sid = f"tm-route-{time.time_ns()}"
    # 127.0.0.1 的端点会被判成本地引擎 → 走 local_llm._engine。别的测试模块可能
    # 留下**桩引擎**（真机全量跑时实测：桩直接吐答复，一次 HTTP 都不发）。这里把
    # 单例换成真 wrapper（生产同款），桩就不会把请求吃在本地。
    _orig_engine = local_llm._engine
    local_llm._engine = local_llm.LocalLLMEngine()
    try:
        with FakeEngine() as engine:
            engine.push(_usage_round(1500, 25))
            r = client.post("/v1/chat/completions", json={
                "session_id": sid,
                "messages": [{"role": "user", "content": "打个招呼"}],
                "model": "fake-model", "stream": True,
                "cloud_config": {"endpoint": _cloud_endpoint(engine.url),
                                 "model": "fake-model", "key": "k"},
            }, headers={"X-Latiao-Token": "tm-route-token"})
    finally:
        # 必须在 finally 还原：post 抛异常时否则会把单例泄漏给后续所有模块
        # （2026-09-29 审计：这正是我刚在 test_app_flow 修掉的那类泄漏）
        local_llm._engine = _orig_engine
    assert r.status_code == 200, r.text[:300]
    assert len(engine.requests) == 1, \
        f"桩引擎必须真收到一次请求｜url={engine.url}｜SSE={r.text[:200]}"

    rows = [x for x in tm.list_turns(sid, limit=5) if x["session_id"] == sid]
    assert len(rows) == 1, f"一轮必须恰好一行：{rows}"
    row = rows[0]
    assert row["input_tokens"] == 1500 and row["gen_tokens"] == 25, row
    assert row["ended_reason"] == "completed" and row["model"] == "fake-model"
    assert row["by_source"]["main_turn"]["input_tokens"] == 1500, row["by_source"]
    assert row["ttft_ms"] is not None, "TTFT 要落库（哪怕是毫秒级的桩引擎）"


def test_route_does_not_500_when_engine_errors(client):
    """引擎报错（404）时：路由不得 500，仍要落一行 ended_reason 可读的记录。"""
    import turn_metrics as tm
    sid = f"tm-route-err-{time.time_ns()}"
    r = client.post("/v1/chat/completions", json={
        "session_id": sid,
        "messages": [{"role": "user", "content": "打个招呼"}],
        "model": "fake-model", "stream": True,
        # 故意指一个连不上的端点（端口 1）
        "cloud_config": {"endpoint": "http://127.0.0.1:1/v1", "model": "fake-model", "key": "k"},
    }, headers={"X-Latiao-Token": "tm-route-token"})
    assert r.status_code == 200, r.text[:300]
    body = r.text
    # 读得出的失败说明：连接类错误走 `{"error": …}` 事件，HTTP 类错误走 ⚠️ 文案
    assert ('"error"' in body or "HTTP" in body or "⚠️" in body), \
        f"要给用户可读的失败说明：{body[:200]}"
    rows = [x for x in tm.list_turns(sid, limit=5) if x["session_id"] == sid]
    assert rows, "失败回合同样要留痕（否则'哪轮最贵/最慢'会漏掉失败轮）"
    assert json.loads(json.dumps(rows[0]["by_source"])) is not None
