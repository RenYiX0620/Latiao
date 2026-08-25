"""Tests for agent loop pure parsing/dedup functions."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import main


class TestParsePromptToolCalls(unittest.TestCase):
    """Tests for main._parse_prompt_tool_calls (```tool fenced format)."""

    def test_fenced_tool_call_parsed(self):
        text = '```tool list_dir\n{"path": "."}\n```'
        clean, calls = main._parse_prompt_tool_calls(text)
        self.assertEqual(clean, "")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "list_dir")
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"path": "."})

    def test_fence_with_surrounding_text(self):
        text = '我先看看目录```tool list_dir\n{"path": "."}\n```然后继续'
        clean, calls = main._parse_prompt_tool_calls(text)
        self.assertEqual(clean, "我先看看目录然后继续")
        self.assertEqual(len(calls), 1)
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"path": "."})

    def test_invalid_json_salvaged(self):
        # 围栏内不是合法 JSON 时走 _salvage_tool_args 容错解析
        text = "```tool run_cmd\ncommand: ls -la\n```"
        clean, calls = main._parse_prompt_tool_calls(text)
        self.assertEqual(clean, "")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "run_cmd")
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"command": "ls -la"})

    def test_multiple_fences(self):
        text = (
            '```tool read_file\n{"path": "/a.txt"}\n```\n'
            '```tool run_cmd\n{"command": "pwd"}\n```'
        )
        clean, calls = main._parse_prompt_tool_calls(text)
        self.assertEqual(clean, "")
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            [c["function"]["name"] for c in calls], ["read_file", "run_cmd"]
        )

    def test_no_tools_returns_empty(self):
        text = "好的，我明白了。"
        clean, calls = main._parse_prompt_tool_calls(text)
        self.assertEqual(clean, "好的，我明白了。")
        self.assertEqual(calls, [])

    def test_think_block_only_no_tools(self):
        text = "<think>让我想想用什么工具</think>"
        clean, calls = main._parse_prompt_tool_calls(text)
        self.assertEqual(calls, [])
        # 没有工具调用时原文保留
        self.assertEqual(clean, text)


class TestParseNativeToolCalls(unittest.TestCase):
    """Tests for main._parse_native_tool_calls (Gemma <|tool_call|> format)."""

    def test_gemma_format(self):
        text = '<|tool_call|>call:list_dir{path:<|"|>.<|"|>}<tool_call|>'
        calls = main._parse_native_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "list_dir")
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"path": "."})

    def test_gemma_unquoted_args(self):
        text = "<|tool_call|>call:read_file{path: /tmp/x}<tool_call|>"
        calls = main._parse_native_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"path": "/tmp/x"})

    def test_gemma_multiple_calls(self):
        text = (
            "<|tool_call|>call:list_dir{path: .}<tool_call|>"
            '<|tool_call|>call:read_file{path:<|"|>/etc/hosts<|"|>}<tool_call|>'
        )
        calls = main._parse_native_tool_calls(text)
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            [c["function"]["name"] for c in calls], ["list_dir", "read_file"]
        )

    def test_broken_args_salvaged_without_crash(self):
        # 参数无法转成合法 JSON 时走 salvage 容错，不崩溃且 arguments 仍是合法 JSON
        text = "<|tool_call|>call:run_cmd{command: ls -la}<tool_call|>"
        calls = main._parse_native_tool_calls(text)
        self.assertEqual(len(calls), 1)
        args = json.loads(calls[0]["function"]["arguments"])
        self.assertIsInstance(args, dict)
        self.assertTrue(args)

    def test_no_native_calls(self):
        text = "这是一个普通回复，没有工具调用。"
        self.assertEqual(main._parse_native_tool_calls(text), [])


class TestDeduplicateResponse(unittest.TestCase):
    """Tests for main._deduplicate_response (identity introduction dedup)."""

    def test_duplicate_prefix_removed(self):
        text = "我是辣条，正在帮你查看文件。我是辣条，请稍等。"
        self.assertEqual(
            main._deduplicate_response(text), "我是辣条，正在帮你查看文件。"
        )

    def test_duplicate_with_hello_prefix(self):
        text = "你好，我是辣条，很高兴见到你。你好，我是辣条，请问有什么可以帮你？"
        self.assertEqual(
            main._deduplicate_response(text), "你好，我是辣条，很高兴见到你。"
        )

    def test_duplicate_english_prefix(self):
        text = "我是Latiao，你好。我是Latiao，再见。"
        self.assertEqual(main._deduplicate_response(text), "我是Latiao，你好。")

    def test_no_duplicate_unchanged(self):
        text = "这是一个普通的回复，不重复身份介绍。"
        self.assertEqual(main._deduplicate_response(text), text)

    def test_empty_text(self):
        self.assertEqual(main._deduplicate_response(""), "")

    def test_none_text(self):
        self.assertIsNone(main._deduplicate_response(None))


class TestReflectionTrigger(unittest.TestCase):
    def test_off_never_reflects(self):
        from agent_loop import _should_reflect
        self.assertFalse(_should_reflect("off", "x" * 5000, False))

    def test_light_cloud_long_only(self):
        from agent_loop import _should_reflect
        self.assertTrue(_should_reflect("light", "x" * 900, False))   # 云端长输出
        self.assertFalse(_should_reflect("light", "x" * 500, False))  # 云端短输出
        self.assertFalse(_should_reflect("light", "x" * 900, True))   # 本地不触发

    def test_number_traceability(self):
        from agent_loop import _find_unverified_numbers
        report = "黄金板块本周上涨 12.35%，银行 +3.25%，美债收益率创 19 年新高（30 年期 5.33%），共 4 个交易日"
        tools = ["黄金(板块) 成份区间涨跌幅 12.35% 2026-08-17至2026-08-20", "银行 +3.25%"]
        unverified = _find_unverified_numbers(report, tools)
        self.assertIn("5.33%", unverified)   # 美债数字无查询来源 → 被标记
        self.assertIn("19 年", unverified)
        self.assertNotIn("12.35%", unverified)  # 黄金有来源 → 不标记
        self.assertNotIn("3.25%", unverified)

    def test_plan_trigger(self):
        from agent_loop import _should_plan
        self.assertTrue(_should_plan("帮我分析一下最近的A股大盘走势，写一份详细的行情分析报告，包括各板块表现", False))
        self.assertFalse(_should_plan("你好", False))            # 太短
        self.assertFalse(_should_plan("帮我分析一下最近的A股大盘走势，写一份详细的行情分析报告，包括各板块表现", True))  # 本地不触发
        self.assertFalse(_should_plan("今天天气怎么样啊，你觉得呢", False))  # 无任务关键词

    def test_deep_any_model_long_output(self):
        from agent_loop import _should_reflect
        self.assertTrue(_should_reflect("deep", "x" * 400, False))
        self.assertTrue(_should_reflect("deep", "x" * 400, True))     # 本地也触发（用户自选）
        self.assertFalse(_should_reflect("deep", "x" * 100, False))


class TestAccessMode(unittest.TestCase):
    def _tools(self):
        from agent_loop import TOOLS
        return list(TOOLS)

    def test_read_only_filters(self):
        from agent_loop import _check_access, _filter_tools_by_access
        tools = self._tools()
        ro = _filter_tools_by_access(tools, "read_only")
        names = {t.get("function", {}).get("name") for t in ro}
        self.assertIn("read_file", names)
        self.assertNotIn("run_cmd", names)
        self.assertNotIn("write_file", names)
        self.assertIsNotNone(_check_access("run_cmd", "read_only"))
        self.assertIsNone(_check_access("read_file", "read_only"))

    def test_workspace_blocks_system(self):
        from agent_loop import _check_access, _filter_tools_by_access
        tools = self._tools()
        ws = _filter_tools_by_access(tools, "workspace")
        names = {t.get("function", {}).get("name") for t in ws}
        self.assertIn("write_file", names)   # 工作区可写
        self.assertNotIn("run_cmd", names)   # 系统级禁用
        self.assertNotIn("open_app", names)
        self.assertIsNotNone(_check_access("run_cmd", "workspace"))
        self.assertIsNone(_check_access("write_file", "workspace"))

    def test_full_allows_all(self):
        from agent_loop import _check_access, _filter_tools_by_access
        tools = self._tools()
        self.assertEqual(len(_filter_tools_by_access(tools, "full")), len(tools))
        self.assertIsNone(_check_access("run_cmd", "full"))


if __name__ == "__main__":
    unittest.main()
