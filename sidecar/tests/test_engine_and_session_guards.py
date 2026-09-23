"""引擎级动作收口：有回合正在用本地引擎时，加载/停止模型必须被拒。

理由：加载/停止会杀掉引擎进程，正在跑的其他会话会当场断流——用户点的却是
"切换模型/停止模型"，未必知道别的会话在跑。force=true 可越过（脚本/迁移用）。
"""
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ["LATIAO_AUTH_TOKEN"] = "e2e-token"  # 必须在导入 api_routes 前设置

import api_routes  # noqa: E402


@pytest.fixture(autouse=True)
def _auth_token():
    """全量套件下 main 可能已被别的测试模块先导入（AUTH_TOKEN 为空）→ 补上。"""
    import main
    if not main.AUTH_TOKEN:
        main.AUTH_TOKEN = os.environ.get("LATIAO_AUTH_TOKEN", "e2e-token")
    yield


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(api_routes.app.router)
    # 每个请求都要带鉴权头（sidecar 中间件按请求校验）
    return TestClient(app, headers={"Authorization": "Bearer e2e-token"})


def _set_busy(monkeypatch, n: int):
    import agent.transport as transport
    monkeypatch.setattr(transport, "_gate_stats", {"capacity": 2.0, "in_flight": float(n),
                                                   "waiting": 0.0}, raising=False)


def test_start_refused_when_engine_busy(client, monkeypatch):
    _set_busy(monkeypatch, 2)
    called = []
    monkeypatch.setattr(api_routes.local_llm, "start_model",
                        lambda *a, **k: called.append(a) or {"status": "running"})
    r = client.post("/v1/local-llm/start", json={"model_id": "m1"})
    d = r.json()
    assert d["status"] == "error" and d["code"] == "engine_busy" and d["busy"] == 2
    assert called == []                       # 根本没去启动
    assert "中断" in d["message"]              # 提示要说清代价


def test_start_allowed_when_idle(client, monkeypatch):
    _set_busy(monkeypatch, 0)
    monkeypatch.setattr(api_routes.local_llm, "start_model",
                        lambda *a, **k: {"status": "running", "model_name": "m1"})
    d = client.post("/v1/local-llm/start", json={"model_id": "m1"}).json()
    assert d["status"] == "running"


def test_start_force_bypasses_guard(client, monkeypatch):
    _set_busy(monkeypatch, 1)
    monkeypatch.setattr(api_routes.local_llm, "start_model",
                        lambda *a, **k: {"status": "running", "model_name": "m1"})
    d = client.post("/v1/local-llm/start", json={"model_id": "m1", "force": True}).json()
    assert d["status"] == "running"


def test_stop_refused_when_engine_busy(client, monkeypatch):
    _set_busy(monkeypatch, 1)
    called = []
    monkeypatch.setattr(api_routes.local_llm, "stop_model",
                        lambda *a, **k: called.append(1) or {"status": "stopped"})
    d = client.post("/v1/local-llm/stop", json={}).json()
    assert d["code"] == "engine_busy" and called == []


def test_stop_allowed_when_idle_and_on_empty_body(client, monkeypatch):
    """无 body 调用（老前端/脚本）不能因为解析失败而误拒。"""
    _set_busy(monkeypatch, 0)
    monkeypatch.setattr(api_routes.local_llm, "stop_model", lambda *a, **k: {"status": "stopped"})
    d = client.post("/v1/local-llm/stop").json()
    assert d["status"] == "stopped"


def test_busy_helper_defaults_to_zero_on_broken_snapshot(monkeypatch):
    monkeypatch.setattr(api_routes, "_busy_local_turns", api_routes._busy_local_turns, raising=False)
    import agent.transport as transport
    monkeypatch.setattr(transport, "stream_gate_snapshot",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert api_routes._busy_local_turns() == 0


def test_subagents_endpoint_filters_by_session(monkeypatch):
    """活动栏按会话过滤：A 会话查不到 B 会话的子任务。"""
    from agent import subagent as sa
    monkeypatch.setattr(sa, "_SUBTASKS", {
        "sub_1": {"agent": "x", "task": "t1", "status": "running", "steps": 1,
                  "result": "", "started_at": 0.0, "updated_at": __import__("time").time(),
                  "session": "sess_a"},
        "sub_2": {"agent": "y", "task": "t2", "status": "running", "steps": 1,
                  "result": "", "started_at": 0.0, "updated_at": __import__("time").time(),
                  "session": "sess_b"},
    }, raising=False)
    assert [s["id"] for s in sa._subtask_snapshot("sess_a")] == ["sub_1"]
    assert [s["id"] for s in sa._subtask_snapshot("sess_b")] == ["sub_2"]
    assert len(sa._subtask_snapshot()) == 2          # 不带会话 = 全部（旧行为）


def test_bg_result_ttl_cleanup(monkeypatch):
    """已投递/过期的后台结果通知要出队（旧实现永不出队 → 无限增长）。"""
    import time as _t
    from agent import subagent as sa
    monkeypatch.setattr(sa, "_PENDING_BG_RESULTS", {}, raising=False)
    monkeypatch.setattr(sa, "_BG_RESULT_TTL", 10.0, raising=False)
    sa.push_bg_result("sess_a", "code-reviewer", "task1", "结果1")
    first = sa.claim_bg_results("sess_a")
    assert len(first) == 1 and first[0]["delivered"] is True
    # 队列里不该无限堆积：已投递 + 未过期会留着（幂等认领），过期后清掉
    assert len(sa._PENDING_BG_RESULTS["sess_a"]) == 1
    sa._PENDING_BG_RESULTS["sess_a"][0]["ts"] = _t.time() - 3600
    assert sa.claim_bg_results("sess_a") == []
    assert "sess_a" not in sa._PENDING_BG_RESULTS


def test_bg_result_no_parent_session_dropped():
    from agent import subagent as sa
    sa.push_bg_result("", "x", "t", "r")
    assert sa._PENDING_BG_RESULTS.get("", []) == []
