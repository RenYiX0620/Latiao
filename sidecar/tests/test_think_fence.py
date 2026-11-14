"""_ThinkFenceFilter 单元测试：跨 delta 拆分的 ```think 围栏必须剥净。"""
import unittest

from agent_loop import _strip_think_fences, _ThinkFenceFilter


class ThinkFenceFilterTest(unittest.TestCase):
    def run_through(self, chunks: list[str]) -> tuple[str, list[str]]:
        f = _ThinkFenceFilter()
        emitted: list[str] = []
        for c in chunks:
            out = f.feed(c)
            if out:
                emitted.append(out)
        tail = f.finalize()
        if tail:
            emitted.append(tail)
        return "".join(emitted), emitted

    def test_split_across_three_deltas(self):
        """```think> 被拆成 3 个 delta：必须零泄漏。"""
        out, _ = self.run_through([
            "用户要求分析今天大盘。",
            "```",
            "think",
            ">",
            "我已经获取了数据。",
            "```think<",
            "让我整理数据：",
        ])
        self.assertNotIn("```", out, f"漏了围栏标记: {out!r}")
        self.assertNotIn("think", out)
        self.assertIn("用户要求分析今天大盘。", out)
        self.assertIn("我已经获取了数据。", out)
        self.assertIn("让我整理数据：", out)

    def test_split_character_by_character(self):
        """反引号逐字符拆（`/`/`/t/h/i/n/k/>）：仍然零泄漏。"""
        out, _ = self.run_through(["`", "`", "`", "t", "h", "i", "n", "k", ">", "正文"])
        self.assertNotIn("```", out, f"漏了围栏标记: {out!r}")
        self.assertIn("正文", out)

    def test_single_delta_whole_fence(self):
        """单个 delta 就包含完整围栏：剥掉。"""
        out, _ = self.run_through(["abc ```think> def"])
        self.assertNotIn("```", out)
        self.assertIn("abc", out)
        self.assertIn("def", out)

    def test_normal_code_block_passes(self):
        """普通代码块（```json）放行，仅延迟若干 delta。"""
        out, _ = self.run_through(["```", "json", "{", "`a`:1", "}", "```"])
        self.assertIn("```json", out)
        self.assertIn("```", out)
        self.assertIn("`a`:1", out)

    def test_no_marker_unchanged(self):
        """无围栏文本原样输出。"""
        out, _ = self.run_through(["你好，", "今日大盘", "走势"])
        self.assertEqual(out, "你好，今日大盘走势")

    def test_trailing_pending_finalize(self):
        """流在反引号残片处结束：纯 ``` 残片保留（可能是代码块闭合）。"""
        f = _ThinkFenceFilter()
        self.assertEqual(f.feed("分析"), "分析")  # 普通文本立即放行
        f.feed("```")  # 尾部残片暂存等待
        tail = f.finalize()
        self.assertEqual(tail, "```")
        self.assertEqual(f.feed("x"), "x")  # 新流 pending 已清，原样放行

    def test_fence_within_long_text(self):
        """长文本中间含多层围栏：剥掉、其余保留。"""
        out, _ = self.run_through(['说完了：```think> 思考中 ```think< 继续，' * 3])
        self.assertNotIn("```", out)
        self.assertNotIn("think", out)
        self.assertIn("说完了：", out)

    def test_finalize_cleans_pending_fence(self):
        """finalize 对残留的完整标记剥净后返回。"""
        f = _ThinkFenceFilter()
        f.feed("```")
        f.feed("think>")
        self.assertEqual(f.finalize(), "")


class StripThinkFencesTest(unittest.TestCase):
    def test_strip(self):
        self.assertEqual(_strip_think_fences("a ```think> b"), "a  b")
        self.assertEqual(_strip_think_fences("```think< b"), " b")


if __name__ == "__main__":
    unittest.main()


class TextLoopDetectorTest(unittest.TestCase):
    """09-04 元认知死循环：180 字段落 ×10 重复必须命中（旧上限 100 为盲区）。"""

    def test_long_paragraph_loop(self):
        from agent_loop import _detect_text_loop
        para = ("实际上，打破循环的最干净方式是实际去做一些新的事情——查询特定板块的主力资金流。"
                "但用户并未指定是哪个板块。\n\n"
                "让我这么做——查询几个关键板块的主力资金流（比如上一轮会话上下文中提到的板块：房地产、医药生物）。"
                "或者我可以查询按主力资金流排列的前几个板块。\n\n")
        text = "开头分析。" + para * 10
        self.assertTrue(_detect_text_loop(text), "180 字段落循环未命中")

    def test_block_paragraph_loop_over_400_chars(self):
        """2026-11-01 真机：重复单元是 4 段、500+ 字（人设长句整块复述），超过
        片段上限 400 → 旧检测器整块漏检，用户看到同一段话重复三四遍而守卫不响。"""
        from agent_loop import _detect_text_loop
        block = (
            "（认真地说）以上是我的判断，非投资建议，具体还要看你的持仓结构。"
            "要不要我帮你看看某个具体代码？主人一句话～ 人家连查询命令都备好了呢。 ✨\n\n"
            "（最后凑到你耳边，声音放轻）说真的，这种震荡市我最讨厌了——追涨杀跌最伤感情。"
            "重仓科技的等企稳信号，别急着抄底也别恐慌割。下周关键就看硬科技能不能借暗盘拉升止跌企稳。\n\n"
            "（指尖在屏幕上画了个圈，又收回）主人，你手里的筹码先给我捂着，别丢。"
            "要是怕了，随时报个代码给人家——我帮你盯着，看它什么时候出现。 😏\n\n"
            "（歪头看你，语气有点得意）主人……你这么问，人家心里还挺开心的。"
            "原来你不是只想听个结论，是想听人家怎么想的呀？这可是正经的持仓判断呢。\n\n"
            "（指尖在屏幕上敲着那根 -5.48% 的线，眉头微微皱起）行情这东西，追高最伤，"
            "等信号最稳，别让情绪替你做决定，也别在恐慌最深处把筹码交出去。\n\n"
            "（坐直身子，正经地说）以上是我的判断，非投资建议，具体还要看你的持仓结构。"
            "要不要我帮你看看某个具体代码？主人一句话～ 人家连查询命令都备好了呢。 ✨\n\n"
        )
        self.assertGreater(len(block), 400, "复现现场：重复单元必须长过旧上限")
        text = "先给一段开头分析，说明今天的情况。" + block * 3
        self.assertTrue(_detect_text_loop(text), "500+ 字段落块复读未命中")

    def test_normal_long_text_not_flagged(self):
        from agent_loop import _detect_text_loop
        text = ("今天大盘三大指数低开高走。\n\n上证指数收盘 3930 点，跌 0.97%，成交 8300 亿。\n\n"
                "深证成指收 13516 点，跌 1.2%。\n\n创业板指收 3286 点，跌 1.5%，跌幅最大。\n\n"
                "资金面上，主力净流出集中于半导体与白酒板块。\n\n"
                "操作建议：短期观望为主，关注 3900 点支撑。")
        self.assertFalse(_detect_text_loop(text), "正常分析被误判循环")
