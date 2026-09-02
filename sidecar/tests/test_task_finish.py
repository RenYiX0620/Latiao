"""任务收尾判定相关单测：未执行意图词形 + 思考段提取。"""
import unittest

from agent_loop import _PENDING_INTENT_PATTERNS, _extract_think_body


class PendingIntentTest(unittest.TestCase):
    def test_three_incident_forms_match(self):
        """16:45（让我读取再分析）/ 21:42（改用搜寻）/ 21:13（让用 skill+先看看）三个事故词形必须命中。"""
        cases = [
            "让我读取数据再分析",
            "我改用联网搜索来查",
            "让我用 market_review skill 的做法来试做分析",
            "不过先看看是否需要查更多数据",
        ]
        for case in cases:
            self.assertTrue(
                any(k in case for k in _PENDING_INTENT_PATTERNS),
                f"词形未命中: {case}",
            )

    def test_complete_answer_not_matched(self):
        """完整分析正文（1398 案例风格，含"让我整理一下"）不应触发未执行意图。"""
        full = (
            "让我整理一下关键数据：\n"
            "**上证指数（000001.SH）**：\n- 最新价：3941.39\n- 今日涨跌幅：-0.97%\n"
            "**深证成指（399001.SZ）**：\n- 最新价：13611.55\n"
            "1. 三大指数全线下跌，且跌幅逐级放大\n2. DDX 全为负值\n3. 成交额方面"
        )
        self.assertFalse(any(k in full for k in _PENDING_INTENT_PATTERNS))


class ExtractThinkBodyTest(unittest.TestCase):
    def test_fence_style(self):
        """```think> … </think> 提取。"""
        text = "好的，我来分析。\n```think>\n用户要求分析。我已经获取了实时数据。\n关键观察：三大指数下跌。\n</think>\n让我用 skill 来做分析。"
        body = _extract_think_body(text)
        self.assertIn("用户要求分析", body)
        self.assertIn("关键观察", body)
        self.assertNotIn("let me", body)

    def test_angle_bracket_style(self):
        """<think> … </think> 提取。"""
        text = "<think>思考内容分析段落</think>正文声明"
        body = _extract_think_body(text)
        self.assertEqual(body, "思考内容分析段落")

    def test_no_think_returns_empty(self):
        self.assertEqual(_extract_think_body("普通完整分析正文，无思考段。"), "")

    def test_fence_tilde_style(self):
        """```think< 闭合风格。"""
        text = "声明\n```think>\n分析内容在这里\n```think<\n继续"
        body = _extract_think_body(text)
        self.assertIn("分析内容在这里", body)


if __name__ == "__main__":
    unittest.main()
