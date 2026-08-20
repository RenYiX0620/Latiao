"""mx_query 查询词规范化与失败重试逻辑测试"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from plugins.mx_query import _is_empty_result, _query_variants  # noqa: E402


class TestQueryVariants(unittest.TestCase):
    def test_weekly_phrase_normalized(self):
        variants = _query_variants("黄金板块本周涨跌幅表现")
        self.assertEqual(variants[0], "黄金板块本周涨跌幅表现")
        self.assertIn("黄金板块本周行情涨跌幅", variants)  # 规范化变体

    def test_plain_query_unchanged(self):
        self.assertEqual(_query_variants("贵州茅台股价"), ["贵州茅台股价"])

    def test_max_three_variants(self):
        self.assertLessEqual(len(_query_variants("半导体板块最新走势本周涨跌幅表现")), 3)

    def test_empty_result_detection(self):
        self.assertTrue(_is_empty_result("错误: 接口返回中无 dataTableDTOList。"))
        self.assertFalse(_is_empty_result("查询成功，返回 3 个表"))


if __name__ == "__main__":
    unittest.main()
