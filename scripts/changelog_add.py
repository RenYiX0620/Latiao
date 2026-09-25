#!/usr/bin/env python3
"""发版流程的一步：把「用户视角发布说明」追加进 CHANGELOG.md（最新在最上）。

用法:
    python3 scripts/changelog_add.py 0.3.49 /tmp/notes.md [标题]

约定见 CHANGELOG.md 头部：同一版说明既写进 GitHub Release 的 body，也进本文件。
版本已存在时**拒绝重复追加**（退出码 1），避免手滑写两遍。
"""
from __future__ import annotations

import pathlib
import sys

CHANGELOG = pathlib.Path(__file__).resolve().parent.parent / "CHANGELOG.md"
MARKER = "## 更早版本"


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    ver = sys.argv[1].lstrip("vV")
    notes = pathlib.Path(sys.argv[2]).read_text(encoding="utf-8").strip()
    title = sys.argv[3] if len(sys.argv) > 3 else ""
    text = CHANGELOG.read_text(encoding="utf-8")
    if f"## v{ver}" in text:
        print(f"⛔ v{ver} 已存在，拒绝重复追加")
        return 1
    if MARKER not in text:
        print(f"⛔ CHANGELOG.md 缺少标记「{MARKER}」，无法定位插入点")
        return 1
    head = f"## v{ver}" + (f" — {title}" if title else "")
    block = f"{head}\n\n{notes}\n\n---\n\n"
    out = text.replace(MARKER, block + MARKER, 1)
    CHANGELOG.write_text(out, encoding="utf-8")
    print(f"✅ 已追加 v{ver} 到 CHANGELOG.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
