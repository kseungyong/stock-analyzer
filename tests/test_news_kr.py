"""src/news_kr.py 단위 테스트."""
import json
import time
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


@pytest.fixture
def _tmp_cache_dir(tmp_path, monkeypatch):
    """매 테스트마다 임시 캐시 디렉토리."""
    monkeypatch.setattr(news_kr, "_CACHE_DIR", tmp_path / "news_cache")
    yield tmp_path / "news_cache"


class TestCache:
    def test_cache_put_then_get_returns_items(self, _tmp_cache_dir):
        items = [{"title": "테스트", "link": "https://example.com"}]
        news_kr._cache_put("005930.KS", items)
        result = news_kr._cache_get("005930.KS")
        assert result == items

    def test_cache_miss_after_ttl_expires(self, _tmp_cache_dir, monkeypatch):
        items = [{"title": "old"}]
        news_kr._cache_put("005930.KS", items)
        # 시간을 TTL 보다 멀리 진행시킨 것처럼 fetched_at 직접 조작
        cache_path = _tmp_cache_dir / "005930.KS.json"
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        payload["fetched_at"] = int(time.time()) - 3700  # 1h + 100s 전
        cache_path.write_text(json.dumps(payload), encoding="utf-8")
        assert news_kr._cache_get("005930.KS") is None

    def test_cache_get_nonexistent_returns_none(self, _tmp_cache_dir):
        assert news_kr._cache_get("UNKNOWN.KS") is None

    def test_cache_put_is_atomic_no_partial_file(self, _tmp_cache_dir):
        """write 직후 .tmp 파일이 남지 않고 .json 만 존재."""
        news_kr._cache_put("005930.KS", [{"title": "t"}])
        files = list(_tmp_cache_dir.iterdir())
        names = sorted(f.name for f in files)
        assert names == ["005930.KS.json"]

    def test_cache_get_handles_corrupt_json(self, _tmp_cache_dir):
        """corrupt JSON 파일이 있어도 None 반환 (crash 안 함)."""
        _tmp_cache_dir.mkdir(parents=True, exist_ok=True)
        (_tmp_cache_dir / "005930.KS.json").write_text("not json", encoding="utf-8")
        assert news_kr._cache_get("005930.KS") is None

    def test_cache_empty_list_is_cached_and_returned(self, _tmp_cache_dir):
        """빈 list 도 정상 캐시 + 조회 가능 (성공+0건 시나리오)."""
        news_kr._cache_put("BORING.KS", [])
        assert news_kr._cache_get("BORING.KS") == []
