"""src/dart_client.py 단위 테스트."""
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from src import dart_client


_FIXTURE_DIR = Path(__file__).parent / "fixtures"


class TestCorpCodeXmlParse:
    def test_parse_extracts_records(self):
        xml_text = (_FIXTURE_DIR / "CORPCODE_sample.xml").read_text(encoding="utf-8")
        rows = dart_client._parse_corp_code_xml(xml_text)
        assert len(rows) == 3
        samsung = next(r for r in rows if r["corp_code"] == "00126380")
        assert samsung["corp_name"] == "삼성전자"
        assert samsung["stock_code"] == "005930"
        # 비상장 (stock_code 빈 문자열) 도 포함
        unlisted = next(r for r in rows if r["corp_code"] == "99999991")
        assert unlisted["stock_code"] == ""


class TestGetCorpCode:
    @patch("src.dart_client.dart_cache.get_corp_code_by_stock")
    def test_finds_listed_stock(self, mock_get):
        mock_get.return_value = "00126380"
        result = dart_client.get_corp_code("005930.KS")
        assert result == "00126380"
        mock_get.assert_called_once_with("005930")

    @patch("src.dart_client.dart_cache.get_corp_code_by_stock")
    def test_returns_none_for_unknown(self, mock_get):
        mock_get.return_value = None
        assert dart_client.get_corp_code("999999.KS") is None


class TestFetchDisclosures:
    @pytest.fixture(autouse=True)
    def _no_sleep(self, monkeypatch):
        """모든 TestFetchDisclosures 테스트에서 real time.sleep 우회 (속도)."""
        monkeypatch.setattr(dart_client.time, "sleep", lambda s: None)
        yield

    @patch("src.dart_client._api_key", return_value="TEST_KEY")
    @patch("src.dart_client.requests.get")
    def test_aggregates_all_endpoints(self, mock_get, _mock_key):
        # 9 endpoint 모두 성공 응답
        def fake_get(url, **kwargs):
            return MagicMock(status_code=200, json=lambda: {"status": "000", "list": [
                {"rcept_no": f"{url[-15:]}_1", "rcept_dt": "20260520"},
            ]})
        mock_get.side_effect = fake_get
        result = dart_client.fetch_disclosures("00126380", days=30)
        # 9 endpoint 모두 카테고리 키 존재 (list 포함)
        expected_keys = ("list", "capital_increase", "capital_decrease",
                         "treasury_acquire", "treasury_dispose", "merger",
                         "major_holders", "exec_holders", "free_increase")
        for key in expected_keys:
            assert key in result
            assert len(result[key]) == 1
        assert len(result) == 9

    @patch("src.dart_client._api_key", return_value="TEST_KEY")
    @patch("src.dart_client.requests.get")
    def test_partial_failure_returns_partial(self, mock_get, _mock_key):
        # 1번째 호출은 실패, 나머지는 성공
        calls = [0]
        def fake_get(url, **kwargs):
            calls[0] += 1
            if calls[0] == 1:
                raise Exception("transient network error")
            return MagicMock(status_code=200, json=lambda: {"status": "000", "list": []})
        mock_get.side_effect = fake_get
        result = dart_client.fetch_disclosures("00126380", days=30)
        # 결과 dict 가 반환됨 (전체 None 이 아님)
        assert isinstance(result, dict)
        assert "list" in result

    @patch("src.dart_client._api_key", return_value="TEST_KEY")
    @patch("src.dart_client.requests.get")
    def test_rate_limit_sleep(self, mock_get, _mock_key, monkeypatch):
        mock_get.return_value = MagicMock(
            status_code=200, json=lambda: {"status": "000", "list": []},
        )
        sleep_calls = []
        monkeypatch.setattr(dart_client.time, "sleep", lambda s: sleep_calls.append(s))
        dart_client.fetch_disclosures("00126380", days=30)
        # 9 endpoint 사이에 sleep 8회 (마지막 호출 후엔 안 함)
        assert len(sleep_calls) == 8
        assert all(s == dart_client._RATE_LIMIT_SLEEP for s in sleep_calls)

    @patch("src.dart_client._api_key", return_value="TEST_KEY")
    @patch("src.dart_client.requests.get")
    def test_dart_status_013_treated_as_no_data(self, mock_get, _mock_key):
        # DART 의 "013" = 조회 데이터 없음 → 정상 (warn 없음, 빈 list)
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"status": "013", "message": "조회된 데이타가 없습니다", "list": []},
        )
        result = dart_client.fetch_disclosures("00126380", days=30)
        for key in ("capital_increase", "treasury_acquire"):
            assert result[key] == []


class TestRefreshCorpCodes:
    @patch("src.dart_client.download_corp_codes")
    @patch("src.dart_client.dart_cache.corp_codes_last_modify_date")
    def test_skips_if_recent(self, mock_last, mock_download):
        # 어제 갱신됨 → skip
        from datetime import datetime, timedelta
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
        mock_last.return_value = yesterday
        result = dart_client.refresh_corp_codes_if_stale(days=7)
        assert result == 0
        mock_download.assert_not_called()
