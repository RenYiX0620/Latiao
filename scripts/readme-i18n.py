#!/usr/bin/env python3
"""Render README.md / README.zh.md and CONTRIBUTING.md / CONTRIBUTING.zh.md
from their .i18n.yaml sources (dsh-style single-source translation workflow).

Usage:  python3 scripts/readme-i18n.py

Only the .i18n.yaml files are edited by hand; the generated .md files are
committed so GitHub renders them directly. Keep all languages in sync in the
source, re-run the script, commit the outputs.
"""
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("PyYAML is required: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

ROOT = Path(__file__).resolve().parent.parent

# source yaml -> base filename (en output uses the bare name, others get a lang suffix)
DOCS = {
    "README.i18n.yaml": "README",
    "CONTRIBUTING.i18n.yaml": "CONTRIBUTING",
}


def lang_switch_line(lang: str, base: str) -> str:
    """Language switcher injected at the top of each rendered file."""
    if base == "README":
        other = "README.zh.md" if lang == "en" else "README.md"
        other_label = "中文" if lang == "en" else "English"
        self_label = "English" if lang == "en" else "中文"
    else:
        other = "CONTRIBUTING.zh.md" if lang == "en" else "CONTRIBUTING.md"
        other_label = "中文" if lang == "en" else "English"
        self_label = "English" if lang == "en" else "中文"
    return f"> **{self_label}** | [{other_label}]({other})\n\n"


def render(src_name: str, base: str) -> list[tuple[str, str]]:
    data = yaml.safe_load((ROOT / src_name).read_text(encoding="utf-8"))
    header = (data.get("header") or "").strip()
    langs = data["languages"]
    blocks = data["blocks"]
    out = []
    for lang in langs:
        parts = [lang_switch_line(lang, base)]
        if header:
            parts.append(header)
        for block in blocks:
            text = block.get(lang)
            if not text or not text.strip():
                continue
            parts.append(text.strip())
        fname = f"{base}.md" if lang == data.get("default_lang", "en") else f"{base}.{lang}.md"
        out.append((fname, "\n\n".join(parts) + "\n"))
    return out


def main() -> None:
    for src_name, base in DOCS.items():
        src = ROOT / src_name
        if not src.exists():
            print(f"skip missing {src_name}")
            continue
        for fname, content in render(src_name, base):
            target = ROOT / fname
            target.write_text(content, encoding="utf-8")
            print(f"generated {fname} ({len(content.splitlines())} lines)")


if __name__ == "__main__":
    main()
