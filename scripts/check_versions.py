#!/usr/bin/env python3
"""版本号一致性守卫（CI 与本地都能跑）。

历史坑：package.json / tauri.conf.json / Cargo.toml / Cargo.lock 四处版本号曾各自漂移
（0.3.17 / 0.3.18 / 0.3.41 并存过），发版时靠人肉对齐。这里固化成一条命令。

用法：python3 scripts/check_versions.py
退出码 0 = 四处一致；1 = 不一致（打印差异）。
"""
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def read_versions() -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        out["package.json"] = json.loads((ROOT / "package.json").read_text("utf-8"))["version"]
    except Exception as e:
        out["package.json"] = f"<读取失败 {e}>"
    try:
        out["src-tauri/tauri.conf.json"] = json.loads(
            (ROOT / "src-tauri/tauri.conf.json").read_text("utf-8"))["version"]
    except Exception as e:
        out["src-tauri/tauri.conf.json"] = f"<读取失败 {e}>"
    try:
        m = re.search(r'^version = "([^"]+)"', (ROOT / "src-tauri/Cargo.toml").read_text("utf-8"), re.M)
        out["src-tauri/Cargo.toml"] = m.group(1) if m else "<未找到>"
    except Exception as e:
        out["src-tauri/Cargo.toml"] = f"<读取失败 {e}>"
    try:
        m = re.search(r'name = "local-ai-os"\nversion = "([^"]+)"',
                      (ROOT / "src-tauri/Cargo.lock").read_text("utf-8"))
        out["src-tauri/Cargo.lock"] = m.group(1) if m else "<未找到>"
    except Exception as e:
        out["src-tauri/Cargo.lock"] = f"<读取失败 {e}>"
    return out


def main() -> int:
    versions = read_versions()
    for k, v in versions.items():
        print(f"  {k:<28} {v}")
    uniq = set(versions.values())
    if len(uniq) != 1:
        print(f"\n❌ 版本号不一致（{len(uniq)} 个不同值）：{sorted(uniq)}", file=sys.stderr)
        print("   发版前必须四处一致：package.json / tauri.conf.json / Cargo.toml / Cargo.lock",
              file=sys.stderr)
        return 1
    print(f"\n✅ 四处一致：{next(iter(uniq))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
