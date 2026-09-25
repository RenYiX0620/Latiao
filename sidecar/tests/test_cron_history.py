"""定时任务历史产出可读（2026-09-23 修：这张表此前只写不读）。

背景：每次 cron 跑完，完整 AI 产出写进 memory 表（type='cron_job'）——从 5 月底起
155 行、全库零读取点，前端只有最近一次 60 字摘要。现在三个读者：本测试覆盖的
`cron.list_cron_history`、`/v1/cron/history` 端点、`cron_history` 工具。
"""
import importlib

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("LATIAO_TEST_PROGRESS_DIR", str(tmp_path / ".local-ai-os"))
    import config
    importlib.reload(config)
    import db
    importlib.reload(db)
    import cron
    importlib.reload(cron)
    db._init_db()
    return db, cron


def _add(conn, task, body, created_at):
    conn.execute(
        "INSERT INTO memory (session_id, type, topic, content, meta, created_at) "
        "VALUES ('cron','cron_job',?,?, '{}', ?)",
        (task, f"Cron: {task}\n执行时间: {created_at}\n\nAI 分析结果:\n{body}", created_at))
    conn.commit()


def test_history_reads_and_strips_body(env):
    db, cron = env
    conn = db._get_db()
    _add(conn, "查询A股大盘资金", "# 报告正文\n主力净流入 3 亿", "2026-09-01 09:00")
    _add(conn, "读取目录文件", "没什么内容", "2026-09-23 03:17")
    out = cron.list_cron_history(limit=10)
    assert out["status"] == "ok" and out["total"] == 2
    assert [i["created_at"] for i in out["items"]] == ["2026-09-23 03:17", "2026-09-01 09:00"], "按时间倒序"
    assert out["items"][1]["result"].startswith("# 报告正文"), "应剥掉 'Cron: …AI 分析结果:' 前缀"
    assert "Cron:" not in out["items"][1]["result"]


def test_history_filter_and_paging(env):
    db, cron = env
    conn = db._get_db()
    for i in range(5):
        _add(conn, f"查询A股大盘资金 {i}", f"报告 {i}", f"2026-09-0{i+1} 09:00")
    _add(conn, "读取目录文件", "无关", "2026-09-23 09:00")
    assert cron.list_cron_history(job="资金")["total"] == 5
    assert cron.list_cron_history(job="不存在的东西")["total"] == 0
    # offset=1 跳过最新的那条（"读取目录文件" 09-23），接下来是 09-05、09-04
    page = cron.list_cron_history(limit=2, offset=1)
    assert [i["created_at"] for i in page["items"]] == ["2026-09-05 09:00", "2026-09-04 09:00"]


def test_history_empty_is_ok_not_error(env):
    _, cron = env
    out = cron.list_cron_history()
    assert out == {"status": "ok", "total": 0, "items": []}


def test_tool_returns_summary_and_hint(env):
    db, cron = env
    conn = db._get_db()
    _add(conn, "查询A股大盘资金", "# 完整报告\n" + "细节 " * 200, "2026-09-01 09:00")
    from pathlib import Path
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "cron_history_under_test",
        Path(cron.__file__).parent / "plugins" / "cron_history.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.PERMISSION == "safe"
    brief = mod.execute({})
    assert "共 1 条历史" in brief and "full=true" in brief
    assert len(brief) < 600, "默认只给摘要，不该把整篇报告塞进上下文"
    full = mod.execute({"full": True})
    assert "细节 细节" in full
    assert "没有找到" in mod.execute({"job": "不存在"})


def test_tool_clamps_limit(env):
    import importlib.util
    from pathlib import Path
    import cron as _cron
    spec = importlib.util.spec_from_file_location(
        "ch2", Path(_cron.__file__).parent / "plugins" / "cron_history.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # limit 越界不该报错（上限 10、下限 1）
    assert "共" in mod.execute({"limit": 999}) or "没有找到" in mod.execute({"limit": 999})
    assert "共" in mod.execute({"limit": "abc"}) or "没有找到" in mod.execute({"limit": "abc"})
