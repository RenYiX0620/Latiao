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
