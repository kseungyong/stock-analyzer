"""src/scheduler.py 테스트."""
from unittest.mock import MagicMock, patch
import pytest

from src.scheduler import start_scheduler


class TestExtraJobs:
    @patch("src.scheduler.BlockingScheduler")
    def test_extra_jobs_added(self, sched_cls):
        instance = MagicMock()
        sched_cls.return_value = instance

        def main_job():
            pass

        def backfill_job():
            pass

        from apscheduler.triggers.cron import CronTrigger
        extra = {
            "backfill_daily": {
                "func": backfill_job,
                "trigger": CronTrigger(hour=18, minute=0),
                "name": "Daily Backfill",
            }
        }

        start_scheduler(main_job, {"hour": 8, "minute": 30}, extra_jobs=extra)

        assert instance.add_job.call_count == 2
        ids = [call.kwargs.get("id") for call in instance.add_job.call_args_list]
        assert "daily_report" in ids
        assert "backfill_daily" in ids

    @patch("src.scheduler.BlockingScheduler")
    def test_no_extra_jobs(self, sched_cls):
        """기존 호환성: extra_jobs=None일 때 daily_report 1개만."""
        instance = MagicMock()
        sched_cls.return_value = instance

        def main_job():
            pass

        start_scheduler(main_job, {"hour": 8, "minute": 30})
        assert instance.add_job.call_count == 1
