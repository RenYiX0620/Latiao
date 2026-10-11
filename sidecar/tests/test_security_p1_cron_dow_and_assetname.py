"""Cron DOW=7 / 更新文件名消毒（2026-10-11 深审 Medium+High）。

① 校验允许 DOW 0-7，但匹配侧周日只认 0 → `7` 与 `5-7` 这类范围永不触发。
② manifest 的 url 经镜像可控，`url.split("/")[-1]` 直接拼路径 → Windows 穿越。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LATIAO_AUTH_TOKEN", "cron-dow-token")


def _match(expr: str, dt: datetime) -> bool:
    from cron import _cron_matches
    return _cron_matches(expr, dt)


def test_dow_seven_matches_sunday():
    """`7` 是周日的合法写法，必须触发。"""
    # 2026-10-11 是周日（Python weekday=6 → cron DOW=0）
    sun = datetime(2026, 10, 11, 9, 0)
    assert sun.weekday() == 6
    assert _match("0 9 * * 7", sun), "DOW=7 应在周日触发"
    assert _match("0 9 * * 0", sun), "DOW=0 也应在周日触发"


def test_dow_range_including_seven_matches_sunday():
    """`5-7`（周五-周日）应覆盖周日。"""
    fri = datetime(2026, 10, 9, 9, 0)   # 周五
    sat = datetime(2026, 10, 10, 9, 0)  # 周六
    sun = datetime(2026, 10, 11, 9, 0)  # 周日
    assert fri.weekday() == 4 and sat.weekday() == 5 and sun.weekday() == 6
    assert _match("0 9 * * 5-7", fri)
    assert _match("0 9 * * 5-7", sat)
    assert _match("0 9 * * 5-7", sun), "5-7 应覆盖周日"


def test_dow_seven_does_not_fire_other_days():
    mon = datetime(2026, 10, 12, 9, 0)  # 周一
    assert mon.weekday() == 0
    assert not _match("0 9 * * 7", mon)
    assert not _match("0 9 * * 5-7", mon)


def test_asset_name_rejects_traversal():
    from update_service import _safe_asset_name
    assert _safe_asset_name("https://x.com/a/Latiao_0.3.77_x64-setup.exe") == "Latiao_0.3.77_x64-setup.exe"
    assert _safe_asset_name("https://x.com/..\\..\\evil.exe") == "evil.exe"
    assert _safe_asset_name("https://x.com/..%2F..%2Fevil.exe") not in ("..",)
    assert "/" not in _safe_asset_name("https://x.com/../../etc/passwd")
    assert "\\" not in _safe_asset_name("https://x.com/..\\..\\evil.exe")
    assert _safe_asset_name("https://x.com/....") == "package.bin"
    assert _safe_asset_name("https://x.com/a/b;rm -rf") == "package.bin"
    assert _safe_asset_name("") == "package.bin"
    # 正常名不被误伤
    assert _safe_asset_name("https://x.com/pkg.zip?sig=1") == "pkg.zip"
