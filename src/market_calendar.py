"""market_calendar — 한국 증시 운영일 체크.

yfinance ^KS11 (KOSPI 지수) 일봉을 fetch해서 오늘 row 존재 여부로 판단.
주말/공휴일 모두 처리.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


def is_kr_market_open_today() -> bool:
    """오늘이 한국 증시 운영일인가?

    Returns:
        True  — 운영일 (KOSPI 일봉에 오늘 row 존재)
        False — 주말 / 공휴일 / 임시휴장
        True  — yfinance fetch 실패 시 (보수적, cron 진행)
    """
    today = datetime.now()
    weekday = today.weekday()
    # 토/일은 빠른 path — yfinance 호출 불필요
    if weekday >= 5:
        return False
    today_str = today.strftime("%Y%m%d")
    try:
        import yfinance as yf
        start = today - timedelta(days=7)
        df = yf.Ticker("^KS11").history(
            start=start, end=today + timedelta(days=1),
        )
        if df.empty:
            logger.warning("KOSPI 지수 fetch 실패 — 보수적으로 운영일 간주")
            return True
        last_date = df.index[-1].strftime("%Y%m%d")
        return last_date == today_str
    except Exception as e:
        logger.warning("market_calendar fetch 예외 — 보수적으로 운영일 간주: %s", e)
        return True
