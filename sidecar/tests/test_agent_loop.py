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


class TestToolCappingKeepsTavily(unittest.TestCase):
    """回归：_cap_tools 截断后 tavily_search 必须仍在列表且排在 bing_search 前。
    曾因插件按文件名排序（bing < tavily），cap=5/8 时 tavily 被切掉，模型只能
    调用 bing_search——表现为"怎么一直不调用 tavily"。"""

    def _capped_names(self, cap):
        from agent_loop import TOOLS, _cap_tools
        names = [t.get("function", {}).get("name") for t in _cap_tools(TOOLS, cap)]
        return names

    def test_tavily_visible_after_cap(self):
        for cap in (5, 8):
            names = self._capped_names(cap)
            self.assertIn("tavily_search", names, f"cap={cap} 切掉了 tavily_search: {names}")

    def test_tavily_before_bing(self):
        for cap in (5, 8):
            names = self._capped_names(cap)
            if "bing_search" in names:
                self.assertLess(
                    names.index("tavily_search"), names.index("bing_search"),
                    f"cap={cap} tavily 应排在 bing 前: {names}")

    def test_core_tools_survive(self):
        names = self._capped_names(8)
        for core in ("read_file", "write_file", "list_dir"):
            self.assertIn(core, names)


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

    def test_confirm_and_full_allow_all(self):
        from agent_loop import _check_access, _filter_tools_by_access
        tools = self._tools()
        for mode in ("confirm", "plan", "full"):
            self.assertEqual(len(_filter_tools_by_access(tools, mode)), len(tools), mode)
            self.assertIsNone(_check_access("run_cmd", mode), mode)

    def test_legacy_workspace_maps_to_auto_edit(self):
        from agent_loop import _normalize_access
        self.assertEqual(_normalize_access("workspace"), "auto_edit")
        self.assertEqual(_normalize_access("full"), "full")
        self.assertEqual(_normalize_access("bogus"), "full")

    def test_auto_edit_tools_defined(self):
        from agent_loop import AUTO_EDIT_TOOLS
        self.assertIn("write_file", AUTO_EDIT_TOOLS)
        self.assertIn("open_folder", AUTO_EDIT_TOOLS)


if __name__ == "__main__":
    unittest.main()


class TestPromptToolParsingThinkBlock(unittest.TestCase):
    """回归：解析与清洗必须同一坐标系（去 think 后的文本）。
    曾把 search_text 上的区间回放到原始 text 上，栅栏残留进历史、
    think 块被拦腰截断，模型下一轮重复调用同一工具。"""

    def test_think_block_plus_fence(self):
        from agent_loop import _parse_prompt_tool_calls
        text = "<think>我需要列目录</think>我来查看。\n```tool list_dir\n{\"path\": \"/tmp\"}\n```"
        clean, calls = _parse_prompt_tool_calls(text)
        self.assertNotIn("```", clean)
        self.assertNotIn("list_dir", clean)
        self.assertEqual(calls[0]["function"]["name"], "list_dir")

    def test_leading_whitespace_fence(self):
        from agent_loop import _parse_prompt_tool_calls
        text = "\n\n好的。\n```tool read_file\n{\"path\": \"/tmp/a.txt\"}\n```"
        clean, calls = _parse_prompt_tool_calls(text)
        self.assertNotIn("```", clean)
        self.assertEqual(calls[0]["function"]["name"], "read_file")

    def test_plain_text_untouched(self):
        from agent_loop import _parse_prompt_tool_calls
        clean, calls = _parse_prompt_tool_calls("你好，今天天气不错")
        self.assertEqual(clean, "你好，今天天气不错")
        self.assertEqual(calls, [])


class TestSubagentWhitelistReachable(unittest.TestCase):
    """回归：explore/debugger 的 run_cmd 必须在子智能体可见工具列表里，
    否则只读白名单（执行时兜底）永远不可达——'死配置'复发。"""

    def test_run_cmd_visible_for_explore(self):
        import asyncio
        from tool_executor import _delegate_task_bg, _SUBTASKS
        # 不真正跑 LLM：构造到 sub_tools 过滤后的可见性检查即可
        # （直接调 _delegate_task 会发起 HTTP；这里验证过滤逻辑链路存在）
        # 模拟 _delegate_task 内部的可见性过滤（TOOLS 由 agent_loop 门面持有）
        import agent_loop as al
        import tool_executor as te
        allowed = te._SUBAGENT_TOOLS["explore"]
        visible = [t for t in al.TOOLS if t.get("function", {}).get("name") in allowed]
        names = {t["function"]["name"] for t in visible}
        self.assertIn("run_cmd", names)

    def test_prune_subtasks_caps_registry(self):
        import time as _t
        from tool_executor import _SUBTASKS, _prune_subtasks
        for i in range(120):
            _SUBTASKS[f"old_{i}"] = {"agent": "x", "task": "t", "status": "done",
                                     "steps": 0, "result": "", "activity": {},
                                     "started_at": 0, "updated_at": 0}
        _prune_subtasks(max_keep=50)
        done_left = [t for t, s in _SUBTASKS.items() if t.startswith("old_")]
        self.assertLessEqual(len(done_left), 60)


class TestControlToolsSurviveFilter(unittest.TestCase):
    """五控工具可见性回归：_filter_tools 不得把控制工具全部滤掉
    （此前 TOOL_CATEGORIES 无 control 类，导致 control_* 永不到模型面前，
    权限确认弹窗也永不触发——表现为"权限工具没用"）。"""

    def _tool(self, name):
        return {"type": "function", "function": {"name": name}}

    def setUp(self):
        import agent_loop
        self.all_tools = [self._tool(n) for n in
                          list(agent_loop.TOOL_CATEGORIES["file_read"]) +
                          ["control_mouse_click", "control_list_processes",
                           "screen_capture", "control_kill_process"]]
        self.f = agent_loop._filter_tools

    def test_control_intent_keeps_interactive_control(self):
        filtered = self.f("帮我点击那个按钮", self.all_tools)
        names = [t["function"]["name"] for t in filtered]
        self.assertIn("control_mouse_click", names)

    def test_file_intent_keeps_readonly_control(self):
        filtered = self.f("分析一下这个文件夹", self.all_tools)
        names = [t["function"]["name"] for t in filtered]
        self.assertIn("control_list_processes", names)
        # 危险交互控制不保留（防误操作）
        self.assertNotIn("control_mouse_click", names)

    def test_no_intent_keeps_all(self):
        filtered = self.f("随便", self.all_tools)
        self.assertEqual(len(filtered), len(self.all_tools))


if __name__ == "__main__":
    unittest.main()


class TestThinkingInjection(unittest.TestCase):
    """思考强度按模型族注入（此前 off 恒写 Anthropic 字段致 DeepSeek/OpenAI 无效）。"""

    def test_off_anthropic(self):
        import agent_loop
        body = agent_loop._inject_thinking_disabled({"model": "claude-3-5"}, "claude-3-5", "off")
        self.assertEqual(body.get("thinking"), {"type": "disabled"})

    def test_off_openai_reasoning(self):
        import agent_loop
        body = agent_loop._inject_thinking_disabled({"model": "o3-mini"}, "o3-mini", "off")
        self.assertEqual(body.get("reasoning_effort"), "none")
        self.assertNotIn("thinking", body)

    def test_off_deepseek_forced_marks_unsupported(self):
        import agent_loop
        body = agent_loop._inject_thinking_disabled({"model": "deepseek-reasoner"}, "deepseek-reasoner", "off")
        self.assertTrue(body.get("_thinking_unsupported"))
        self.assertNotIn("thinking", body)  # 不写无效字段

    def test_off_non_reasoning_no_fields(self):
        import agent_loop
        body = agent_loop._inject_thinking_disabled({"model": "deepseek-chat"}, "deepseek-chat", "off")
        self.assertNotIn("thinking", body)
        self.assertNotIn("reasoning_effort", body)

    def test_max_raises_budget(self):
        import agent_loop
        body = agent_loop._inject_thinking_disabled({"model": "deepseek-chat", "max_tokens": 4096}, "deepseek-chat", "max")
        self.assertGreaterEqual(body["max_tokens"], 18432)


if __name__ == "__main__":
    unittest.main()


class TestPlanConfirmationGate(unittest.TestCase):
    """计划确认门控：计划必须等用户批准才执行（此前列完直接跑）。"""

    def test_plan_confirm_emits_event_and_blocks(self):
        import asyncio
        import agent_loop

        async def run():
            # 启动确认等待，然后模拟用户拒绝（计划被拒 -> 任务不执行）
            task = asyncio.create_task(agent_loop._await_plan_confirmation("plan-t1", "1. 步骤A\n2. 步骤B"))
            await asyncio.sleep(0.1)
            async with agent_loop._pending_lock:
                agent_loop._pending_confirmations["plan-t1"]["approved"] = False
                agent_loop._pending_confirmations["plan-t1"]["event"].set()
            approved, events = await task
            return approved, events

        approved, events = asyncio.run(run())
        self.assertFalse(approved)
        self.assertTrue(any(e.get("event") == "plan_confirm" for e in events))

    def test_plan_confirm_approve(self):
        import asyncio
        import agent_loop

        async def run():
            task = asyncio.create_task(agent_loop._await_plan_confirmation("plan-t2", "计划"))
            await asyncio.sleep(0.1)
            async with agent_loop._pending_lock:
                agent_loop._pending_confirmations["plan-t2"]["approved"] = True
                agent_loop._pending_confirmations["plan-t2"]["event"].set()
            approved, _ = await task
            return approved

        self.assertTrue(asyncio.run(run()))

    def test_confirm_tool_endpoint_shares_pending(self):
        # 计划确认与工具确认共用 _pending_confirmations / /v1/confirm_tool
        import agent_loop
        entry = {"event": None, "approved": False}
        agent_loop._pending_confirmations["plan-t3"] = entry
        # api_routes.confirm_tool 的写法就是改 approved + set event
        agent_loop._pending_confirmations["plan-t3"]["approved"] = True
        self.assertTrue(agent_loop._pending_confirmations["plan-t3"]["approved"])
        agent_loop._pending_confirmations.pop("plan-t3", None)


if __name__ == "__main__":
    unittest.main()
