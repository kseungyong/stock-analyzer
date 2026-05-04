"""src/stock_search.py 단위 테스트."""
from src.stock_search import search_stocks


class TestShortQuery:
    def test_empty_returns_empty_list(self):
        assert search_stocks("") == []

    def test_one_char_returns_empty_list(self):
        assert search_stocks("a") == []

    def test_whitespace_only_returns_empty_list(self):
        assert search_stocks("   ") == []


import pytest
import pandas as pd
from unittest.mock import patch


_FAKE_KRX_DF = pd.DataFrame([
    {"Code": "005930", "Name": "삼성전자", "Market": "KOSPI"},
    {"Code": "000660", "Name": "SK하이닉스", "Market": "KOSPI"},
    {"Code": "035720", "Name": "카카오", "Market": "KOSPI"},
    {"Code": "247540", "Name": "에코프로비엠", "Market": "KOSDAQ"},
])


class TestKoreaSearch:
    @pytest.fixture(autouse=True)
    def reset_krx_cache(self):
        """각 테스트 사이에 KRX 캐시를 초기화."""
        import src.stock_search as ss
        ss._krx_cache["loaded_at"] = None
        ss._krx_cache["data"] = []
        yield

    @patch("src.stock_search._fetch_krx_listing")
    @patch("src.stock_search._search_us", return_value=[])
    def test_substring_match_korean_name(self, _us, fetch_mock):
        fetch_mock.return_value = _FAKE_KRX_DF
        results = search_stocks("삼성")
        symbols = [r["symbol"] for r in results]
        assert "005930.KS" in symbols
        assert all(r["market"] == "korea" for r in results)

    @patch("src.stock_search._fetch_krx_listing")
    @patch("src.stock_search._search_us", return_value=[])
    def test_kosdaq_uses_kq_suffix(self, _us, fetch_mock):
        fetch_mock.return_value = _FAKE_KRX_DF
        results = search_stocks("에코프로")
        assert any(r["symbol"] == "247540.KQ" for r in results)

    @patch("src.stock_search._fetch_krx_listing")
    @patch("src.stock_search._search_us", return_value=[])
    def test_symbol_prefix_match(self, _us, fetch_mock):
        fetch_mock.return_value = _FAKE_KRX_DF
        results = search_stocks("00593")
        assert any(r["symbol"] == "005930.KS" for r in results)

    @patch("src.stock_search._fetch_krx_listing", side_effect=RuntimeError("net"))
    @patch("src.stock_search._search_us", return_value=[])
    def test_fetch_failure_returns_empty(self, _us, _fetch):
        assert search_stocks("삼성") == []

    @patch("src.stock_search._fetch_krx_listing")
    @patch("src.stock_search._search_us", return_value=[])
    def test_cache_reused_within_ttl(self, _us, fetch_mock):
        fetch_mock.return_value = _FAKE_KRX_DF
        search_stocks("삼성")
        search_stocks("카카오")
        assert fetch_mock.call_count == 1
