"""「先想后干」两段式（2026-10-12，用户拍板）的接线测试。

设计：开启档的**首个轮次**不给 JSON 工具表（实测：带表 → 思考 129 字、走
"调度模式"直接调工具；只给**一行工具名字清单** → 思考 4773 字且点名工具），
工具从"开始执行"的那一刻恢复。关闭档/开关关闭时行为与旧版完全一致。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LATIAO_AUTH_TOKEN", "thinkfirst-token")

_URL = "http://127.0.0.1:9/v1/chat/completions"   # 不可达即可：这些断言只看请求体


@pytest.fixture(autouse=True)
def _native_tools_on():
    """本文件断言的是**原生工具形态**（请求体里带 tools 数组）。

    CI 是 Linux 且只装轻量依赖集，`_local_native_tools_ok()` 因没有本地引擎返回
    False → 走围栏路径（工具说明进尾部、请求里**没有** tools 键）→ 断言全线失败
    （2026-10-12 v0.3.79 的 CI 红，本机因为有真引擎在跑而"侥幸"绿）。与
    test_thin_loop 同款：显式覆盖成原生，让断言与"本机有没有引擎"解耦。
    """
    import agent.context as agent_context
    old = agent_context._LOCAL_NATIVE_TOOLS_OVERRIDE
    agent_context._LOCAL_NATIVE_TOOLS_OVERRIDE = True
    yield
    agent_context._LOCAL_NATIVE_TOOLS_OVERRIDE = old


def _exec_tool_names() -> set:
    """执行阶段应有的工具**名字集合**（不写死数量）。

    数量随环境变：CI 的轻量依赖集会让某个工具注册不进来（实测本机 32、CI 31），
    平台差异不该让断言红——比较集合，而不是比较个数。
    """
    body = _loop("off")._build_request("m")
    assert isinstance(body.get("tools"), list), "off 档必须走原生路径（带 tools 数组）"
    return {t["function"]["name"] for t in body["tools"]}


def _loop(level="max", text="帮我查一下贵州茅台今天的股价。"):
    from agent.loop import ThinAgentLoop
    loop = ThinAgentLoop([{"role": "user", "content": text}], "m", _URL, {}, "s-tf1",
                         thinking_level=level)
    loop.steps = 1          # 生产语义：请求只在轮内构造（run() 先 ++ 再 build）
    return loop


def _sysmsg(body):
    return next((m["content"] for m in body["messages"] if m.get("role") == "system"), "")


def test_thinking_round_has_no_tool_table_but_has_name_hint():
    """开启档首轮：无 JSON 工具表 + 系统提示里有工具名字清单（点名可查）。"""
    body = _loop("max")._build_request("m")
    assert not (body.get("tools") or []), "思考轮不应携带 JSON 工具表"
    sys0 = _sysmsg(body)
    assert "本轮先思考规划" in sys0
    assert "mx_query" in sys0 and "tavily_search" in sys0, "清单应点名真实工具"


def test_hint_carries_the_completeness_discipline():
    """思考轮的提示词必须保留"要写完整回答"的纪律（2026-10-13 用户的反馈修）。

    回归背景：思考轮把 _NATIVE_LEAN_PROMPT 整段换成名字清单，而那段里的纪律
    （"直接写出完整回答""把完整结论写进正文"）被一起换没了 → 第 1 轮（闲聊/角色
    扮演/知识问答真正作答的那一轮）失去长度约束；原措辞"再给出下一步"还在暗示
    简版。实测同一句人设对话：正文 214 → 150 字；补回纪律并明说"先思考过不等于
    回答可以更简略"后回到 205 字（知识问答 610 字）。
    """
    from agent.loop import _tool_name_hint_line
    hint = _tool_name_hint_line()
    assert "完整" in hint and "饱满" in hint, "必须要求完整回答"
    assert "更简略" in hint, "必须明说'先思考过不代表回答可以更简略'"
    assert "再给出下一步" not in hint, "旧措辞「再给出下一步」在暗示简版，不许回来"


def test_execution_phase_restores_full_tool_table():
    """执行阶段（第 2 轮起）：工具表恢复、清单不再注入。"""
    loop = _loop("max")
    loop.steps = 2
    body = loop._build_request("m")
    assert isinstance(body.get("tools"), list)
    assert {t["function"]["name"] for t in body["tools"]} == _exec_tool_names()
    assert "本轮先思考规划" not in _sysmsg(body)


def test_round2_gets_tools_without_any_tool_success():
    """轮次判据（回归：初版用 `_turn_tool_ok` 判执行阶段 → 思考轮无工具表 →
    永远拿不到工具结果 → 第 2 轮仍是思考轮 → 任务型提问空手撞收口轮）。"""
    loop = _loop("max")
    assert loop._turn_tool_ok is False
    assert loop._thinking_first_round() is True
    loop.steps = 2
    assert loop._thinking_first_round() is False
    _names = {t["function"]["name"] for t in loop._build_request("m").get("tools") or []}
    assert _names == _exec_tool_names()


def test_plan_gate_not_blind_in_thinking_round():
    """空转闸门在思考轮必须仍能识别"只预告不行动"（回归：`_active_tools()` 为空
    → `_looks_like_plan_only(text, False)` 一律放行 → 第 1 轮的计划句直接交付）。"""
    from agent.loop import _looks_like_plan_only
    loop = _loop("max")
    assert loop._active_tools() == []
    assert loop._plan_gate_has_tools() is True
    plan = "好的，我这就用 mx_query 帮你查一下贵州茅台的实时股价～"
    assert _looks_like_plan_only(plan, loop._plan_gate_has_tools()) is True
    assert _looks_like_plan_only(plan, False) is False   # 旧口径的失明现场


def test_platform_independence_controls(monkeypatch):
    """正控：忠实模拟 CI 的两个平台条件，断言仍然成立。

    CI 是 Linux + 轻量依赖集：① 没有本地引擎 → 能力探测为假（走围栏路径、请求里
    没有 tools 键）；② 某个工具注册不进来（本机 32 个、CI 31 个）。2026-10-12
    v0.3.79 的 CI 就是被这两点弄红的。注意模拟方式：**不能**直接替换
    `_local_native_tools_ok`（那会连覆盖层一起绕过，覆盖层正是要验证的机制）——
    而是把 `local_llm._engine` 换成无后端桩，让真实探测函数自己返回假。
    """
    import agent.context as agent_context
    import agent.loop as _L
    import agent_loop as _A
    import local_llm

    class _NoEngine:          # 模拟 CI：没有本地引擎
        _active_backend = ""
        backend = ""

    monkeypatch.setattr(local_llm, "_engine", _NoEngine(), raising=False)
    # ① 模拟条件本身生效：覆盖层关掉时，探测为假（这正是 CI 的处境）
    monkeypatch.setattr(agent_context, "_LOCAL_NATIVE_TOOLS_OVERRIDE", None)
    assert _L._local_native_tools_ok() is False, "模拟条件没生效（探测仍为真）"
    # ② 覆盖层在场（autouse fixture 的语义）→ 断言不受引擎影响
    monkeypatch.setattr(agent_context, "_LOCAL_NATIVE_TOOLS_OVERRIDE", True)
    body = _loop("off")._build_request("m")
    assert isinstance(body.get("tools"), list) and body["tools"], \
        "能力探测为假时也必须走原生路径（覆盖层生效）"
    # ③ 工具注册表少一个 → 按名字比集合，仍然一致
    monkeypatch.setattr(_A, "TOOLS", [t for t in _A.TOOLS
                                      if (t.get("function") or {}).get("name") != "screen_capture"])
    body2 = _loop("off")._build_request("m")
    assert isinstance(body2.get("tools"), list)
    assert {t["function"]["name"] for t in body2["tools"]} == _exec_tool_names(), \
        "工具集合少一个也要与基线一致（按名字比，不按个数）"


def test_statement_plan_leak_flagged_with_positive_and_negative_controls():
    """陈述式计划声明（真机实测的交付漏网）必须被拦，真答案不得误伤。

    实测原句（Hermes 35B，思考轮 1083 字后的正文）："使用 ak_finance 查询贵州茅台
    （600519）的行情数据。"——无意图词，旧判据全漏，被当终答交付。
    """
    loop = _loop("max")
    assert loop._thinking_round_plan_leak("使用 ak_finance 查询贵州茅台（600519）的行情数据。") is True
    assert loop._thinking_round_plan_leak("我先用 web_search 搜一下最近新闻。") is True
    # 反控一：真汇报（带数字）即使提到工具名也不算计划
    assert loop._thinking_round_plan_leak(
        "ak_finance 返回：贵州茅台 600519 收于 1500.25 元，涨 1.20%，成交 3.4 万手。") is False
    # 反控二：普通闲聊答复（无工具名）
    assert loop._thinking_round_plan_leak("今天天气不错，出去走走吧，别老坐着～") is False
    # 反控三：执行阶段（第 2 轮起）不启用这道闸
    loop.steps = 2
    assert loop._thinking_round_plan_leak("使用 ak_finance 查询贵州茅台（600519）的行情数据。") is False


def test_off_level_never_gated():
    """关闭档：首轮就带全套工具（旧行为不变）。"""
    body = _loop("off")._build_request("m")
    assert isinstance(body.get("tools"), list)
    assert {t["function"]["name"] for t in body["tools"]} == _exec_tool_names()
    assert "本轮先思考规划" not in _sysmsg(body)


def test_env_switch_disables_the_gate(monkeypatch):
    """LATIAO_THINKING_FIRST=0：整体回退旧行为（回退阀）。"""
    monkeypatch.setenv("LATIAO_THINKING_FIRST", "0")
    body = _loop("max")._build_request("m")
    assert isinstance(body.get("tools"), list)
    assert {t["function"]["name"] for t in body["tools"]} == _exec_tool_names()
    monkeypatch.delenv("LATIAO_THINKING_FIRST")


@pytest.mark.asyncio
async def test_two_phase_plan_then_execute_end_to_end():
    """集成验收（真机流程的复刻）：思考轮出计划 → 撤回气泡+催促 → 执行轮真调工具
    → 终答。计划句（真机实测原句，点名的是 ak_finance）不得进交付文本。"""
    import time

    import agent.context as agent_context
    import local_llm
    from agent.loop import RoundTextAccumulator, ThinAgentLoop
    from tests.fake_engine import FakeEngine
    from tests.test_loop_scenarios import _StubEngine

    plan = "使用 ak_finance 查询贵州茅台（600519）的行情数据。"
    answer = "目录里有 agents、skills、tests 等目录，清单见上。" * 3
    with FakeEngine() as engine:
        engine.push(engine.text_response(plan))
        engine.push(engine.native_tool_response("list_dir", {"path": "."}))
        engine.push(engine.text_response(answer))
        old = local_llm._engine
        _old_override = agent_context._LOCAL_NATIVE_TOOLS_OVERRIDE
        local_llm._engine = _StubEngine()
        agent_context._LOCAL_NATIVE_TOOLS_OVERRIDE = True
        try:
            loop = ThinAgentLoop([{"role": "user", "content": "帮我看看当前目录里有什么"}],
                                 "fake-model", engine.url, {},
                                 session_id=f"think-e2e-{time.time()}",
                                 access_mode="full", thinking_level="max")
            acc = RoundTextAccumulator()
            events = []
            async for ev in loop.run():
                acc.feed(ev)
                events.append(ev)
        finally:
            local_llm._engine = old
            # 恢复成**原值**而不是 None：本文件有 autouse 覆盖层（_native_tools_on），
            # 清成 None 会让同一条用例**之后**的断言（_exec_tool_names 基线）在
            # 无引擎环境（CI）退回围栏路径 → 2026-10-12 CI 第二次红的原因。
            agent_context._LOCAL_NATIVE_TOOLS_OVERRIDE = _old_override

        assert not (engine.requests[0].get("tools") or []), "思考轮不下发工具表"
        _r1 = engine.requests[1].get("tools") or []
        assert isinstance(_r1, list) and _r1, "执行轮恢复工具表"
        assert {t["function"]["name"] for t in _r1} == _exec_tool_names()
        assert any(e.get("event") == "tool_start" and e.get("tool") == "list_dir"
                   for e in events), events
        assert any(e.get("event") == "content_revised" and not e.get("content")
                   for e in events), "思考轮计划句必须撤回气泡"
        assert plan not in acc.join(), "计划句不得进交付文本"
        assert "目录里有" in acc.join(), "终答必须交付"


def test_config_switch_defaults_on_and_can_turn_off(tmp_path, monkeypatch):
    """config.json 的 agent.local_thinking_first 是权威开关（默认 true）。

    只 monkeypatch `agent_loop.CONFIG_FILE`（本函数真正的读取源，agent_loop
    自己算了一份路径），**不 reload**——初版用 delenv+reload 收口，把模块常量
    留在临时/真机路径，单跑绿、全量红（10-11 覆盖事故的同一个坑）。
    """
    import json

    import agent_loop
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(agent_loop, "CONFIG_FILE", cfg, raising=False)
    monkeypatch.delenv("LATIAO_THINKING_FIRST", raising=False)
    from agent.loop import _local_thinking_first_enabled as enabled

    assert not cfg.exists()
    assert enabled() is True            # 缺文件 → 默认开
    cfg.write_text(json.dumps({"agent": {"local_thinking_first": False}}), encoding="utf-8")
    assert enabled() is False           # 显式关 → 关
    cfg.write_text(json.dumps({"agent": {"local_thinking_first": True}}), encoding="utf-8")
    assert enabled() is True            # 显式开 → 开
