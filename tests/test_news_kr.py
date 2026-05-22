"""src/news_kr.py 단위 테스트."""
import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src import news_kr


class TestScrapeNaverFinance:
    def _fixture_json(self):
        import json
        path = Path(__file__).parent / "fixtures" / "naver_news_sample.json"
        return json.loads(path.read_text(encoding="utf-8"))

    @patch("src.news_kr.requests.get")
    def test_parses_fixture_json(self, mock_get):
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = self._fixture_json()
        mock_get.return_value = mock_resp

        items = news_kr._scrape_naver_finance("005930")
        assert items is not None
        assert len(items) == 5
        first = items[0]
        assert first["title"] == "삼성전자, 2분기 호실적 발표"  # titleFull preferred
        assert first["publisher"] == "한국경제"
        assert first["published"] == "2026-05-22 14:23"
        assert first["link"].startswith("https://n.news.naver.com/mnews/article/")

    @patch("src.news_kr.requests.get")
    def test_summary_populated_from_body(self, mock_get):
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = self._fixture_json()
        mock_get.return_value = mock_resp

        items = news_kr._scrape_naver_finance("005930")
        # Mobile API 가 body 제공 — summary 채워짐
        assert items[0]["summary"]
        assert items[0]["summary_en"] == ""  # translation still pending

    @patch("src.news_kr.requests.get")
    def test_link_fallback_when_mobileNewsUrl_absent(self, mock_get):
        # mobileNewsUrl 없으면 office_id + article_id 로 구성
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = [
            {"total": 1, "items": [{
                "officeId": "023", "articleId": "0003978030",
                "officeName": "한국경제", "datetime": "202605221423",
                "title": "test", "titleFull": "test full", "body": "",
                # no mobileNewsUrl
            }]},
        ]
        mock_get.return_value = mock_resp

        items = news_kr._scrape_naver_finance("005930")
        assert items[0]["link"] == "https://n.news.naver.com/mnews/article/023/0003978030"

    @patch("src.news_kr.requests.get")
    def test_http_failure_returns_none(self, mock_get):
        mock_get.side_effect = Exception("network down")
        assert news_kr._scrape_naver_finance("005930") is None

    @patch("src.news_kr.requests.get")
    def test_non_200_status_returns_none(self, mock_get):
        mock_get.return_value = MagicMock(status_code=404)
        assert news_kr._scrape_naver_finance("005930") is None

    @patch("src.news_kr.requests.get")
    def test_invalid_json_returns_none(self, mock_get):
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.side_effect = ValueError("invalid json")
        mock_get.return_value = mock_resp
        assert news_kr._scrape_naver_finance("005930") is None

    @patch("src.news_kr.requests.get")
    def test_non_list_response_returns_none(self, mock_get):
        # 정상 JSON 이지만 list 아님 (구조 변경)
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"error": "not implemented"}
        mock_get.return_value = mock_resp
        assert news_kr._scrape_naver_finance("005930") is None

    @patch("src.news_kr.requests.get")
    def test_empty_groups_returns_empty_list(self, mock_get):
        # JSON list 비어있음 — 성공 (뉴스 0건)
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = []
        mock_get.return_value = mock_resp
        assert news_kr._scrape_naver_finance("005930") == []


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


class TestPreprocessKR:
    def test_replaces_glossary_terms(self):
        result = news_kr._preprocess_kr("삼성전자 상한가 임박")
        assert "Upper limit" in result
        assert "상한가" not in result

    def test_passthrough_no_match(self):
        result = news_kr._preprocess_kr("삼성전자 2분기 호실적")
        # 일반 헤드라인은 그대로
        assert result == "삼성전자 2분기 호실적"


class TestTranslateWithBackoff:
    def test_returns_translated_text_on_success(self, monkeypatch):
        monkeypatch.setattr(
            news_kr, "_translate_ko_to_en",
            lambda t: t.replace("삼성전자", "Samsung Electronics"),
        )
        result = news_kr._translate_with_backoff("삼성전자 호실적")
        assert "Samsung Electronics" in result

    def test_returns_original_on_failure(self, monkeypatch):
        def boom(text):
            raise Exception("network down")
        monkeypatch.setattr(news_kr, "_translate_ko_to_en", boom)
        result = news_kr._translate_with_backoff("삼성전자 호실적")
        assert result == "삼성전자 호실적"  # fallback to original

    def test_429_triggers_backoff_sleep(self, monkeypatch):
        """429/rate-limit 검출 시 _TRANSLATE_BACKOFF 만큼 sleep + 한국어 fallback."""
        def rate_limited(text):
            raise Exception("429 too many requests")
        monkeypatch.setattr(news_kr, "_translate_ko_to_en", rate_limited)
        sleep_calls = []
        monkeypatch.setattr(news_kr.time, "sleep", lambda s: sleep_calls.append(s))

        result = news_kr._translate_with_backoff("삼성전자 호실적")

        assert result == "삼성전자 호실적"  # fallback to Korean
        assert news_kr._TRANSLATE_BACKOFF in sleep_calls


class TestFetchNewsKr:
    def test_uses_cache_when_hit(self, _tmp_cache_dir, monkeypatch):
        """캐시 hit 이면 _scrape 호출되지 않음."""
        cached = [{"title": "캐시된 뉴스", "title_en": "cached news",
                   "link": "x", "publisher": "p", "published": "d",
                   "summary": "", "summary_en": ""}]
        news_kr._cache_put("005930.KS", cached)
        scrape_mock = MagicMock()
        monkeypatch.setattr(news_kr, "_scrape_naver_finance", scrape_mock)

        result = news_kr.fetch_news_kr("005930.KS")
        assert result == cached
        scrape_mock.assert_not_called()

    def test_cache_miss_fetches_translates_caches(self, _tmp_cache_dir, monkeypatch):
        """캐시 miss → scrape → translate → cache."""
        raw = [{"title": "삼성전자 호실적", "title_en": "",
                "link": "x", "publisher": "p", "published": "d",
                "summary": "", "summary_en": ""}]
        monkeypatch.setattr(news_kr, "_scrape_naver_finance", lambda c: raw)
        monkeypatch.setattr(
            news_kr, "_translate_with_backoff",
            lambda t: "Samsung Electronics good earnings" if t else "",
        )
        monkeypatch.setattr(news_kr.time, "sleep", lambda s: None)  # speed test

        result = news_kr.fetch_news_kr("005930.KS")
        assert len(result) == 1
        assert result[0]["title"] == "삼성전자 호실적"
        assert result[0]["title_en"] == "Samsung Electronics good earnings"
        # 캐시 파일 생성됨
        assert (_tmp_cache_dir / "005930.KS.json").exists()

    def test_empty_success_is_cached(self, _tmp_cache_dir, monkeypatch):
        """scrape 성공 + 0건 → 빈 list 도 캐시."""
        monkeypatch.setattr(news_kr, "_scrape_naver_finance", lambda c: [])
        monkeypatch.setattr(news_kr.time, "sleep", lambda s: None)

        result = news_kr.fetch_news_kr("BORING.KS")
        assert result == []
        assert (_tmp_cache_dir / "BORING.KS.json").exists()

    def test_failure_not_cached(self, _tmp_cache_dir, monkeypatch):
        """scrape 실패 (None) → 빈 list 반환 but 캐시 안 함."""
        monkeypatch.setattr(news_kr, "_scrape_naver_finance", lambda c: None)
        monkeypatch.setattr(news_kr.time, "sleep", lambda s: None)

        result = news_kr.fetch_news_kr("005930.KS")
        assert result == []
        assert not (_tmp_cache_dir / "005930.KS.json").exists()

    def test_krx_code_zfilled_for_short_symbol(self, _tmp_cache_dir, monkeypatch):
        """5930.KS 같은 잘못된 입력도 005930 으로 정규화."""
        captured = {}
        def fake_scrape(code):
            captured["code"] = code
            return []
        monkeypatch.setattr(news_kr, "_scrape_naver_finance", fake_scrape)
        monkeypatch.setattr(news_kr.time, "sleep", lambda s: None)
        news_kr.fetch_news_kr("5930.KS")
        assert captured["code"] == "005930"
