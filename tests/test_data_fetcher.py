"""src/data_fetcher.py 단위 테스트."""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

# yfinance, deep_translator 미설치 환경에서도 테스트 가능하도록 모킹
_yf_mock = types.ModuleType("yfinance")
_yf_mock.Ticker = MagicMock()
sys.modules.setdefault("yfinance", _yf_mock)

_dt_mock = types.ModuleType("deep_translator")
_dt_mock.GoogleTranslator = MagicMock(return_value=MagicMock(translate=lambda t: t))
sys.modules.setdefault("deep_translator", _dt_mock)

from src.data_fetcher import (  # noqa: E402
    _is_english,
    _translate,
    fetch_news,
    fetch_stock_data,
    fetch_multiple,
)


class TestIsEnglish:
    def test_english_text(self):
        assert _is_english("Hello world") is True

    def test_korean_text(self):
        assert _is_english("안녕하세요") is False

    def test_empty_string(self):
        assert _is_english("") is False

    def test_mixed_text_mostly_english(self):
        assert _is_english("AAPL stock 상승") is True


class TestTranslate:
    def test_non_english_not_translated(self):
        text = "이미 한국어"
        assert _translate(text) == text

    def test_empty_not_translated(self):
        assert _translate("") == ""

    def test_translation_error_returns_original(self):
        with patch("src.data_fetcher._translate_cached", side_effect=Exception("API error")):
            text = "Hello world this is english"
            result = _translate(text)
            assert result == text


class TestFetchStockData:
    def _make_df(self):
        idx = pd.date_range("2024-01-01", periods=5, tz="UTC")
        return pd.DataFrame({"Close": [100, 101, 102, 103, 104]}, index=idx)

    def test_returns_dataframe(self):
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = self._make_df()
        with patch("src.data_fetcher.yf.Ticker", return_value=mock_ticker):
            df = fetch_stock_data("AAPL", period_days=5, retries=0)
        assert isinstance(df, pd.DataFrame)
        assert not df.empty

    def test_raises_on_empty_data(self):
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = pd.DataFrame()
        with patch("src.data_fetcher.yf.Ticker", return_value=mock_ticker):
            with pytest.raises(ValueError):
                fetch_stock_data("INVALID", period_days=5, retries=0)

    def test_retries_on_failure(self):
        mock_ticker = MagicMock()
        mock_ticker.history.side_effect = [Exception("network"), self._make_df()]
        with patch("src.data_fetcher.yf.Ticker", return_value=mock_ticker):
            with patch("src.data_fetcher.time.sleep"):
                df = fetch_stock_data("AAPL", period_days=5, retries=1)
        assert not df.empty

    def test_index_is_timezone_naive(self):
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = self._make_df()
        with patch("src.data_fetcher.yf.Ticker", return_value=mock_ticker):
            df = fetch_stock_data("AAPL", period_days=5, retries=0)
        assert df.index.tz is None


class TestFetchNews:
    def _make_news_item(self, title="Stock rises", summary="The stock rose today"):
        return {
            "content": {
                "title": title,
                "summary": summary,
                "canonicalUrl": {"url": "http://example.com"},
                "provider": {"displayName": "Reuters"},
                "pubDate": "2024-01-01",
            }
        }

    def test_returns_list(self):
        mock_ticker = MagicMock()
        mock_ticker.news = [self._make_news_item()]
        with patch("src.data_fetcher.yf.Ticker", return_value=mock_ticker):
            with patch("src.data_fetcher._translate", side_effect=lambda x: x):
                result = fetch_news("AAPL")
        assert isinstance(result, list)

    def test_empty_on_exception(self):
        with patch("src.data_fetcher.yf.Ticker", side_effect=Exception("fail")):
            result = fetch_news("AAPL")
        assert result == []

    def test_respects_max_items(self):
        mock_ticker = MagicMock()
        mock_ticker.news = [self._make_news_item() for _ in range(20)]
        with patch("src.data_fetcher.yf.Ticker", return_value=mock_ticker):
            with patch("src.data_fetcher._translate", side_effect=lambda x: x):
                result = fetch_news("AAPL", max_items=3)
        assert len(result) <= 3

    def test_html_stripped_from_summary(self):
        mock_ticker = MagicMock()
        mock_ticker.news = [self._make_news_item(summary="<b>Bold</b> text")]
        with patch("src.data_fetcher.yf.Ticker", return_value=mock_ticker):
            with patch("src.data_fetcher._translate", side_effect=lambda x: x):
                result = fetch_news("AAPL")
        assert "<b>" not in result[0]["summary_en"]


class TestFetchMultiple:
    def test_returns_dict(self):
        stocks = [{"symbol": "AAPL", "name": "Apple"}]
        df = pd.DataFrame({"Close": [100]}, index=pd.date_range("2024-01-01", periods=1))
        with patch("src.data_fetcher.fetch_stock_data", return_value=df):
            result = fetch_multiple(stocks)
        assert "AAPL" in result

    def test_skips_failed_symbol(self):
        stocks = [
            {"symbol": "FAIL", "name": "Fail"},
            {"symbol": "AAPL", "name": "Apple"},
        ]
        df = pd.DataFrame({"Close": [100]}, index=pd.date_range("2024-01-01", periods=1))

        def side_effect(symbol, *args, **kwargs):
            if symbol == "FAIL":
                raise ValueError("no data")
            return df

        with patch("src.data_fetcher.fetch_stock_data", side_effect=side_effect):
            result = fetch_multiple(stocks)
        assert "FAIL" not in result
        assert "AAPL" in result


from unittest.mock import patch, MagicMock


class TestFetchNewsDispatch:
    @patch("src.data_fetcher.fetch_news_kr")
    @patch("src.data_fetcher.yf.Ticker")
    def test_ks_suffix_dispatches_to_kr(self, mock_ticker, mock_kr):
        mock_kr.return_value = [{"title": "테스트"}]
        from src.data_fetcher import fetch_news
        result = fetch_news("005930.KS")
        mock_kr.assert_called_once_with("005930.KS", max_items=10)
        mock_ticker.assert_not_called()
        assert result == [{"title": "테스트"}]

    @patch("src.data_fetcher.fetch_news_kr")
    @patch("src.data_fetcher.yf.Ticker")
    def test_kq_suffix_dispatches_to_kr(self, mock_ticker, mock_kr):
        mock_kr.return_value = []
        from src.data_fetcher import fetch_news
        fetch_news("247540.KQ")
        mock_kr.assert_called_once_with("247540.KQ", max_items=10)
        mock_ticker.assert_not_called()

    @patch("src.data_fetcher.fetch_news_kr")
    @patch("src.data_fetcher.yf.Ticker")
    def test_us_symbol_dispatches_to_yfinance(self, mock_ticker, mock_kr):
        # yfinance ticker.news returns yfinance shape
        mock_ticker.return_value.news = []
        from src.data_fetcher import fetch_news
        fetch_news("AAPL")
        mock_ticker.assert_called_once_with("AAPL")
        mock_kr.assert_not_called()
