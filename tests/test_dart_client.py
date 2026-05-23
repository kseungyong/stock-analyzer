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
