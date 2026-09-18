"""上下文统计测试：估算器边界、类别归集、真实用量与缓存命中率。

注意：测试环境（CI）没有模型文件，因此只覆盖"估算 + 归集 + 用量记录"这些
不依赖 tokenizer 的路径；精确计数（vocab_only / tokenizers）在实现时用真机
实测校验过（GGUF 与 MLX 两路计数一致，误差 0）。
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import context_stats as cs


class TestEstimateTokens(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(cs.estimate_tokens(""), 0)

    def test_chinese_bounds(self):
        # 纯中文：约 0.5~0.9 token/字（Qwen 系实测 ~0.67）
        text = "上下文统计面板需要展示各类别占比，包括消息与系统提示词。" * 5
        n = cs.estimate_tokens(text)
        cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
        self.assertGreater(n, cjk * 0.4)
        self.assertLess(n, cjk * 1.0)

    def test_english_bounds(self):
        # 英文散文：约 0.15~0.32 token/字符（实测 ~0.21）
        text = ("The quick brown fox jumps over the lazy dog. "
                "This sentence calibrates latin-script estimation.") * 3
        n = cs.estimate_tokens(text)
        self.assertGreater(n, len(text) * 0.12)
        self.assertLess(n, len(text) * 0.32)

    def test_monotonic(self):
        short = cs.estimate_tokens("你好")
        long = cs.estimate_tokens("你好" * 50)
        self.assertLess(short, long)

    def test_whitespace_only_min_one(self):
        self.assertGreaterEqual(cs.estimate_tokens("   \n\t "), 1)


class TestCountTokensFallback(unittest.TestCase):
    def test_missing_model_falls_back_to_estimate(self):
        text = "没有模型文件时应当退回估算，而不是抛错。"
        n, src = cs.count_tokens(text, "/nonexistent/model.gguf")
        self.assertEqual(src, "estimated")
        self.assertEqual(n, cs.estimate_tokens(text))

    def test_empty_text(self):
        self.assertEqual(cs.count_tokens("", ""), (0, "exact"))


class TestSnapshot(unittest.TestCase):
    SESSION = "sess-test"

    def setUp(self):
        cs.reset()
        cs.record_system_parts(self.SESSION, [
            ("system_prompt", "## 系统规则\n你是一个助手。"),
            ("skills", "## 可用技能\n- 分析: 数据分析"),
            ("other", "## 上次会话进展\n- 读了文件"),
        ])

    def tearDown(self):
        cs.reset()

    def _messages(self):
        return [
            {"role": "system", "content": "## 系统规则\n你是一个助手。\n\n## 可用技能\n- 分析: 数据分析\n\n"
                                          "## 上次会话进展\n- 读了文件\n\n知识注入：4 条相关学习"},
            {"role": "user", "content": "帮我看看这个文件。"},
            {"role": "assistant", "content": "好的，我先读取它。"},
        ]

    def test_categories_are_attributed(self):
        cs.record_request(self.SESSION, self._messages(), [], model_path="", limit=64000,
                          limit_source="local_engine")
        st = cs.stats(self.SESSION)
        self.assertTrue(st["available"])
        by_key = {b["key"]: b["tokens"] for b in st["breakdown"]}
        self.assertGreater(by_key["messages"], 0)
        self.assertGreater(by_key["system_prompt"], 0)
        self.assertGreater(by_key["skills"], 0)
        self.assertGreater(by_key["other"], 0)   # 进展 + 知识注入
        self.assertEqual(by_key["system_tools"], 0)
        self.assertEqual(by_key["mcp_tools"], 0)

    def test_share_sums_to_100(self):
        cs.record_request(self.SESSION, self._messages(), [], limit=64000)
        st = cs.stats(self.SESSION)
        total_pct = sum(b["percent"] for b in st["breakdown"])
        self.assertAlmostEqual(total_pct, 100.0, delta=1.0)

    def test_tools_split_by_source(self):
        tools = [
            {"type": "function", "function": {"name": "read_file", "description": "read"}},
            {"type": "function", "function": {"name": "mcp__fs__list", "description": "list"}},
        ]
        cs.record_request(self.SESSION, self._messages(), tools, limit=64000)
        by_key = {b["key"]: b["tokens"] for b in cs.stats(self.SESSION)["breakdown"]}
        self.assertGreater(by_key["system_tools"], 0)
        self.assertGreater(by_key["mcp_tools"], 0)

    def test_percent_uses_limit(self):
        cs.record_request(self.SESSION, self._messages(), [], limit=1000)
        st = cs.stats(self.SESSION)
        self.assertEqual(st["limit"], 1000)
        self.assertIsNotNone(st["percent"])
        self.assertGreater(st["percent"], 0)

    def test_no_snapshot_is_unavailable(self):
        st = cs.stats("never-seen")
        self.assertFalse(st["available"])
        self.assertEqual(st["breakdown"], [])
        self.assertIsNone(st["cache_hit_rate"])


class TestUsageAndCache(unittest.TestCase):
    SESSION = "sess-usage"

    def setUp(self):
        cs.reset()
        cs.record_request(self.SESSION, [{"role": "user", "content": "你好"}], [], limit=8000)

    def tearDown(self):
        cs.reset()

    def test_llama_timings(self):
        cs.record_usage(self.SESSION, None, {"prompt_n": 100, "cache_n": 80})
        st = cs.stats(self.SESSION)
        self.assertAlmostEqual(st["cache_hit_rate"], 0.8, places=4)
        self.assertEqual(st["real_prompt_tokens"], 100)

    def test_openai_cached_tokens(self):
        cs.record_usage(self.SESSION, {"prompt_tokens": 200,
                                       "prompt_tokens_details": {"cached_tokens": 150}}, None)
        st = cs.stats(self.SESSION)
        self.assertAlmostEqual(st["cache_hit_rate"], 0.75, places=4)

    def test_real_prompt_tokens_override_estimate(self):
        before = cs.stats(self.SESSION)["total"]
        cs.record_usage(self.SESSION, {"prompt_tokens": before + 5000}, None)
        after = cs.stats(self.SESSION)["total"]
        self.assertEqual(after, before + 5000)

    def test_rolling_average_and_cap(self):
        for i in range(cs.CACHE_SAMPLES + 5):
            cs.record_usage(self.SESSION, None, {"prompt_n": 100, "cache_n": 50})
        st = cs.stats(self.SESSION)
        self.assertEqual(st["cache_samples"], cs.CACHE_SAMPLES)
        self.assertAlmostEqual(st["cache_hit_rate"], 0.5, places=4)


    def _msgs(self):
        return [{"role": "system", "content": "## 系统规则\n你是助手。"},
                {"role": "user", "content": "帮我看看这个文件。"}]

    def test_rows_sum_to_real_total_after_delta(self):
        """真实总量大于各部分之和时，差额补给系统工具（模板为每个工具包裹的特殊 token）。"""
        tools = [{"type": "function", "function": {"name": "read_file", "description": "read"}}]
        cs.record_request(self.SESSION, self._msgs(), tools, limit=64000)
        est = sum(b["tokens"] for b in cs.stats(self.SESSION)["breakdown"])
        cs.record_usage(self.SESSION, {"prompt_tokens": est + 300}, None)
        st = cs.stats(self.SESSION)
        by_key = {b["key"]: b["tokens"] for b in st["breakdown"]}
        self.assertEqual(sum(by_key.values()), st["total"])       # 各行相加 = 标题总量
        self.assertGreaterEqual(by_key["system_tools"], 300)      # 差额归到系统工具
        self.assertAlmostEqual(sum(b["percent"] for b in st["breakdown"]), 100.0, delta=1.0)

    def test_delta_without_tools_goes_to_other(self):
        cs.record_request(self.SESSION, self._msgs(), [], limit=64000)
        est = sum(b["tokens"] for b in cs.stats(self.SESSION)["breakdown"])
        cs.record_usage(self.SESSION, {"prompt_tokens": est + 120}, None)
        st = cs.stats(self.SESSION)
        by_key = {b["key"]: b["tokens"] for b in st["breakdown"]}
        self.assertEqual(by_key["system_tools"], 0)
        self.assertEqual(sum(by_key.values()), st["total"])

    def test_usage_without_cache_fields_is_ignored(self):
        cs.record_usage(self.SESSION, {"prompt_tokens": 10}, None)
        st = cs.stats(self.SESSION)
        self.assertIsNone(st["cache_hit_rate"])
        self.assertEqual(st["real_prompt_tokens"], 10)

    def test_garbage_input_does_not_crash(self):
        cs.record_usage(self.SESSION, None, None)
        cs.record_usage(self.SESSION, {"prompt_tokens": None}, {"prompt_n": 0})
        self.assertIsInstance(cs.stats(self.SESSION), dict)


class TestSystemPartsReset(unittest.TestCase):
    def tearDown(self):
        cs.reset()

    def test_parts_from_previous_turn_do_not_leak(self):
        cs.record_system_parts("s1", [("skills", "## 可用技能\n- A")])
        cs.record_request("s1", [{"role": "system", "content": "普通系统消息"}], [])
        by_key = {b["key"]: b["tokens"] for b in cs.stats("s1")["breakdown"]}
        self.assertEqual(by_key["skills"], 0)   # 本轮系统消息里没有那段，不应再计入
        cs.reset("s1")
        self.assertFalse(cs.stats("s1")["available"])


if __name__ == "__main__":
    unittest.main()
