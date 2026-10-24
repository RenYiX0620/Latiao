"""2026-10-24 六层审计修复的回归测试。

覆盖：
- P0 快照守卫：save_session 拒绝"快照变少"（防前端 slice(-200)/通道截断视图裁剪
  权威库）；import replace 空载荷拒绝。
- P0 通道桥：_load_history 返回完整历史（截断只属于模型上下文视图）。
- P1 管道右半封印：`cat x | tee ~/.zshrc` / `| dd of=` 被拦；只读管道不受影响。
- P1 内联 fail-closed：node -e 拼接路径、python 拆分字面量被拦；正常代码放行。
- P1 rm/chown 旗标位置：`rm -i -rf` / `rm x -rf` / `chown -R` 拦截。
- P2 读清单补齐：gh/gcloud 目录、.npmrc、credentials.json、memory.db。
- P2 排队上限独立变量 + 默认 170s（压在前端看门狗内）。
- P2 解析 1.2b 文档示例豁免（带 description 键的围栏 JSON 不算调用）。
- P2 疑问句豁免（"怎么卸载X？"不按任务型推进执行轮）。
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cmd_safety  # noqa: E402
import db as db_mod  # noqa: E402
import sessions  # noqa: E402


@pytest.fixture(autouse=True, scope="module")
def _init_db():
    db_mod._init_db()


# ═══ P0：save_session 快照守卫 ═══════════════════════════════

def _msg(role: str, content: str, mid: str) -> dict:
    return {"id": mid, "role": role, "content": content, "ts": 1}


def test_snapshot_shrink_rejected():
    sid = "sess-guard-shrink"
    msgs = [_msg("user", f"消息 {i}", f"{sid}_m{i}") for i in range(10)]
    assert sessions.save_session(sid, name="守卫", messages=msgs)["status"] == "ok"
    # 只带后 5 条的"截断快照"必须被拒
    res = sessions.save_session(sid, name="守卫", messages=msgs[-5:])
    assert res["status"] == "error", res
    assert "拒绝覆盖" in res["message"]
    # 权威库仍是 10 条
    got = sessions.get_session(sid)
    assert len(got["messages"]) == 10


def test_snapshot_empty_rejected_on_existing_session():
    sid = "sess-guard-empty"
    msgs = [_msg("user", "你好", f"{sid}_u")]
    assert sessions.save_session(sid, name="守卫", messages=msgs)["status"] == "ok"
    res = sessions.save_session(sid, name="守卫", messages=[])
    assert res["status"] == "error", res
    got = sessions.get_session(sid)
    assert len(got["messages"]) == 1


def test_snapshot_allow_shrink_opt_in():
    sid = "sess-guard-optin"
    msgs = [_msg("user", f"m{i}", f"{sid}_m{i}") for i in range(8)]
    assert sessions.save_session(sid, name="守卫", messages=msgs)["status"] == "ok"
    res = sessions.save_session(sid, name="守卫", messages=msgs[-3:], allow_shrink=True)
    assert res["status"] == "ok", res
    assert len(sessions.get_session(sid)["messages"]) == 3


def test_snapshot_grow_and_equal_still_ok():
    sid = "sess-guard-grow"
    msgs = [_msg("user", f"m{i}", f"{sid}_m{i}") for i in range(4)]
    assert sessions.save_session(sid, name="守卫", messages=msgs)["status"] == "ok"
    # 等长（内容变了）与变长都照常
    assert sessions.save_session(sid, name="守卫2", messages=msgs)["status"] == "ok"
    msgs2 = msgs + [_msg("assistant", "回", f"{sid}_a")]
    assert sessions.save_session(sid, name="守卫2", messages=msgs2)["status"] == "ok"


def test_import_replace_empty_payload_rejected():
    res = sessions.import_sessions([], replace=True)
    assert res["status"] == "error", res
    assert "载荷为空" in res["message"]


# ═══ P0：通道桥完整历史 ═════════════════════════════════════

def test_channels_history_full_and_model_view_trimmed():
    import channels_bridge as cb
    sid = "channel:test:full-history"
    msgs = [_msg("user" if i % 2 == 0 else "assistant", f"第{i}轮", f"{sid}_m{i}")
            for i in range(60)]
    assert sessions.save_session(sid, name="通道", messages=msgs)["status"] == "ok"
    history = cb._load_history(sid)
    # 完整历史：60 条都在（此前 out[-40:] 只剩 40 条，保存回去即丢 20 条）
    assert len(history) == 60, len(history)
    # 模型上下文视图截到 40 条且只含 role/content
    ctx = cb._model_context(history)
    assert len(ctx) == 40
    assert set(ctx[0]) == {"role", "content"}


# ═══ P1：管道右半写入封印 ═══════════════════════════════════

def test_pipe_readonly_lhs_with_tee_blocked():
    home = os.path.expanduser("~")
    denied = cmd_safety.reject_sensitive_write(f"cat /etc/hosts | tee {home}/.zshrc")
    assert denied and "不允许用命令写入" in denied


def test_pipe_tee_registry_file_blocked():
    home = os.path.expanduser("~")
    denied = cmd_safety.reject_sensitive_write(
        f"cat /etc/hosts | tee {home}/.local-ai-os/permissions.json")
    assert denied


def test_pipe_dd_of_blocked():
    home = os.path.expanduser("~")
    denied = cmd_safety.reject_sensitive_write(
        f"cat /etc/hosts | dd of={home}/.zshrc bs=1")
    assert denied


def test_pipe_readonly_both_sides_allowed():
    # 纯只读管道不受影响（正控：防误伤）
    assert cmd_safety.reject_sensitive_write("cat /etc/hosts | head -5") is None
    assert cmd_safety.reject_sensitive_write("grep foo bar.txt") is None


def test_plain_read_cmd_still_allowed():
    # 只读单命令读非敏感文件照常放行
    assert cmd_safety.reject_sensitive_write("cat /tmp/notes.txt") is None


# ═══ P1：内联 fail-closed + 拼接归一化 ═══════════════════════

def test_inline_node_split_path_blocked():
    # 变体一：这段 JS 恰好也是合法 Python（函数调用语法相同）→ AST 路径按
    # 字面量 id_rsa 拦截；变体二：const/=> 在 Python 里非法 → AST 失效，
    # 由拼接归一化扫描兜底拦截（fail-closed 的新面）。
    v1 = ("console.log(require('fs').readFileSync("
          "require('os').homedir()+'/.s'+'sh/id_rsa','utf8'))")
    assert cmd_safety._inline_path_audit(v1), v1
    v2 = "const f=require('fs');f.readFileSync('/.s'+'sh/id_rsa')"
    denied = cmd_safety._inline_path_audit(v2)
    assert denied and "拼接形态" in denied, denied


def test_inline_python_split_path_blocked():
    code = "print(open('/.s'+'sh/id_rsa').read())"
    denied = cmd_safety._inline_path_audit(code)
    assert denied


def test_inline_benign_code_allowed():
    assert cmd_safety._inline_path_audit("console.log('hello ' + 'world')") is None
    assert cmd_safety._inline_path_audit("print(sum(range(10)))") is None


# ═══ P1：rm/chown 旗标位置 ═══════════════════════════════════

def test_rm_flag_anywhere_blocked():
    for cmd in ("rm -i -rf /tmp/x", "rm /tmp/x -rf", "rm --no-preserve-root -rf /tmp/x"):
        denied = cmd_safety.check_cmd(cmd)
        assert denied, cmd


def test_chown_recursive_blocked():
    assert cmd_safety.check_cmd("chown -R nobody /tmp/x") is not None


def test_rm_single_file_still_passes_flag_gate():
    # 无 r/f 旗标的 rm 不被"旗标过程式检查"拦（后续语义/正则层另有判据）
    # 正控：防止我们把普通 rm 误拦
    assert "递归/强制" not in (cmd_safety.check_cmd("rm /tmp/single.txt") or "")


# ═══ P2：敏感读清单补齐 ═════════════════════════════════════

def test_sensitive_read_block_new_entries():
    home = os.path.expanduser("~")
    for p in (f"{home}/.config/gh/hosts.yml",
              f"{home}/.config/gcloud/application_default_credentials.json",
              f"{home}/.npmrc",
              f"{home}/.local-ai-os/memory.db",
              f"{home}/project/credentials.json"):
        assert cmd_safety.sensitive_read_block(p), p


def test_sensitive_read_block_project_json_unaffected():
    # 正控：普通项目文件不受新清单影响
    assert cmd_sensitive_read_ok("/Users/x/proj/config.json")


def cmd_sensitive_read_ok(path: str) -> bool:
    return cmd_safety.sensitive_read_block(path) is None


# ═══ P2：排队上限独立变量 ═══════════════════════════════════

def test_queue_wait_default_170_under_watchdog(monkeypatch):
    from agent import transport
    monkeypatch.delenv("LATIAO_QUEUE_WAIT_MAX", raising=False)
    monkeypatch.delenv("LATIAO_ENGINE_WAIT_MAX", raising=False)
    assert transport._queue_wait_max() == 170.0


def test_queue_wait_env_separated_from_engine_wait(monkeypatch):
    from agent import transport
    # 调大引擎等待不再把排队带过看门狗线（2026-09-30 P0 的复刻条件）
    monkeypatch.setenv("LATIAO_ENGINE_WAIT_MAX", "600")
    monkeypatch.delenv("LATIAO_QUEUE_WAIT_MAX", raising=False)
    assert transport._queue_wait_max() == 170.0
    monkeypatch.setenv("LATIAO_QUEUE_WAIT_MAX", "300")
    assert transport._queue_wait_max() == 300.0


# ═══ P2：解析 1.2b 文档示例豁免 ═════════════════════════════

def _parse(text: str):
    from agent.parsing import _parse_prompt_tool_calls
    import agent_loop
    saved = list(agent_loop.TOOLS)
    try:
        agent_loop.TOOLS.clear()
        agent_loop.TOOLS.extend([
            {"type": "function", "function": {"name": "tavily_search",
                                              "parameters": {}}}])
        clean, calls = _parse_prompt_tool_calls(text)
        return clean, calls
    finally:
        agent_loop.TOOLS[:] = saved


def test_doc_example_fence_not_a_call():
    text = ("tavily_search 是联网搜索工具，参数如下：\n"
            "```json\n"
            '{"name": "tavily_search", "description": "联网搜索工具"}\n'
            "```")
    clean, calls = _parse(text)
    assert not calls, calls
    # 正文保留（文档示例不再是"可执行调用"）
    assert "tavily_search" in clean


def test_bad_quote_dialect_still_salvaged():
    # 正控（10-20 真机方言，见 test_promise_gate_2026_10_20）：坏引号 + params
    # 仍要被 1.2b 打捞——新的"文档示例豁免"不能吃掉真调用。
    text = ('```json\n'
            '{"tool": "tavily_search, "query": "今日大盘"}\n'
            '```')
    clean, calls = _parse(text)
    assert len(calls) == 1, calls
    assert calls[0]["function"]["name"] == "tavily_search"


# ═══ P2：疑问句豁免 ═════════════════════════════════════════

def _loop_with(text: str):
    from agent.loop import ThinAgentLoop
    lo = object.__new__(ThinAgentLoop)
    lo.last_user_text = text
    return lo


def test_question_sentence_not_task_like():
    assert _loop_with("怎么卸载微信？")._request_task_like() == (False, "")
    assert _loop_with("如何删除这个文件?")._request_task_like() == (False, "")


def test_imperative_task_still_task_like():
    assert _loop_with("把智谱清言 app 完整卸载掉")._request_task_like()[0] is True
    # 行情疑问句仍按任务型处理（要数据）
    assert _loop_with("今天大盘怎么样？")._request_task_like() == (True, "行情")


# ═══ 复读扣留兜底（2026-10-24 15:12 真机"第二个任务中途停了"）═════

_SRC = Path(__file__).resolve().parents[1].joinpath("agent", "loop.py").read_text("utf-8")  # noqa: C901


def test_dup_suppressed_final_round_has_fallback():
    """终轮被复读闸扣留后不得静默交付：必须先收口重采、收口仍扣则原文交付。"""
    idx = _SRC.find("_body_lost = ((body_text or \"\").strip()")
    assert idx > 0, "兜底分支缺失"
    seg = _SRC[idx:idx + 2200]
    assert "正文丢失兜底" in seg
    assert "_round_delivered_any" in _SRC, "必须有'正文是否到达前端'的跟踪标志"
    # 两起事故都要覆盖：复读扣留（_round_dup）与思考轮丢弃（think_hold）
    assert "_round_delivered_any = True" in _SRC, "流式/leftover 放出处置标志"
    assert "self._finalize_round = True" in seg, "未收口过 → 必须转收口重采"
    assert "continue" in seg, "转收口后必须 continue 进收口轮"
    assert "_msg(\"repeat_calls\"" in seg, "收口轮仍被扣 → 原文交付并附说明"


def test_replay_gate_distinguishes_same_preamble_different_body():
    """口癖同开场、正文不同 → 放行（任务1 的 1137 字终答能显示的机制）；
    逐字级复读（151 vs 149 字预告互比）→ 扣留（任务2 step3 被扣的机制）。"""
    from agent.loop import _looks_like_replay
    pre = "（轻咬下唇，眼含笑意）主人想看看人家是怎么挑出这些宝贝板块的呀？没问题～人家这就帮主人把上周五那些涨得最欢、资金抢得最凶的板块都列出来给主人看哦～"
    assert _looks_like_replay(pre[:151], pre[:149]) is True, "逐字复读必须扣"
    real_answer = ("（轻咬下唇，眼含笑意）主人～人家已经帮主人把上周五的行情和周末消息"
                   "都扒得干干净净啦！上周五A股上演了一出惊天深V大反转，收盘数据如下")
    assert _looks_like_replay(pre, real_answer) is False, "同口癖不同正文必须放行"


def test_idle_gate_retract_only_undelivered():
    """2026-10-24 16:06 闪现事故：空转撤回只删从未到达前端的气泡；
    已流出的正文保留（防"打了 45 秒的字瞬间消失"）。"""
    idx = _SRC.find("字已流出，保留气泡")
    assert idx > 0, "撤回防闪现分支缺失"
    seg = _SRC[idx - 600:idx + 400]
    assert "_round_delivered_any" in seg, "判据必须是'正文是否到达前端'"
