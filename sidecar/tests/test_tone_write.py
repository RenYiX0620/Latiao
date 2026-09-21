"""语气写入路径单测（09-21）。

修的问题：`style` 意图映射到追加式的 `_apply_style_change`，语气被写成文件末尾一条
裸行（实测 SOUL.md 尾部出现 `- 暧昧但简洁`，落在 `## 输出规范` 下面），而按位置、
按替换写好的 `_apply_tone_change` 从未被调用。同时它没进幂等检查 —— 用户重复同一句
语气会每轮触发"身份已更新"注入，把前缀缓存打掉。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import identity  # noqa: E402


def _soul(tmp_path, body="# Soul\n\n## 语气风格\n- 简洁直接\n"):
    p = tmp_path / "SOUL.md"
    p.write_text(body, encoding="utf-8")
    return p


def test_style_maps_to_structured_tone_writer():
    assert identity._INTENT_APPLIERS["style"] is identity._apply_tone_change


def test_tone_lands_under_its_section(tmp_path, monkeypatch):
    monkeypatch.setattr(identity, "PROGRESS_DIR", tmp_path)
    p = _soul(tmp_path)
    identity._apply_tone_change("露骨直白")
    text = p.read_text(encoding="utf-8")
    assert "## 语气风格\n- 对话语气：露骨直白" in text
    # 不该像旧路径那样把裸行追加到文件末尾
    assert not text.rstrip().endswith("- 露骨直白")


def test_tone_replaces_instead_of_stacking(tmp_path, monkeypatch):
    monkeypatch.setattr(identity, "PROGRESS_DIR", tmp_path)
    p = _soul(tmp_path)
    for value in ("暧昧", "露骨直白", "温柔"):
        identity._apply_tone_change(value)
    text = p.read_text(encoding="utf-8")
    assert text.count("对话语气：") == 1
    assert "对话语气：温柔" in text


def test_tone_created_when_section_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(identity, "PROGRESS_DIR", tmp_path)
    p = _soul(tmp_path, "# Soul\n\n随便写点。\n")
    identity._apply_tone_change("直白")
    assert "## 语气风格\n- 对话语气：直白" in p.read_text(encoding="utf-8")


def test_repeated_tone_is_idempotent(tmp_path, monkeypatch):
    """同一句语气说两遍：第二遍不再写文件、也不再报告"身份已更新"。"""
    monkeypatch.setattr(identity, "PROGRESS_DIR", tmp_path)
    p = _soul(tmp_path)
    text = "以后回复我的时候温柔一点"
    first = identity._process_identity_intents(text)
    assert first and "style=" in first
    before = p.read_text(encoding="utf-8")
    second = identity._process_identity_intents(text)
    assert second is None, f"重复同一句不该再报变更：{second}"
    assert p.read_text(encoding="utf-8") == before
