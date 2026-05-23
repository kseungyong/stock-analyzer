"""src/dart_cache.py 단위 테스트."""
import time
import pytest
from src import dart_cache


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    db = tmp_path / "test_predictions.db"
    monkeypatch.setattr(dart_cache, "_DB_PATH", db)
    dart_cache.init_db()
    yield


class TestCorpCodes:
    def test_upsert_dedup(self):
        rows = [
            {"corp_code": "00126380", "corp_name": "삼성전자",
             "stock_code": "005930", "modify_date": "20240501"},
        ]
        dart_cache.upsert_corp_codes(rows)
        # 같은 corp_code 두 번 → 1 row
        rows2 = [
            {"corp_code": "00126380", "corp_name": "삼성전자(주)",
             "stock_code": "005930", "modify_date": "20240601"},
        ]
        dart_cache.upsert_corp_codes(rows2)
        result = dart_cache.get_corp_code_by_stock("005930")
        assert result == "00126380"
        # corp_name 도 최신 값 (last writer wins)
