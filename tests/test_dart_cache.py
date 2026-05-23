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


class TestDisclosures:
    def test_insert_dedup_by_rcept_no(self):
        rows = [
            {"rcept_no": "X1", "rcept_dt": "20260520", "raw_json": '{"a":1}'},
        ]
        dart_cache.insert_disclosures("005930", "00126380", "treasury_acquire", rows)
        # 동일 rcept_no 다시 → 1 row 유지 (INSERT OR IGNORE)
        dart_cache.insert_disclosures("005930", "00126380", "treasury_acquire", rows)
        count = dart_cache.count_disclosures("005930")
        assert count == 1

    def test_purge_old_disclosures(self):
        import time as _t
        now = int(_t.time())
        # 30일 전 row (old) + 1일 전 row (new)
        old_rows = [{"rcept_no": "OLD", "rcept_dt": "20240101", "raw_json": "{}"}]
        new_rows = [{"rcept_no": "NEW", "rcept_dt": "20260522", "raw_json": "{}"}]
        dart_cache.insert_disclosures("005930", "X", "treasury_acquire", old_rows,
                                       fetched_at=now - 30 * 86400)
        dart_cache.insert_disclosures("005930", "X", "treasury_acquire", new_rows,
                                       fetched_at=now - 86400)
        deleted = dart_cache.purge_old(days=14)
        assert deleted == 1
        assert dart_cache.count_disclosures("005930") == 1


class TestDartSummaries:
    def test_upsert_atomic(self):
        # INSERT
        dart_cache.upsert_summary(
            symbol="005930.KS", summary_json='{"summary":"x"}',
            sentiment="긍정", critical_count=1, model="rule_based", source="rule",
        )
        # 동일 symbol UPDATE
        dart_cache.upsert_summary(
            symbol="005930.KS", summary_json='{"summary":"y"}',
            sentiment="부정", critical_count=2, model="gemini-2.5-flash", source="llm",
        )
        result = dart_cache.get_summary("005930.KS")
        assert result["sentiment"] == "부정"
        assert result["critical_count"] == 2
        assert result["source"] == "llm"

    def test_list_summaries_returns_dict_keyed_by_symbol(self):
        dart_cache.upsert_summary(
            symbol="005930.KS", summary_json='{"a":1}',
            sentiment="긍정", critical_count=1, model="rule_based", source="rule",
        )
        dart_cache.upsert_summary(
            symbol="AAPL", summary_json='{"a":2}',
            sentiment="중립", critical_count=0, model=None, source="empty",
        )
        result = dart_cache.list_summaries()
        assert "005930.KS" in result
        assert "AAPL" in result
        assert result["005930.KS"]["sentiment"] == "긍정"
