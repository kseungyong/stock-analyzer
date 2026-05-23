"""src/market_calendar.py 단위 테스트."""
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
import pytest

from src import market_calendar


class TestIsKrMarketOpenToday:
    @patch("src.market_calendar.datetime")
    def test_saturday_returns_false_without_fetch(self, mock_dt):
        # 2026-05-23 토 (weekday=5)
        mock_dt.now.return_value = datetime(2026, 5, 23, 12, 0)
        mock_dt.strftime = datetime.strftime
        # yfinance 호출되지 않아야 함 (fast path)
        with patch("yfinance.Ticker") as mock_ticker:
            assert market_calendar.is_kr_market_open_today() is False
            mock_ticker.assert_not_called()

    @patch("src.market_calendar.datetime")
    def test_sunday_returns_false(self, mock_dt):
        # 2026-05-24 일 (weekday=6)
        mock_dt.now.return_value = datetime(2026, 5, 24, 12, 0)
        with patch("yfinance.Ticker") as mock_ticker:
            assert market_calendar.is_kr_market_open_today() is False
            mock_ticker.assert_not_called()

    @patch("src.market_calendar.datetime")
    def test_weekday_with_today_in_kospi_returns_true(self, mock_dt):
        # 2026-05-22 금 (weekday=4), KOSPI에 같은 날짜 row 있음
        today = datetime(2026, 5, 22, 12, 0)
        mock_dt.now.return_value = today
        mock_dt.strptime = datetime.strptime

        import pandas as pd
        idx = pd.to_datetime(["2026-05-20", "2026-05-21", "2026-05-22"])
        df = pd.DataFrame({"Close": [3000.0, 3010.0, 3020.0]}, index=idx)
        mock_ticker_inst = MagicMock()
        mock_ticker_inst.history.return_value = df
        with patch("yfinance.Ticker", return_value=mock_ticker_inst):
            assert market_calendar.is_kr_market_open_today() is True

    @patch("src.market_calendar.datetime")
    def test_weekday_holiday_returns_false(self, mock_dt):
        # 2026-05-05 화 (어린이날, weekday=1), KOSPI에 그날 row 없음
        today = datetime(2026, 5, 5, 12, 0)
        mock_dt.now.return_value = today

        import pandas as pd
        # 5/4(월), 5/6(수), 5/7(목) — 5/5는 빠짐
        idx = pd.to_datetime(["2026-05-02", "2026-05-04", "2026-05-07"])
        df = pd.DataFrame({"Close": [3000.0, 3010.0, 3020.0]}, index=idx)
        mock_ticker_inst = MagicMock()
        mock_ticker_inst.history.return_value = df
        with patch("yfinance.Ticker", return_value=mock_ticker_inst):
            assert market_calendar.is_kr_market_open_today() is False

    @patch("src.market_calendar.datetime")
    def test_weekday_fetch_failure_returns_true_conservatively(self, mock_dt):
        # 평일에 yfinance 실패 → True (보수적, cron 진행)
        today = datetime(2026, 5, 22, 12, 0)
        mock_dt.now.return_value = today
        with patch("yfinance.Ticker", side_effect=Exception("network down")):
            assert market_calendar.is_kr_market_open_today() is True

    @patch("src.market_calendar.datetime")
    def test_weekday_empty_df_returns_true_conservatively(self, mock_dt):
        # 평일에 yfinance가 빈 df → True (보수적)
        today = datetime(2026, 5, 22, 12, 0)
        mock_dt.now.return_value = today
        import pandas as pd
        empty_df = pd.DataFrame()
        mock_ticker_inst = MagicMock()
        mock_ticker_inst.history.return_value = empty_df
        with patch("yfinance.Ticker", return_value=mock_ticker_inst):
            assert market_calendar.is_kr_market_open_today() is True
