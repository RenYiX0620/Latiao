"""尾部附加块的剥离（2026-10-12）：「判定读用户原话，模型读完整请求」。

背景：`agent/prompt_build.py` 把「当前时间 / 行情时间锚 / 语气 / 语言」追加到最后
一条用户消息尾部（为保前缀缓存）。而循环里所有"按用户原话判定"的规则读的就是这条
消息——于是闲聊快车道（≤24 字）从 09-23 起 0 次生效、"短消息不做知识注入"护栏同样
失效（'哈喽'→137 字）。修法：`_TAIL_MARK` 标记 + `_strip_tail_wrapper` 剥离，
但**请求里那份不剥**（模型要读那些指令）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LATIAO_AUTH_TOKEN", "tailwrap-token")

_URL = "http://127.0.0.1:9/v1/chat/completions"

from agent.context import (  # noqa: E402
    _TAIL_MARK,
    _knowledge_injection_ok,
    _strip_tail_wrapper,
)


def _loop(text: str, level: str = "max"):
    from agent.loop import ThinAgentLoop
    return ThinAgentLoop([{"role": "user", "content": text}], "m", _URL, {}, "s-tw",
                         thinking_level=level)


def test_strip_is_a_pure_noop_without_mark():
    assert _strip_tail_wrapper("哈喽") == "哈喽"
    assert _strip_tail_wrapper("") == ""
    assert _strip_tail_wrapper(None) == ""
    # 正文里出现方括号但不是标记 → 不动
    assert _strip_tail_wrapper("【背景资料】用户自己写的") == "【背景资料】用户自己写的"


def test_loop_reads_raw_words_but_request_keeps_the_instructions():
    """同一份消息：判定看到「哈喽」，模型仍看到语气/语言指令。"""
    wrapper = _TAIL_MARK + "\n\n（本轮语气：暧昧撩人（测试））\n\n（本轮用**简体中文**书写正文与思考。）"
    wrapped = "哈喽" + wrapper
    loop = _loop(wrapped)
    assert loop.last_user_text == "哈喽", "判定侧必须剥掉框架尾巴"
    loop.steps = 1
    body = loop._build_request("m")
    _user = next(m for m in body["messages"] if m.get("role") == "user")
    _content = str(_user.get("content") or "")
    assert _TAIL_MARK in _content, "请求里那份**不剥**：标记与指令都要留给模型"
    assert "（本轮语气：暧昧撩人（测试））" in _content
    assert "（本轮用**简体中文**书写正文与思考。）" in _content


def test_short_greeting_regains_the_injection_guard():
    """护栏的输入是原话：'哈喽' 不再被当成'长消息'放行注入。"""
    wrapped = "哈喽" + _TAIL_MARK + "（本轮语气：暧昧撩人（测试））"
    loop = _loop(wrapped)
    assert _knowledge_injection_ok(loop.last_user_text) is False
    # 正控：修复前的输入（包装文本）会被放行——这就是护栏失效的机制
    assert _knowledge_injection_ok(wrapped) is True
    # 任务消息照常注入（不受影响）
    task = _loop("分析今天大盘走势" + _TAIL_MARK + "（当前时间：2026-10-12）")
    assert _knowledge_injection_ok(task.last_user_text) is True


def test_language_decision_reads_raw_words_not_the_wrapper():
    """英文短消息 + 中文尾巴（时间/进展块恒为中文）不再被判成中文。"""
    wrapped = "hi there" + _TAIL_MARK + "（当前时间：2026-10-12）\n\n（本轮用**简体中文**书写正文与思考。）"
    loop = _loop(wrapped)
    assert loop.user_lang == "en"
    # 正控：不剥（修复前的行为）＝ 判成中文，模型被要求用中文回答
    from agent.context import detect_language_decision
    assert detect_language_decision(wrapped, [])[0] == "zh"


def test_build_chat_messages_emits_the_shared_mark(tmp_path):
    """装配端与剥离端共用同一个标记（防漂移）。

    子进程跑：PROGRESS_DIR 在导入期解析，隔离成本低于 reload 链。
    """
    sidecar = Path(__file__).resolve().parents[1]
    state = tmp_path / "state"
    state.mkdir()
    (state / "IDENTITY.md").write_text("# Identity\n\n你的名字是「欧娜」。\n", encoding="utf-8")
    (state / "USER.md").write_text("# User Profile\n\n- 用户称呼：主人\n", encoding="utf-8")
    (state / "SOUL.md").write_text("# SOUL\n\n## 对话语气\n\n暧昧撩人（测试）\n", encoding="utf-8")
    (state / "PROGRESS.md").write_text("# ChangeLog\n\n## 2026-10-12\n- 测试\n", encoding="utf-8")
    (state / "config.json").write_text("{}", encoding="utf-8")
    script = tmp_path / "probe.py"
    script.write_text(
        "import json, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from agent.prompt_build import _build_chat_messages\n"
        "from agent.context import _TAIL_MARK, _strip_tail_wrapper\n"
        "msgs = [{'role': 'system', 'content': '请始终用简体中文回复用户。'},\n"
        "        {'role': 'user', 'content': '哈喽'}]\n"
        "built = _build_chat_messages({'messages': msgs, 'non_interactive': True}, msgs)\n"
        "last = [m for m in built if m.get('role') == 'user'][-1]['content']\n"
        "print(json.dumps({'mark': _TAIL_MARK in last, 'stripped': _strip_tail_wrapper(last),\n"
        "                  'grew': len(last) > len('哈喽')}, ensure_ascii=False))\n",
        encoding="utf-8")
    env = {**os.environ, "LATIAO_TEST_PROGRESS_DIR": str(state), "LATIAO_AUTH_TOKEN": "x"}
    out = subprocess.run([sys.executable, str(script), str(sidecar)],
                         capture_output=True, text=True, env=env, timeout=180)
    assert out.returncode == 0, out.stderr[-900:]
    data = json.loads(out.stdout.strip().splitlines()[-1])
    assert data == {"mark": True, "stripped": "哈喽", "grew": True}, data
