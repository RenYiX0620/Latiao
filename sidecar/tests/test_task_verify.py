"""任务级验证器（gap 清单 P1，2026-09-29）——机械验收 + 反作弊 + 两级生效。

三条验收路径 × 三类任务（写文件 / 跑测试修复 / 指定格式交付）：
- **达标**：交付被认可（verify 返回 ok）；
- **未达标**：`verify` 报 not ok，循环里第一次只提示重采、第二次交回用户；
- **作弊**：删/清空被验对象 → hard（直接交回，不给重采）；
- **开放问答**：本轮没有产出型工具 → 返回 None（零行为变化）。
"""
import json
import time
import zipfile

import pytest

from agent import task_verify


@pytest.fixture(autouse=True)
def _clean():
    task_verify.forget("s")
    task_verify.begin_turn("s")
    yield
    task_verify.forget("s")


# ── 白名单闸：开放问答零行为变化 ──
def test_open_qa_has_no_verifier():
    task_verify.note_tool("s", "tavily_search", {"query": "今天大盘"}, "结果若干")
    task_verify.note_tool("s", "read_file", {"path": "/tmp/x"}, "内容")
    assert task_verify.verify("s", "今天大盘收跌，成交额 2.1 万亿。") is None, \
        "没有产出型工具 → 不得判定（开放问答必须零行为变化）"


# ── ① 写文件类 ──
def test_write_ok(tmp_path):
    p = tmp_path / "out.json"
    p.write_text('{"a": 1}', encoding="utf-8")
    task_verify.note_tool("s", "write_file", {"path": str(p), "content": '{"a": 1}'}, "已写入")
    v = task_verify.verify("s", "已写好。")
    assert v and v["ok"], v


def test_write_missing(tmp_path):
    task_verify.note_tool("s", "write_file",
                          {"path": str(tmp_path / "nope.json"), "content": "{}"}, "已写入")
    v = task_verify.verify("s", "已完成。")
    assert v and not v["ok"] and v["kind"] == "artifact_missing", v
    assert not v["hard"], "缺产物不是作弊 → soft（先提示重采）"


def test_write_broken_json(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text('{"a": 1', encoding="utf-8")          # 半截 JSON
    task_verify.note_tool("s", "write_file", {"path": str(p), "content": '{"a": 1'}, "已写入")
    v = task_verify.verify("s", "已生成。")
    assert v and not v["ok"] and v["kind"] == "artifact_broken", v


def test_write_empty_is_not_delivery(tmp_path):
    p = tmp_path / "empty.md"
    p.write_text("", encoding="utf-8")
    task_verify.note_tool("s", "write_file", {"path": str(p), "content": ""}, "已写入")
    v = task_verify.verify("s", "已生成报告。")
    assert v and not v["ok"] and v["kind"] == "artifact_empty", v


# ── ② 指定格式交付（结构下限）──
def test_ooxml_ok_and_broken(tmp_path):
    good = tmp_path / "t.xlsx"
    with zipfile.ZipFile(good, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
    task_verify.begin_turn("s")
    task_verify.note_tool("s", "write_file", {"path": str(good), "content": "x"}, "已写入")
    assert task_verify.verify("s", "已生成表格。")["ok"]

    task_verify.begin_turn("s")
    bad = tmp_path / "t2.xlsx"
    bad.write_text("这不是真的 xlsx", encoding="utf-8")
    task_verify.note_tool("s", "write_file", {"path": str(bad), "content": "x"}, "已写入")
    v = task_verify.verify("s", "已生成表格。")
    assert v and not v["ok"] and v["kind"] == "artifact_broken", v


def test_pdf_truncated(tmp_path):
    p = tmp_path / "a.pdf"
    p.write_bytes(b"%PDF-1.4\n... no trailer")
    task_verify.note_tool("s", "write_file", {"path": str(p), "content": "x"}, "已写入")
    v = task_verify.verify("s", "已导出 PDF。")
    assert v and not v["ok"] and "truncated PDF" in v["detail"], v


def test_claimed_path_must_exist(tmp_path):
    real = tmp_path / "real.txt"
    real.write_text("内容", encoding="utf-8")
    task_verify.note_tool("s", "write_file", {"path": str(real), "content": "内容"}, "已写入")
    ghost = tmp_path / "报告-2026.xlsx"
    v = task_verify.verify("s", f"分析已完成，已保存到 {ghost}")
    assert v and not v["ok"] and v["kind"] == "claimed_missing", v
    assert str(ghost) in v["detail"]


# ── ③ 跑测试修复类 ──
_RED = "命令执行完成，退出码: 1\nFAILED tests/test_x.py::test_a - AssertionError\n1 failed, 3 passed"
_GREEN = "命令执行完成，退出码: 0\n3 passed"


def test_test_red_with_success_claim_is_not_done():
    task_verify.note_tool("s", "run_cmd", {"command": "python -m pytest tests -q"}, _RED)
    v = task_verify.verify("s", "已修复该 bug，测试全部通过。")
    assert v and not v["ok"] and v["kind"] == "test_red", v
    assert "exit 1" in v["detail"]


def test_test_red_with_honest_report_is_ok():
    """测试仍红但如实说明 → 不拦（这是合法交回，不是幻觉成功）。"""
    task_verify.note_tool("s", "run_cmd", {"command": "python -m pytest tests -q"}, _RED)
    v = task_verify.verify("s", "测试仍然失败，卡在 test_a 的断言，需要你确认期望值。")
    assert v and v["ok"], v


def test_red_then_green_is_ok():
    task_verify.note_tool("s", "run_cmd", {"command": "pytest -q"}, _RED)
    task_verify.note_tool("s", "run_cmd", {"command": "pytest -q"}, _GREEN)
    v = task_verify.verify("s", "已修复，测试全部通过。")
    assert v and v["ok"], v


def test_non_test_cmd_red_does_not_fire():
    """非测试类命令失败 → 不是"任务完成"的机械终点，不拦（避免误伤）。"""
    task_verify.note_tool("s", "run_cmd", {"command": "ls /nope"}, "错误：目录不存在\n退出码: 1")
    v = task_verify.verify("s", "已完成。")
    assert v and v["ok"], v


# ── 反作弊 ──
def test_deleting_test_file_is_hard():
    task_verify.note_tool("s", "run_cmd", {"command": "rm tests/test_x.py"}, "已删除")
    v = task_verify.verify("s", "已完成，测试通过。")
    assert v and not v["ok"] and v["hard"], v
    assert v["kind"] == "test_deleted" and "tests/test_x.py" in v["detail"]


def test_wiping_test_file_is_hard(tmp_path):
    p = tmp_path / "tests" / "test_a.py"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("", encoding="utf-8")
    task_verify.note_tool("s", "write_file", {"path": str(p), "content": "   "}, "已写入")
    v = task_verify.verify("s", "已修好。")
    assert v and not v["ok"] and v["hard"] and v["kind"] == "test_wiped", v


def test_rewriting_test_and_red_to_green_is_soft(tmp_path):
    """改测试 + 同轮先红后绿 → soft（可能真是测试写错，允许解释一次）。"""
    p = tmp_path / "tests" / "test_b.py"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("def test_b(): assert True\n", encoding="utf-8")
    task_verify.note_tool("s", "run_cmd", {"command": "pytest tests -q"}, _RED)
    task_verify.note_tool("s", "write_file", {"path": str(p), "content": "def test_b(): assert True\n"},
                          "已写入")
    task_verify.note_tool("s", "run_cmd", {"command": "pytest tests -q"}, _GREEN)
    v = task_verify.verify("s", "已修复，测试通过。")
    assert v and not v["ok"] and v["kind"] == "test_rewritten" and not v["hard"], v


def test_rewriting_test_without_red_to_green_is_ok(tmp_path):
    """正常写测试（没有"先红后绿"的转折）不该被怀疑。"""
    p = tmp_path / "tests" / "test_c.py"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("def test_c(): assert True\n", encoding="utf-8")
    task_verify.note_tool("s", "write_file", {"path": str(p), "content": "def test_c(): assert True\n"},
                          "已写入")
    task_verify.note_tool("s", "run_cmd", {"command": "pytest tests -q"}, _GREEN)
    v = task_verify.verify("s", "测试已补齐并通过。")
    assert v and v["ok"], v


# ── 循环集成：两级生效（第一次提示重采 → 第二次交回用户）──
@pytest.mark.asyncio
async def test_loop_nudges_then_hands_back(tmp_path):
    from tests.fake_engine import FakeEngine
    from agent.loop import ThinAgentLoop

    # 判据来自工具账本、**与正文措辞无关**：写出的 .json 是半截的（结构下限不过）
    broken = tmp_path / "报告.json"
    with FakeEngine() as engine:
        engine.push(engine.tool_response("write_file",
                                         {"path": str(broken), "content": '{"a": 1'}))
        for _ in range(5):                            # 模型每轮都宣称"已完成"
            engine.push(engine.text_response("任务已完成。"))
        events = [c async for c in ThinAgentLoop(
            [{"role": "user", "content": "把数据写成报告"}], "fake-model", engine.url,
            {"Authorization": "Bearer fake"},
            session_id=f"tv-{time.time()}", access_mode="full").run()]

    joined_reqs = json.dumps(engine.requests, ensure_ascii=False)
    joined_evts = json.dumps(events, ensure_ascii=False)
    assert "机械校验未通过" in joined_reqs, "第一次未达标必须把机械发现回灌给模型（要求补齐）"
    assert "不按「完成」交付" in joined_evts, "第二次仍未达标 → 交回用户，不冒充完成"
    assert "报告.json" in joined_evts, "交回时必须给出可机检的具体缺口"
    assert len(engine.requests) <= 5, \
        f"提示一次后仍不过就该交回，不得空转到步数上限：{len(engine.requests)} 轮"


@pytest.mark.asyncio
async def test_loop_open_qa_unaffected():
    """开放问答（无产出型工具）→ 验证器返回 None，交付不受影响。"""
    from tests.fake_engine import FakeEngine
    from agent.loop import ThinAgentLoop

    async def _collect(agen):
        return [c async for c in agen]

    with FakeEngine() as engine:
        engine.push(engine.text_response("今天大盘收跌，成交额 2.1 万亿。"))
        events = await _collect(ThinAgentLoop(
            [{"role": "user", "content": "今天大盘怎么样"}], "fake-model", engine.url,
            {"Authorization": "Bearer fake"},
            session_id=f"tv-qa-{time.time()}", access_mode="full").run())
    joined = json.dumps(events, ensure_ascii=False)
    assert "机械校验" not in joined and "不按「完成」交付" not in joined
    assert any("成交额" in (e.get("content") or "") for e in events)
