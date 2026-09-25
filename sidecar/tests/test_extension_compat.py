"""扩展包兼容层 + 数据时点标注（09-21）。

背景（用户报"数据还是有问题"）：
 ① 官方 finance-pack 的 market_insight 写的是 `from tool_executor import execute_tool`，
    而该函数只在 agent_loop.py 里 → 每次调用都 ImportError，模型退回多次手查，
    数据来源/时点因此全乱。tool_executor 加模块级 __getattr__ 惰性转发兜住。
 ② 时间敏感工具结果头部的锚行原来叫「[数据时刻] 当前时间」，把"查询时刻"当成
    "数据时点"，用户无法分辨盘中/收盘快照。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_execute_tool_importable_from_tool_executor():
    """扩展包的写法必须能跑（惰性转发到 agent_loop 的真身）。"""
    import agent_loop
    from tool_executor import execute_tool
    assert execute_tool is agent_loop.execute_tool
    assert callable(execute_tool)


def test_unknown_attribute_still_raises():
    import tool_executor
    try:
        tool_executor.definitely_not_a_tool  # noqa: B018
    except AttributeError as e:
        assert "definitely_not_a_tool" in str(e)
    else:
        raise AssertionError("未知属性应当抛 AttributeError")


def test_time_stamp_distinguishes_query_time_from_data_time():
    import agent_loop
    anchor = agent_loop._stamp_time_sensitive()
    assert "查询时刻" in anchor
    assert "不等于数据本身的时点" in anchor
    assert "date/时间列" in anchor


def test_prompt_requires_source_and_timestamp_consistency():
    """硬规则里的"来源与时点一致性"必须在构建出的系统提示里（四个语种都补了）。"""
    import agent_loop
    zh = agent_loop._build_chat_messages(
        {"model": "t", "session_id": "compat-zh"}, [{"role": "user", "content": "你好"}])[0]["content"]
    assert "来源与时点一致性" in zh
    assert "不要混用、不要取平均" in zh
