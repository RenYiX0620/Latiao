"""Tests for reply-language anchoring fixes (09-03 新会话英文回复事故)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_loop import _build_chat_messages, _detect_user_language, _progress_tail
from agent.context import detect_language_decision as _detect_lang_decision


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

class TestLanguageDetectionWords(unittest.TestCase):
    """09-19 根因回归：口径改为「汉字字数 vs 拉丁词数」。

    旧口径按字母个数比较，"你的SOUL.md是什么"（5 汉字 / 6 字母）被判成英文用户，
    导致提示词、语言锚、翻译方向全按英文走 —— 中文用户收到英文回答的根因。
    """

    ZH_CASES = ["你的SOUL.md是什么", "你的IDENTITY.md是什么", "读取 SOUL.md 告诉我语气设定",
                "这轮 LLM 花了多久", "解释一下 GPTQ 量化", "帮我看看 MCP 工具",
                "你的 tok/s 是多少", "这个 bug 怎么修", "读取这个文件"]

    def test_chinese_with_latin_terms_stays_chinese(self):
        for text in self.ZH_CASES:
            self.assertEqual(_detect_user_language(text), "zh", text)

    def test_english_still_detected(self):
        for text in ("read the SOUL.md file for me", "your tok/s is too slow",
                     "hello there, what can you do"):
            self.assertEqual(_detect_user_language(text), "en", text)

    def test_four_languages_unchanged(self):
        self.assertEqual(_detect_user_language("このファイルを読んで、何が書いてあるか教えて"), "ja")
        self.assertEqual(_detect_user_language("прочитай этот файл и скажи что там"), "ru")
        self.assertEqual(_detect_user_language("读取这个文件"), "zh")

    def test_url_case_still_chinese(self):
        self.assertEqual(_detect_user_language("https://example.com/some/long/path 看看这个"), "zh")

    def test_decision_reports_confidence(self):
        lang, confident = _detect_lang_decision("你的SOUL.md是什么")
        self.assertEqual((lang, confident), ("zh", True))
        self.assertFalse(_detect_lang_decision("12345")[1])      # 纯数字：不表态
        self.assertFalse(_detect_lang_decision("!!!???")[1])     # 纯符号：不表态

    def test_history_rescues_ambiguous_message(self):
        # 当前消息全是拉丁术语、汉字很少，但历史明显中文 → 按中文
        lang, confident = _detect_lang_decision(
            "SOUL.md IDENTITY.md tok/s MCP", ["帮我看看这个文件", "再读一下那个配置"])
        self.assertEqual(lang, "zh")
        self.assertTrue(confident)


class TestGateDirection(unittest.TestCase):
    """闸门方向：中文用户 + 中文回答绝不触发翻译（09-19 实际误触发点）。"""

    def test_chinese_user_chinese_reply(self):
        from agent.gates import _reply_lang_mismatch
        zh_long = "这段话用于测试语言闸门方向，内容足够长以超过阈值。" * 6
        en_long = "This paragraph exists to exercise the gate thresholds. " * 6
        self.assertFalse(_reply_lang_mismatch("你的SOUL.md是什么", zh_long))
        self.assertTrue(_reply_lang_mismatch("你的SOUL.md是什么", en_long))
        self.assertTrue(_reply_lang_mismatch("read the SOUL.md file", zh_long))

    def test_rewrite_instruction_localized(self):
        from agent.gates import _rewrite_lang_instruction
        self.assertIn("简体中文", _rewrite_lang_instruction("zh"))
        self.assertIn("English", _rewrite_lang_instruction("en"))
        self.assertIn("日本語", _rewrite_lang_instruction("ja"))
        self.assertIn("русском", _rewrite_lang_instruction("ru"))


# ── 交付期翻译的两条修正（2026-09-23 真机事故）──────────────────────

def test_translation_chunks_single_long_paragraph():
    """模型的长回复常常是**一个没有空行的大段落**——只按空行切会整块留下，
    仍是一次超时的大请求（真机：1491 字英文回复 → 两次各 60s 超时）。"""
    from agent.gates import _split_translation_chunks
    one_para = "这是一个很长的段落。" * 300          # 无空行
    chunks = _split_translation_chunks(one_para)
    assert len(chunks) > 1, "单段超长文本必须被切开"
    assert max(len(c) for c in chunks) <= 900, f"块过大：{max(len(c) for c in chunks)}"
    assert "".join(chunks) == one_para, "切块不得丢字"
    # 短文不切
    assert _split_translation_chunks("短短一句话。") == ["短短一句话。"]
    # 无标点也能切开并给上界
    nopunct = _split_translation_chunks("a" * 3000)
    assert len(nopunct) > 1 and max(len(c) for c in nopunct) <= 900


def test_translation_request_disables_thinking():
    """翻译请求必须关思考：实测开思考时 6867 字推理吃掉 55s（客户端超时 60s），
    关掉后同一段文本 6.4s。这条用源码断言钉住，防止有人把开关删了。"""
    import pathlib
    import agent.gates as gates
    src = pathlib.Path(gates.__file__).read_text("utf-8")
    idx = src.index("_tb = {\"model\": engine_model")
    body = src[idx:idx + 700]
    assert "enable_thinking" in body and "False" in body, \
        "翻译请求里少了 enable_thinking: False（实测会因思考而撞 60s 超时）"


# ── 语言要求贴在最后一行（2026-09-23 真机事故）────────────────────────

def _tail_of(out: list) -> str:
    """取发给模型的最后一条消息的尾注部分（语言行就在其中）。"""
    return str(out[-1].get("content") or "")


def test_language_requirement_is_the_last_line(monkeypatch, tmp_path):
    """中文用户：尾注里必须有简体中文的要求，且它在**最后**（离生成最近）。

    真机事故：语言规则只在系统提示开头 → 模型在角色扮演类长回复里漂成英文，
    兜底翻译要 2 分钟才换上中文，用户实感"还是没中文"。
    """
    monkeypatch.setenv("LATIAO_TEST_PROGRESS_DIR", str(tmp_path / ".local-ai-os"))
    import importlib
    import config
    importlib.reload(config)
    import agent.prompt_build as PB
    body = {"messages": [{"role": "user", "content": "来嘛来嘛。屁股撅起来"}]}
    out = PB._build_chat_messages(body, list(body["messages"]))
    tail = _tail_of(out)
    assert "简体中文" in tail, f"尾注里没有中文要求：{tail[-160:]!r}"
    assert tail.rstrip().endswith("）") or "英文只是数据" in tail[-200:], \
        "语言要求应该在最后一段（离生成最近）"


def test_language_requirement_follows_user_language(monkeypatch, tmp_path):
    monkeypatch.setenv("LATIAO_TEST_PROGRESS_DIR", str(tmp_path / ".local-ai-os"))
    import importlib
    import config
    importlib.reload(config)
    import agent.prompt_build as PB
    body = {"messages": [{"role": "user", "content": "hello, what is the weather today?"}]}
    out = PB._build_chat_messages(body, list(body["messages"]))
    tail = _tail_of(out)
    assert "English" in tail and "简体中文" not in tail, f"英文用户不该被要求中文：{tail[-160:]!r}"
