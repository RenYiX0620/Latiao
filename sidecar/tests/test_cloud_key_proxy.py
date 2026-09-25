"""云模型密钥代理化回归锁（2026-09-24）。

密钥代理化后前端不回传明文 key（只带 has_key）。锁住：
1. 空 key 保存 = 沿用已存密钥（不许抹成空串）
2. 有新 key 才覆盖
3. 测试连接可从 config 回填 key（不再要求 body 带明文）
4. 两端都没有 key 才报 "Key and endpoint required"
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# setdefault：不能覆盖其它测试模块（如 app_flow 的 e2e-token）在导入期设好的 token
os.environ.setdefault("LATIAO_AUTH_TOKEN", "cloud-key-token")

from fastapi.testclient import TestClient  # noqa: E402


def _patch_cfg(monkeypatch, tmp_path):
    """CONFIG_FILE 在多处导入时绑定，测试必须钉到同一个临时文件。"""
    import main  # noqa: F401 — 先拉起完整路由图，避免 api_routes_admin↔main 循环导入
    import api_routes_admin
    import agent_loop
    import config as config_mod
    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "CONFIG_FILE", cfg_path)
    monkeypatch.setattr(agent_loop, "CONFIG_FILE", cfg_path)
    monkeypatch.setattr(api_routes_admin, "CONFIG_FILE", cfg_path)
    return cfg_path


@pytest.fixture()
def client(tmp_path, monkeypatch):
    _patch_cfg(monkeypatch, tmp_path)
    import main as m
    # 全量套件下其他模块可能已把 AUTH_TOKEN 设成自己的值——钉回本文件的 token
    m.AUTH_TOKEN = "cloud-key-token"
    return TestClient(m.app, raise_server_exceptions=False)


@pytest.fixture()
def headers():
    return {"X-Latiao-Token": "cloud-key-token", "Content-Type": "application/json"}


def _seed(cfg_path: Path, models: list):
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({"cloud_models": models}), encoding="utf-8")


def _stored(cfg_path: Path) -> list:
    return json.loads(cfg_path.read_text(encoding="utf-8")).get("cloud_models", [])


def test_empty_key_preserves_stored_secret(client, headers, tmp_path, monkeypatch):
    cfg_path = _patch_cfg(monkeypatch, tmp_path)
    _seed(cfg_path, [{
        "name": "gpt-4o-mini", "endpoint": "https://api.openai.com/v1",
        "key": "sk-saved-secret", "protocol": "openai", "max_tokens": 4096,
    }])
    # 前端密钥代理化：key 回空串，只调 max_tokens
    resp = client.post("/v1/settings/cloud-models", headers=headers, json={
        "models": [{
            "name": "gpt-4o-mini", "endpoint": "https://api.openai.com/v1",
            "key": "", "protocol": "openai", "max_tokens": 8192,
        }],
    })
    assert resp.status_code == 200 and resp.json()["status"] == "ok"
    got = _stored(cfg_path)
    assert len(got) == 1
    assert got[0]["key"] == "sk-saved-secret", "空 key 不许抹掉已存密钥"
    assert got[0]["max_tokens"] == 8192


def test_explicit_key_overwrites(client, headers, tmp_path, monkeypatch):
    cfg_path = _patch_cfg(monkeypatch, tmp_path)
    _seed(cfg_path, [{
        "name": "gpt-4o-mini", "endpoint": "https://api.openai.com/v1",
        "key": "sk-old", "protocol": "openai",
    }])
    resp = client.post("/v1/settings/cloud-models", headers=headers, json={
        "models": [{
            "name": "gpt-4o-mini", "endpoint": "https://api.openai.com/v1",
            "key": "sk-new", "protocol": "openai",
        }],
    })
    assert resp.json()["status"] == "ok"
    assert _stored(cfg_path)[0]["key"] == "sk-new"


def test_omitted_model_is_deleted(client, headers, tmp_path, monkeypatch):
    cfg_path = _patch_cfg(monkeypatch, tmp_path)
    _seed(cfg_path, [
        {"name": "a", "endpoint": "https://a.example/v1", "key": "ka", "protocol": "openai"},
        {"name": "b", "endpoint": "https://b.example/v1", "key": "kb", "protocol": "openai"},
    ])
    resp = client.post("/v1/settings/cloud-models", headers=headers, json={
        "models": [{"name": "a", "endpoint": "https://a.example/v1", "key": "", "protocol": "openai"}],
    })
    assert resp.json()["status"] == "ok"
    names = [m["name"] for m in _stored(cfg_path)]
    assert names == ["a"]
    assert _stored(cfg_path)[0]["key"] == "ka"


def test_test_connection_fills_key_from_config(client, headers, tmp_path, monkeypatch):
    cfg_path = _patch_cfg(monkeypatch, tmp_path)
    _seed(cfg_path, [{
        "name": "gpt-4o-mini", "endpoint": "https://api.openai.com/v1",
        "key": "sk-from-store", "protocol": "openai",
    }])

    seen = {}

    class _Resp:
        status_code = 200
        text = "ok"

    class _Client:
        def __init__(self, *a, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, json=None, headers=None):
            seen["url"] = url
            seen["headers"] = headers
            seen["json"] = json
            return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    # 前端测试连接不带 key（密钥代理化）
    resp = client.post("/v1/test_connection", headers=headers, json={
        "name": "gpt-4o-mini", "model": "gpt-4o-mini",
        "endpoint": "https://api.openai.com/v1", "protocol": "openai",
    })
    data = resp.json()
    assert data["status"] == "ok", data
    assert seen["headers"].get("Authorization") == "Bearer sk-from-store"


def test_test_connection_without_any_key_still_errors(client, headers, tmp_path, monkeypatch):
    cfg_path = _patch_cfg(monkeypatch, tmp_path)
    _seed(cfg_path, [])
    resp = client.post("/v1/test_connection", headers=headers, json={
        "name": "x", "endpoint": "https://api.openai.com/v1", "protocol": "openai",
    })
    assert resp.json()["status"] == "error"
    assert "Key" in resp.json()["message"]


def test_test_connection_requires_endpoint(client, headers):
    resp = client.post("/v1/test_connection", headers=headers, json={
        "name": "x", "key": "sk", "endpoint": "", "protocol": "openai",
    })
    assert resp.json()["status"] == "error"
    assert resp.json()["message"] == "Key and endpoint required"


def test_empty_list_without_clear_is_rejected(client, headers, tmp_path, monkeypatch):
    """冷启动 GET 失败后前端曾误回写空表——必须显式 clear=true 才能清空。"""
    cfg_path = _patch_cfg(monkeypatch, tmp_path)
    _seed(cfg_path, [{"name": "a", "endpoint": "https://a.example/v1", "key": "ka", "protocol": "openai"}])
    resp = client.post("/v1/settings/cloud-models", headers=headers, json={"models": []})
    data = resp.json()
    assert data["status"] == "error" and "clear" in data["message"]
    assert _stored(cfg_path)[0]["key"] == "ka"


def test_empty_list_with_clear_wipes(client, headers, tmp_path, monkeypatch):
    cfg_path = _patch_cfg(monkeypatch, tmp_path)
    _seed(cfg_path, [{"name": "a", "endpoint": "https://a.example/v1", "key": "ka", "protocol": "openai"}])
    resp = client.post("/v1/settings/cloud-models", headers=headers,
                       json={"models": [], "clear": True})
    assert resp.json()["status"] == "ok"
    assert _stored(cfg_path) == []
