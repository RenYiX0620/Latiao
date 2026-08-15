"""Cron scheduler tests: validation, missed-run catch-up, result recording."""
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
        cron._save_cron = lambda jobs: saved.append(jobs)  # 避免写盘
        cron._save_cron_state = lambda: None  # 避免事件写盘污染真实状态文件
        try:
            cron._record_cron_result(job, "success", "任务完成 summary")
            self.assertTrue(job["last_run"])
            self.assertEqual(job["last_status"], "success")
            self.assertEqual(len(job["history"]), 1)
            self.assertEqual(cron._cron_state["events"][-1]["task"], "测试")
            self.assertEqual(cron._cron_state["events"][-1]["status"], "success")
        finally:
            cron._cron_state["events"] = []
            del cron._save_cron
            del cron._save_cron_state

    def test_history_capped_at_20(self):
        job = {"id": "j1", "schedule": "0 9 * * *", "task": "t", "enabled": True, "action": "notify"}
        cron._cron_jobs = [job]
        cron._save_cron = lambda jobs: None
        cron._save_cron_state = lambda: None
        try:
            for i in range(30):
                cron._record_cron_result(job, "success", f"run {i}")
            self.assertEqual(len(job["history"]), 20)
            self.assertEqual(job["history"][-1]["summary"], "run 29")
        finally:
            cron._cron_state["events"] = []
            del cron._save_cron
            del cron._save_cron_state


if __name__ == "__main__":
    unittest.main()
