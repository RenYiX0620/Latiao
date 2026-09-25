"""识图消息体不得被 prompt 尾部拼接压扁（2026-09-24 HTTP 400 根因）。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LATIAO_AUTH_TOKEN", "img-token")


def test_append_tail_keeps_image_url_parts():
    from agent.prompt_build import _append_tail_to_content
    content = [
        {"type": "text", "text": "这是啥"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA", "detail": "auto"}},
    ]
    out = _append_tail_to_content(content, "\n\n（本轮用简体中文）")
    assert isinstance(out, list)
    types = [p.get("type") for p in out if isinstance(p, dict)]
    assert "image_url" in types
    assert any(p.get("type") == "text" and "简体中文" in (p.get("text") or "") for p in out)
    assert any(p.get("type") == "text" and "这是啥" in (p.get("text") or "") for p in out)


def test_append_tail_string_content_still_works():
    from agent.prompt_build import _append_tail_to_content
    assert _append_tail_to_content("hello", "\nTAIL") == "hello\nTAIL"
