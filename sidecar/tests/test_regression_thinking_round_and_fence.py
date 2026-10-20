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


# ══ 2026-10-14「卸载 app」事故（零执行 + 编造完成）═══════════════════════
# 真机：请求"把智谱清言app完整卸载掉"→ 四轮全部 tools=0、工具调用=0；第 3 轮
# 模型编了"已执行 rm -rf /Applications/ZhipuAI.app，应用已移除"（真机上根本没
# 有这个名字，app 原封未动）。根因三条，下面每条一个回归。

class _LeakStub:
    """_thinking_round_plan_leak 只需 last_user_text + 思考轮为真。"""
    def __init__(self, user_text):
        self.last_user_text = user_text

    def _thinking_round_first_stub(self):
        return True


def _plan_leak(user_text, body):
    from agent.loop import ThinAgentLoop
    stub = _LeakStub(user_text)
    stub._thinking_first_round = lambda: True
    return ThinAgentLoop._thinking_round_plan_leak(stub, body)


def test_list_numbers_are_not_data_signals():
    """①a：计划里的列表编号不是"多个数字事实"（真机放行的决定性原因）。

    真机原样：第 1 轮正文 555 字 = 人设开场 + **子任务/执行**编号清单 + 点了
    run_cmd 名字。旧护栏 `len(re.findall(r"\\d[\\d.,]*", t)) >= 2` 数出 5 个片段
    （1. 2. 3. 1. 2.——全是列表编号）→ 按"像真报告"放行 → 计划被当终答交付。
    实验：同一条正文只删掉数字，闸门立刻从放行变命中——编号剥离后 run_cmd
    的点名判据就能生效。
    """
    plan = ("（轻咬下唇，指尖在键盘上轻轻划过）主人想把它赶出家门呀？没问题，"
            "人家这就帮你把根拔起～\n\n**子任务：**\n"
            "1. 查找 /Applications 下的智谱清言应用。\n2. 删除该应用。\n"
            "3. （可选）清理 ~/Library 下的相关残留文件。\n\n**执行：**\n"
            "1. `run_cmd`: `ls /Applications`\n2. `run_cmd`: `grep -i zhipu`")
    # 前置：注册表可导入且含 run_cmd（本用例的工具名判据依赖它；
    # CI 的轻量依赖集也满足——数量可能少一个，名字必须在）
    from agent_loop import TOOLS
    assert any((t.get("function") or {}).get("name") == "run_cmd" for t in TOOLS)
    assert _plan_leak("把智谱清言app完整卸载掉", plan) is True
    # 反控：编号列表里带**真数字事实**（行情汇报）仍按数据放行
    report = "1. 上证收于 3797.24 点\n2. 深证收于 12001.55 点\n3. 创业板 2560.31 点"
    assert _plan_leak("把智谱清言app完整卸载掉", report) is False


def test_tool_code_fence_is_a_plan_signal():
    """①a'：<tool_code> 伪调用块（模型没有工具表时的替代写法）按计划信号处理。

    真机原样：`<tool_code>print(run_cmd("ls /Applications | grep -i zhipu"))</tool_code>`。
    解析器不认这个方言（不执行），但它就是"我要动手"的声明——不能当终答交付。
    这里用**不带工具函数名**的裸命令块，证明信号来自 <tool_code> 本身。
    """
    body = ("（轻笑）人家这就去翻翻看都有什么好东西～\n\n<tool_code>\n"
            "ls /Applications | grep -i zhipu\n</tool_code>")
    assert _plan_leak("把智谱清言app完整卸载掉", body) is True


def test_action_verbs_generalize_task_like_gate():
    """①b：任务型判据从行情词扩到命令动作词（源码层 tripwire + 判据本身）。

    真机请求"把智谱清言app完整卸载掉"不带任何行情词 → 旧判据下"思考轮默认进
    执行轮"整条闸门不生效。桌面/文件任务与行情任务同型：不可能靠嘴完成。
    """
    src = Path(__file__).resolve().parents[1].joinpath("agent", "loop.py").read_text("utf-8")
    start = src.find("思考轮默认进执行轮")
    idx = src.find("思考轮转执行")
    assert start > 0 and idx > start
    seg = src[start:idx + 200]
    # 2026-10-20 收进 _request_task_like 助手（转执行闸与防闪现缓冲共用）
    assert "_request_task_like" in seg, "转执行闸的任务型判据丢了"
    h = src.find("def _request_task_like")
    helper = src[h:h + 900] if h > 0 else ""
    assert "_MARKET_KW_RE" in helper and "_ACTION_KW_RE" in helper, \
        "任务型判据丢了动作词（卸载/删除/安装/打开…）"
    # 列表编号/日期的剥离 2026-10-20 收进 _prose_data_fact_count 统一口径
    # （原先各闸门自己的 _t_nomarker 数各的，日期被数成 3 个"事实"骗过两道闸）
    assert "_prose_data_fact_count" in src, \
        "列表编号/日期剥离（_prose_data_fact_count）被删了"
    assert "<tool_code" in src, "<tool_code> 计划信号被删了"

    from agent.prompt_build import _ACTION_KW_RE
    for task in ("把智谱清言app完整卸载掉", "找残留文件清除", "帮我删除这个文件夹",
                 "安装一下这个软件", "把这个文件复制到桌面", "帮我截屏"):
        assert _ACTION_KW_RE.search(task), task
    for chat in ("智谱清言这个 app 是干嘛的", "今天好累呀", "这个文件是干嘛用的",
                 "帮我看看今天的大盘", "分析一下这个数据"):
        assert not _ACTION_KW_RE.search(chat), chat
