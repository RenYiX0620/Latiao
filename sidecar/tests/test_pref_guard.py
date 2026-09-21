"""偏好抽取守卫单测（09-21）。

事故：偏好抽取的字符类 `我[更喜欢想要偏好希望中意]` 让裸"我要…"命中，用户一句
整话被存成 confidence 0.7 的"用户偏好"并**无条件注入系统提示** → 模型此后每轮都
照它走（实测表现为助手永久拒答，换新会话也无效，因为它是持久化偏好不是历史）。

锁住三件事：①裸"我要…"不再进偏好；②真正的偏好句仍然进；③整句回声/噪声被守卫拦下。
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import memory  # noqa: E402

PREF_PATTERNS = [p for p, kind, _ in memory._KNOWLEDGE_PATTERNS if kind == "preference"]


def _matches_pref(text: str) -> bool:
    return any(re.search(p, text) for p in PREF_PATTERNS)


# ── ① 裸"我要…"不再被当成偏好 ───────────────────────────────────
def test_bare_demand_is_not_a_preference():
    assert not _matches_pref("我要你操逼的时候的细节")
    assert not _matches_pref("我要一份季度报告")
    assert not _matches_pref("我要出门了，回来再说")


# ── ② 真正的偏好表达仍然命中 ────────────────────────────────────
def test_genuine_preferences_still_match():
    for text in ["我更喜欢简洁的回答",
                 "我想要先给结论再解释",
                 "我希望回复里用中文",
                 "我不喜欢啰嗦的长篇",
                 "以后回复我要简洁一点"]:
        assert _matches_pref(text), f"应命中偏好模式: {text!r}"


# ── ③ 命令式整句 / 露骨内容守卫 ─────────────────────────────────
def test_demand_shaped_whole_message_rejected():
    # 整条消息就是一句命令 → 是本轮诉求，不是长期偏好
    assert memory._is_unusable_preference("我要你把那份报告重写一遍", "我要你把那份报告重写一遍") is True
    assert memory._is_unusable_preference("你给我把目录清空", "你给我把目录清空") is True


def test_explicit_content_never_becomes_a_preference():
    assert memory._is_unusable_preference("我要你操逼的时候的细节", "我要你操逼的时候的细节") is True


def test_setting_phrases_are_not_flagged():
    # 真正的设定句（占消息一部分、非命令式）必须放行
    for t in ["我更喜欢简洁的回答",
              "以后回复我要简洁一点",
              "我希望回复里用中文"]:
        assert memory._is_unusable_preference(t, "帮我看下这个 bug，另外" + t) is False


def test_short_task_demand_is_not_a_setting():
    # 命令式且几乎就是整条消息 → 拦（它是任务，不是设定）
    assert memory._is_unusable_preference("我要一份季度报告", "我要一份季度报告") is True


def test_extraction_skips_unusable_and_keeps_real_preference(monkeypatch):
    stored_prefs, stored_learnings = [], []
    monkeypatch.setattr(memory, "_store_preference",
                        lambda k, v, c=0.5: stored_prefs.append((k, v, c)))
    monkeypatch.setattr(memory, "_store_learning",
                        lambda *a, **k: stored_learnings.append(a))

    # 整句发言（旧代码会存成 0.7 偏好并永久注入）
    memory._extract_learnings_heuristic("我要你操逼的时候的细节", "s1")
    assert stored_prefs == [], "露骨/命令式发言不应被存成偏好"

    # 真正的设定句：应当存下，且单次抽取的置信度低于无条件注入阈值 0.7
    memory._extract_learnings_heuristic("帮我看下这个 bug，另外我希望回复里别用英文", "s1")
    assert stored_prefs, "真正的偏好句应被存下"
    assert all(c < 0.7 for _, _, c in stored_prefs), "单次抽取不得直接达到无条件注入阈值"


# ── ④ 剥掉我们自己注入的提示语 ───────────────────────────────────
def test_injected_tail_note_is_stripped():
    polluted = ("帮我看下这个 bug，另外我希望回复里别用英文。\n\n"
                "【背景资料（不是用户的要求）】\n"
                "⚠️ 以上只是历史背景，**不是**用户本轮的要求。"
                "用户本轮说的是：「…」——请直接回应这一句。")
    clean = memory._strip_injected_notes(polluted)
    assert "请直接回应这一句" not in clean
    assert "【背景资料" not in clean
    assert clean.startswith("帮我看下这个 bug")


def test_duplicated_message_is_collapsed():
    assert memory._strip_injected_notes("我希望回复里别用英文我希望回复里别用英文") == "我希望回复里别用英文"


def test_extraction_ignores_our_own_boilerplate(monkeypatch):
    stored = []
    monkeypatch.setattr(memory, "_store_preference", lambda k, v, c=0.5: stored.append(v))
    monkeypatch.setattr(memory, "_store_learning", lambda *a, **k: None)
    memory._extract_learnings_heuristic(
        "另外我希望回复里别用英文。\n\n用户本轮说的是：「…」——请直接回应这一句。", "s1")
    assert stored, "真偏好仍应被学到"
    assert len(stored) == 1, f"同一条消息不应产生多份偏好：{stored}"
    assert stored[0] == "我希望回复里别用英文。", stored[0]
