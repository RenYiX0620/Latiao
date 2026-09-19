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

class TestLanguageAnchor(unittest.TestCase):
    """语言锚（09-19）：每轮按用户消息语言生成、置于系统提示最前，且四语齐备。"""

    CASES = [
        ("读取这个文件", "简体中文"),
        ("read this file for me", "English"),
        ("このファイルを読んで", "日本語"),
        ("прочитай этот файл", "русском"),
    ]

    def test_anchor_present_and_first(self):
        for text, marker in self.CASES:
            body = {"messages": [{"role": "user", "content": text}]}
            sys_content = _build_chat_messages(body, body["messages"])[0]["content"]
            self.assertIn(marker, sys_content, f"{text!r} 缺少 {marker} 语言锚")
            self.assertLess(sys_content.index(marker), 1200,
                            f"{text!r} 的语言锚不在提示前部（位置 {sys_content.index(marker)}）")

    def test_no_anchor_without_user_text(self):
        # 首轮无文本（如仅图片）时不注入，避免误判成中文
        body = {"messages": [{"role": "user", "content": ""}]}
        sys_content = _build_chat_messages(body, body["messages"])[0]["content"]
        self.assertNotIn("覆盖下方所有语言规则", sys_content)


class TestRussianSupport(unittest.TestCase):
    def test_russian_detected(self):
        self.assertEqual(_detect_user_language("прочитай этот файл и скажи что там"), "ru")

    def test_russian_hard_rules_and_hint(self):
        from agent.gates import lang_retry_hint
        self.assertIn("русском", lang_retry_hint("ru"))
        body = {"messages": [{"role": "user", "content": "прочитай файл"}]}
        sys_content = _build_chat_messages(body, body["messages"])[0]["content"]
        self.assertIn("Три жёстких правила", sys_content)   # 俄语硬规则，而非回落中文

    def test_retry_hint_languages(self):
        from agent.gates import lang_retry_hint
        for lang, marker in (("zh", "中文"), ("en", "English"), ("ja", "日本語"), ("ru", "русском")):
            self.assertIn(marker, lang_retry_hint(lang))
