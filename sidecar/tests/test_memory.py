"""Tests for memory module pure functions."""
import unittest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from memory import _quick_reflect


class TestQuickReflect(unittest.TestCase):

    def test_error_in_result(self):
        r = _quick_reflect("read_file", "Error: file not found")
        self.assertIn("出错", r)

    def test_failed_in_chinese(self):
        r = _quick_reflect("write_file", "失败：磁盘空间不足")
        self.assertIn("出错", r)

    def test_permission_denied(self):
        r = _quick_reflect("read_file", "permission denied")
        self.assertIn("出错", r)

    def test_not_found(self):
        r = _quick_reflect("search_files", "not found")
        self.assertIn("不存在", r)

    def test_traceback(self):
        r = _quick_reflect("run_cmd", "Traceback (most recent call last)")
        self.assertIn("出错", r)

    def test_empty_result(self):
        r = _quick_reflect("run_cmd", "ok")
        self.assertIn("为空", r)

    def test_large_output(self):
        r = _quick_reflect("read_file", "x" * 5001)
        self.assertIn("输出较大", r)

    def test_normal_result(self):
        r = _quick_reflect("read_file", "some content here")
        self.assertEqual(r, "")


# Local copy of main.py's dedup function for testing
import re as _re

def _test_dedup(text):
    if not text:
        return text
    for prefix in ["我是辣条", "我是 LaTiao", "我是LaTiao"]:
        pat = _re.escape(prefix)
        m = _re.search(r'^(' + pat + r'.*?)' + pat, text, _re.DOTALL)
        if m:
            return m.group(1).strip()
        m = _re.search(r'^(你好[，！、\s]*' + pat + r'.*?)(?:你好[，！、\s]*)?' + pat, text, _re.DOTALL)
        if m:
            return m.group(1).strip()
    return text


class TestDeduplicateResponse(unittest.TestCase):

    def test_direct_repetition(self):
        self.assertEqual(_test_dedup("我是辣条我是辣条"), "我是辣条")

    def test_hello_prefix_repetition(self):
        r = _test_dedup("你好，我是辣条，有什么可以帮你？你好，我是辣条，")
        self.assertIn("你好", r)
        self.assertLess(len(r), 40)

    def test_no_repetition(self):
        r = _test_dedup("我是辣条，你的本地AI助手。")
        self.assertEqual(r, "我是辣条，你的本地AI助手。")

    def test_empty_text(self):
        self.assertEqual(_test_dedup(""), "")

    def test_three_repetitions(self):
        r = _test_dedup("我是辣条我是辣条我是辣条")
        self.assertEqual(r, "我是辣条")


if __name__ == "__main__":
    unittest.main()
