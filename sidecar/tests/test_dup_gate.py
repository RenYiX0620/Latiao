"""复读闸门（_gate_round_content）单测。

09-21 修的 bug：闸门无条件把本轮正文扣到攒满 _DUP_PROBE_CHARS(60) 才下发，
实测每轮开头 60 字被扣约 0.9 秒再整块蹦出，短于 60 字的回复整轮不显示；
而 `_looks_like_replay` 在上一轮不足 60 字时本来就必然判 False —— 那段时间白扣。

这些用例锁住三件事：①没有可比基准时立即放行；②有基准时仍会拦截复读；
③缓冲到时限（LATIAO_DUP_HOLD_MS）必须放行，不许无限期扣字。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.loop import ThinAgentLoop, _DUP_HOLD_SECS, _replay_possible  # noqa: E402

LONG_PREV = "根据刚才的目录输出，这个目录里包含 agents、skills、tests 等目录以及若干 python 文件。" * 2


def _gate():
    """只带闸门所需状态的轻量实例（不跑循环、不连引擎）。"""
    obj = object.__new__(ThinAgentLoop)
    obj._round_buf = []
    obj._round_buf_t0 = 0.0
    obj._round_dup = False
    obj._round_dup_decided = False
    obj._last_round_text = ""
    obj._step_log = lambda *a, **k: None
    return obj


# ── 前置条件 ────────────────────────────────────────────────────
def test_replay_possible_matches_judge_precondition():
    assert _replay_possible(LONG_PREV) is True
    assert _replay_possible("好的") is False          # 短句：判定必然 False
    assert _replay_possible("") is False
    assert _replay_possible("   \n  ") is False
    assert _replay_possible("短" * 59) is False
    assert _replay_possible("短" * 60) is True


# ── ① 没有可比基准：不扣字 ──────────────────────────────────────
def test_no_baseline_streams_immediately():
    g = _gate()
    # 每个 delta 都必须立刻放行（原来会返回 None 扣到 60 字）
    for chunk in ["你好", "！", "我", "是", "辣条"]:
        assert g._gate_round_content(chunk) == chunk
    assert g._round_dup is False


def test_short_previous_round_also_streams_immediately():
    g = _gate()
    g._last_round_text = "好的"                        # 上一轮太短，判不出复读
    assert g._gate_round_content("今天") == "今天"


# ── ② 有基准时仍能拦截复读 ──────────────────────────────────────
def test_replay_still_suppressed_with_baseline():
    g = _gate()
    g._last_round_text = LONG_PREV
    out = [g._gate_round_content(LONG_PREV[i:i + 20]) for i in range(0, 60, 20)]
    assert out[0] is None and out[1] is None           # 攒够之前不下发
    assert out[2] is None                              # 攒满即判定为复读 → 整轮不下发
    assert g._round_dup is True
    assert g._gate_round_content("后续内容") is None     # 判定后仍然不再下发


def test_non_replay_released_after_probe():
    g = _gate()
    g._last_round_text = LONG_PREV
    first = "今天"
    filler = "杭州西湖今天天气很好。"          # 11 字
    body = filler * 6                          # 攒够 60 字阈值
    assert len(first + body) >= 60, "用例前提：两段之和必须够 _DUP_PROBE_CHARS"
    assert g._gate_round_content(first) is None          # 未攒满 → 仍缓冲
    got = g._gate_round_content(body)
    assert got == first + body                           # 攒满即一次性放出（原行为）
    assert g._round_dup is False
    assert g._gate_round_content("继续") == "继续"        # 判定后逐字放行


# ── ③ 时限放行：不允许无限期扣字 ────────────────────────────────
def test_hold_deadline_releases_partial_buffer():
    g = _gate()
    g._last_round_text = LONG_PREV
    assert g._gate_round_content("杭州") is None         # 刚开始攒
    g._round_buf_t0 = time.monotonic() - (_DUP_HOLD_SECS + 0.05)   # 模拟已超时
    got = g._gate_round_content("是浙江省会。")
    assert got == "杭州是浙江省会。"                      # 按时限放行，而不是扣到 60 字
    assert g._round_dup is False
    assert g._gate_round_content("下一句") == "下一句"


def test_flush_round_gate_releases_leftover():
    g = _gate()
    g._last_round_text = LONG_PREV
    g._gate_round_content("半句话")
    assert g._flush_round_gate() == "半句话"             # 轮末残余照常下发
    assert g._flush_round_gate() == ""                  # 二次调用不重复
