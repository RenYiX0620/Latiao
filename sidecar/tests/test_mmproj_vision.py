"""识图：模型旁 mmproj*.gguf 必须被自动发现并挂上（2026-09-24）。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LATIAO_AUTH_TOKEN", "mmproj-token")


def test_find_mmproj_next_to_model(tmp_path, monkeypatch):
    import local_llm_launch_common as lc
    import local_llm_probe as probe
    models = tmp_path / "models"
    models.mkdir()
    (models / "mmproj-Hermes.gguf").write_bytes(b"x")
    model = models / "Hermes-Final.gguf"
    model.write_bytes(b"y")
    monkeypatch.setattr(probe, "MODELS_DIR", models)
    hit = lc.find_mmproj_for(str(model))
    assert hit is not None and hit.name.startswith("mmproj")


def test_find_mmproj_none_when_missing(tmp_path, monkeypatch):
    import local_llm_launch_common as lc
    import local_llm_probe as probe
    models = tmp_path / "empty"
    models.mkdir()
    model = models / "text-only.gguf"
    model.write_bytes(b"y")
    monkeypatch.setattr(probe, "MODELS_DIR", models)
    assert lc.find_mmproj_for(str(model)) is None


def test_native_cmd_gets_mmproj(tmp_path, monkeypatch):
    import local_llm_engine as eng_mod
    import local_llm_launch_native as nat
    import local_llm_probe as probe

    models = tmp_path / "m"
    models.mkdir()
    mm = models / "mmproj-x.gguf"
    mm.write_bytes(b"x")
    model = models / "chat.gguf"
    model.write_bytes(b"GGUF")
    monkeypatch.setattr(probe, "MODELS_DIR", models)
    monkeypatch.setattr(eng_mod, "MODELS_DIR", models)

    e = eng_mod.EngineProcess()
    monkeypatch.setattr(e, "_find_llama_server", lambda p="": tmp_path / "llama-server")
    monkeypatch.setattr(e, "_wait_for_http", lambda *a, **k: True)
    monkeypatch.setattr(e, "_save_engine_state", lambda: None)
    monkeypatch.setattr(e, "get_status", lambda: {"status": "ok"})
    monkeypatch.setattr(nat.subprocess, "Popen", lambda *a, **k: type("P", (), {
        "pid": 1, "poll": lambda self: None, "kill": lambda self: None,
        "wait": lambda self, timeout=None: 0, "stderr": None,
    })())

    res = nat.start_llama_native(e, str(model), 1235)
    assert res is not None
    assert e.has_image_support is True, "挂上 mmproj 后 has_image_support 必须为 True"


def test_hint_when_mmproj_arrived_after_load(tmp_path, monkeypatch):
    """投影器比引擎晚下完 → 卡片上要出现"重载即可识图"（2026-09-27 用户实测踩过）。"""
    import local_llm_launch_common as lc
    import local_llm_probe as probe
    models = tmp_path / "late"
    models.mkdir()
    model = models / "Ornith-35B.gguf"
    model.write_bytes(b"y")
    (models / "mmproj-Ornith-BF16.gguf").write_bytes(b"x")
    monkeypatch.setattr(probe, "MODELS_DIR", models)
    hint = lc.mmproj_hint(str(model))
    assert "mmproj-Ornith-BF16.gguf" in hint and "重新加载模型" in hint


def test_hint_when_mmproj_still_downloading(tmp_path, monkeypatch):
    import local_llm_launch_common as lc
    import local_llm_probe as probe
    models = tmp_path / "partial"
    models.mkdir()
    model = models / "m.gguf"
    model.write_bytes(b"y")
    (models / "mmproj-m.gguf.part").write_bytes(b"x")
    monkeypatch.setattr(probe, "MODELS_DIR", models)
    hint = lc.mmproj_hint(str(model))
    assert "没下完" in hint and "重新加载模型" in hint


def test_hint_silent_for_text_only(tmp_path, monkeypatch):
    """纯文本模型目录里没有 mmproj 类文件 → 不出声（不给正常模型添噪音）。"""
    import local_llm_launch_common as lc
    import local_llm_probe as probe
    models = tmp_path / "plain"
    models.mkdir()
    model = models / "text-only.gguf"
    model.write_bytes(b"y")
    (models / "tokenizer.json").write_text("{}")
    monkeypatch.setattr(probe, "MODELS_DIR", models)
    assert lc.mmproj_hint(str(model)) == ""


def test_status_message_carries_hint(tmp_path, monkeypatch):
    """get_status 把提示拼进 message —— 模型页显示的就是这个字段。"""
    import local_llm_engine as eng_mod
    import local_llm_probe as probe
    models = tmp_path / "st"
    models.mkdir()
    model = models / "m.gguf"
    model.write_bytes(b"y")
    (models / "mmproj-m.gguf").write_bytes(b"x")
    monkeypatch.setattr(probe, "MODELS_DIR", models)

    e = eng_mod.EngineProcess()
    e.server_status = "running"
    e.current_model_id = str(model)
    e.current_model_name = "m.gguf"
    e.has_image_support = False
    monkeypatch.setattr(e, "get_available_backends", lambda: ["llama-cpp-native"])
    st = e.get_status()
    assert st["mmproj_hint"] and "重新加载模型" in st["mmproj_hint"]
    assert "重新加载模型" in st["message"]


def test_status_no_hint_when_vision_on(tmp_path, monkeypatch):
    """已挂上投影器就不提示（正常情况下不打扰）。"""
    import local_llm_engine as eng_mod
    import local_llm_probe as probe
    models = tmp_path / "on"
    models.mkdir()
    model = models / "m.gguf"
    model.write_bytes(b"y")
    (models / "mmproj-m.gguf").write_bytes(b"x")
    monkeypatch.setattr(probe, "MODELS_DIR", models)

    e = eng_mod.EngineProcess()
    e.server_status = "running"
    e.current_model_id = str(model)
    e.has_image_support = True
    monkeypatch.setattr(e, "get_available_backends", lambda: ["llama-cpp-native"])
    st = e.get_status()
    assert st["mmproj_hint"] == ""
    assert "重新加载模型" not in (st["message"] or "")
