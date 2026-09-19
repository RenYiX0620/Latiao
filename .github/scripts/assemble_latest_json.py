#!/usr/bin/env python3
"""汇总两个平台的签名，生成 updater 用的 latest.json。

背景（v0.3.30 实测）：macOS 与 Windows 两个 job 并行跑，历史实现里两边都写
latest.json——Windows 会先下载 macOS 生成的清单再补 windows-x86_64 条目回传，
但 macOS 的"上传发布资产"步骤可能晚于这一步执行，把合并结果覆盖回仅 darwin 的
版本，导致 Windows 用户收不到更新。现在改为两个 job 都完成后由独立 job 调用本
脚本一次性生成，两个平台缺一即失败。

用法：python3 assemble_latest_json.py <version> <tag> <sig 目录> <输出路径>
（环境变量 GITHUB_REPOSITORY 用于拼下载地址）
"""
import json
import os
import pathlib
import sys
from datetime import datetime, timezone

MAC_SUFFIX = ".app.tar.gz.sig"
WIN_SUFFIX = "-setup.exe.sig"


def assemble(version: str, tag: str, sig_dir: pathlib.Path, repo: str) -> dict:
    sigs = {p.name: p.read_text(encoding="utf-8").strip()
            for p in sig_dir.iterdir() if p.name.endswith(".sig")}
    mac = next(((k, v) for k, v in sigs.items() if k.endswith(MAC_SUFFIX)), None)
    win = next(((k, v) for k, v in sigs.items() if k.endswith(WIN_SUFFIX)), None)
    if not mac or not win:
        raise SystemExit(f"缺少签名文件：mac={bool(mac)} windows={bool(win)}；"
                         f"目录内为 {sorted(sigs)}")
    if not mac[1] or not win[1]:
        raise SystemExit("签名文件为空——不要发布一个没有签名的更新清单")
    base = f"https://github.com/{repo}/releases/download/{tag}"
    return {
        "version": version,
        "notes": f"Latiao v{version}",
        "pub_date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "platforms": {
            "darwin-aarch64": {"signature": mac[1], "url": f"{base}/{mac[0][:-4]}"},
            "windows-x86_64": {"signature": win[1], "url": f"{base}/{win[0][:-4]}"},
        },
    }


def main() -> int:
    if len(sys.argv) != 5:
        raise SystemExit(__doc__)
    version, tag, sig_dir, out_path = sys.argv[1:5]
    repo = os.environ.get("GITHUB_REPOSITORY", "RenYiX0620/Latiao")
    manifest = assemble(version, tag, pathlib.Path(sig_dir), repo)
    pathlib.Path(out_path).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
