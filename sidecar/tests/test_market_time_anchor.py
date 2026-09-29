"""行情时间锚 / int_ 假源清洗 / 承诺闸门（2026-09-28 美股事故）。

事故复盘：模型心算时区 4 轮全错（开盘中说成未开盘/已收盘、AM/PM 颠倒）；
网页搜索撞上新浪 int_ 冻结快照（道指 46247 vs 真实 51828）；「收盘了叫你」
却 cron.json=[]。修法：代码给时间事实、封杀 int_、承诺必须 create_cron。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LATIAO_AUTH_TOKEN", "verify-token")


def test_market_anchor_open_status():
    from agent.prompt_build import _market_time_anchor

    # 北京 2026-09-28 22:24（周一）= 美东 10:24 AM → 开盘中
    bj = datetime(2026, 9, 28, 22, 24, tzinfo=ZoneInfo("Asia/Shanghai"))
    anchor = _market_time_anchor(bj)
    assert "北京 2026-09-28 22:24" in anchor
    assert "美东 2026-09-28 10:24 AM" in anchor
    assert "开盘中" in anchor
    assert "已开盘 54 分钟" in anchor
    assert "禁止自行推算" in anchor or "不要自行推算" in anchor


def test_market_anchor_before_open_and_closed():
    from agent.prompt_build import _market_time_anchor

    # 美东 08:00 → 未开盘
    bj = datetime(2026, 9, 28, 20, 0, tzinfo=ZoneInfo("Asia/Shanghai"))  # ET 08:00
    assert "未开盘" in _market_time_anchor(bj)
    # 美东 17:00 → 已收盘
    bj2 = datetime(2026, 9, 29, 5, 0, tzinfo=ZoneInfo("Asia/Shanghai"))  # ET 17:00 prev day? 9/29 05:00 BJ = 9/28 17:00 ET
    assert "已收盘" in _market_time_anchor(bj2)


def test_market_anchor_weekend():
    from agent.prompt_build import _market_time_anchor

    bj = datetime(2026, 9, 26, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))  # Saturday
    assert "周末休市" in _market_time_anchor(bj)


def test_market_keyword_detection():
    from agent.prompt_build import _MARKET_KW_RE, _PROMISE_GATE_NOTE

    for q in ("你看看美股现在是啥情况", "今天的A股大盘", "期指怎么样", "nasdaq futures"):
        assert _MARKET_KW_RE.search(q), q
    assert not _MARKET_KW_RE.search("帮我写一首诗")
    assert "create_cron" in _PROMISE_GATE_NOTE


def test_scrub_stale_sina_us():
    from threat_scan import scrub_stale_sina_us

    bad = (
        "var hq_str_int_dji=\"xx,46247.29,299.97,0.65\";\n"
        "var hq_str_int_nasdaq=\"xx,22484.07,99.37,0.44\";\n"
        "正常文本 51828.62"
    )
    out = scrub_stale_sina_us(bad)
    assert "46247" not in out and "22484" not in out
    assert "已屏蔽冻结数据源" in out
    assert "51828.62" in out  # 正常数字保留
    # 无 int_ 时零改动
    clean = "gb_dji=51510 real-time"
    assert scrub_stale_sina_us(clean) == clean


def test_guard_tool_result_scrubs_int():
    from threat_scan import guard_tool_result

    out = guard_tool_result("tavily_search", "quote int_dji 46247.29 end")
    assert "46247" not in out
    assert "int_" in out  # 提示语里提到被屏蔽


def test_us_rt_targets_and_blacklist():
    from plugins.ak_finance import _STALE_SINA_US, _us_index_targets, _us_rt_targets, _US_RT_KW

    # 「美股现在」必须命中三大指数（此前匹配不到 → 掉进网页搜索 → int_ 假源）
    idx = _us_index_targets("美股现在是啥情况")
    assert len(idx) == 3
    rt = _us_rt_targets("美股现在是多少")
    codes = [t[0] for t in rt]
    assert "gb_dji" in codes and "gb_ixic" in codes and "gb_inx" in codes
    assert "int_dji" in _STALE_SINA_US
    assert any(k in _US_RT_KW for k in ("现在", "实时"))
