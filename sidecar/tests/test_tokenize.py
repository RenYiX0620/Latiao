"""Tests for Chinese tokenization (_tokenize_zh)."""
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from memory import _tokenize_zh


class TestTokenizeZh(unittest.TestCase):

    def test_empty_string(self):
        self.assertEqual(_tokenize_zh(""), [])

    def test_chinese_text(self):
        result = _tokenize_zh("项目结构")
        for token in ["项目", "目结", "结构"]:
            self.assertIn(token, result, f"{token} should be in {result}")

    def test_chinese_bigrams(self):
        result = _tokenize_zh("测试")
        self.assertIn("测试", result)

    def test_english_text(self):
        result = _tokenize_zh("hello world")
        self.assertIn("hello", result)
        self.assertIn("world", result)

    def test_mixed_text(self):
        result = _tokenize_zh("API接口")
        eng = [t for t in result if t.isascii()]
        chn = [t for t in result if not t.isascii()]
        self.assertGreater(len(eng), 0, "Should have English tokens")
        self.assertGreater(len(chn), 0, "Should have Chinese tokens")

    def test_numbers_and_underscore(self):
        result = _tokenize_zh("test_123 v2")
        self.assertIn("test_123", result)
        self.assertIn("v2", result)

    def test_each_char_as_token(self):
        """Each Chinese char should be a single-char token."""
        result = _tokenize_zh("你好世界")
        for char in "你好世界":
            self.assertIn(char, result, f"'{char}' should be a token")


if __name__ == "__main__":
    unittest.main()
