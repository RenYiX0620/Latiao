"""raw_decode 扫描式 JSON 提取（_extract_first_json_object）与三处方言接线验收。

2026-10-04 借 EnSIMem 复现包 llm.py 的手法（raw_decode 位置扫描）替换
贪婪/非贪婪正则。每条方言接线都配一个"旧逻辑必红"的病例：

- 1.2 json 栅栏：旧 `\{.*?\}` 要求 { 紧跟围栏 → 栅栏里有句中文开场白，
  整个方言失明、调用丢失；
- 1.25 <tool_call>{json}：旧兜底贪婪 `\{.*\}` → 对象后跟含 } 的尾巴时
  区间抓错 → 调用丢失；
- 1.3 <function=名> XML：同款贪婪区间 → 旧逻辑解析出垃圾参数，
  工具拿烂参数白跑一轮。
"""
import json
import unittest

from agent.parsing import (
    _extract_first_json_object,
    _parse_prompt_tool_calls,
)


class TestExtractFirstJsonObject(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(_extract_first_json_object('{"a": 1}'), {"a": 1})

    def test_brace_inside_string(self):
        s = '{"note": "数据含{异常}大括号", "n": 2}'
        self.assertEqual(_extract_first_json_object(s)["note"], "数据含{异常}大括号")

    def test_nested_object(self):
        s = '{"name": "mx_query", "arguments": {"query": "半导体"}}'
        self.assertEqual(
            _extract_first_json_object(s)["arguments"]["query"], "半导体")

    def test_leading_prose_and_trailing_brace_junk(self):
        s = '好的，这是查询：{"query": "上证指数"} 备注 }'
        self.assertEqual(_extract_first_json_object(s)["query"], "上证指数")

    def test_prose_braces_skipped(self):
        s = '使用{工具名}时：{"query": "x"}'
        self.assertEqual(_extract_first_json_object(s)["query"], "x")

    def test_no_json_returns_none(self):
        self.assertIsNone(_extract_first_json_object("完全没有对象"))
        self.assertIsNone(_extract_first_json_object("{残缺"))

    def test_empty_or_none(self):
        self.assertIsNone(_extract_first_json_object(""))
        self.assertIsNone(_extract_first_json_object(None))

    def test_array_wrapped_object_is_returned(self):
        # 数组壳不拦：[{...}] 里的对象照样返回——数组包裹的调用是真实方言
        # （旧代码 json.loads 得到 list → isinstance 校验拒收，这里明确放行）
        s = '[{"name": "mx_query", "arguments": {"query": "半导体"}}]'
        self.assertEqual(
            _extract_first_json_object(s)["arguments"]["query"], "半导体")


class TestDialectWiring(unittest.TestCase):
    def _args(self, call):
        return json.loads(call["function"]["arguments"])

    def test_json_fence_with_leading_prose(self):
        # 旧逻辑：围栏里有说明文字 → 正则不命中 → 整个方言失明，调用丢失
        text = ('```json\n好的，这是查询：\n'
                '{"name": "mx_query", "arguments": {"query": "上证指数"}}\n```')
        clean, calls = _parse_prompt_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "mx_query")
        self.assertEqual(self._args(calls[0])["query"], "上证指数")

    def test_json_fence_plain_still_works(self):
        text = ('```json\n'
                '{"name": "read_file", "arguments": {"path": "/tmp/a"}}\n```')
        clean, calls = _parse_prompt_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self._args(calls[0])["path"], "/tmp/a")

    def test_toolcall_json_with_trailing_brace_junk(self):
        # 旧逻辑：贪婪 \{.*\} 连尾巴一起抓 → 解析失败 → salvage 垃圾 → 无 name → 调用丢失
        text = ('<tool_call>{"name": "read_file", '
                '"arguments": {"path": "/tmp/x"}} 备注 }</tool_call>')
        clean, calls = _parse_prompt_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "read_file")
        self.assertEqual(self._args(calls[0])["path"], "/tmp/x")
        self.assertNotIn("<tool_call>", clean)

    def test_toolcall_json_clean_body_still_works(self):
        text = '<tool_call>{"name": "mx_query", "arguments": {"query": "半导体"}}</tool_call>'
        clean, calls = _parse_prompt_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self._args(calls[0])["query"], "半导体")

    def test_qwen_xml_with_trailing_brace_junk(self):
        # 旧逻辑：贪婪区间 + salvage → 垃圾参数键（"{"query" 之类），工具白跑一轮
        text = ('<tool_call><function=mx_query>'
                '{"query": "半导体", "limit": 4} 注意}'
                '</function></tool_call>')
        clean, calls = _parse_prompt_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "mx_query")
        self.assertEqual(self._args(calls[0])["query"], "半导体")

    def test_qwen_xml_parameter_form_still_works(self):
        text = ('<tool_call><function=mx_query>'
                '<parameter=query>上证指数</parameter>'
                '</function></tool_call>')
        clean, calls = _parse_prompt_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self._args(calls[0])["query"], "上证指数")


if __name__ == "__main__":
    unittest.main()
