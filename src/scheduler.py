from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz


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

    print(f"[SCHEDULER] 매일 {config.get('hour', 8)}:{config.get('minute', 30):02d} "
          f"({config.get('timezone', 'Asia/Seoul')}) 실행 예정")
    print("[SCHEDULER] Ctrl+C로 종료")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("\n[SCHEDULER] 종료됨")
