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


def test_task_request_must_not_be_answered_with_a_bare_promise():
    """要干活的请求 + 短正文 + 无数据 = 空手说话，思考轮必须拦下（2026-10-13 真机事故）。

    欧娜人设让"承诺"的句式千变万化，逐个加词追不上——实测两种漏网：
    ①"人家会乖乖把数据找出来…"（无人称命中旧意图词表）；
    ②"那人家可要拿出看家本领了哦～ 保证让你听得心痒痒的。"（末句连动词都没有）。
    判据改为**请求侧**：非闲聊/非轻量请求 + 短正文 + 无数据 → 一律进执行轮。
    """
    task = "分析今天的大盘看看"
    for body in (
        "（…）主人想看大盘呀？人家这就帮你查～（…）人家会乖乖把数据找出来，"
        "然后贴在主人面前慢慢念给你听哦～ ✨",
        "（眼波流转…）主人想听人家分析大盘呀？那人家可要拿出看家本领了哦～ "
        "**保证让你听得心痒痒的。** ✨",
        "使用 ak_finance 查询贵州茅台（600519）的行情数据。",
    ):
        loop = _loop("max", task)
        assert loop._thinking_round_plan_leak(body) is True, body[:40]
    # 反控：闲聊类请求的短答不拦（否则寒暄也要多跑一轮）
    for body, turn in (("（摸摸头）主人今天辛苦啦，早点休息哦～", "今天好累呀"),
                       ("（轻笑）主人嘴这么甜，人家好开心呢。", "小骚货")):
        loop = _loop("max", turn)
        assert loop._thinking_round_plan_leak(body) is False, body[:30]
    # 反控：带数据的短答 / 带数据的长汇报不拦（"这是汇报"的主判据是**数据信号**，
    # 长度只当宽松上界——2026-10-13 二连事故：267 字的两步计划被旧的 200 字护栏挡在门外）
    loop = _loop("max", task)
    assert loop._thinking_round_plan_leak("今日大盘收跌，上证跌0.89%。") is False
    assert loop._thinking_round_plan_leak(
        "分析如下：" + "上证 3797.24 点，跌 0.43%，成交 7149 亿。" * 40) is False
    # 正例：真机原句——267 字、点了两个工具名的两步计划，必须拦下
    plan267 = ("（轻咬下唇，指尖在桌面上轻轻画着圈）主人想看看今天的盘面呀？"
               "那欧娜得先帮主人把数据都抓过来才行呢～\n\n"
               "**第一步：抓取今日A股核心指数行情**\n我将调用 `mx_query` 获取上证指数、"
               "深证成指和创业板指的实时收盘数据。\n\n"
               "**第二步：搜索今日市场热点与复盘新闻**\n既然已经收盘了，光看数字不够，"
               "还得知道资金都去了哪。我将调用 `tavily_search` 搜索今日的市场复盘和")
    assert loop._thinking_round_plan_leak(plan267) is True


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
    # 反控二：闲聊请求下的普通答复（无工具名、非行情请求）不拦。
    # 注意口径（2026-10-13 收窄后）：请求侧判据只认**行情/数据类**请求——
    # "股价问题被回了一句天气闲聊"属于空手说话，**应当**被拦（这是正例，不是反例）。
    chat = _loop("max", "今天好累呀")
    assert chat._thinking_round_plan_leak("今天天气不错，出去走走吧，别老坐着～") is False
    assert loop._thinking_round_plan_leak("今天天气不错，出去走走吧，别老坐着～") is True
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
        # 2026-10-24 撤回合同：计划句被缓冲未流出 → 不发空撤回（防旧前端误删）
        assert not any(e.get("event") == "content_revised" and not e.get("content")
                       for e in events), "未流出的计划句不发空撤回"
        assert plan not in acc.join(), "计划句不得进交付文本"
        assert "目录里有" in acc.join(), "终答必须交付"


@pytest.mark.asyncio
async def test_desktop_task_reaches_execution_round_145_incident(caplog):
    """端到端（2026-10-14「卸载 app」真机事故复刻）：桌面任务思考轮"只表态"→
    动作词闸门推进执行轮 → run_cmd 真执行 → 终答交付；计划句不得进交付文本。

    与上一条 E2E 的区别：计划体**不带工具名、不带 <tool_code>**——能救它的只有
    ①b 的动作词闸门（请求"把智谱清言app完整卸载掉"），专门锁这条新判据的端到端。
    真机现场（修复前）：四轮 tools=0、工具调用=0，模型编了"已删除"报完成。
    """
    import logging
    import time

    import agent.context as agent_context
    import local_llm
    from agent.loop import RoundTextAccumulator, ThinAgentLoop
    from tests.fake_engine import FakeEngine
    from tests.test_loop_scenarios import _StubEngine

    caplog.set_level(logging.INFO, logger="latiao-sidecar")

    plan = ("（轻咬下唇，指尖在键盘上轻轻划过）主人想把这个叫智谱清言的东西彻底"
            "赶出家门呀？没问题，人家这就帮你把它连根拔起～（凑近屏幕，眼神专注）"
            "人家先翻遍应用程序的每个角落把它揪出来，再把它体面地请出去，最后连它"
            "藏在书房角落里的那些小零碎也一并收拾干净，保证一点痕迹都不给主人留下"
            "哦～（指尖在屏幕上轻快地游走）主人只管坐着等成果就好啦，人家心里早就"
            "把步骤排得清清楚楚，一步都不会落下，保管做得又利索又干净，比新装的还"
            "清爽呢～（回头冲你眨眨眼，等你夸人家一句）")
    assert len(plan) > 200, "计划体必须长过空转闸门的两百字上限（专测动作词闸门）"
    answer = "已按主人吩咐处理完毕：应用本体与相关残留都已清理干净。" * 3

    with FakeEngine() as engine:
        engine.push(engine.text_response(plan))
        engine.push(engine.native_tool_response("run_cmd", {"cmd": "ls /Applications"}))
        engine.push(engine.text_response(answer))
        old = local_llm._engine
        _old_override = agent_context._LOCAL_NATIVE_TOOLS_OVERRIDE
        local_llm._engine = _StubEngine()
        agent_context._LOCAL_NATIVE_TOOLS_OVERRIDE = True
        import agent.confirm as _CF
        import agent.tool_exec as TE
        _orig_wait = TE._wait_tool_confirmation

        async def _auto_approve(call_id, tool_name, event_obj, timeout=0):
            # run_cmd 在 full 档也走确认（_FULL_ALWAYS_CONFIRM）：测试里自动批准，
            # 并像真实路径一样清掉 pending 条目（真实函数在 finally 里 pop）。
            _CF._pending_confirmations.pop(call_id, None)
            return True, []

        TE._wait_tool_confirmation = _auto_approve
        try:
            loop = ThinAgentLoop([{"role": "user", "content": "把智谱清言app完整卸载掉"}],
                                 "fake-model", engine.url, {},
                                 session_id=f"uninstall-e2e-{time.time()}",
                                 access_mode="full", thinking_level="max")
            acc = RoundTextAccumulator()
            events = []
            async for ev in loop.run():
                acc.feed(ev)
                events.append(ev)
        finally:
            TE._wait_tool_confirmation = _orig_wait
            local_llm._engine = old
            agent_context._LOCAL_NATIVE_TOOLS_OVERRIDE = _old_override

        assert not (engine.requests[0].get("tools") or []), "思考轮不下发工具表"
        _r1 = engine.requests[1].get("tools") or []
        assert isinstance(_r1, list) and _r1, "动作词闸门必须推进到执行轮（恢复工具表）"
        assert {t["function"]["name"] for t in _r1} == _exec_tool_names()
        assert "思考轮转执行" in caplog.text, "本用例锁的是动作词闸门，不是空转闸门"
        # 2026-10-24 撤回合同：计划句被缓冲未流出 → 不发空撤回（防旧前端误删）
        assert not any(e.get("event") == "content_revised" and not e.get("content")
                       for e in events), "未流出的计划句不发空撤回"
        assert any(e.get("event") == "tool_start" and e.get("tool") == "run_cmd"
                   for e in events), events
        assert plan not in acc.join(), "计划句不得进交付文本"
        assert "已按主人吩咐处理完毕" in acc.join(), "终答必须交付"


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
