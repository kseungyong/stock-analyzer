"""_next_trading_day_unix: 거래소 캘린더 기반 다음 거래일 산출 검증.

회귀 방지: pd.tseries.offsets.BDay 는 토/일만 skip 했으나, 한국 부처님오신날 /
미국 Memorial Day 같은 공휴일을 모름 → 영원히 평가 불가 row 생성됐음.
이제 XKRX (.KS/.KQ) / XNYS (그 외) 캘린더로 진짜 다음 거래일 산출.
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from main import _next_trading_day_unix


def _to_kst_date(unix_ts: int) -> str:
    return datetime.fromtimestamp(unix_ts, tz=pd.Timestamp("now", tz="Asia/Seoul").tz).strftime("%Y-%m-%d")


@pytest.mark.parametrize("last, symbol, expected_kst", [
    # 한국: 평일 → 다음 평일
    ("2026-05-21", "005930.KS", "2026-05-22"),
    # 한국: 부처님오신날 (5/25 월) 직전 금요일 → 다음 거래일은 5/26 화 (5/25 skip)
    ("2026-05-22", "005930.KS", "2026-05-26"),
    # 한국: 어린이날 (5/5 화 2026) 직전 → 5/5 skip
    ("2026-05-04", "005930.KS", "2026-05-06"),
    # 한국: 광복절 (8/15 토 2026) → 대체공휴일 8/17 월 → 8/14 금 다음 거래일 = 8/18 화
    # (BDay 만으로는 8/17 로 잘못 산출 — 이 라이브러리 도입 명분)
    ("2026-08-14", "000660.KS", "2026-08-18"),
    # 한국 코스닥 (.KQ) 도 동일 캘린더
    ("2026-05-22", "035720.KQ", "2026-05-26"),
    # 미국: 평일
    ("2026-05-21", "AAPL", "2026-05-22"),
    # 미국: Memorial Day (5/25 월 2026) 직전 금 → 5/26 화 skip
    ("2026-05-22", "AAPL", "2026-05-26"),
])
def test_next_trading_day(last, symbol, expected_kst):
    unix = _next_trading_day_unix(pd.Timestamp(last, tz="Asia/Seoul"), symbol)
    assert _to_kst_date(unix) == expected_kst, (
        f"{symbol} last={last} → got={_to_kst_date(unix)}, want={expected_kst}"
    )


def test_naive_timestamp_localized_to_seoul():
    """tz-naive Timestamp 입력도 처리 (yfinance df.index 일부 케이스)."""
    naive = pd.Timestamp("2026-05-22")  # tz=None
    unix = _next_trading_day_unix(naive, "005930.KS")
    assert _to_kst_date(unix) == "2026-05-26"
