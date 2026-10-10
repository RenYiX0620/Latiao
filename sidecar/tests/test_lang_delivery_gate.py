"""语言交付闸门两处修复（2026-10-10 用户实测："英文版变成中英文混合"）。

现场：用户要"中英文双版提示词" → 模型输出中文叙述 + 英文提示词块（2511 字，
流式时英文块是全英文）→ 交付闸门判"回复英文占优 = 模型漂移" → 分块翻译
（每块 ~600 字）部分成功部分失败 → "英文开头 + 中文主体"的拼接版经
content_revised 替换掉用户已看到的完整版（日志对得上：handled=True events=1，
库里最终 1713 字 vs 流式 2511 字）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LATIAO_AUTH_TOKEN", "lang-gate-token")


def test_user_requesting_english_is_exempt():
    """用户点名要英文/双语 → 不判语言漂移（交付原样，不翻译、不替换）。"""
    from agent.gates import _reply_lang_mismatch, _user_wants_other_lang
    assert _user_wants_other_lang("给我自画像提示词，要中英文双版", "zh")
    assert _user_wants_other_lang("Please reply in English", "zh")
    assert _user_wants_other_lang("用中文回答我", "en")
    # 否定语境不算（"不要英文" = 用户要中文）
    assert not _user_wants_other_lang("不要用英文，用中文回答", "zh")
    assert not _user_wants_other_lang("别整 English 那些", "zh")
    # 没提语言 → 不豁免
    assert not _user_wants_other_lang("看看今天的大盘", "zh")
    # 端到端：英文占优的正文 + 用户点名要英文 → 不误判
    en_heavy = "### English version\nTwo people in a passionate pose, " * 20
    assert not _reply_lang_mismatch("给我自画像提示词，要中英文双版", en_heavy, "zh")
    # 对照：用户没点名语言时，英文占优仍判漂移（原有行为不变）
    assert _reply_lang_mismatch("看看今天的大盘", en_heavy, "zh")


def test_chunked_translation_never_joins_half_translated():
    """分块翻译：任一块失败（返回原文）→ 整体返回原文，绝不拼接混合文本。"""
    from agent.gates import _join_translations
    original = "AAA 英文块一\nBBB 英文块二\nCCC 英文块三"
    # 全成功：拼接译文
    assert _join_translations(["AAA", "BBB", "CCC"], ["甲", "乙", "丙"], original) == "甲乙丙"
    # 中间块失败（译文 == 原文）→ 整体放弃
    assert _join_translations(["AAA", "BBB", "CCC"], ["甲", "BBB", "丙"], original) == original
    # 首块失败同理（就是"英文开头保留 + 后面中文"的事故形态）
    assert _join_translations(["AAA", "BBB"], ["AAA", "乙"], original) == original
