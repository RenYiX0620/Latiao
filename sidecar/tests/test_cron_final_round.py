"""cron 两处修复的回归测试：

A1 —— 收尾轮（10 轮跑满后的强制总结）漏做 tool→user 转换：本地引擎只认
      system/user/assistant，role:"tool" 会让收尾轮空响应 → 用户拿到的是原始
      工具输出兜底而不是总结。
A4 —— 重试块每次尝试前不回置 resp，且 resp.json() 在重试之外：第 1 次 4xx/5xx、
      第 2/3 次传输失败时会对残留的错误响应做 json()；最后一次是 500 时还会
      因为 resp_data 未赋值而 NameError。
"""
import asyncio
import cron  # noqa: E402  （被测模块）
import httpx  # noqa: E402  （桩也要用真实的异常类）

# 共享桩与夹具在 conftest.py（pytest 自动可见）；这里只取用到的构造器
from tests.conftest import (  # noqa: E402
    _LONG_FINAL,
    _Resp,
    _job,
    _text_msg,
    _tool_call_msg,
)



def test_final_round_has_no_tool_role_locally(cron_env):
    """A1：本地收尾轮的请求体里不能有 role:"tool"（否则空响应 → 生数据兜底）。"""
    cron_env["set_script"]([_tool_call_msg() for _ in range(10)] + [_text_msg("最终总结正文")])
    asyncio.run(cron._execute_cron_job(_job()))

    seen = cron_env["seen"]
    assert len(seen) == 11, f"应有 10 轮工具 + 1 轮收尾，实际 {len(seen)}"
    final_body = seen[-1]
    roles = [m.get("role") for m in final_body["messages"]]
    assert "tool" not in roles, f"本地收尾轮仍有 role:'tool'：{roles}"
    # 收尾轮不得再带工具清单（否则模型继续要工具）
    assert "tools" not in final_body
    status, content = cron_env["recorded"][-1]
    assert status == "success" and content == "最终总结正文", \
        f"用户拿到的应是总结而不是原始工具输出：{content[:60]!r}"


def test_final_round_keeps_tool_role_for_cloud(cron_env, monkeypatch):
    """对照：云端收尾轮保持原样（转换只该作用于本地引擎）。"""
    import agent_loop

    async def _resolve_cloud(cloud):
        return ("openai", "https://api.example.com/v1/chat/completions", {"Authorization": "x"}, False)
    monkeypatch.setattr(agent_loop, "_resolve_api_target", _resolve_cloud, raising=False)

    cron_env["set_script"]([_tool_call_msg() for _ in range(10)] + [_text_msg("云总结")])
    asyncio.run(cron._execute_cron_job(_job()))
    final_roles = [m.get("role") for m in cron_env["seen"][-1]["messages"]]
    assert "tool" in final_roles, "云端路径不该被本地转换改动"


def test_retry_survives_status_then_transport_then_ok(cron_env):
    """A4：500 → 传输错误 → 200 正常：必须重试成功（旧实现会拿残留响应做 json）。"""
    # 脚本 = 第 1 轮的 3 次尝试 + 一个"空 choices"终止符（cron 循环在拿到正文后
    # 仍会继续下一轮，直到模型返回空响应才收口——所以要显式给终止符）
    cron_env["set_script"]([
        _Resp({"error": "boom"}, status=500),
        httpx.ConnectError("connection refused"),
        _text_msg("重试后成功"),
        {"choices": []},
    ])
    asyncio.run(cron._execute_cron_job(_job()))
    assert len(cron_env["seen"]) == 4, f"500→传输错误→200 应三次尝试后成功：{len(cron_env['seen'])}"
    status, content = cron_env["recorded"][-1]
    assert status == "success" and "重试后成功" in content


def test_retry_recovers_from_html_error_page(cron_env):
    """A4：代理返回 200 + HTML 错误页（json() 抛错）也要纳入重试。"""
    cron_env["set_script"]([
        _Resp(ValueError("Expecting value: line 1 column 1 (char 0)")),
        _text_msg("第二次成功"),
        {"choices": []},
    ])
    asyncio.run(cron._execute_cron_job(_job()))
    assert len(cron_env["seen"]) == 3
    status, content = cron_env["recorded"][-1]
    assert status == "success" and "第二次成功" in content


def test_retry_all_failed_reports_clear_error(cron_env):
    """A4：三次都是 500 → 必须报出可读错误（旧实现会在 resp_data 上 NameError）。"""
    cron_env["set_script"]([_Resp({"error": "boom"}, status=500) for _ in range(3)])
    asyncio.run(cron._execute_cron_job(_job()))
    status, content = cron_env["recorded"][-1]
    assert len(cron_env["seen"]) == 3
    assert status == "error"
    assert "500" in content or "HTTPStatusError" in content, content
    assert "NameError" not in content


# ── 收口判据（09-23）：非空正文 + 本轮无工具调用 + 不像过渡句 ──────────────

def test_finalize_requires_length_and_not_transitional():
    """判据单测：长度门槛（分"用过工具"和"纯聊天"两档）+ 过渡句识别。"""
    f = cron._cron_can_finalize
    assert f("", 1)[0] is False                                  # 空正文
    assert f("   ", 3)[0] is False
    # 过渡句：即使够长也不收口（模型还打算继续干活）
    assert f("我先查一下数据，然后再给出完整分析。", 1)[0] is False
    assert f("接下来我将逐个核对每个板块的资金流向并汇总。", 2)[0] is False
    # 工具轮之后：120 字即可交付
    assert f("配置包含四项：cloud_models、custom_engine、tts 与 tavily_api_key。" * 3, 1)[0] is True
    # 一次工具都没用过：更保守（250 字）
    assert f("x" * 130, 0)[0] is False
    assert f("x" * 260, 0)[0] is True
    # 过渡标记只在开头 80 字内判定：正文里的"接下来建议…"不该被误判
    long_body = "结论如下：" + "y" * 130 + "接下来建议关注资金面。"
    assert len(long_body) >= 120, "本用例要够长，否则会被长度门槛拦下（不是过渡句判定）"
    assert f(long_body, 1)[0] is True


def test_loop_finalizes_after_tool_round(cron_env):
    """接线测试：工具轮之后的正文轮应立刻收口（不再空转下一轮）。"""
    cron_env["set_script"]([
        _tool_call_msg(),                                   # 第 1 轮：调工具
        _text_msg(_LONG_FINAL),                                 # 第 2 轮：可交付（够长）
        _text_msg("这一轮不该被请求到"),                      # 若收口失效会被消费
    ])
    asyncio.run(cron._execute_cron_job(_job()))
    assert len(cron_env["seen"]) == 2, f"应在第 2 轮收口，实际请求了 {len(cron_env['seen'])} 次"
    status, content = cron_env["recorded"][-1]
    assert status == "success" and "cloud_models" in content
    assert "这一轮不该被请求到" not in content


def test_loop_does_not_finalize_on_transitional(cron_env):
    """过渡句不收口：模型说"我先查一下"时必须继续，直到写出真结论。"""
    cron_env["set_script"]([
        _tool_call_msg(),
        _text_msg("我先查一下配置文件的各项含义，稍等。"),      # 过渡句（短 + 过渡标记）
        _text_msg(_LONG_FINAL),
        _text_msg("不该被请求到"),
    ])
    asyncio.run(cron._execute_cron_job(_job()))
    assert len(cron_env["seen"]) == 3, f"过渡句不该收口，实际 {len(cron_env['seen'])} 次"
    _status, content = cron_env["recorded"][-1]
    assert "cloud_models" in content


def test_loop_still_uses_old_paths_when_no_finalize(cron_env):
    """收口不改变老路径：一直只回工具调用 → 跑满 10 轮后仍走收尾轮/兜底。"""
    cron_env["set_script"]([_tool_call_msg(cid=f"c{i}") for i in range(12)])
    asyncio.run(cron._execute_cron_job(_job()))
    assert len(cron_env["seen"]) >= 10, "应跑满 10 轮上限"
    status, content = cron_env["recorded"][-1]
    assert status == "success" and content.strip(), "必须有兜底正文，不能空"
