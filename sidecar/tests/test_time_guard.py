"""Tests for the relative-date guard (_time_guard) and time-sensitive stamping."""
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "plugins"))

from plugins._time_guard import check_query_date  # noqa: E402

# 2026-09-03 是周四
NOW = datetime(2026, 9, 3, 8, 57, 0)


class TestCheckQueryDate(unittest.TestCase):
    """搜索词日期滑差自检：早于今天 ≥1 天才警告。"""

    def test_day_before_yesterday_triggers(self):
        # 09-03 事故原词：模型把"昨晚"多减一天写成 9月1日（前天）→ 必须警告
        out = check_query_date("2026年9月1日 美股收盘 道琼斯 纳斯达克 标普500", NOW)
        self.assertIsNotNone(out)
        self.assertIn("2026-09-01", out)
        self.assertIn("2026-09-03", out)
        self.assertIn("重新搜索", out)

    def test_yesterday_date_no_trigger(self):
        # "昨晚"的正确换算就是昨天（9月3日 问昨晚 → 搜 9月2日 合法），
        # 不得警告——重放实测对昨天警告会诱导模型循环重搜
        self.assertIsNone(check_query_date("2026-09-02 美股收盘", NOW))
        self.assertIsNone(check_query_date("2026年9月2日 美股 道琼斯", NOW))

    def test_today_date_no_trigger(self):
        self.assertIsNone(check_query_date("2026年9月3日 A股行情", NOW))

    def test_no_date_no_trigger(self):
        self.assertIsNone(check_query_date("上证指数 今日涨跌幅", NOW))
        self.assertIsNone(check_query_date("", NOW))

    def test_no_year_uses_current_year(self):
        out = check_query_date("9月1日 美股收盘", NOW)
        self.assertIsNotNone(out)
        self.assertIn("2026-09-01", out)

    def test_no_year_december_maps_to_last_year(self):
        # 1月查"12月31日"应解析为上一年，避免误报未来日期
        jan_now = datetime(2027, 1, 2, 9, 0, 0)
        out = check_query_date("12月31日 美股收盘", jan_now)
        self.assertIsNotNone(out)
        self.assertIn("2026-12-31", out)

    def test_invalid_date_ignored(self):
        self.assertIsNone(check_query_date("13月40日 数据", NOW))


class TestStampTimeSensitive(unittest.TestCase):
    """agent_loop._stamp_time_sensitive 只对时间敏感工具生效。"""

    def test_tavily_stamped(self):
        from agent_loop import _stamp_time_sensitive, _TIME_SENSITIVE_TOOLS
        self.assertIn("tavily_search", _TIME_SENSITIVE_TOOLS)
        self.assertIn("headless_read", _TIME_SENSITIVE_TOOLS)
        stamp = _stamp_time_sensitive()
        self.assertTrue(stamp.startswith("⏱ [数据时刻] "))
        self.assertIn("以当前时间为准", stamp)

    def test_read_file_not_in_set(self):
        from agent_loop import _TIME_SENSITIVE_TOOLS
        self.assertNotIn("read_file", _TIME_SENSITIVE_TOOLS)
        self.assertNotIn("write_file", _TIME_SENSITIVE_TOOLS)


if __name__ == "__main__":
    unittest.main()
