import logging
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

logger = logging.getLogger(__name__)


def start_scheduler(job_func, config: dict) -> None:
    """APScheduler로 매일 지정 시간에 job_func을 실행한다.

    Args:
        job_func: 실행할 함수
        config: schedule 설정 (hour, minute, timezone)
    """
    tz = pytz.timezone(config.get("timezone", "Asia/Seoul"))
    scheduler = BlockingScheduler(timezone=tz)

    trigger = CronTrigger(
        hour=config.get("hour", 8),
        minute=config.get("minute", 30),
        timezone=tz,
    )

    scheduler.add_job(job_func, trigger, id="daily_report", name="Daily Stock Report")

    logger.info(
        "스케줄러 시작 — 매일 %d:%02d (%s) 실행 예정. Ctrl+C로 종료.",
        config.get("hour", 8), config.get("minute", 30), config.get("timezone", "Asia/Seoul"),
    )

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("스케줄러 종료됨.")
