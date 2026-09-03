"""Tests for the wrap-up gate fixes (13:58 英文规划文本被当最终答案事故)."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_loop import _looks_like_planning, _reply_lang_mismatch, _count_successful_duplicates

# 13:58 事故真实回复（891 字符英文规划文本，从用户会话 z440ab 提取）
ENGLISH_PLANNING = (
    "The user is asking for an analysis of the overall market trend. According to the rules:\n"
    "1. First, I need to read ~/.local-ai-os/PROGRESS.md to understand the current project "
    "progress (mandatory startup protocol)\n"
    "2. For financial data (stocks, overall market, quotes), I must use mx_query\n"
    "3. For the \"overall market/sector/quote\" type of task, I should use the market-review skill\n\n"
    "Let me follow the protocol. First, read PROGRESS.md. Next, use the market-review skill. "
    "After that, query the market data.\n\n"
    "Let's get started. The current time is 2026-09-03 (Thursday) 13:58.\n\n"
    "Let me plan the subtasks:\n"
    "1. Read PROGRESS.md (startup protocol)\n"
    "2. Get the market-review skill\n"
    "3. Query the overall market data (Shanghai Composite Index, Shenzhen Component Index, "
    "ChiNext Index)\n"
    "4. Analyze and present the results\n\n"
    "Let's start with the mandatory startup protocol."
)


class TestLooksLikePlanning(unittest.TestCase):
    """规划话术判定：≥2 个信号才算规划。"""

    def test_english_planning_detected(self):
        self.assertTrue(_looks_like_planning(ENGLISH_PLANNING))

    def test_chinese_real_answer_not_planning(self):
        text = ("大盘今天缩量下跌 0.8%，上证收 3961 点。板块方面半导体资金净流入 12 亿，"
                "消费板块承压。下一步建议关注 3960 支撑位是否放量收复，跌破则减仓观望。")
        self.assertFalse(_looks_like_planning(text))

    def test_short_text_not_planning(self):
        self.assertFalse(_looks_like_planning("你好"))
        self.assertFalse(_looks_like_planning(""))

    def test_single_signal_not_planning(self):
        # 单个"下一步"不应误拦（17:07 重复堆叠事故教训）
        self.assertFalse(_looks_like_planning("今日大盘收涨 0.5%，下一步我们看创业板表现，具体数据如上。"))


class TestReplyLangMismatch(unittest.TestCase):
    """回复语言与用户语言不符判定。"""

    def test_zh_user_english_reply(self):
        self.assertTrue(_reply_lang_mismatch("分析大盘走势\n", ENGLISH_PLANNING))

    def test_zh_user_chinese_reply(self):
        self.assertFalse(_reply_lang_mismatch("分析大盘走势\n", "今天大盘跌了 0.8%，上证收在 3961 点，量能萎缩明显。"))

    def test_short_mixed_fragment_ok(self):
        # 短小混合片段（如 NVIDIA涨5%）不误伤
        self.assertFalse(_reply_lang_mismatch("帮我看看美股", "NVIDIA 涨了 5%"))

    def test_zh_user_mixed_english_dominant(self):
        # 14:45 重放真实形态：598 字母 vs 42 汉字 的混合英文分析 → 判不符
        mixed = ("I've got the 10-day trend data. Shanghai Composite Index: "
                 "08-26: +0.59% → 3912.52; 08-27: +1.13% → 3956.57; 09-02: -0.97% → 3941.39. "
                 "Shenzhen Component Index: 08-26: +0.69% → 13841; 09-02: -1.88% → 13612. "
                 "ChiNext: 09-02: -2.39% → 3312.24. Overall the market shows weakness. 大盘整体偏弱。")
        self.assertTrue(_reply_lang_mismatch("分析大盘走势\n", mixed))

    def test_zh_user_chinese_dominant_with_numbers_ok(self):
        # 中文为主、含数字/代码的正常回答不误伤
        text = ("今日大盘低开高走，上证指数收 3961.32 点，涨幅 0.5%；创业板指涨 1.2% 收 3415 点。"
                "板块方面半导体领涨，主力净流入 12.3 亿元。结论：缩量反弹，情绪修复中，可轻仓参与。")
        self.assertFalse(_reply_lang_mismatch("分析大盘走势\n", text))

    def test_en_user_chinese_long_reply(self):
        long_zh = "根据最新数据，上证指数今日小幅收涨，成交量较昨日有所放大，市场情绪回暖。" * 3
        self.assertTrue(_reply_lang_mismatch("analyze the US stock market", long_zh))

    def test_en_user_english_reply(self):
        self.assertFalse(_reply_lang_mismatch("analyze the market", "The market closed higher today by 0.5 percent."))

    def test_ja_user_english_reply(self):
        self.assertTrue(_reply_lang_mismatch("今日の相場を分析して", "The market trend analysis follows below. " * 6))


class TestCountSuccessfulDuplicates(unittest.TestCase):
    """相同调用防重复：成功 ≥2 次才计数（14:26 反复重读 PROGRESS.md 循环）。"""

    def _msg(self, role, **kw):
        return {"role": role, **kw}

    def _assistant_call(self, cid, name, args):
        return self._msg("assistant", tool_calls=[{"id": cid, "function": {"name": name, "arguments": json.dumps(args)}}])

    def test_two_successes_counted(self):
        msgs = [
            self._assistant_call("c1", "read_file", {"path": "~/.local-ai-os/PROGRESS.md"}),
            self._msg("tool", tool_call_id="c1", content="(早期进度已轮转) ..."),
            self._assistant_call("c2", "read_file", {"path": "~/.local-ai-os/PROGRESS.md"}),
            self._msg("tool", tool_call_id="c2", content="(早期进度已轮转) ..."),
        ]
        self.assertEqual(_count_successful_duplicates(msgs, "read_file", {"path": "~/.local-ai-os/PROGRESS.md"}), 2)

    def test_tilde_and_absolute_paths_are_same(self):
        # 17:10 重放：模型用 "~" 与绝对路径交替重读同一文件绕过护栏
        msgs = [
            self._assistant_call("c1", "read_file", {"path": "~/.local-ai-os/PROGRESS.md"}),
            self._msg("tool", tool_call_id="c1", content="(早期进度已轮转) ..."),
            self._assistant_call("c2", "read_file", {"path": "/Users/langzuxiang/.local-ai-os/PROGRESS.md"}),
            self._msg("tool", tool_call_id="c2", content="(早期进度已轮转) ..."),
        ]
        self.assertEqual(_count_successful_duplicates(msgs, "read_file", {"path": "~/.local-ai-os/PROGRESS.md"}), 2)
        self.assertEqual(_count_successful_duplicates(msgs, "read_file", {"path": "/Users/langzuxiang/.local-ai-os/PROGRESS.md"}), 2)

    def test_failure_not_counted(self):
        msgs = [
            self._assistant_call("c1", "tavily_search", {"query": "q"}),
            self._msg("tool", tool_call_id="c1", content="Error: timeout"),
            self._assistant_call("c2", "tavily_search", {"query": "q"}),
            self._msg("tool", tool_call_id="c2", content="🔍 Tavily 搜索: q ..."),
        ]
        # 只有 1 次成功 → 允许再试一次
        self.assertEqual(_count_successful_duplicates(msgs, "tavily_search", {"query": "q"}), 1)

    def test_different_args_not_counted(self):
        msgs = [
            self._assistant_call("c1", "mx_query", {"query": "上证指数"}),
            self._msg("tool", tool_call_id="c1", content="..."),
            self._assistant_call("c2", "mx_query", {"query": "创业板指"}),
            self._msg("tool", tool_call_id="c2", content="..."),
        ]
        self.assertEqual(_count_successful_duplicates(msgs, "mx_query", {"query": "上证指数"}), 1)
        self.assertEqual(_count_successful_duplicates(msgs, "mx_query", {"query": "创业板指"}), 1)


class TestProgressSummary(unittest.TestCase):
    """PROGRESS.md 中文摘要（read_file 特例）。"""

    def test_summary_extracts_entries(self):
        sys.path.insert(0, str(Path(__file__).parent.parent / "plugins"))
        from read_file import summarize_progress_tail
        sample = (
            "### 2026-09-03T14:35:03 read_file Args: {\"path\": \"x\"}\n"
            "### 2026-09-03T13:59:18 tavily_search Args: {\"query\": \"US stock\"}\n"
            "### 2026-09-02T22:01:30 mx_query Args: {\"query\": \"上证指数\"}\n"
        )
        out = summarize_progress_tail(sample)
        self.assertIn("最近工作记录", out)
        self.assertIn("read_file", out)
        self.assertIn("tavily_search", out)
        self.assertNotIn("Args", out)  # 不含原始英文日志
        self.assertEqual(summarize_progress_tail("无条目"), "")

    def test_summary_caps_limit(self):
        sys.path.insert(0, str(Path(__file__).parent.parent / "plugins"))
        from read_file import summarize_progress_tail
        lines = "".join(f"### 2026-09-03T{i:02d}:00:00 run_cmd x\n" for i in range(20))
        out = summarize_progress_tail(lines, limit=5)
        self.assertEqual(len(out.splitlines()) - 1, 5)


if __name__ == "__main__":
    unittest.main()
