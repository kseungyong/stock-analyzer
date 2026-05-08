import logging
from collections.abc import Callable
from typing import Optional
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

logger = logging.getLogger(__name__)


def start_scheduler(
    job_func: Callable[[], None],
    config: dict,
    extra_jobs: Optional[dict] = None,
) -> None:
    """APScheduler로 매일 지정 시간에 job_func을 실행한다.

    Args:
        job_func: 메인 일일 작업 함수
        config: schedule 설정 (hour, minute, timezone)
        extra_jobs: 추가 작업 dict, 형식:
            {job_id: {"func": callable, "trigger": Trigger, "name": str(optional)}}
    """
    tz = pytz.timezone(config.get("timezone", "Asia/Seoul"))
    scheduler = BlockingScheduler(timezone=tz)

    trigger = CronTrigger(
        hour=config.get("hour", 8),
        minute=config.get("minute", 30),
        timezone=tz,
    )
    # max_instances=1 + misfire_grace_time=0: 이전 cron 이 다음 trigger 시점까지
    # 안 끝나면 새 실행 skip (overlap 방지). settings.yaml + analysis_cache 동시
    # write race 차단.
    scheduler.add_job(
        job_func, trigger, id="daily_report", name="Daily Stock Report",
        max_instances=1, misfire_grace_time=300,
    )

    if extra_jobs:
        for job_id, job in extra_jobs.items():
            scheduler.add_job(
                job["func"],
                job["trigger"],
                id=job_id,
                name=job.get("name", job_id),
                max_instances=1,
                misfire_grace_time=300,
            )

    logger.info(
        "스케줄러 시작 — daily_report 매일 %d:%02d (%s) + extra_jobs %d개. Ctrl+C로 종료.",
        config.get("hour", 8), config.get("minute", 30),
        config.get("timezone", "Asia/Seoul"),
        len(extra_jobs) if extra_jobs else 0,
    )

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("스케줄러 종료됨.")
