"""压缩可靠性与验证失败升级（2026-09-29，gap 清单第 1 / 1b 步）。

两条都属于"闸门咬合"：
- **验证 ❌ 进同错升级**：`_auto_verify` 的报告追加在工具结果**末尾**，头部扫描看不见
  → 此前"同一类校验反复不过"既不停也不计数。签名取检查项名（回读比对/文件存在/…）。
- **压缩可靠性**：旧截断 `c[:300] + c[-100:]` **从行中间切开**（数字/文件名被劈半），
  且压完没有任何提示（模型可能凭半截数字复述，撞数据诚实规则）。现在按整行截断、
  写明丢弃量、并在尾部提示"要具体数字就重新调用"。
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ── A. 验证 ❌ → 同错升级 ──
def _tool_msg(content: str, name="write_file", cid="c1"):
    return [
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": cid, "type": "function",
                         "function": {"name": name, "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": cid, "content": content},
    ]


def _loop():
    from agent.loop import ThinAgentLoop
    loop = ThinAgentLoop.__new__(ThinAgentLoop)
    loop.current_msgs = []
    loop._fail_sigs = {}
    loop._fail_scan_from = 0
    loop._escalated = False
    return loop


VERIFY_FAIL = ("已写入 /tmp/报告.md（1204 字符）\n"
               "❌ 回读比对：内容不一致！期望 1204 字符，实际 1180 (差 24)")


def test_verify_failure_escalates_after_three_rounds():
    loop = _loop()
    for i in range(3):
        loop.current_msgs.extend(_tool_msg(VERIFY_FAIL, cid=f"c{i}"))
        out = loop._scan_tool_failures()
    assert out is not None, "同类验证失败连续三轮必须升级（此前完全不计）"
    assert out["n"] == 3


def test_verify_failure_would_be_missed_by_head_only_scan():
    """正控：头部是成功文案（"已写入…"），只扫头部的旧判据必然看不见这条失败。"""
    from agent.tool_exec import _looks_like_tool_failure
    assert not _looks_like_tool_failure(VERIFY_FAIL), "头部不应被判为失败"
    assert "❌" in VERIFY_FAIL[-600:], "失败标记在尾部，靠尾部扫描才能发现"


def test_different_verify_checks_are_different_signatures():
    loop = _loop()
    a = "已写入 x\n❌ 回读比对：内容不一致！期望 1 字符，实际 0 (差 1)"
    b = "已写入 x\n❌ 文件存在：写入后文件不存在: /tmp/x"
    loop.current_msgs.extend(_tool_msg(a, cid="c1"))
    loop.current_msgs.extend(_tool_msg(b, cid="c2"))
    assert loop._scan_tool_failures() is None, "不同检查项不该合并成同一签名"


# ── B. 压缩可靠性 ──
def test_truncate_keeps_whole_lines_and_reports_drops():
    from agent.plugins.builtin import _truncate_keep_lines
    lines = [f"行{i:03d} 数据 1376.5{i % 10}" for i in range(40)]
    c = "\n".join(lines)
    out = _truncate_keep_lines(c)
    assert out != c and "轮内已压缩" in out and "省略" in out
    body = [l for l in out.splitlines() if "轮内已压缩" not in l]
    for l in body:
        assert l in lines, f"截断切碎了行：{l!r}"
    # 关键：数字没有被劈成两半
    assert all(not l.endswith("1376.5") for l in body), "数字被从中间切断"


def test_truncate_leaves_short_content_alone():
    from agent.plugins.builtin import _truncate_keep_lines
    c = "短结果，不用压"
    assert _truncate_keep_lines(c) == c


def _run_compaction_hook(current_msgs, *, is_local=True, user_lang="zh"):
    import asyncio
    from agent.core import Scope
    from agent.plugins import builtin

    scope = Scope("test")
    builtin.setup_compaction(scope)
    wf = scope.waterfall("pre_step")
    hook = [h for _o, name, h in wf._hooks if name == "compaction"][0]
    loop = SimpleNamespace(current_msgs=current_msgs, steps=1, is_local=is_local,
                           user_lang=user_lang, _last_compact_total=0)

    class _Ctx:
        def get_service(self, _name):
            return loop

    asyncio.run(hook({}, _Ctx()))
    return loop


def _big_tool_msg(i: int, lines: int = 320) -> list:
    """单条 ~7k 字符的工具结果（3 条合计 > 18000 阈值才真会触发压缩）。

    注意轮内截断只作用于"最近 2 条之外"的工具结果 → 至少 3 条才会被裁。
    """
    body = "\n".join(f"数据行{i:03d}-{j:03d} 金额 88.{j % 100:02d} 元" for j in range(lines))
    return [{"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}"}]},
            {"role": "tool", "tool_call_id": f"c{i}", "content": body}]


def test_compaction_truncates_long_tool_result_and_warns():
    msgs = [{"role": "user", "content": "帮我汇总这些数据"}]
    for i in range(3):
        msgs.extend(_big_tool_msg(i))
    loop = _run_compaction_hook(msgs)
    tool_msgs = [m for m in loop.current_msgs if m.get("role") == "tool"]
    assert "轮内已压缩" in tool_msgs[0]["content"], "长结果应被截断并标记丢弃量"
    joined = json.dumps(loop.current_msgs, ensure_ascii=False)
    assert "不要凭记忆复述" in joined, "压缩后必须提示别再凭记忆引用具体数字"


def test_compaction_warning_localized():
    msgs = [{"role": "user", "content": "x" * 300}]
    for i in range(3):
        msgs.extend(_big_tool_msg(i))
    loop = _run_compaction_hook(msgs, user_lang="en")
    joined = json.dumps(loop.current_msgs, ensure_ascii=False)
    assert "do not quote from memory" in joined, "提示应随用户语言本地化"


def test_truncate_single_huge_line_cuts_at_separator():
    """行少但单行极长（压缩过的 JSON/单行 CSV）：按行取整等于没压 → 就近分隔符切。"""
    from agent.plugins.builtin import _truncate_keep_lines
    c = ",".join(f'"k{i}":{i * 137}.5' for i in range(600))     # 单行、无换行
    out = _truncate_keep_lines(c)
    assert len(out) < len(c), f"单行超长也必须被压（{len(c)} → {len(out)}）"
    assert "轮内已压缩" in out and "单行内容" in out
    # 不能出现被切断的键值（形如 "k12":16 这种半截）
    body = [l for l in out.splitlines() if "轮内已压缩" not in l]
    for l in body:
        assert not l.endswith('"') or l.count('"') % 2 == 0, f"切出了半截键值：{l[-30:]!r}"


def test_truncate_no_drop_returns_unchanged():
    from agent.plugins.builtin import _truncate_keep_lines
    c = "行A\n" + "行B" + "x" * 500        # 只有两行，首尾都能整行容纳
    assert _truncate_keep_lines(c, head_chars=10, tail_chars=10) == c, \
        "既然一行都没丢，就不该加'已压缩'标记"
