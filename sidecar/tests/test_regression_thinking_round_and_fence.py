"""任务半途停修复（2026-10-13）：思考轮反转放行 + 围栏奇偶 + 思考预算硬收口。

真机事故：先想后干第 1 轮（tools=0）的"我这就帮你查～"被当终答交付，
第 2 轮（真正的工具执行）永远不来 → 用户看到"任务执行一半就停了"。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LATIAO_AUTH_TOKEN", "think-round-token")


def test_thinking_round_defaults_to_execution():
    """源码层：**任务型请求**思考轮无工具且无实质数据 → 进执行轮（不再靠句式匹配）。

    闲聊短答仍是合法终答（只对行情/数据类请求强制进第 2 轮）。
    """
    src = Path(__file__).resolve().parents[1].joinpath("agent", "loop.py").read_text("utf-8")
    assert "思考轮转执行" in src
    assert "_has_substance" in src
    assert "_task_like" in src
    assert "_thinking_round_done" in src
    assert "_MARKET_KW_RE" in src


def test_thinking_round_done_blocks_reentry():
    """_thinking_round_done 必须接进 _thinking_first_round（防重采回 step 1）。"""
    src = Path(__file__).resolve().parents[1].joinpath("agent", "loop.py").read_text("utf-8")
    idx = src.find("def _thinking_first_round")
    seg = src[idx:idx + 1500]
    assert "_thinking_round_done" in seg


def test_think_leak_retraction_kept():
    """思考泄漏收口必须保留（2026-10-09 加的保险）。

    历史（2026-10-13）：另一模型把这里误诊成"见到围栏就砍"，换成了按 ``` 奇偶
    裁切——而旧代码检查的是 </think>，与代码围栏无关；那个改动会把**正常的、
    含未闭合代码块的答案**前半段砍掉（正是它声称要修的症状）。已回退。
    """
    src = Path(__file__).resolve().parents[1].joinpath("agent", "loop.py").read_text("utf-8")
    assert 'if "</think>" in body_text:' in src, "思考泄漏收口被删了"
    assert 'count("```") % 2 == 1' not in src, "按代码围栏奇偶裁切正文会砍坏正常答案"


def test_length_retry_covers_thin_body():
    """思考吃光预算、正文半截（<300 字）也要重跑（旧判据要求 empty_body）。"""
    from agent.loop import _needs_length_retry

    # 旧漏网：finish=length、正文 224 字、思考 10543 字
    assert _needs_length_retry(
        "length", {}, False, False,
        body_chars=224, think_chars=10543) is True

    # 正常完整答案（正文 800 字）不误触发
    assert _needs_length_retry(
        "length", {}, False, False,
        body_chars=800, think_chars=5000) is False

    # 既有判据仍生效：native 截断
    assert _needs_length_retry("length", {"x": 1}, False, False) is True
    # 空正文
    assert _needs_length_retry("length", {}, True, False) is True
    # 已重试过不再触发
    assert _needs_length_retry("length", {}, True, True) is False
    # 非 length 不触发
    assert _needs_length_retry("stop", {}, True, False) is False


def test_thinking_round_substance_threshold():
    """有实质数据 + 正文够长才允许思考轮终答。"""
    from agent.loop import _DATA_SIGNAL_RE
    # 有数据信号
    assert _DATA_SIGNAL_RE.search("上证指数涨 1.66%，成交额 3.2 万亿")
    # 纯承诺无数据
    assert not _DATA_SIGNAL_RE.search("我这就帮你查一下数据哦～")
