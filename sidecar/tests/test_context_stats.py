"""上下文统计测试：估算器边界、类别归集、真实用量与缓存命中率。

注意：测试环境（CI）没有模型文件，因此只覆盖"估算 + 归集 + 用量记录"这些
不依赖 tokenizer 的路径；精确计数（vocab_only / tokenizers）在实现时用真机
实测校验过（GGUF 与 MLX 两路计数一致，误差 0）。
"""
from unittest import mock
import time
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent.tool_exec as TE  # noqa: E402  （工具执行簇 2026-09-23 拆出：patch 面在此）

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
        # 09-19 修正：prompt_n 是"重算"的 token，cache_n 是"复用"的，分母应为两者之和
        cs.record_usage(self.SESSION, None, {"prompt_n": 271, "cache_n": 883})
        st = cs.stats(self.SESSION)
        self.assertAlmostEqual(st["cache_hit_rate"], 883 / 1154, places=4)

    def test_timings_full_hit_and_cold(self):
        cs.record_usage(self.SESSION, None, {"prompt_n": 1, "cache_n": 1153})    # 全命中
        self.assertGreater(cs.stats(self.SESSION)["cache_hit_rate"], 0.99)   # 1153/1154
        cs.reset(self.SESSION)
        cs.record_request(self.SESSION, [{"role": "user", "content": "x"}], [], limit=8000)
        cs.record_usage(self.SESSION, None, {"prompt_n": 1154, "cache_n": 0})   # 冷启动
        self.assertAlmostEqual(cs.stats(self.SESSION)["cache_hit_rate"], 0.0, places=3)

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
            cs.record_usage(self.SESSION, None, {"prompt_n": 50, "cache_n": 50})   # 50/(50+50)
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

class TestInfoSaturation(unittest.TestCase):
    """信息增量饱和（09-19）：把"数据够了没"变成可计算状态，治换词重搜。"""

    def test_info_points_normalize(self):
        from agent.loop import _info_points
        pts = _info_points("道琼斯指数下跌 0.18%（2026-09-18），来源 https://a.b/c/d?e=1")
        self.assertIn("#0.18", pts)
        self.assertIn("#d:09-18", pts)   # 年月日归一（年可省，避免带年/不带年算两个 token）
        self.assertTrue(any(p.startswith("道琼") for p in pts))
        self.assertFalse(any("a.b" in p or "https" in p for p in pts))   # 链接被剥

    def test_repeat_scores_low_gain(self):
        # 真实形态：换个 query 重搜，返回的仍是同一批快讯（内容重复、仅顺序/措辞不同）
        from agent.loop import _info_points, _info_gain, _INFO_GAIN_RATIO
        base = ("美股三大指数涨跌不一，道指跌0.18%报51682.64点，纳指涨0.39%报26522.55点，"
                "标普500涨0.17%报7650.5点。半导体板块走强，闪迪涨近11%。")
        reordered = ("标普500涨0.17%报7650.5点；纳指涨0.39%报26522.55点；道指跌0.18%报51682.64点。"
                     "闪迪涨近11%，半导体板块走强。（来源：同花顺）")
        gain, _new = _info_gain(reordered, _info_points(base))
        self.assertLess(gain, _INFO_GAIN_RATIO)      # 同批数据复述 → 低增量（实测 12%）

    def test_new_topic_scores_high_gain(self):
        from agent.loop import _info_points, _info_gain
        seen = _info_points("道指跌0.18%报51682.64点，纳指涨0.39%")
        gain, _new = _info_gain("苹果发布新款芯片，出货量同比增长23%，营收120亿美元", seen)
        self.assertGreater(gain, 0.5)

    def test_search_tools_filtered(self):
        from agent.loop import _filter_search_tools, _search_tool_names
        tools = [
            {"type": "function", "function": {"name": "tavily_search"}},
            {"type": "function", "function": {"name": "mx_query"}},
            {"type": "function", "function": {"name": "read_file"}},
            {"type": "function", "function": {"name": "use_skill"}},
        ]
        kept = [t["function"]["name"] for t in _filter_search_tools(tools)]
        self.assertEqual(kept, ["read_file", "use_skill"])
        self.assertIn("tavily_search", _search_tool_names())


def _request_blob(body) -> str:
    """请求里"工具清单"可能在两处：body['tools']（原生工具后端：mlx / llama-server）
    或尾部围栏说明（python 引擎走文字教学）。两种都算"工具仍可用"。"""
    import json as _json
    return (_json.dumps(body.get("tools", []), ensure_ascii=False)
            + _json.dumps(body["messages"], ensure_ascii=False))


def _system_text(body) -> str:
    """请求里所有 system 角色消息的拼接（提示头）。"""
    return "\n".join(str(m.get("content") or "") for m in body["messages"]
                     if m.get("role") == "system")


class TestNarrowedRequest(unittest.TestCase):
    """软饱和后的约束表达：工具表保持不变，约束走**尾部提醒**。

    09-19 缓存实测：tools 参与提示头渲染，撤工具＝换提示头＝整段缓存失效（用户会话
    读到 0% 的直接成因之一）。所以软饱和只改提示词，不改请求形态。
    """

    def _loop(self):
        from agent.loop import ThinAgentLoop
        return ThinAgentLoop([{"role": "user", "content": "分析周五美股走势"}], "m",
                             "http://127.0.0.1:9/v1/chat/completions", {}, "s-test")

    def test_narrow_keeps_tools_and_reminds_at_tail(self):
        loop = self._loop()
        loop._narrow_search = True
        loop._search_used = 3
        body = loop._build_request("m")
        blob = _request_blob(body)
        self.assertIn("tavily_search", blob)       # 工具表稳定 → 前缀缓存可用
        self.assertIn("mx_query", blob)
        self.assertNotIn("检索预算", _system_text(body))   # 提醒不得进系统提示（提示头）
        self.assertIn("检索预算", body["messages"][-1]["content"])
        self.assertIn("不再检索", body["messages"][-1]["content"])

    def test_wide_open_request_has_search_tools(self):
        loop = self._loop()
        body = loop._build_request("m")
        blob = _request_blob(body)
        self.assertIn("tavily_search", blob)
        self.assertNotIn("检索预算", _system_text(body))

    def test_finalize_round_is_append_only(self):
        """收口轮必须是"同一前缀 + 尾部追加"：前面逐字不变，只在末尾接指令。

        末尾若已是 user 消息就并进去、否则新起一条——两者在 token 层面都是纯追加，
        都能复用前缀缓存（这正是收口轮从 0% 变成接近全命中的原因）。
        """
        loop = self._loop()
        before = loop._build_request("m")
        loop._finalize_round = True
        after = loop._build_request("m")
        self.assertGreaterEqual(len(after["messages"]), len(before["messages"]))
        self.assertEqual(after["messages"][:len(before["messages"]) - 1],
                         before["messages"][:len(before["messages"]) - 1])
        self.assertTrue(after["messages"][len(before["messages"]) - 1]["content"]
                        .startswith(before["messages"][-1]["content"]))
        self.assertIn("收尾", after["messages"][-1]["content"])
        # tools 不撤：撤了下一轮的提示头就与上一轮无公共前缀，缓存照样 0%
        self.assertEqual([t["function"]["name"] for t in after.get("tools", [])],
                         [t["function"]["name"] for t in before.get("tools", [])])

class TestBoundedToolExecution(unittest.TestCase):
    """工具执行护栏（09-19 事故：search_files 递归扫家目录 → 整轮卡死 21 分钟无输出）。"""

    def setUp(self):
        from pathlib import Path
        self.tmp = Path(tempfile.mkdtemp(prefix="search-guard-"))
        (self.tmp / "keep").mkdir()
        (self.tmp / "keep" / "a.md").write_text("x", encoding="utf-8")
        (self.tmp / "node_modules").mkdir()
        (self.tmp / "node_modules" / "b.md").write_text("x", encoding="utf-8")
        deep = self.tmp
        for i in range(10):
            deep = deep / f"d{i}"
        deep.mkdir(parents=True, exist_ok=True)
        (deep / "c.md").write_text("x", encoding="utf-8")
        for i in range(260):
            (self.tmp / "keep" / f"many{i}.md").write_text("x", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_large_dirs_skipped(self):
        from tool_executor import search_files
        out = search_files(str(self.tmp), "b.md")          # b.md 只在 node_modules 里
        self.assertIn("No files matching", out)            # 被跳过的目录确实没扫
        self.assertIn("已跳过", out)                        # 且明确告知
        out2 = search_files(str(self.tmp), "a.md")         # 正常文件仍能找到
        self.assertIn("a.md", out2)

    def test_results_capped(self):
        from tool_executor import search_files
        out = search_files(str(self.tmp), "many*.md")
        self.assertIn("上限", out)                          # 明确告知已截断
        self.assertLessEqual(out.count("📄"), 50)           # 展示条数上限

    def test_depth_limited(self):
        from tool_executor import search_files
        out = search_files(str(self.tmp), "c.md")
        self.assertIn("No files matching", out)             # 10 层深 > 默认 6 层

    def test_missing_dir(self):
        from tool_executor import search_files
        self.assertIn("No such directory", search_files("/definitely/not/here", "*.md"))

    def test_tool_timeout_table(self):
        from agent_loop import _tool_timeout_for, _TOOL_TIMEOUT_DEFAULT
        self.assertLessEqual(_tool_timeout_for("search_files"), 30)
        self.assertLessEqual(_tool_timeout_for("tavily_search"), 60)
        self.assertEqual(_tool_timeout_for("no_such_tool"), _TOOL_TIMEOUT_DEFAULT)

class TestToolTimeoutWrapper(unittest.TestCase):
    """工具执行超时（09-19）：把"工具超时"作为结果回报模型，而不是卡住整轮。"""

    def test_timeout_returns_result_not_raise(self):
        import asyncio
        import agent_loop
        from agent_loop import _handle_tool_execution

        async def slow_inner(*a, **k):
            await asyncio.sleep(3)
            return False, []

        orig_inner, orig_limit = TE._handle_tool_execution_inner, agent_loop._TOOL_TIMEOUTS.get("read_file")
        TE._handle_tool_execution_inner = slow_inner
        agent_loop._TOOL_TIMEOUTS["read_file"] = 0.2
        try:
            tc = {"id": "call-1", "function": {"name": "read_file", "arguments": "{}"}}
            # asyncio.run 而非 get_event_loop().run_until_complete：后者自 Python 3.12
            # 起不再隐式创建事件循环，在 3.14（本仓测试 venv）上直接抛 RuntimeError
            # （产品自带解释器是 3.11，所以这条只在测试环境炸）。
            verify_failed, events = asyncio.run(
                _handle_tool_execution(tc, [], "sess-timeout", "latiao", "read_only"))
        finally:
            TE._handle_tool_execution_inner = orig_inner
            if orig_limit is not None:
                agent_loop._TOOL_TIMEOUTS["read_file"] = orig_limit
        self.assertFalse(verify_failed)                  # 超时不算验证失败
        self.assertEqual(events[0]["event"], "tool_end")
        self.assertIn("工具超时", events[0]["result"])
        self.assertIn("直接作答", events[0]["result"])     # 给出可执行指引

class TestReplayGate(unittest.TestCase):
    """流式复读拦截 + 预算提醒口径（09-19 回归：工具还在却下发"不要再检索"→模型原地复读）。"""

    def _loop(self):
        from agent.loop import ThinAgentLoop
        return ThinAgentLoop([{"role": "user", "content": "分析周五美股走势"}], "m",
                             "http://127.0.0.1:9/v1/chat/completions", {}, "s-replay")

    def test_verbatim_replay_detected(self):
        from agent.loop import _looks_like_replay
        dup = "The user is asking me to analyze Friday's US stock market trends. " * 3
        self.assertTrue(_looks_like_replay(dup, dup[:60]))
        self.assertFalse(_looks_like_replay(dup, "苹果发布新款芯片，营收120亿美元。"))
        self.assertFalse(_looks_like_replay("好的，我马上查。", "好的，我马上查。"))   # 短句不误伤

    def test_gate_suppresses_whole_round(self):
        loop = self._loop()
        dup = "The user is asking me to analyze Friday's US stock market trends. " * 3
        loop._last_round_text = dup
        self.assertIsNone(loop._gate_round_content(dup[:30]))    # 缓冲
        self.assertIsNone(loop._gate_round_content(dup[30:70]))  # 判定复读 → 不下发
        self.assertIsNone(loop._gate_round_content("...same text again..."))

    def test_gate_passes_new_text(self):
        loop = self._loop()
        loop._last_round_text = "上一轮的正文内容，长度足够触发判定阈值" * 5
        fresh = "本轮是全新的分析结论：道指跌0.18%，纳指涨0.39%，半导体走强。" * 2
        first = loop._gate_round_content(fresh[:30])
        self.assertIsNone(first)                                  # 仍在缓冲
        rest = loop._gate_round_content(fresh[30:])
        self.assertIn("道指跌0.18%", rest)                         # 判定非复读 → 放出缓冲

    def test_reminder_does_not_contradict_available_tools(self):
        loop = self._loop()
        loop._search_used = 2
        loop._narrow_search = False
        text = loop._search_reminder()
        self.assertIn("还剩", text)                                # 报额度
        self.assertNotIn("不要再检索", text)                        # 不说与工具可用相矛盾的话
        loop._narrow_search = True
        # 软饱和：工具仍在册（不撤），提醒改口径为"本轮不再检索"
        self.assertIn("不再检索", loop._search_reminder())
        self.assertIn("不要再调用检索类工具", loop._search_reminder())

    def test_budget_exhaustion_keeps_tools_and_reminds(self):
        """预算耗尽不再撤工具：改由尾部提醒表达（撤工具会换提示头 → 缓存失效）。"""
        from agent.loop import _SEARCH_BUDGET_CALLS
        loop = self._loop()
        loop._search_used = _SEARCH_BUDGET_CALLS
        loop._narrow_search = True
        body = loop._build_request("m")
        self.assertIn("tavily_search", _request_blob(body))
        self.assertIn("不再检索", body["messages"][-1]["content"])

class TestToolCallDialects(unittest.TestCase):
    """模型自带方言（09-20）：<tool_call>{"name":…,"arguments":…}</tool_call>。

    27B coder 类模型的默认形态；此前不认 → 标记被流式清洗丢掉、只剩那句
    "我来帮你分析…先调取数据" → 循环当终答交付（"执行到一半就停"）。
    """

    def _parse(self, text):
        from agent.parsing import _parse_prompt_tool_calls
        return _parse_prompt_tool_calls(text)

    def test_json_body_dialect(self):
        body, calls = self._parse(
            '<tool_call>\n{"name": "tavily_search", "arguments": {"query": "A股 周五"}}\n</tool_call>')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "tavily_search")
        self.assertIn("A股", calls[0]["function"]["arguments"])
        self.assertEqual(body.strip(), "")          # 标记必须从正文剥离

    def test_unknown_tool_name_still_parsed(self):
        """编造的工具名也要解析出来 → 循环据此回"可用工具清单"让它自纠，而不是静默丢弃。"""
        _body, calls = self._parse('<tool_call>{"name": "web_search", "arguments": {"query": "x"}}</tool_call>')
        self.assertEqual([c["function"]["name"] for c in calls], ["web_search"])

    def test_alias_keys_and_string_arguments(self):
        _b, calls = self._parse('<tool_call>{"tool": "mx_query", "args": {"query": "大盘"}}</tool_call>')
        self.assertEqual([c["function"]["name"] for c in calls], ["mx_query"])
        _b2, calls2 = self._parse(
            '<tool_call>{"name": "tavily_search", "arguments": "{\\"query\\": \\"A股\\"}"}</tool_call>')
        self.assertEqual([c["function"]["name"] for c in calls2], ["tavily_search"])

    def test_plain_prose_not_misparsed(self):
        text = "我来帮你分析周五的大盘走势，先调取相关数据和复盘方法论。"
        _b, calls = self._parse(text)
        self.assertEqual(calls, [])


class TestPlanOnlyGate(unittest.TestCase):
    """空转闸门：只声明计划、工具调用=0 → 不能当终答交付。"""

    def test_detects_plan_declaration(self):
        from agent.loop import _looks_like_plan_only
        self.assertTrue(_looks_like_plan_only(
            "我来帮你分析上周五（9月18日）的大盘走势。先调取相关数据和复盘方法论。", True))
        self.assertTrue(_looks_like_plan_only("让我先查询一下最近的行情数据。", True))

    def test_ignores_substantive_or_toolfree(self):
        from agent.loop import _looks_like_plan_only
        long_answer = "我来总结：" + "道指跌0.18%，标普涨0.17%，纳指涨0.39%。" * 12
        self.assertFalse(_looks_like_plan_only(long_answer, True))   # 长文＝真内容
        self.assertFalse(_looks_like_plan_only("我来帮你分析大盘走势。", False))  # 无工具时不拦
        self.assertFalse(_looks_like_plan_only("1 + 1 = 2。", True))


class TestHeadStabilityAcrossTurns(unittest.TestCase):
    """提示头必须逐轮一致（含工具表）：混合/线性注意力模型的缓存只在"严格续写"
    时复用，头部一变该轮就是 0%（09-20 用户实测：普通对话两轮 0%）。"""

    def _loop(self, text):
        from agent.loop import ThinAgentLoop
        return ThinAgentLoop([{"role": "user", "content": text}], "m",
                             "http://127.0.0.1:9/v1/chat/completions", {}, "s-head")

    def test_head_stable_from_first_turn(self):
        """本地引擎：**从第 1 轮起**头部就固定（工具表=整套），不随措辞变化。"""
        import local_llm, context_stats as cs
        local_llm._engine._active_backend = "llama-cpp-native"
        heads, names = [], []
        for q in ("你好呀", "分析周五A股大盘走势", "帮我写个 python 脚本存文件",
                  "读一下 README.md", "今天心情不错"):
            b = self._loop(q)._build_request("m")
            heads.append(cs._head_fp(b["messages"], b.get("tools")))
            names.append([t["function"]["name"] for t in b.get("tools", [])])
        self.assertEqual(len(set(heads)), 1, f"头部应逐轮一致，实际 {heads}")
        self.assertEqual(len({tuple(n) for n in names}), 1, "工具表应逐轮一致")

    def test_head_stabilizes_once_tool_set_saturates(self):
        """关键不变量：工具表**饱和后**，无论问什么（闲聊/行情/写文件），头部逐字一致。

        用户实测的 0% 就出在这里：旧实现每轮按措辞重算工具表（14↔12 交替）→ 头部
        每轮都变 → 混合/线性注意力模型无法复用。新实现只增不减，饱和后即稳定。
        """
        import local_llm, context_stats as cs
        local_llm._engine._active_backend = "llama-cpp-native"
        for q in ("分析周五A股大盘走势", "你好呀", "帮我写个 python 脚本存文件",
                  "读一下 README.md", "我心情不错"):
            self._loop(q)._build_request("m")          # 走几轮不同类型的请求 → 收敛
        a = self._loop("随便聊聊")._build_request("m")
        b = self._loop("分析一下今天大盘")._build_request("m")
        self.assertEqual(cs._head_fp(a["messages"], a.get("tools")),
                         cs._head_fp(b["messages"], b.get("tools")))
        self.assertEqual([t["function"]["name"] for t in a.get("tools", [])],
                         [t["function"]["name"] for t in b.get("tools", [])])

    def test_tool_set_grows_monotonically_never_shrinks(self):
        """只增不减：先闲聊（少）再任务类（多）→ 第二轮增长，之后保持。"""
        import local_llm
        local_llm._engine._active_backend = "llama-cpp-native"
        self._loop("你好呀")._build_request("m")                       # 闲聊定型（较少）
        grow = self._loop("分析周五A股大盘走势")._build_request("m")     # 任务类 → 增长
        after = self._loop("随便聊聊天")._build_request("m")            # 再闲聊 → 不回退
        g = [t["function"]["name"] for t in grow.get("tools", [])]
        a = [t["function"]["name"] for t in after.get("tools", [])]
        self.assertEqual(g, a)
        self.assertGreaterEqual(len(g), 1)


class TestToolPromptPlacement(unittest.TestCase):
    """围栏工具说明必须放尾部（放系统提示头部会被长提示稀释 → 模型自述不行动）。"""

    def _loop(self):
        from agent.loop import ThinAgentLoop
        return ThinAgentLoop([{"role": "user", "content": "分析周五大盘走势"}], "m",
                             "http://127.0.0.1:9/v1/chat/completions", {}, "s-fence")

    def test_native_gate_uses_active_backend_not_default(self):
        """病根回归：默认后端是 mlx 但实际跑 python 引擎时，必须判为"不支持原生工具"
        （否则既不发工具 schema 也不发围栏说明 → 模型只能编工具名、只说不做）。"""
        import local_llm
        from agent.context import _local_native_tools_ok
        eng = local_llm._engine
        old_active, old_default = eng._active_backend, eng.backend
        try:
            eng.backend = "mlx"                   # 装了 mlx-lm 时的默认后端
            eng._active_backend = "llama-cpp"     # 实际在跑 python 引擎
            self.assertFalse(_local_native_tools_ok())
            eng._active_backend = "llama-cpp-native"
            self.assertTrue(_local_native_tools_ok())     # 原生引擎 → 原生工具
            eng._active_backend = "mlx"
            self.assertTrue(_local_native_tools_ok())
        finally:
            eng._active_backend, eng.backend = old_active, old_default

    def test_fence_prompt_is_tail_not_system(self):
        import local_llm
        loop = self._loop()
        old_active = local_llm._engine._active_backend
        local_llm._engine._active_backend = "llama-cpp"   # python 引擎场景
        try:
            body = loop._build_request("m")
        finally:
            local_llm._engine._active_backend = old_active
        self.assertNotIn("调用格式", _system_text(body))       # 不在系统提示里
        self.assertNotIn("工具使用说明", _system_text(body))
        self.assertIn("工具使用说明", str(body["messages"][-1].get("content") or ""))
        self.assertIn("```tool", body["messages"][-1]["content"])


class TestCustomEngine(unittest.TestCase):
    """路线 A：custom_engine 配置（第三方 fork 引擎当辣条本地后端）。"""

    def setUp(self):
        import pathlib as _pl, os
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = _pl.Path(self.tmp.name) / "their-llama-server"
        self.bin.write_text("#!/bin/sh\n")
        self.cfg = _pl.Path(self.tmp.name) / "config.json"
        import local_llm
        self._ll = local_llm
        self._old_cfg_fn = local_llm._config_file
        local_llm._config_file = lambda: self.cfg
        self._env = dict(os.environ)
        for k in ("LATIAO_CUSTOM_ENGINE", "LATIAO_CUSTOM_ENGINE_BIN",
                  "LATIAO_CUSTOM_ENGINE_ARGS"):
            os.environ.pop(k, None)

    def tearDown(self):
        import os
        self._ll._config_file = self._old_cfg_fn
        os.environ.clear(); os.environ.update(self._env)
        self.tmp.cleanup()

    def _write(self, obj):
        import json
        self.cfg.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")

    def test_disabled_by_default(self):
        from local_llm import _custom_engine_spec
        self._write({})
        self.assertEqual(_custom_engine_spec("/x/y.gguf"), {})

    def test_reads_binary_args_and_name(self):
        from local_llm import _custom_engine_spec
        self._write({"custom_engine": {"enabled": True, "binary": str(self.bin),
                                       "args": ["--jinja", "-ngl", "999"], "name": "prism"}})
        spec = _custom_engine_spec("/x/y.gguf")
        self.assertEqual(spec["binary"], str(self.bin))
        self.assertEqual(spec["args"], ["--jinja", "-ngl", "999"])
        self.assertEqual(spec["name"], "prism")

    def test_match_filter_limits_to_that_model(self):
        from local_llm import _custom_engine_spec
        self._write({"custom_engine": {"enabled": True, "binary": str(self.bin),
                                       "match": "Bonsai"}})
        self.assertEqual(_custom_engine_spec("/models/Ternary-Bonsai-2-27B-PQ2_0.gguf/x.gguf")["name"], "custom")
        self.assertEqual(_custom_engine_spec("/models/Qwen3.8-27B-Q4_K_M.gguf"), {})   # 不匹配 → 走自带引擎

    def test_missing_binary_or_disable_flag(self):
        import os
        from local_llm import _custom_engine_spec
        self._write({"custom_engine": {"enabled": True, "binary": "/nope/llama-server"}})
        self.assertEqual(_custom_engine_spec("/x.gguf"), {})          # 二进制不存在 → 忽略
        self._write({"custom_engine": {"enabled": True, "binary": str(self.bin)}})
        os.environ["LATIAO_CUSTOM_ENGINE"] = "0"
        self.assertEqual(_custom_engine_spec("/x.gguf"), {})          # 显式关闭

    def test_env_overrides_config(self):
        import os
        from local_llm import _custom_engine_spec
        self._write({"custom_engine": {"binary": "/nope/x"}})
        os.environ["LATIAO_CUSTOM_ENGINE_BIN"] = str(self.bin)
        os.environ["LATIAO_CUSTOM_ENGINE_ARGS"] = "--jinja -c 4096"
        spec = _custom_engine_spec("/x.gguf")
        self.assertEqual(spec["binary"], str(self.bin))
        self.assertEqual(spec["args"], ["--jinja", "-c", "4096"])

    def test_custom_engine_omits_our_sampling_params(self):
        """自定义引擎不推我们的 temp 0.0 / freq_penalty 0.6，交给模型自带推荐值。"""
        import local_llm
        from agent.loop import ThinAgentLoop
        loop = ThinAgentLoop([{"role": "user", "content": "分析周五大盘走势"}], "m",
                             "http://127.0.0.1:9/v1/chat/completions", {}, "s-custom")
        eng = local_llm._engine
        old_active = eng._active_backend
        try:
            eng._active_backend = "llama-cpp-native"
            body_native = loop._build_request("m")
            self.assertIn("temperature", body_native)
            self.assertIn("frequency_penalty", body_native)
            eng._active_backend = "llama-cpp-custom"
            body_custom = loop._build_request("m")
            self.assertNotIn("temperature", body_custom)
            self.assertNotIn("frequency_penalty", body_custom)
        finally:
            eng._active_backend = old_active

    def test_custom_backend_counts_as_native_tools(self):
        """自定义后端＝llama.cpp 系服务：应视作支持原生工具调用（否则模型看不到工具清单）。"""
        import local_llm
        from agent.context import _local_native_tools_ok
        eng = local_llm._engine
        old = eng._active_backend
        try:
            eng._active_backend = "llama-cpp-custom"
            self.assertTrue(_local_native_tools_ok())
        finally:
            eng._active_backend = old


class TestGgufPrecheck(unittest.TestCase):
    """GGUF 预检（09-20）：把"量化类型不支持 / 文件不完整"从"架构不支持"里分出来。

    背景：Ternary-Bonsai-2-27B-PQ2_0 用 ggml 类型 142（fork 私有），而应用旧逻辑
    把引擎的 "invalid ggml type" 一并说成"架构不被支持，去换 MLX / 等上游"。
    """

    def _gguf(self, tmp, tensor_type: int, dims, data_bytes: int, offset: int = 0):
        import struct
        from pathlib import Path
        buf = bytearray()
        buf += b"GGUF" + struct.pack("<I", 3)
        buf += struct.pack("<QQ", 1, 1)                      # 1 tensor, 1 kv
        k = b"general.architecture"
        buf += struct.pack("<Q", len(k)) + k
        buf += struct.pack("<I", 8)                          # 字符串
        v = b"qwen35"
        buf += struct.pack("<Q", len(v)) + v
        name = b"output.weight"
        buf += struct.pack("<Q", len(name)) + name
        buf += struct.pack("<I", len(dims))
        for d in dims:
            buf += struct.pack("<Q", d)
        buf += struct.pack("<I", tensor_type)
        buf += struct.pack("<Q", offset)
        path = Path(tmp) / "m.gguf"
        path.write_bytes(bytes(buf) + b"\0" * data_bytes)
        return str(path)

    def test_flags_private_quant_type(self):
        from local_llm import _gguf_scan, _gguf_precheck
        with tempfile.TemporaryDirectory() as tmp:
            path = self._gguf(tmp, 142, [5120, 248320], 4096)
            scan = _gguf_scan(path)
            self.assertIsNotNone(scan)
            self.assertEqual(scan["types"], {142: 1})
            msg = _gguf_precheck(path)
            self.assertIn("量化格式不被当前引擎支持", msg)
            self.assertIn("类型 142", msg)
            self.assertNotIn("架构", msg.split("这通常不是架构问题")[0])   # 不再甩给架构

    def test_flags_truncated_file(self):
        from local_llm import _gguf_precheck
        with tempfile.TemporaryDirectory() as tmp:
            # Q8_0(8)：256 元素/块、34 字节/块 → dims [256,256] 需 69632 字节，只给 4096
            path = self._gguf(tmp, 8, [256, 256], 4096)
            msg = _gguf_precheck(path)
            self.assertIn("文件不完整", msg)
            self.assertIn("还缺", msg)

    def test_complete_standard_file_passes(self):
        from local_llm import _gguf_precheck
        with tempfile.TemporaryDirectory() as tmp:
            path = self._gguf(tmp, 8, [256, 256], 69632)
            self.assertEqual(_gguf_precheck(path), "")

    def test_garbage_file_fails_open(self):
        import pathlib as _pl
        from local_llm import _gguf_precheck, _gguf_scan
        with tempfile.TemporaryDirectory() as tmp:
            path = _pl.Path(tmp) / "x.gguf"
            path.write_bytes(b"NOT-A-GGUF" * 100)
            self.assertIsNone(_gguf_scan(str(path)))
            self.assertEqual(_gguf_precheck(str(path)), "")   # 读不懂 → 放行


class TestParallelToolSafety(unittest.TestCase):
    """并发工具名单（09-20）：mx_query 在并发下会偶发 NoneType 失败，必须串行。"""

    def test_mx_query_not_parallel(self):
        import inspect
        from agent import loop as L
        src = inspect.getsource(L.ThinAgentLoop.run)
        start = src.index("_SAFE_PARALLEL_TOOLS")
        block = src[start:start + 260]
        self.assertNotIn("\"mx_query\"", block)


class TestCustomEngineTarget(unittest.TestCase):
    """自定义引擎的加载目标检查（09-20）：GGUF 文件 / MLX 目录都要能识别。"""

    def test_gguf_file_and_directory(self):
        import pathlib as _pl
        from local_llm import _custom_engine_target
        with tempfile.TemporaryDirectory() as tmp:
            d = _pl.Path(tmp)
            (d / "m.gguf").write_bytes(b"GGUF" + b"\0" * 32)
            self.assertEqual(_custom_engine_target(str(d / "m.gguf"))[0], "ok")
            self.assertEqual(_custom_engine_target(str(d))[0], "ok")          # 目录里有 gguf
            self.assertEqual(_custom_engine_target(str(d))[1], str(d / "m.gguf"))
            bad = d / "bad.gguf"
            bad.write_bytes(b"NOPE" + b"\0" * 32)
            self.assertEqual(_custom_engine_target(str(bad))[0], "err")

    def test_mlx_pack_directory(self):
        import pathlib as _pl, json
        from local_llm import _custom_engine_target
        with tempfile.TemporaryDirectory() as tmp:
            d = _pl.Path(tmp)
            (d / "config.json").write_text(json.dumps({"model_type": "prism_hadamard_qwen35"}))
            (d / "model.safetensors").write_bytes(b"")
            ok, target = _custom_engine_target(str(d))
            self.assertEqual(ok, "ok")
            self.assertEqual(target, str(d))
            empty = _pl.Path(tmp) / "empty"
            empty.mkdir()
            self.assertEqual(_custom_engine_target(str(empty))[0], "err")


class TestShortMessageNoInjection(unittest.TestCase):
    """短消息/寒暄不注入"上次进展/记忆"（09-20 事故：用户只发两个字，模型跑去查了
    上个会话的半导体板块 —— 因为注入块贴在最后一条用户消息尾部，权重最高）。"""

    def setUp(self):
        import agent_loop as A
        import agent.prompt_build as PB     # _build_chat_messages 的规范模块（2026-09-23 拆出）
        self.A, self.PB = A, PB
        # monkeypatch 面必须跟着代码搬：_build_chat_messages 现在在 prompt_build 里
        # 按自己的全局名查这些符号，改 agent_loop 的同名属性不会再生效
        self._tail, self._mem, self._onb = (
            PB._progress_tail, PB._retrieve_relevant_learnings, PB._process_onboarding)
        PB._progress_tail = lambda *a, **k: "- 上次在查半导体板块的资金流向"
        PB._retrieve_relevant_learnings = lambda *a, **k: []
        PB._process_onboarding = lambda t, l: ("", False)

    def tearDown(self):
        self.PB._progress_tail = self._tail
        self.PB._retrieve_relevant_learnings = self._mem
        self.PB._process_onboarding = self._onb

    def _last(self, text):
        b = {"messages": [{"role": "user", "content": text}]}
        out = self.A._build_chat_messages(b, b["messages"])
        return str(out[-1].get("content") or "")

    def test_short_or_chat_no_injection(self):
        for t in ("骚货", "在吗", "你好", "谢谢", "嗯"):
            last = self._last(t)
            self.assertNotIn("【背景资料", last, f"短消息不应注入: {t}")
            self.assertNotIn("半导体", last, f"短消息不应带出历史主题: {t}")

    def test_task_message_still_injects_with_guard(self):
        last = self._last("分析周五大盘走势，给出关键数字和结论")
        self.assertIn("【背景资料", last)          # 正常任务仍注入背景
        self.assertIn("以上只是历史背景", last)      # 且明确标注"不是任务"


class TestUsIndexRouting(unittest.TestCase):
    """美股指数识别（09-20）：道指/标普/纳指/罗素 要能识别，A股/个股不能误判。"""

    def _targets(self, q):
        import sys, pathlib as _pl
        sys.path.insert(0, str(_pl.Path(__file__).resolve().parents[1]))
        sys.path.insert(0, str(_pl.Path(__file__).resolve().parents[1] / "plugins"))
        from plugins.ak_finance import _us_index_targets
        return [n for _, n in _us_index_targets(q)]

    def test_detects_us_indices(self):
        self.assertEqual(len(self._targets("道指 标普500 纳指 9月18日收盘")), 3)
        self.assertEqual(self._targets("纳斯达克指数最新"), ["纳斯达克综合指数"])
        self.assertEqual(self._targets("Dow Jones closing price"), ["道琼斯工业平均指数"])

    def test_does_not_misfire_on_cn(self):
        for q in ("上证指数", "苹果股价", "半导体板块", "今天天气", "帮我写代码"):
            self.assertEqual(self._targets(q), [], f"误判: {q}")


class TestMultiCustomEngines(unittest.TestCase):
    """同一模型家族多个包（GGUF→fork 引擎、MLX→自带运行时）各用各的 binary（09-20）。"""

    def setUp(self):
        import pathlib as _pl, os
        self.tmp = tempfile.TemporaryDirectory()
        self.a = _pl.Path(self.tmp.name) / "fork-llama-server"
        self.b = _pl.Path(self.tmp.name) / "mlx-shim.sh"
        self.a.write_text("#!/bin/sh\n"); self.b.write_text("#!/bin/sh\n")
        self.cfg = _pl.Path(self.tmp.name) / "config.json"
        import local_llm
        self._ll = local_llm
        self._old = local_llm._config_file
        local_llm._config_file = lambda: self.cfg
        self._env = dict(os.environ)
        os.environ.pop("LATIAO_CUSTOM_ENGINE_BIN", None)

    def tearDown(self):
        import os
        self._ll._config_file = self._old
        os.environ.clear(); os.environ.update(self._env)
        self.tmp.cleanup()

    def test_picks_entry_by_match(self):
        import json
        from local_llm import _custom_engine_spec
        self.cfg.write_text(json.dumps({"custom_engine": [
            {"enabled": True, "binary": str(self.a), "name": "fork", "match": "PQ2_0"},
            {"enabled": True, "binary": str(self.b), "name": "mlx", "match": "mlx-2bit"},
        ]}), encoding="utf-8")
        gguf = "/m/Ternary-Bonsai-2-27B-PQ2_0.gguf/Ternary-Bonsai-2-27B-PQ2_0.gguf"
        mlx = "/m/prism-ml:Ternary-Bonsai-2-27B-mlx-2bit"
        self.assertEqual(_custom_engine_spec(gguf)["name"], "fork")
        self.assertEqual(_custom_engine_spec(mlx)["name"], "mlx")
        self.assertEqual(_custom_engine_spec(mlx)["binary"], str(self.b))

    def test_single_object_still_works(self):
        import json
        from local_llm import _custom_engine_spec
        self.cfg.write_text(json.dumps({"custom_engine": {
            "enabled": True, "binary": str(self.a), "name": "solo"}}), encoding="utf-8")
        self.assertEqual(_custom_engine_spec("/any/model")["name"], "solo")


class TestMlxRejectReason(unittest.TestCase):
    """MLX 拒绝加载的原因必须准确（09-20）：旧实现只看 preprocessor_config.json，
    把"需要自带运行时"的包（Prism Bonsai MLX，无视觉塔）说成"多模态 MLX-VLM"。"""

    def _mk(self, files: dict) -> str:
        import json, pathlib as _pl
        d = _pl.Path(tempfile.mkdtemp())
        for name, content in files.items():
            f = d / name.replace("__", "/")
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
        return str(d)

    def test_self_contained_runtime_pack(self):
        from local_llm import _mlx_reject_reason
        kind, msg = _mlx_reject_reason(self._mk({
            "files.json": "{}", "hadamard.json": "{}", "preprocessor_config.json": "{}",
            "config.json": {"model_type": "prism_hadamard_qwen35"}}), "prism_hadamard_qwen35")
        self.assertEqual(kind, "runtime")
        self.assertIn("自带的运行时", msg)
        self.assertNotIn("MLX-VLM", msg)

    def test_plain_text_with_preprocessor_config_is_not_vlm(self):
        """回归：纯文本模型也常带 preprocessor_config.json，不能被判成多模态。"""
        from local_llm import _mlx_reject_reason
        kind, msg = _mlx_reject_reason(self._mk({
            "preprocessor_config.json": "{}",
            "config.json": {"model_type": "some_text_arch"}}), "some_text_arch")
        self.assertEqual(kind, "arch")
        self.assertNotIn("MLX-VLM", msg)

    def test_real_vlm_detected_by_config_or_weights(self):
        from local_llm import _mlx_reject_reason
        k1, _ = _mlx_reject_reason(self._mk({
            "config.json": {"model_type": "qwen2_vl", "vision_config": {}}}), "qwen2_vl")
        self.assertEqual(k1, "vlm")
        k2, _ = _mlx_reject_reason(self._mk({
            "config.json": {"model_type": "x"},
            "model.safetensors.index.json": {"weight_map": {"visual.patch_embed.weight": "a"}}}), "x")
        self.assertEqual(k2, "vlm")


class TestMaxTokensAndLengthRetry(unittest.TestCase):
    """生成长度上限与"思考吃光预算"的自动重试（09-20 实测 Bonsai 6144/6144）。"""

    def test_local_engine_gets_room_for_thinking(self):
        from agent.context import _resolve_max_tokens
        self.assertEqual(_resolve_max_tokens("Ternary-Bonsai-2-27B-PQ2_0", local=True), 16384)
        self.assertEqual(_resolve_max_tokens("Whatever", local=False), 6144)      # 云端小预算
        self.assertEqual(_resolve_max_tokens("deepseek-r1", local=False), 12288)  # 云端推理名
        self.assertEqual(_resolve_max_tokens("x", local=True, override=32768), 32768)  # 配置优先

    def test_custom_engine_max_tokens_from_config(self):
        import json, pathlib as _pl
        import agent_loop as A
        from agent.context import _custom_engine_max_tokens
        old = A.CONFIG_FILE
        with tempfile.TemporaryDirectory() as tmp:
            f = _pl.Path(tmp) / "config.json"
            try:
                f.write_text(json.dumps({"custom_engine": {"max_tokens": 24576}}), encoding="utf-8")
                A.CONFIG_FILE = f
                self.assertEqual(_custom_engine_max_tokens(), 24576)
                f.write_text(json.dumps({}), encoding="utf-8")
                self.assertEqual(_custom_engine_max_tokens(), 0)
            finally:
                A.CONFIG_FILE = old

    def test_length_retry_conditions(self):
        from agent.loop import _needs_length_retry
        # 思考吃光预算（正文空、有思考）→ 该重试
        self.assertTrue(_needs_length_retry("length", 0, True, False))
        # 工具调用被截断（半截 JSON 不可执行）→ 该重试
        self.assertTrue(_needs_length_retry("length", 3, False, False))
        # 正常结束 / 已重试过 / 空正文但无思考（另有闸门管）→ 不重试
        self.assertFalse(_needs_length_retry("stop", 0, True, False))
        self.assertFalse(_needs_length_retry("length", 0, True, True))
        self.assertFalse(_needs_length_retry("length", 0, False, False))


class TestHeadFingerprint(unittest.TestCase):
    """提示头指纹（09-19）：缓存 0% 必须能分清"头部变了"还是"引擎没复用"。"""

    def test_fingerprint_tracks_system_and_tools(self):
        import context_stats as cs
        msgs = [{"role": "system", "content": "A"}, {"role": "user", "content": "hi"}]
        tools = [{"type": "function",
                  "function": {"name": "read_file", "parameters": {"type": "object"}}}]
        fp = cs._head_fp(msgs, tools)
        self.assertEqual(fp, cs._head_fp(msgs, tools))
        self.assertNotEqual(fp, cs._head_fp(   # 系统提示变了 → 头部变
            [{"role": "system", "content": "B"}, msgs[1]], tools))
        self.assertNotEqual(fp, cs._head_fp(   # 工具表变了 → 头部变
            msgs, tools + [{"type": "function", "function": {"name": "x"}}]))
        self.assertEqual(fp, cs._head_fp(      # 只追加对话消息 → 头部不变
            msgs + [{"role": "assistant", "content": "yo"}], tools))

    def test_record_request_stores_and_compares(self):
        import context_stats as cs
        sid = "s-head-fp"
        m = [{"role": "system", "content": "A"}, {"role": "user", "content": "hi"}]
        cs.record_request(sid, m, [], model_path="", limit=0, limit_source="t")
        first = cs._sessions[sid]["head_fp"]
        self.assertIsNone(cs._sessions[sid]["head_prev"])          # 首次无上次
        cs.record_request(sid, m + [{"role": "user", "content": "again"}], [],
                          model_path="", limit=0, limit_source="t")
        self.assertEqual(cs._sessions[sid]["head_fp"], first)      # 追加 → 头部不变
        self.assertEqual(cs._sessions[sid]["head_prev"], first)


class TestProcessNarrationFold(unittest.TestCase):
    """工具轮过程叙述折叠（09-19：关思考后推理倒进正文，实测单轮 11171 字）。"""

    def test_long_narration_folded(self):
        from agent.loop import _trim_process_narration, _ROUND_CONTENT_MAX
        long_text = "The search results are messy. Let me analyze: " * 300
        out = _trim_process_narration(long_text)
        self.assertLess(len(out), _ROUND_CONTENT_MAX + 120)
        self.assertIn("已折叠", out)
        self.assertTrue(out.startswith("The search results"))

    def test_short_text_untouched(self):
        from agent.loop import _trim_process_narration
        short = "结论：道指跌0.18%，纳指涨0.39%。"
        self.assertEqual(_trim_process_narration(short), short)


class TestChunkedTranslation(unittest.TestCase):
    """长文本分块翻译：一次性翻译 16k 字会超出输出预算（实测翻译轮失败）。"""

    def test_long_text_is_chunked(self):
        from agent.gates import _split_translation_chunks
        long_text = "段落一的内容，用来凑长度。\n\n" * 400
        chunks = _split_translation_chunks(long_text)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 3000 for c in chunks))
        self.assertEqual("".join(chunks), long_text)      # 不丢内容

    def test_short_text_single_chunk(self):
        from agent.gates import _split_translation_chunks
        self.assertEqual(len(_split_translation_chunks("很短的一段话。")), 1)

class TestThinkingLevelRespected(unittest.TestCase):
    """思考档位跟随用户选择（09-19：此前 steps>1 一律关思考 → 推理倒进正文）。"""

    def _body(self, level, steps):
        from agent.loop import ThinAgentLoop
        loop = ThinAgentLoop([{"role": "user", "content": "分析周五美股走势"}], "m",
                             "http://127.0.0.1:9/v1/chat/completions", {}, "s-th",
                             thinking_level=level)
        loop.steps = steps
        return loop._build_request("m").get("chat_template_kwargs", {})

    def test_high_keeps_thinking_after_tools(self):
        self.assertTrue(self._body("high", 2).get("enable_thinking"))

    def test_max_keeps_thinking_after_tools(self):
        self.assertTrue(self._body("max", 2).get("enable_thinking"))

    def test_off_stays_off(self):
        self.assertIs(self._body("off", 1).get("enable_thinking"), False)
        self.assertIs(self._body("off", 2).get("enable_thinking"), False)

    def test_truncation_retry_still_forces_off(self):
        from agent.loop import ThinAgentLoop
        loop = ThinAgentLoop([{"role": "user", "content": "写个长回答"}], "m",
                             "http://127.0.0.1:9/v1/chat/completions", {}, "s-th2",
                             thinking_level="high")
        loop.steps = 2
        loop._force_thinking_off_next = True          # 截断重试轮
        self.assertIs(loop._build_request("m")["chat_template_kwargs"]["enable_thinking"], False)

class TestThinkingLowLevel(unittest.TestCase):
    """低档（09-19 新增）：规划轮与作答轮开思考、工具轮关闭——兼顾"想清楚"与"别每轮想几分钟"。"""

    def _body(self, level, steps, prev_tools):
        from agent.loop import ThinAgentLoop
        loop = ThinAgentLoop([{"role": "user", "content": "分析周五美股走势"}], "m",
                             "http://127.0.0.1:9/v1/chat/completions", {}, "s-low",
                             thinking_level=level)
        loop.steps = steps
        loop._last_round_had_tools = prev_tools
        return loop._build_request("m").get("chat_template_kwargs", {}).get("enable_thinking")

    def test_low_keeps_thinking_on(self):
        # 低档=思考开着+限长（关思考既不省时、又把英文推理倒进正文，09-19 实测）
        self.assertTrue(self._body("low", 1, False))
        self.assertTrue(self._body("low", 2, True))

    def test_low_caps_tool_round_output(self):
        """低档=限长：工具轮单轮输出受限（速度唯一直接杠杆是少生成 token）。"""
        from agent.loop import ThinAgentLoop, _LOW_MAX_TOKENS
        loop = ThinAgentLoop([{"role": "user", "content": "分析周五美股走势"}], "m",
                             "http://127.0.0.1:9/v1/chat/completions", {}, "s-lowcap",
                             thinking_level="low")
        loop.steps = 2
        self.assertEqual(loop._build_request("m")["max_tokens"], _LOW_MAX_TOKENS)
        loop.steps = 1                                     # 首轮规划不限长
        self.assertGreater(loop._build_request("m")["max_tokens"], _LOW_MAX_TOKENS)

    def test_same_search_tool_third_call_saturates(self):
        from agent.loop import ThinAgentLoop
        loop = ThinAgentLoop([{"role": "user", "content": "x"}], "m",
                             "http://127.0.0.1:9/v1/chat/completions", {}, "s-cnt")
        call = [{"function": {"name": "tavily_search"}}]
        self.assertFalse(loop._note_tool_counts(call))
        self.assertFalse(loop._note_tool_counts(call))
        self.assertTrue(loop._note_tool_counts(call))      # 第 3 次 → 饱和收口

    def test_high_and_max_always_on_after_tools(self):
        self.assertTrue(self._body("high", 2, True))
        self.assertTrue(self._body("max", 2, True))

    def test_off_never_on(self):
        self.assertIs(self._body("off", 1, False), False)
        self.assertIs(self._body("off", 2, True), False)

class TestLocalizedMessages(unittest.TestCase):
    """运行时提示四语齐备（09-19 审计：这些提示原为中文写死，英文/日文/俄文用户会看到中文）。"""

    def test_all_keys_have_four_languages(self):
        from agent.messages import MESSAGES
        for key, table in MESSAGES.items():
            self.assertTrue({"zh", "en", "ja", "ru"} <= set(table), key)

    def test_en_and_ru_have_no_han(self):
        import re
        from agent.messages import MESSAGES
        for key, table in MESSAGES.items():
            for lang in ("en", "ru"):
                self.assertIsNone(re.search(r"[\u4e00-\u9fff]", table[lang]),
                                  f"{key}.{lang} 含汉字")

    def test_ja_differs_from_zh(self):
        from agent.messages import MESSAGES
        for key, table in MESSAGES.items():
            self.assertNotEqual(table["ja"], table["zh"], key)

    def test_search_reminder_language(self):
        from agent.loop import ThinAgentLoop
        for lang, marker in (("en", "Search budget"), ("ja", "検索予算"), ("ru", "Бюджет поиска")):
            loop = ThinAgentLoop([{"role": "user", "content": "analyze friday us stocks"}], "m",
                                 "http://127.0.0.1:9/v1/chat/completions", {}, f"s-msg-{lang}")
            loop.user_lang = lang
            loop._search_used = 1
            self.assertIn(marker, loop._search_reminder())

    def test_fold_note_language(self):
        from agent.loop import _trim_process_narration
        long_text = "reasoning text " * 200
        self.assertNotRegex(_trim_process_narration(long_text, lang="en"), r"[\u4e00-\u9fff]")
        self.assertIn("Промежуточные" .lower()[:6], _trim_process_narration(long_text, lang="ru").lower())

    def test_lang_of_derives_from_messages(self):
        from agent.messages import lang_of
        self.assertEqual(lang_of([{"role": "user", "content": "прочитай файл SOUL.md"}]), "ru")
        self.assertEqual(lang_of([{"role": "user", "content": "read the SOUL.md file"}]), "en")
        self.assertEqual(lang_of([{"role": "user", "content": "读取 SOUL.md"}]), "zh")

class TestQwenXmlToolCalls(unittest.TestCase):
    """Qwen「<function=NAME><parameter=K>V」方言解析（09-19 事故：XML 被当回答交付给用户，
    还因拉丁字符占多触发语言闸门误判）。"""

    def test_function_parameter_dialect(self):
        from agent.parsing import _parse_prompt_tool_calls
        text = ("<tool_call>\n<function=use_skill>\n<parameter=name>\nmarket-review\n"
                "</parameter>\n</function>\n</tool_call>")
        clean, calls = _parse_prompt_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "use_skill")
        self.assertIn("market-review", calls[0]["function"]["arguments"])
        self.assertEqual(clean.strip(), "")          # 标记不残留、不交付

    def test_multi_param_with_number(self):
        from agent.parsing import _parse_prompt_tool_calls
        text = ("<tool_call><function=mx_query><parameter=query>上证指数收盘</parameter>"
                "<parameter=limit>5</parameter></function></tool_call>")
        _clean, calls = _parse_prompt_tool_calls(text)
        args = json.loads(calls[0]["function"]["arguments"])
        self.assertEqual(args["query"], "上证指数收盘")
        self.assertEqual(args["limit"], 5)

    def test_text_around_call_kept(self):
        from agent.parsing import _parse_prompt_tool_calls
        text = ("先看看数据\n<tool_call><function=tavily_search>"
                "<parameter=query>friday us stocks</parameter></function></tool_call>")
        clean, calls = _parse_prompt_tool_calls(text)
        self.assertIn("先看看数据", clean)
        self.assertEqual(calls[0]["function"]["name"], "tavily_search")

    def test_unknown_markup_stripped(self):
        from agent.parsing import _parse_prompt_tool_calls
        clean, calls = _parse_prompt_tool_calls("先查一下\n<tool_call><whatever/></tool_call>")
        self.assertEqual(clean.strip(), "先查一下")
        self.assertEqual(calls, [])

    def test_gate_ignores_tool_markup(self):
        from agent.gates import _reply_lang_mismatch
        markup = ("<tool_call><function=use_skill><parameter=name>market-review</parameter>"
                  "</function></tool_call>")
        self.assertFalse(_reply_lang_mismatch("你看看昨天大盘走势", markup))
        self.assertTrue(_reply_lang_mismatch("你看看昨天大盘走势", "US stocks closed mixed. " * 6))

class TestThinkBlockStreaming(unittest.TestCase):
    """content 里的 <think> 块转思考通道（09-19：Qwen GGUF 把推理写进 content，原样流给用户）。"""

    def _loop(self):
        from agent.loop import ThinAgentLoop
        return ThinAgentLoop([{"role": "user", "content": "x"}], "m",
                             "http://127.0.0.1:9/v1/chat/completions", {}, "s-think")

    def test_single_chunk(self):
        self.assertEqual(self._loop()._split_think_stream("前<think>推理</think>后"),
                         ("前后", "推理"))

    def test_split_across_chunks(self):
        loop = self._loop()
        out = think = ""
        for c in ("开始<thi", "nk>推理第一段", "继续推理</thi", "nk>正文部分"):
            o, t = loop._split_think_stream(c)
            out += o; think += t
        self.assertEqual(out, "开始正文部分")
        self.assertEqual(think, "推理第一段继续推理")

    def test_tool_call_inside_think_block_still_parsed(self):
        from agent.parsing import _parse_prompt_tool_calls
        text = ('<think>先查数据<tool_call><function=mx_query>'
                '<parameter=query>上证指数收盘</parameter></function></tool_call></think>'
                '我来看看。')
        clean, calls = _parse_prompt_tool_calls(text)
        self.assertEqual(calls[0]["function"]["name"], "mx_query")
        self.assertNotIn("先查数据", clean)      # think 内容不进正文
        self.assertIn("我来看看", clean)

class TestFinalizeDigestLanguage(unittest.TestCase):
    """收口轮提问必须跟随用户语言（09-19：原写死"简体中文"，英文/日文/俄文用户被强制中文）。"""

    def test_ask_language_follows_user(self):
        from agent.loop import _build_finalize_digest
        msgs = [{"role": "user", "content": "analyze friday us stocks"},
                {"role": "tool", "content": "data"}]
        self.assertIn("简体中文", _build_finalize_digest(msgs, lang="zh")[-1]["content"])
        self.assertIn("English", _build_finalize_digest(msgs, lang="en")[-1]["content"])
        self.assertIn("日本語", _build_finalize_digest(msgs, lang="ja")[-1]["content"])
        self.assertIn("русский", _build_finalize_digest(msgs, lang="ru")[-1]["content"])

    def test_no_chinese_forced_for_english_user(self):
        import re
        from agent.loop import _build_finalize_digest
        msgs = [{"role": "user", "content": "analyze friday us stocks"}]
        ask = _build_finalize_digest(msgs, lang="en")[-1]["content"]
        self.assertIsNone(re.search(r"[\u4e00-\u9fff]", ask))

class TestPromptCacheFriendly(unittest.TestCase):
    """前缀缓存友好（09-19 实测：尾部追加命中 99.9%，中段改动 0%）。

    易变块（当前时间/进展/记忆/偏好）必须排在稳定块（系统规则/技能目录）之后，
    否则每轮都从改动点开始全量重算（8k token 多花 5–6 秒）。
    """

    def test_volatile_blocks_come_last(self):
        import os
        os.environ["LATIAO_TEST_PROGRESS_DIR"] = tempfile.mkdtemp()
        from agent_loop import _build_chat_messages
        body = {"messages": [{"role": "user", "content": "读取这个文件并分析"}]}
        content = _build_chat_messages(body, body["messages"])[0]["content"]
        i_rules = content.find("## 系统规则")
        i_skills = content.find("可用技能")
        i_env = content.find("运行环境")             # 唯一锚点（"当前时间"在硬规则里也出现）
        self.assertGreaterEqual(i_rules, 0)
        self.assertGreater(i_env, i_rules)           # 易变的环境/时间块在稳定块之后
        self.assertGreater(i_env, i_skills)
        self.assertIn("下方【当前时间】", content)     # 方位指代同步修正

    def test_compaction_skips_mid_turn(self):
        """轮内不压缩（steps>1 直接返回）——mid-turn 改写历史会打掉前缀缓存。"""
        import inspect
        from agent.plugins import builtin
        src = inspect.getsource(builtin.setup_compaction)
        self.assertIn('getattr(loop, "steps", 0) > 1', src)


# ── GGUF 路径解析（09-21）─────────────────────────────────────────
# 背景：LM Studio 的布局是「目录名以 .gguf 结尾、文件在目录里同名」，此前只判后缀
# 就把目录当文件喂给 llama_cpp → 每轮一条 ValueError Traceback + 永远退回估算。
def test_resolve_gguf_in_lmstudio_directory(tmp_path):
    from context_stats import _resolve_gguf_file
    d = tmp_path / "Spark-X2.5-4B-uncensored-Q8_0.gguf"
    d.mkdir()
    inner = d / "Spark-X2.5-4B-uncensored-Q8_0.gguf"   # 目录内同名文件
    inner.write_bytes(b"GGUF")
    assert _resolve_gguf_file(str(d)) == str(inner)


def test_resolve_gguf_directory_with_differently_named_file(tmp_path):
    from context_stats import _resolve_gguf_file
    d = tmp_path / "some-model.gguf"
    d.mkdir()
    inner = d / "weights-q4.gguf"
    inner.write_bytes(b"GGUF")
    assert _resolve_gguf_file(str(d)) == str(inner)


def test_resolve_gguf_directory_without_gguf_is_none(tmp_path):
    from context_stats import _resolve_gguf_file
    d = tmp_path / "mlx-pack.gguf"      # 目录里只有 MLX 权重
    d.mkdir()
    (d / "model.safetensors").write_bytes(b"x")
    assert _resolve_gguf_file(str(d)) is None


def test_resolve_gguf_plain_file_and_non_gguf(tmp_path):
    from context_stats import _resolve_gguf_file
    f = tmp_path / "model.gguf"
    f.write_bytes(b"GGUF")
    assert _resolve_gguf_file(str(f)) == str(f)
    assert _resolve_gguf_file(str(tmp_path / "nope.txt")) is None
    assert _resolve_gguf_file("") is None


class TestCounterFailureMemory(unittest.TestCase):
    """加载不了的模型只试一次：否则每轮对每个文本块重试一次，日志被 traceback 灌爆。"""

    def setUp(self):
        import context_stats
        self.cs = context_stats
        self.cs._counters.clear(); self.cs._counter_order.clear()
        self.cs._FAILED_COUNTERS.clear()

    def test_failure_attempted_once_per_ttl(self):
        calls = []

        def boom(path):
            calls.append(path)
            raise ValueError("failed to load model")

        orig = self.cs._gguf_counter
        self.cs._gguf_counter = boom
        try:
            with mock.patch.object(self.cs, "_resolve_gguf_file", return_value="/tmp/x.gguf"):
                for _ in range(5):
                    n, src = self.cs.count_tokens("今天大盘", "/tmp/model.gguf")
                    self.assertEqual(src, "estimated")
        finally:
            self.cs._gguf_counter = orig
        self.assertEqual(len(calls), 1, "同一模型半小时内只应尝试加载一次")

    def test_ttl_expiry_allows_retry(self):
        calls = []

        def boom(path):
            calls.append(path)
            raise ValueError("nope")

        orig = self.cs._gguf_counter
        self.cs._gguf_counter = boom
        try:
            with mock.patch.object(self.cs, "_resolve_gguf_file", return_value="/tmp/x.gguf"):
                self.cs.count_tokens("x", "/tmp/m.gguf")
                # 手动把失败时间往前拨，模拟 TTL 过期
                self.cs._FAILED_COUNTERS["/tmp/m.gguf"] = time.monotonic() - 3600
                self.cs.count_tokens("x", "/tmp/m.gguf")
        finally:
            self.cs._gguf_counter = orig
        self.assertEqual(len(calls), 2, "过了 TTL 应重新尝试（模型可能被换掉）")

    def test_only_one_warning_per_model(self):
        with mock.patch.object(self.cs, "_resolve_gguf_file", return_value="/tmp/x.gguf"), \
             mock.patch.object(self.cs, "_gguf_counter",
                               side_effect=ValueError("load failed")), \
             self.assertLogs("latiao-sidecar", level="WARNING") as logs:
            for _ in range(4):
                self.cs.count_tokens("今天", "/tmp/warn.gguf")
        warnings = [r for r in logs.output if "精确计数不可用" in r]
        self.assertEqual(len(warnings), 1, "同一模型的告警只打一次")
