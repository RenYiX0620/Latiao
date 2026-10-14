"""Cron scheduler tests: validation, missed-run catch-up, result recording.

2026-09-29 审计修复：本文件原先用"赋值覆盖 + finally `del`"来临时替换
`cron._save_cron` / `_save_cron_state` / `_load_cron`——`del` 删的是**覆盖后**的名字，
等于把原函数从模块里永久删掉，后续测试（任何模块）再用 `cron._save_cron` 就 AttributeError
（同类泄漏在 test_app_flow 也出现过）。改为"存原值 + finally 还原"。
"""
import asyncio
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import cron  # noqa: E402


class TestValidateSchedule(unittest.TestCase):
    def test_valid_expressions(self):
        for expr in ["0 9 * * *", "*/30 * * * *", "0 18 * * 5", "9,17 * * * *",
                     "0 9-17 * * *", "30 8 1 * *", "0 0 1 1 0", "*/15 9-17 * * 1-5"]:
            self.assertIsNone(cron._validate_schedule(expr), expr)

    def test_invalid_expressions(self):
        cases = ["", "每天9点", "0 9 * *", "60 * * * *", "* 24 * * *", "0 9 0 * *",
                 "0 9 * * 8", "9-5 * * * *", "*/0 * * * *", "a b c d e", "0 9 * * -1"]
        for expr in cases:
            self.assertIsNotNone(cron._validate_schedule(expr), expr)

    def test_error_message_is_readable(self):
        msg = cron._validate_schedule("60 * * * *")
        self.assertIn("分", msg)


class TestCronMatch(unittest.TestCase):
    def test_every_30min(self):
        self.assertTrue(cron._cron_matches("*/30 * * * *", datetime(2026, 8, 15, 9, 0)))
        self.assertTrue(cron._cron_matches("*/30 * * * *", datetime(2026, 8, 15, 9, 30)))
        self.assertFalse(cron._cron_matches("*/30 * * * *", datetime(2026, 8, 15, 9, 45)))

    def test_friday_only(self):
        # 2026-08-14 is a Friday
        self.assertTrue(cron._cron_matches("0 18 * * 5", datetime(2026, 8, 14, 18, 0)))
        self.assertFalse(cron._cron_matches("0 18 * * 5", datetime(2026, 8, 15, 18, 0)))


class TestFindMissedJobs(unittest.TestCase):
    def _job(self, schedule, last_run=None, created_days_ago=0):
        return {
            "id": "j1", "schedule": schedule, "task": "t", "enabled": True,
            "action": "notify",
            "last_run": last_run or "",
            "created_at": datetime(2026, 8, 1).isoformat(),
        }

    def test_missed_daily_job_is_found(self):
        # Daily 09:00 job last ran yesterday 09:00; now today 10:00 -> one occurrence missed
        now = datetime(2026, 8, 15, 10, 0)
        job = self._job("0 9 * * *", last_run=datetime(2026, 8, 14, 9, 0).isoformat())
        cron._cron_jobs = [job]
        missed = cron._find_missed_jobs(now)
        self.assertEqual(len(missed), 1)

    def test_recently_ran_is_not_missed(self):
        # Job ran 10 minutes ago for a */30 schedule -> no missed run
        now = datetime(2026, 8, 15, 10, 0)
        job = self._job("*/30 * * * *", last_run=datetime(2026, 8, 15, 9, 30).isoformat())
        cron._cron_jobs = [job]
        self.assertEqual(cron._find_missed_jobs(now), [])

    def test_disabled_job_never_missed(self):
        now = datetime(2026, 8, 15, 10, 0)
        job = self._job("0 9 * * *", last_run=datetime(2026, 8, 14, 9, 0).isoformat())
        job["enabled"] = False
        cron._cron_jobs = [job]
        self.assertEqual(cron._find_missed_jobs(now), [])

    def test_not_due_yet_not_caught_up(self):
        # Daily 09:00 job ran yesterday 09:00; now today 08:00 -> today's run not due yet
        now = datetime(2026, 8, 15, 8, 0)
        job = self._job("0 9 * * *", last_run=datetime(2026, 8, 14, 9, 0).isoformat())
        cron._cron_jobs = [job]
        self.assertEqual(cron._find_missed_jobs(now), [])

    def test_long_gap_still_catches_up_latest_occurrence(self):
        # Last run 3 days ago: the window内最近一次到期（今天 09:00）仍补跑一次
        now = datetime(2026, 8, 15, 10, 0)
        job = self._job("0 9 * * *", last_run=datetime(2026, 8, 12, 9, 0).isoformat())
        cron._cron_jobs = [job]
        missed = cron._find_missed_jobs(now)
        self.assertEqual(len(missed), 1)  # 只补一次，不补 3 次


class TestRecordResult(unittest.TestCase):
    def test_record_updates_job_and_history(self):
        job = {"id": "j1", "schedule": "0 9 * * *", "task": "测试", "enabled": True, "action": "notify"}
        cron._cron_jobs = [job]
        saved = []
        _orig_save = cron._save_cron
        cron._save_cron = lambda jobs: saved.append(jobs)  # 避免写盘
        _orig_save_state = cron._save_cron_state
        cron._save_cron_state = lambda: None
        try:
            cron._record_cron_result(job, "success", "任务完成 summary")
            self.assertTrue(job["last_run"])
            self.assertEqual(job["last_status"], "success")
            self.assertEqual(len(job["history"]), 1)
            self.assertEqual(cron._cron_state["events"][-1]["task"], "测试")
            self.assertEqual(cron._cron_state["events"][-1]["status"], "success")
        finally:
            cron._cron_state["events"] = []
            _orig_save = cron._save_cron
            cron._save_cron = _orig_save
            _orig_save_state = cron._save_cron_state
            cron._save_cron_state = _orig_save_state

    def test_history_capped_at_20(self):
        job = {"id": "j1", "schedule": "0 9 * * *", "task": "t", "enabled": True, "action": "notify"}
        cron._cron_jobs = [job]
        _orig_save = cron._save_cron
        cron._save_cron = lambda jobs: None
        _orig_save_state = cron._save_cron_state
        cron._save_cron_state = lambda: None
        try:
            for i in range(30):
                cron._record_cron_result(job, "success", f"run {i}")
            self.assertEqual(len(job["history"]), 20)
            self.assertEqual(job["history"][-1]["summary"], "run 29")
        finally:
            cron._cron_state["events"] = []
            _orig_save = cron._save_cron
            cron._save_cron = _orig_save
            _orig_save_state = cron._save_cron_state
            cron._save_cron_state = _orig_save_state


class TestSeedDefaultCron(unittest.TestCase):
    def test_seeded_prevents_reseeding(self):
        # 已初始化（seeded=True）且用户删光任务 → 重启不恢复默认任务
        cron._cron_state["seeded"] = True
        saved = []
        _orig_load = cron._load_cron
        cron._load_cron = lambda: []  # 模拟用户删光后磁盘上的空列表
        _orig_save = cron._save_cron
        cron._save_cron = lambda jobs: saved.append(jobs)
        _orig_save_state = cron._save_cron_state
        cron._save_cron_state = lambda: None
        try:
            cron._seed_default_cron()
            self.assertEqual(cron._cron_jobs, [])
            self.assertEqual(saved, [])  # 不写盘、不播种
        finally:
            cron._cron_state["seeded"] = False
            cron._load_cron = _orig_load
            _orig_save = cron._save_cron
            cron._save_cron = _orig_save
            _orig_save_state = cron._save_cron_state
            cron._save_cron_state = _orig_save_state

    def test_load_state_restores_seeded(self):
        # 文件里有 seeded 标记 → 重启加载后 _seed_default_cron 不再播种
        import tempfile
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w", encoding="utf-8")
        tmp.write('{"last_run": {}, "events": [], "seeded": true}')
        tmp.close()
        old_file = cron.CRON_STATE_FILE
        cron.CRON_STATE_FILE = __import__("pathlib").Path(tmp.name)
        cron._cron_state = {"last_run": {}, "events": []}
        try:
            cron._load_cron_state()
            self.assertTrue(cron._cron_state.get("seeded"))
            cron._cron_jobs = []
            _orig_load = cron._load_cron
            cron._load_cron = lambda: []
            saved = []
            _orig_save = cron._save_cron
            cron._save_cron = lambda jobs: saved.append(jobs)
            try:
                cron._seed_default_cron()
                self.assertEqual(cron._cron_jobs, [])  # seeded 生效，不播种
            finally:
                _orig_load = cron._load_cron
                cron._load_cron = _orig_load
                _orig_save = cron._save_cron
                cron._save_cron = _orig_save
        finally:
            cron.CRON_STATE_FILE = old_file
            cron._cron_state = {"last_run": {}, "events": []}
            cron._cron_state.pop("seeded", None)
            __import__("os").unlink(tmp.name)

    def test_first_launch_seeds_once(self):
        # 首次启动：无 seeded 标记 + 空列表 → 播种并写入标记
        cron._cron_state.pop("seeded", None)
        saved = []
        _orig_load = cron._load_cron
        cron._load_cron = lambda: []
        _orig_save = cron._save_cron
        cron._save_cron = lambda jobs: saved.append(jobs)
        _orig_save_state = cron._save_cron_state
        cron._save_cron_state = lambda: None
        try:
            cron._seed_default_cron()
            self.assertEqual(len(cron._cron_jobs), 3)
            self.assertTrue(cron._cron_state["seeded"])
        finally:
            cron._cron_state.pop("seeded", None)
            cron._load_cron = _orig_load
            _orig_save = cron._save_cron
            cron._save_cron = _orig_save
            _orig_save_state = cron._save_cron_state
            cron._save_cron_state = _orig_save_state


if __name__ == "__main__":
    unittest.main()


# ── 审计（2026-09-29）：超时/取消/异常都必须留痕（此前只有 logger.warning）──
def _job(job_id="audit-1"):
    return {"id": job_id, "schedule": "0 9 * * *", "task": "分析今天大盘并总结",
            "action": "notify", "enabled": True}


@pytest.mark.asyncio
async def test_cron_timeout_is_recorded_and_visible(monkeypatch):
    """1200s 超时此前**完全静默**：状态/历史/事件都不更新，用户以为任务跑过。"""
    import cron

    async def _hang(_job, force_local=False):
        await asyncio.sleep(30)

    monkeypatch.setattr(cron, "_execute_cron_job", _hang)
    monkeypatch.setattr(cron, "_CRON_JOB_TIMEOUT", 0.05)
    monkeypatch.setattr(cron, "_save_cron", lambda *a, **k: None)
    j = _job("audit-timeout")
    await cron._run_cron_job_guarded(j)

    assert j.get("last_status") == "error", f"超时必须记为 error（前端只认 success/error）：{j.get('last_status')}"
    assert "[超时]" in (j.get("last_result") or ""), j.get("last_result")
    assert j.get("history") and j["history"][-1]["status"] == "error"
    events = [e for e in cron.get_recent_cron_events(minutes=10) if "超时" in str(e.get("summary", ""))]
    assert events, "超时必须推一条用户可见的事件（否则前端那条摘要还停在上一轮）"
    assert not cron._running_jobs, "收尾必须把 job 从运行集里摘掉"


@pytest.mark.asyncio
async def test_cron_executor_exception_is_recorded(monkeypatch):
    import cron

    async def _boom(_job, force_local=False):
        raise RuntimeError("引擎挂了")

    monkeypatch.setattr(cron, "_execute_cron_job", _boom)
    monkeypatch.setattr(cron, "_save_cron", lambda *a, **k: None)
    j = _job("audit-boom")
    await cron._run_cron_job_guarded(j)
    assert j.get("last_status") == "error" and "RuntimeError" in (j.get("last_result") or "")


@pytest.mark.asyncio
async def test_cron_cancel_is_recorded_and_reraised(monkeypatch):
    """应用退出/重启会取消任务：既要留痕，也要让取消语义照常传播。"""
    import cron

    async def _hang(_job, force_local=False):
        await asyncio.sleep(30)

    monkeypatch.setattr(cron, "_execute_cron_job", _hang)
    monkeypatch.setattr(cron, "_save_cron", lambda *a, **k: None)
    j = _job("audit-cancel")
    task = asyncio.create_task(cron._run_cron_job_guarded(j))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert j.get("last_status") == "error" and "[已取消]" in (j.get("last_result") or ""), j
    assert not cron._running_jobs
