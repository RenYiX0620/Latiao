"""技能描述规范（2026-10-19）：`Use when <触发场景>. <一句话行为>`。

外部审计报告的第 P2 条：技能描述没有"第一行写触发场景"的规范，规模化后容易误触发。
模型只读 description 决定要不要加载——写清触发条件，别只写能力名。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _descriptions():
    for f in sorted((ROOT / "skills").glob("*/SKILL.md")):
        text = f.read_text("utf-8")
        m = re.search(r"^description:\s*(.+)$", text, re.M)
        assert m, f"{f} 缺少 description"
        yield f, m.group(1).strip()


def test_every_skill_description_starts_with_use_when():
    bad = [str(f) for f, desc in _descriptions() if not desc.startswith("Use when ")]
    assert not bad, f"这些技能描述没有以 'Use when ' 开头：{bad}"


def test_every_skill_description_is_short():
    long_ones = [(str(f), len(desc)) for f, desc in _descriptions() if len(desc) > 100]
    assert not long_ones, f"描述过长（>100 字符）：{long_ones}"


def test_readme_documents_the_convention():
    readme = (ROOT / "skills" / "README.md").read_text("utf-8")
    assert "Use when <触发场景>" in readme
