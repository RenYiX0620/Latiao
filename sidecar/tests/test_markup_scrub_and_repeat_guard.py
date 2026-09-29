"""半截工具标记的清理与交付拒收 + cron 重复调用守卫（09-23）。

真机背景（两个都实测过）：
1. 输出被 max_tokens=2048 截断（或缺外层 `<tool_call>` 包裹）时，调用标记解析失败，
   载荷 JSON 裸在正文里被当"分析结果"交付——6 次真机运行里 2 次命中，
   例如 `<tool_call>\n<function=read_file>\n{"path": "…config.json"}`。
   旧 `_scrub_tool_markup` 只治完整标记，清理后仍残留 68 字原始 JSON。
2. cron 没有主循环的重复调用守卫：实测同一个 read_file 被同参调 3 次，每轮吃掉
   一次 10 轮预算 + 2-35s 生成时间，并把上下文撑长（更容易撞上面的截断）。
"""

import pytest

from agent.parsing import (
    _looks_like_tool_markup,
    _parse_prompt_tool_calls,
    _scrub_tool_markup,
)

# 复用 cron 测试的桩环境与消息构造器（同一份，避免两套假客户端漂移）

_P = "/Users/langzuxiang/Desktop/Agent/local-ai-os/config.json"

# 必须清成空（都是"整轮其实是一次调用"的形态）
SCRUB_TO_EMPTY = {
    "完整 tool_call": f'<tool_call>\n<function=read_file>\n{{"path": "{_P}"}}\n</function>\n</tool_call>',
    "截断：缺全部闭合": f'<tool_call>\n<function=read_file>\n{{"path": "{_P}"}}',
    "截断：JSON 中间": f'<tool_call>\n<function=read_file>\n{{"path": "{_P[:20]}',
    "缺外层包裹（有 </function>）": f'<function=read_file>\n{{"path": "{_P}"}}\n</function>',
    "arg_key/arg_value 式（09-01 历史）": '<tool_call>ak_finance<arg_key>query</arg_key><arg_value>300750</arg_value></tool_call>',
    "Gemma 原生式": "<|tool_call|>call:read_file{\"path\": \"/tmp/x\"}<tool_call|>",
}


@pytest.mark.parametrize("name,text", SCRUB_TO_EMPTY.items())
def test_scrub_removes_markup_entirely(name, text):
    """整轮都是调用标记（含截断的半截形态）→ 清理后必须为空。"""
    assert _scrub_tool_markup(text) == "", f"{name} 清理后仍有残留"
    assert _looks_like_tool_markup(text) is True


def test_scrub_keeps_real_prose_and_drops_markup_tail():
    """正文 + 截断尾巴：正文留下，尾巴丢掉（旧实现会把 JSON 留在正文后面）。"""
    text = f'已读取配置文件。\n<tool_call>\n<function=read_file>\n{{"path": "{_P}"}}'
    out = _scrub_tool_markup(text)
    assert out == "已读取配置文件。"
    assert not _looks_like_tool_markup(out)


def test_scrub_does_not_eat_normal_report():
    """正常报告（含数字、英文键名、标点）一字不动。"""
    text = "配置包含四项：cloud_models、custom_engine、tts、tavily_api_key。"
    assert _scrub_tool_markup(text) == text


def test_truncated_markup_is_not_parseable_but_is_scrubbable():
    """这两件事必须一起成立：解析失败（所以不会执行），但清理得掉（所以不交付）。"""
    truncated = f'<tool_call>\n<function=read_file>\n{{"path": "{_P}"}}'
    _clean, calls = _parse_prompt_tool_calls(truncated)
    assert calls == [], "半截标记不该被当成可执行的调用"
    assert _scrub_tool_markup(truncated) == "", "但必须能清掉，别把 JSON 发给用户"


# ── 关于 cron 的旧用例（2026-09-29 删除）─────────────────────────────
# 这里原有 4 条针对 **cron 自带循环** 的用例（收口判据 / 交付前清洗 / 重复调用守卫）。
# cron 现在走与聊天同一个 ThinAgentLoop（审计：结构性问题——三套循环只装一套闸门），
# 这些契约因此由循环承担：清洗由 `_clean`/`_scrub_tool_markup` 单元用例（本文件上半）
# 守住，重复调用守卫由 test_dup_gate.py 守住；cron 侧只保留"确实走了循环"的接线用例
# （见 tests/test_cron_via_loop.py）。
