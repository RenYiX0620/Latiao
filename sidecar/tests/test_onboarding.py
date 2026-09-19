"""首启引导（onboarding）测试：状态机、答案归一化、防重问保险。

覆盖真实事故模式：短回答被当名字、多截字（"老王好了"）、把提问当回答、
老装机被重新打扰、语气行重复堆积。
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import identity
import onboarding
from agent_loop import _build_chat_messages


class OnboardingBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="onb-test-"))
        self._onb_dir, self._onb_state = onboarding.PROGRESS_DIR, onboarding.STATE_FILE
        self._id_dir = identity.PROGRESS_DIR
        onboarding.PROGRESS_DIR = self.tmp
        onboarding.STATE_FILE = self.tmp / ".onboarding.json"
        identity.PROGRESS_DIR = self.tmp
        identity._create_default_identity()

    def tearDown(self):
        onboarding.PROGRESS_DIR = self._onb_dir
        onboarding.STATE_FILE = self._onb_state
        identity.PROGRESS_DIR = self._id_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def read(self, name: str) -> str:
        p = self.tmp / name
        return p.read_text(encoding="utf-8") if p.exists() else ""


class TestInit(OnboardingBase):
    def test_fresh_install_enables_onboarding(self):
        state = onboarding.init_onboarding()
        self.assertFalse(state["done"])
        self.assertEqual(state["field"], "user_name")

    def test_existing_install_not_prompted(self):
        (self.tmp / "IDENTITY.md").write_text("# Identity\n\n你的名字是「小助手」\n", encoding="utf-8")
        state = onboarding.init_onboarding()
        self.assertTrue(state["done"])

    def test_user_name_marker_alone_marks_done(self):
        # 状态文件被删但引导结果还在 → 也不能重新打扰
        (self.tmp / "USER.md").write_text("# User Profile\n\n- 用户称呼：老王\n", encoding="utf-8")
        self.assertTrue(onboarding.init_onboarding()["done"])

    def test_untouched_defaults_still_prompt(self):
        # 装完只跑过一次、全是默认模板 → 仍算新用户
        self.assertFalse(onboarding.init_onboarding()["done"])


class TestAskFlow(OnboardingBase):
    def test_first_turn_asks_exactly_one_question(self):
        onboarding.init_onboarding()
        directive, handled = onboarding.process_message("你好", "zh")
        self.assertIn("我该怎么称呼你？", directive)
        self.assertFalse(handled)
        self.assertNotIn("用户称呼：", self.read("USER.md"))

    def test_greeting_is_not_taken_as_name(self):
        onboarding.init_onboarding()
        onboarding.process_message("你好", "zh")
        directive, handled = onboarding.process_message("你好", "zh")
        self.assertFalse(handled)
        self.assertIn("我该怎么称呼你？", directive)   # 重问，而不是记成"你好"
        self.assertNotIn("用户称呼：", self.read("USER.md"))

    def test_question_is_not_taken_as_name(self):
        onboarding.init_onboarding()
        onboarding.process_message("你好", "zh")
        _, handled = onboarding.process_message("你能帮我读个文件吗？", "zh")
        self.assertFalse(handled)                       # 未消费 → 常规意图识别仍会跑
        self.assertNotIn("用户称呼：", self.read("USER.md"))

    def test_overcapture_regression(self):
        onboarding.init_onboarding()
        onboarding.process_message("你好", "zh")
        onboarding.process_message("你就叫我老王好了", "zh")
        user = self.read("USER.md")
        self.assertIn("用户称呼：老王", user)
        self.assertNotIn("老王好了", user)

    def test_english_name(self):
        onboarding.init_onboarding()
        onboarding.process_message("hi", "en")
        onboarding.process_message("call me Alex", "en")
        self.assertIn("用户称呼：Alex", self.read("USER.md"))

    def test_name_in_first_message_skips_question(self):
        onboarding.init_onboarding()
        directive, handled = onboarding.process_message("你好，我叫老王", "zh")
        self.assertTrue(handled)
        self.assertIn("用户称呼：老王", self.read("USER.md"))
        self.assertIn("名字", directive)                # 直接进入第二个问题


class TestThreeSteps(OnboardingBase):
    def _walk_to(self, field: str):
        onboarding.init_onboarding()
        onboarding.process_message("你好", "zh")          # 问称呼
        if field == "user_name":
            return
        onboarding.process_message("老王", "zh")          # 问名字
        if field == "agent_name":
            return
        onboarding.process_message("小助手", "zh")        # 问语气

    def test_agent_name_written_and_advanced(self):
        self._walk_to("agent_name")
        onboarding.process_message("小助手", "zh")
        self.assertIn("「小助手」", self.read("IDENTITY.md"))
        self.assertEqual(onboarding.load_state()["field"], "tone")

    def test_agent_name_answer_strips_you_call_prefix(self):
        # "叫你小助手吧" 曾会把"叫你"一起记进名字
        self._walk_to("agent_name")
        onboarding.process_message("叫你小助手吧", "zh")
        ident = self.read("IDENTITY.md")
        self.assertIn("「小助手」", ident)
        self.assertNotIn("叫你", ident)

    def test_agent_rename_answer_at_user_name_step_is_not_a_name(self):
        # 问"怎么称呼你"却回答给 Agent 改名 → 不算答案，交给常规意图识别
        onboarding.init_onboarding()
        onboarding.process_message("你好", "zh")
        _, handled = onboarding.process_message("以后叫你小助手", "zh")
        self.assertFalse(handled)
        self.assertNotIn("用户称呼：", self.read("USER.md"))


    def test_english_agent_name_phrasing(self):
        """英文起名句式（"you can call yourself Nova"）必须能识别。"""
        self._walk_to("agent_name")
        onboarding.process_message("you can call yourself Nova", "en")
        self.assertIn("「Nova」", self.read("IDENTITY.md"))

    def test_tone_answer_not_stored_as_name(self):
        """把语气描述当名字存（实测把 "keep it concise" 记成名字）必须被挡住。"""
        self._walk_to("agent_name")
        _, handled = onboarding.process_message("keep it concise please", "en")
        self.assertFalse(handled)
        self.assertNotIn("keep it concise", self.read("IDENTITY.md"))

    def test_english_tone_preset_stored_in_english(self):
        """英文用户的语气存英文写法（人设文件不该出现中文句）。"""
        self._walk_to("tone")
        onboarding.process_message("be concise and direct", "en")
        soul = self.read("SOUL.md")
        self.assertIn("concise and direct", soul)
        self.assertNotIn("简洁直接", soul)

    def test_tone_preset_normalized(self):
        self._walk_to("tone")
        onboarding.process_message("简洁一点", "zh")
        soul = self.read("SOUL.md")
        self.assertIn("- 对话语气：简洁直接，先给结论再解释", soul)

    def test_tone_freeform_kept(self):
        self._walk_to("tone")
        onboarding.process_message("像老朋友一样说话就好", "zh")
        self.assertIn("- 对话语气：像老朋友一样说话就好", self.read("SOUL.md"))

    def test_tone_replaced_not_appended(self):
        self._walk_to("tone")
        onboarding.process_message("简洁一点", "zh")
        onboarding.reset()
        self._walk_to("tone")
        onboarding.process_message("轻松幽默一些", "zh")
        soul = self.read("SOUL.md")
        self.assertEqual(soul.count("- 对话语气："), 1)
        self.assertIn("幽默", soul)

    def test_completion_marks_done_and_stops_asking(self):
        self._walk_to("tone")
        _, handled = onboarding.process_message("正式专业", "zh")
        self.assertTrue(handled)
        self.assertTrue(onboarding.load_state()["done"])
        directive, handled = onboarding.process_message("你好", "zh")
        self.assertIsNone(directive)
        self.assertFalse(handled)

    def test_skip_field_keeps_default(self):
        onboarding.init_onboarding()
        onboarding.process_message("你好", "zh")
        onboarding.process_message("跳过", "zh")
        self.assertNotIn("用户称呼：", self.read("USER.md"))
        self.assertEqual(onboarding.load_state()["field"], "agent_name")

    def test_skip_all_finishes(self):
        onboarding.init_onboarding()
        onboarding.process_message("你好", "zh")
        _, handled = onboarding.process_message("都跳过", "zh")
        self.assertTrue(handled)
        self.assertTrue(onboarding.load_state()["done"])


class TestWiring(OnboardingBase):
    def test_system_prompt_contains_onboarding_block(self):
        onboarding.init_onboarding()
        body = {"messages": [{"role": "user", "content": "你好"}]}
        msgs = _build_chat_messages(body, body["messages"])
        self.assertIn("首次使用引导", msgs[0]["content"])
        self.assertIn("我该怎么称呼你？", msgs[0]["content"])


    def test_english_user_gets_english_onboarding_wrapper(self):
        """英文用户的首启引导外壳必须是英文（中文外壳会把模型带成中文提问）。"""
        onboarding.init_onboarding()
        body = {"messages": [{"role": "user", "content": "hello there, what can you do for me"}]}
        msgs = _build_chat_messages(body, body["messages"])
        sys_content = msgs[0]["content"]
        self.assertIn("first-run onboarding", sys_content)
        self.assertNotIn("本轮最重要的动作", sys_content)

    def test_no_onboarding_block_once_done(self):
        onboarding.init_onboarding()
        onboarding.complete()
        body = {"messages": [{"role": "user", "content": "你好"}]}
        msgs = _build_chat_messages(body, body["messages"])
        self.assertNotIn("首次使用引导", msgs[0]["content"])

    def test_legacy_intent_path_still_works_after_onboarding(self):
        onboarding.init_onboarding()
        onboarding.complete()
        identity._process_identity_intents("以后叫我老王")
        self.assertIn("用户称呼：老王", self.read("USER.md"))

    def test_identity_files_unchanged_for_normal_chat(self):
        onboarding.init_onboarding()
        onboarding.complete()
        before = self.read("SOUL.md")
        identity._process_identity_intents("今天天气怎么样")
        self.assertEqual(before, self.read("SOUL.md"))


if __name__ == "__main__":
    unittest.main()


class TestPendingQuestionSuffix(OnboardingBase):
    """兜底：模型没问出该问的问题时，后端补上那一个问题。"""

    def test_suffix_added_when_model_did_not_ask(self):
        onboarding.init_onboarding()
        onboarding.process_message("你好", "zh")
        suffix = onboarding.pending_question_suffix("你好！我是辣条，随时待命。")
        self.assertIn("我该怎么称呼你？", suffix)

    def test_no_suffix_when_model_asked(self):
        onboarding.init_onboarding()
        onboarding.process_message("你好", "zh")
        self.assertIsNone(onboarding.pending_question_suffix("你好！我是辣条。我该怎么称呼你？"))

    def test_no_suffix_after_done(self):
        onboarding.init_onboarding()
        onboarding.complete()
        onboarding.process_message("你好", "zh")
        self.assertIsNone(onboarding.pending_question_suffix("好的。"))
