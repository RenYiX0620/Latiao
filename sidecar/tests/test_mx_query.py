"""mx_query 查询词规范化与失败重试逻辑测试"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import plugins.mx_query as mx_query  # noqa: E402
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


# ── 名称核对（2026-09-30）：空结果时说"名字不对"，别让模型换同义名 ──
class _EmptyRun:
    returncode = 1
    stdout = "接口返回中无 dataTableDTOList"
    stderr = ""


def _force_empty(monkeypatch):
    monkeypatch.setattr(mx_query.subprocess, "run", lambda *a, **k: _EmptyRun())


class TestNameCheckGuidance(unittest.TestCase):
    """空结果时的名称核对（2026-09-30）：说清"名字不对"，别让模型换同义名。"""

    def _text(self, query, suggest):
        from unittest import mock
        with mock.patch.object(mx_query.subprocess, "run", lambda *a, **k: _EmptyRun()), \
             mock.patch.object(mx_query, "_suggest_names", suggest):
            return mx_query.execute({"query": query})

    def test_unknown_name_says_not_in_universe(self):
        t = self._text("超节点 涨跌幅 资金流向", lambda kw: [])
        self.assertIn("不在东财标的库", t)
        self.assertIn("产业链个股", t)
        self.assertNotIn("汇总查询", t, "非汇总查询不该套汇总提示（此前正是它把模型引向换名字）")

    def test_known_name_returns_official_candidates(self):
        t = self._text("半导体 涨跌幅 资金流向",
                       lambda kw: [("半导体", "BK1036"), ("半导体ETF", "512480")])
        self.assertIn("相近的正式名称", t)
        self.assertIn("半导体(BK1036)", t)
        self.assertIn("正式名称重新查询", t)
        self.assertNotIn("汇总查询", t)

    def test_interface_failure_degrades_without_swallowing_error(self):
        def _boom(kw):
            raise RuntimeError("网络断了")
        t = self._text("超节点 涨跌幅 资金流向", _boom)
        self.assertTrue(t.startswith("Error: 查询未返回数据"), t[:60])
        self.assertIn("tavily_search", t, "核对失败也要给一般性指引")

    def test_summary_query_keeps_summary_hint(self):
        t = self._text("全部板块 资金流向排行", lambda kw: [])
        self.assertIn("汇总查询", t, "汇总类查询仍应给汇总提示")


if __name__ == "__main__":
    unittest.main()
