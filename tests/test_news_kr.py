"""src/news_kr.py 단위 테스트."""
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from src import news_kr


_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "naver_news_sample.html"


class TestScrapeNaverFinance:
    def _fixture_html(self) -> str:
        return _FIXTURE_PATH.read_text(encoding="utf-8")

    @patch("src.news_kr.requests.get")
    def test_parses_fixture_html(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200, text=self._fixture_html(),
        )
        items = news_kr._scrape_naver_finance("005930")
        assert items is not None
        assert len(items) == 5
        first = items[0]
        assert first["title"] == "삼성전자, 2분기 호실적 발표"
        assert first["publisher"] == "한국경제"
        assert first["published"] == "2026-05-22 14:23"

    @patch("src.news_kr.requests.get")
    def test_relative_url_absolutized(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200, text=self._fixture_html(),
        )
        items = news_kr._scrape_naver_finance("005930")
        # First item has relative href "/item/news_read.naver?..."
        assert items[0]["link"].startswith("https://finance.naver.com/item/news_read.naver?")
        # Fourth item had absolute URL — preserved
        assert items[3]["link"].startswith("https://finance.naver.com/item/news_read.naver?")

    @patch("src.news_kr.requests.get")
    def test_summary_always_empty(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200, text=self._fixture_html(),
        )
        items = news_kr._scrape_naver_finance("005930")
        for item in items:
            assert item["summary"] == ""
            assert item["summary_en"] == ""

    @patch("src.news_kr.requests.get")
    def test_http_failure_returns_none(self, mock_get):
        mock_get.side_effect = Exception("network down")
        result = news_kr._scrape_naver_finance("005930")
        assert result is None

    @patch("src.news_kr.requests.get")
    def test_no_table_returns_none(self, mock_get):
        # HTML 구조 변경 — table.type5 없음
        mock_get.return_value = MagicMock(
            status_code=200, text="<html><body><p>Not found</p></body></html>",
        )
        result = news_kr._scrape_naver_finance("005930")
        assert result is None

    @patch("src.news_kr.requests.get")
    def test_empty_tbody_returns_empty_list(self, mock_get):
        # table 있고 tr 없음 — 성공 (뉴스 0건)
        mock_get.return_value = MagicMock(
            status_code=200,
            text='<html><body><table class="type5"><tbody></tbody></table></body></html>',
        )
        result = news_kr._scrape_naver_finance("005930")
        assert result == []

    @patch("src.news_kr.requests.get")
    def test_non_200_status_returns_none(self, mock_get):
        # 404 / 429 등 비정상 HTTP status → None (실패)
        mock_get.return_value = MagicMock(status_code=404, text="Not Found")
        result = news_kr._scrape_naver_finance("005930")
        assert result is None
