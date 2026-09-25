"""2026-09-24 语义审计 P0 回归锁。

1. 门面写遮蔽：`_engine.model_token_limit = x` 必须落到 EngineProcess
2. `_find_gguf` 缓存兜底不得因 _cache_dir 归属错位 AttributeError
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LATIAO_AUTH_TOKEN", "p0-token")


def test_facade_setattr_forwards_to_engine():
    from local_llm import LocalLLMEngine
    from local_llm_engine import EngineProcess

    facade = LocalLLMEngine()
    assert isinstance(facade.engine, EngineProcess)
    old = facade.engine.model_token_limit
    try:
        facade.model_token_limit = 12345
        assert facade.engine.model_token_limit == 12345, "写遮蔽：set 落在门面上"
        assert facade.model_token_limit == 12345
        facade.mlx_available = True
        assert facade.engine.mlx_available is True
    finally:
        facade.engine.model_token_limit = old


def test_find_gguf_cache_fallback_no_attribute_error(tmp_path, monkeypatch):
    """模型不在 ~/Models、LM Studio、Ollama 时走 HF 缓存兜底，不得 500。"""
    import local_llm_engine as eng_mod

    fake_home = tmp_path / "home"
    (fake_home / ".cache" / "huggingface" / "models").mkdir(parents=True)
    (fake_home / "Models").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setattr(eng_mod, "MODELS_DIR", fake_home / "Models")

    e = eng_mod.EngineProcess()
    # 组合体上 _cache_dir 属于 ModelDownloader；EngineProcess 不该靠它
    assert not hasattr(e, "_cache_dir")
    out = e._find_gguf_uncached("definitely-not-a-real-model-xyz")
    assert out is None
