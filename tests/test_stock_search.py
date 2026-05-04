"""src/stock_search.py 단위 테스트."""
from unittest.mock import patch

import pandas as pd
import pytest

from src.stock_search import search_stocks


class TestShortQuery:
    def test_empty_returns_empty_list(self):
        assert search_stocks("") == []

    def test_one_char_returns_empty_list(self):
        assert search_stocks("a") == []

    def test_whitespace_only_returns_empty_list(self):
        assert search_stocks("   ") == []


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

    @patch("src.stock_search._fetch_krx_listing")
    @patch("src.stock_search._search_us", return_value=[])
    def test_kr_dedup_by_symbol(self, _us, fetch_mock):
        # 동일 Code/Market을 두 번 포함 — dedup 후 한 번만 등장해야 함
        fetch_mock.return_value = pd.DataFrame([
            {"Code": "005930", "Name": "삼성전자", "Market": "KOSPI"},
            {"Code": "005930", "Name": "삼성전자", "Market": "KOSPI"},
        ])
        results = search_stocks("삼성")
        samsung = [r for r in results if r["symbol"] == "005930.KS"]
        assert len(samsung) == 1


class _FakeSearch:
    """yf.Search mock — `quotes` 속성을 노출."""
    def __init__(self, quotes):
        self.quotes = quotes


class TestUSSearch:
    @pytest.fixture(autouse=True)
    def reset_krx_cache(self):
        """각 테스트 사이에 KRX 캐시를 초기화."""
        import src.stock_search as ss
        ss._krx_cache["loaded_at"] = None
        ss._krx_cache["data"] = []
        yield

    @patch("src.stock_search._fetch_yf_search")
    @patch("src.stock_search._fetch_krx_listing", return_value=pd.DataFrame())
    def test_us_quote_returned(self, _krx, yf_mock):
        yf_mock.return_value = _FakeSearch([
            {"symbol": "AAPL", "shortname": "Apple Inc.", "quoteType": "EQUITY", "exchange": "NMS"},
        ])
        results = search_stocks("apple")
        assert results == [{"symbol": "AAPL", "name": "Apple Inc.", "market": "us"}]

    @patch("src.stock_search._fetch_yf_search")
    @patch("src.stock_search._fetch_krx_listing", return_value=pd.DataFrame())
    def test_excludes_non_equity_etf(self, _krx, yf_mock):
        yf_mock.return_value = _FakeSearch([
            {"symbol": "BTC-USD", "shortname": "Bitcoin", "quoteType": "CRYPTOCURRENCY", "exchange": "CCC"},
            {"symbol": "SPY", "shortname": "SPDR S&P 500", "quoteType": "ETF", "exchange": "PCX"},
        ])
        results = search_stocks("spy")
        symbols = [r["symbol"] for r in results]
        assert "SPY" in symbols
        assert "BTC-USD" not in symbols

    @patch("src.stock_search._fetch_yf_search")
    @patch("src.stock_search._fetch_krx_listing", return_value=pd.DataFrame())
    def test_excludes_korean_exchange(self, _krx, yf_mock):
        yf_mock.return_value = _FakeSearch([
            {"symbol": "005930.KS", "shortname": "Samsung Electronics", "quoteType": "EQUITY", "exchange": "KSC"},
            {"symbol": "247540.KQ", "shortname": "EcoPro BM", "quoteType": "EQUITY", "exchange": "KSQ"},
            {"symbol": "AAPL", "shortname": "Apple Inc.", "quoteType": "EQUITY", "exchange": "NMS"},
        ])
        results = search_stocks("app")
        symbols = [r["symbol"] for r in results]
        assert "AAPL" in symbols
        assert "005930.KS" not in symbols
        assert "247540.KQ" not in symbols

    @patch("src.stock_search._fetch_yf_search", side_effect=RuntimeError("api"))
    @patch("src.stock_search._fetch_krx_listing", return_value=pd.DataFrame())
    def test_us_failure_returns_empty(self, _krx, _yf):
        assert search_stocks("apple") == []

    @patch("src.stock_search._fetch_yf_search")
    @patch("src.stock_search._fetch_krx_listing")
    def test_korea_first_then_us(self, krx_mock, yf_mock):
        krx_mock.return_value = _FAKE_KRX_DF
        yf_mock.return_value = _FakeSearch([
            {"symbol": "AAPL", "shortname": "Apple Inc.", "quoteType": "EQUITY", "exchange": "NMS"},
        ])
        # 'ap' — KRX fake 데이터엔 매칭 없음, yfinance만 응답 → US 결과만 반환
        results = search_stocks("ap")
        assert results == [{"symbol": "AAPL", "name": "Apple Inc.", "market": "us"}]

    @patch("src.stock_search._fetch_yf_search")
    @patch("src.stock_search._fetch_krx_listing")
    def test_kr_wins_when_us_returns_korean_symbol(self, krx_mock, yf_mock):
        # yfinance가 KSC 거래소 코드로 한국 심볼을 반환하면 _KR_EXCHANGES 필터에서 걸려 US 결과에 포함되지 않는다.
        # 결과적으로 KR 결과만 남아 한 번만 등장하며 market이 korea임을 검증.
        krx_mock.return_value = _FAKE_KRX_DF
        yf_mock.return_value = _FakeSearch([
            {"symbol": "005930.KS", "shortname": "Samsung Electronics", "quoteType": "EQUITY", "exchange": "KSC"},
        ])
        results = search_stocks("삼성")
        samsung = [r for r in results if r["symbol"] == "005930.KS"]
        assert len(samsung) == 1
        assert samsung[0]["market"] == "korea"

    @patch("src.stock_search._search_us")
    @patch("src.stock_search._search_kr")
    def test_search_stocks_dedup_keeps_kr(self, kr_mock, us_mock):
        """search_stocks의 dedup 루프 자체를 검증한다 (필터 우회).

        같은 심볼이 KR/US 양쪽에서 반환되면 KR 결과가 유지되어야 한다.
        실제 필터에서는 도달 불가능하지만, 향후 필터가 변경되어도 중복 방지가
        동작하는지 보장하기 위한 방어 코드 검증.
        """
        kr_mock.return_value = [{"symbol": "FOO.KS", "name": "Foo (KR)", "market": "korea"}]
        us_mock.return_value = [{"symbol": "FOO.KS", "name": "Foo (US)", "market": "us"}]
        results = search_stocks("foo")
        assert len(results) == 1
        assert results[0]["market"] == "korea"
