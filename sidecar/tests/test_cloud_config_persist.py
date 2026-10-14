"""兜底持久化收紧的回归（2026-10-11）：只更新、不新增。

背景：chat 路由此前会把**任意请求体**里的 cloud_config append 进 config.json 的
cloud_models——任一能说这个 API 的进程都能往持久配置里塞 endpoint/key。这正是
10-11 覆盖事故的放大器：测试夹具的假 cloud_config（端到端走真实路由）就是这样
把假条目写进用户真配置的。收紧后：只更新已存在且 endpoint 一致的条目。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LATIAO_AUTH_TOKEN", "persist-token")

SEED = {
    "mx_api_key": "mkt_real",
    "tts": {"enabled": True},
    "cloud_models": [{"name": "mimo", "endpoint": "https://api.xiaomimimo.com/v1",
                      "key": "sk-real", "protocol": "openai"}],
}


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv("LATIAO_TEST_PROGRESS_DIR", str(tmp_path))
    import importlib

    import config as cfg_mod
    importlib.reload(cfg_mod)
    target = Path(str(cfg_mod.CONFIG_FILE))
    assert target.parent == tmp_path  # 确认指向临时目录
    target.write_text(json.dumps(SEED, ensure_ascii=False), encoding="utf-8")
    # api_routes 在模块导入时就绑定了 CONFIG_FILE（全量跑时指向别处）——
    # 必须显式改它自己的绑定，否则测试写的是另一个文件（单跑绿、全量红）。
    import api_routes
    monkeypatch.setattr(api_routes, "CONFIG_FILE", target, raising=False)
    import main as m
    m.AUTH_TOKEN = "persist-token"
    from fastapi.testclient import TestClient
    return TestClient(m.app, raise_server_exceptions=False), target


_H = {"X-Latiao-Token": "persist-token", "Content-Type": "application/json"}


def _post_chat(client, endpoint: str, key: str):
    """模拟一次带 cloud_config 的流式请求（引擎不可达无所谓——兜底持久化在路由解析之前）。"""
    body = {
        "model": "mimo", "stream": True,
        "cloud_config": {"model": "mimo", "endpoint": endpoint, "key": key, "protocol": "openai"},
        "messages": [{"role": "user", "content": "hi"}],
    }
    return client.post("/v1/chat/completions", json=body, headers=_H)


def test_unknown_endpoint_does_not_create_entry(app_client):
    """攻击载荷（未配置过的 address）不得写进 config.json。"""
    client, cfgfile = app_client
    _post_chat(client, "http://unused", "k")
    cfg = json.loads(cfgfile.read_text(encoding="utf-8"))
    assert [m["name"] for m in cfg["cloud_models"]] == ["mimo"], "新增必须被拒"
    assert cfg["mx_api_key"] == "mkt_real" and cfg["tts"]["enabled"] is True, "其余键不得被动"


def test_known_endpoint_updates_key(app_client):
    """已存在且 endpoint 一致的条目：允许更新 key（合法兜底用途）。"""
    client, cfgfile = app_client
    _post_chat(client, "https://api.xiaomimimo.com/v1", "sk-new")
    cfg = json.loads(cfgfile.read_text(encoding="utf-8"))
    assert len(cfg["cloud_models"]) == 1
    assert cfg["cloud_models"][0]["key"] == "sk-new"
    assert cfg["mx_api_key"] == "mkt_real"


def test_no_cloud_config_no_write(app_client):
    """不带 cloud_config 的普通请求不该碰配置文件（mtime/内容都不变）。"""
    client, cfgfile = app_client
    before = cfgfile.read_bytes()
    client.post("/v1/chat/completions", json={"model": "mimo", "stream": True,
                                              "messages": [{"role": "user", "content": "hi"}]},
                headers=_H)
    assert cfgfile.read_bytes() == before
