"""Tests for reply-language anchoring fixes (09-03 新会话英文回复事故)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_loop import _build_chat_messages, _detect_user_language, _progress_tail


class TestDetectUserLanguage(unittest.TestCase):
    """URL 字母不得参与语言判定（中文+链接 曾被误判为 en）。"""

    def test_pure_chinese(self):
        self.assertEqual(_detect_user_language("分析昨晚美股和消息面"), "zh")

    def test_chinese_with_url(self):
        # 09-03 事故原话：16 汉字 vs URL 23 字母，修复前误判 en
        msg = "读取这个网页讲了什么 https://m.toutiao.com/article/7680161745830183439/ 简要回答即可"
        self.assertEqual(_detect_user_language(msg), "zh")

    def test_chinese_with_www_url(self):
        self.assertEqual(_detect_user_language("打开 www.example.com 看看"), "zh")

    def test_genuine_english(self):
        self.assertEqual(_detect_user_language("hello how are you today my friend"), "en")

    def test_empty(self):
        self.assertEqual(_detect_user_language(""), "zh")


class TestFreshSessionSystemPrompt(unittest.TestCase):
    """新会话 system 提示：含语言规则、不含强制英文规则。"""

    def test_contains_language_rule_no_critical(self):
        body = {"messages": [{"role": "user", "content": "你好"}]}
        msgs = _build_chat_messages(body, body["messages"])
        sys_content = msgs[0]["content"]
        self.assertIn("语言规则", sys_content)
        self.assertNotIn("CRITICAL LANGUAGE RULE", sys_content)
        self.assertIn("时间规则", sys_content)

    def test_english_user_gets_english_rule(self):
        body = {"messages": [{"role": "user", "content": "hello there my good friend"}]}
        msgs = _build_chat_messages(body, body["messages"])
        sys_content = msgs[0]["content"]
        self.assertIn("Language rule", sys_content)
        self.assertIn("CRITICAL LANGUAGE RULE", sys_content)  # 真·英文用户，强制规则应存在


class TestProgressTail(unittest.TestCase):
    """PROGRESS 尾部注入截到 600 字符（英文日志减量）。"""

    def test_capped_at_600(self):
        tail = _progress_tail()
        self.assertLessEqual(len(tail), 600)

    def test_explicit_limit_respected(self):
        self.assertLessEqual(len(_progress_tail(100)), 100)


if __name__ == "__main__":
    unittest.main()
