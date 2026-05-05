"""src/analysis_cache.py 단위 테스트."""
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src import analysis_cache as ac


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """임시 DB 경로 사용. 모듈 전역 상태 격리."""
    db_path = tmp_path / "predictions.db"
    monkeypatch.setattr(ac, "_DB_PATH", db_path)
    yield db_path


class TestInitDb:
    def test_creates_db_file(self, tmp_db):
        assert not tmp_db.exists()
        ac.init_db()
        assert tmp_db.exists()

    def test_creates_parent_dir(self, tmp_path, monkeypatch):
        db = tmp_path / "deep" / "nested" / "predictions.db"
        monkeypatch.setattr(ac, "_DB_PATH", db)
        ac.init_db()
        assert db.exists()

    def test_creates_analysis_cache_table(self, tmp_db):
        ac.init_db()
        with sqlite3.connect(tmp_db) as conn:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='analysis_cache'"
            )
            assert cur.fetchone() is not None

    def test_creates_market_index(self, tmp_db):
        ac.init_db()
        with sqlite3.connect(tmp_db) as conn:
            names = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()]
        assert "idx_analysis_cache_market" in names

    def test_idempotent(self, tmp_db):
        ac.init_db()
        ac.init_db()
        assert tmp_db.exists()


class TestPutGet:
    def test_put_then_get_roundtrip(self, tmp_db):
        ac.init_db()
        ac.put(cache_key="AAPL", market="us",
               result_html="<p>hi</p>", source="manual")
        row = ac.get("AAPL")
        assert row is not None
        assert row["cache_key"] == "AAPL"
        assert row["market"] == "us"
        assert row["result_html"] == "<p>hi</p>"
        assert row["source"] == "manual"
        assert isinstance(row["generated_at"], int)

    def test_get_missing_returns_none(self, tmp_db):
        ac.init_db()
        assert ac.get("NOSUCH") is None

    def test_put_upsert_overwrites(self, tmp_db):
        ac.init_db()
        ac.put("AAPL", "us", "<p>v1</p>", "auto_cron")
        time.sleep(0.01)
        ac.put("AAPL", "us", "<p>v2</p>", "manual")
        row = ac.get("AAPL")
        assert row["result_html"] == "<p>v2</p>"
        assert row["source"] == "manual"
        # row 가 1개만 존재
        with sqlite3.connect(tmp_db) as conn:
            (n,) = conn.execute(
                "SELECT COUNT(*) FROM analysis_cache WHERE cache_key='AAPL'"
            ).fetchone()
            assert n == 1

    def test_put_all_key(self, tmp_db):
        ac.init_db()
        ac.put("ALL", "all", "<p>full</p>", "manual")
        row = ac.get("ALL")
        assert row["market"] == "all"


class TestListSymbols:
    def test_returns_only_symbol_rows(self, tmp_db):
        ac.init_db()
        ac.put("AAPL", "us", "<p>a</p>", "auto_cron")
        ac.put("005930.KS", "korea", "<p>k</p>", "auto_cron")
        ac.put("ALL", "all", "<p>full</p>", "manual")
        rows = ac.list_symbols()
        keys = [r["cache_key"] for r in rows]
        assert "AAPL" in keys
        assert "005930.KS" in keys
        assert "ALL" not in keys

    def test_empty_db_returns_empty_list(self, tmp_db):
        ac.init_db()
        assert ac.list_symbols() == []

    def test_sorted_by_market_then_key(self, tmp_db):
        ac.init_db()
        ac.put("NVDA", "us", "<p>n</p>", "auto_cron")
        ac.put("AAPL", "us", "<p>a</p>", "auto_cron")
        ac.put("005930.KS", "korea", "<p>k</p>", "auto_cron")
        rows = ac.list_symbols()
        assert [(r["market"], r["cache_key"]) for r in rows] == [
            ("korea", "005930.KS"),
            ("us", "AAPL"),
            ("us", "NVDA"),
        ]


def _kst_unix(year, month, day, hour, minute) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("Asia/Seoul")).timestamp())


class TestNextMarketOpenKoreaKst:
    def test_after_close_same_day(self, tmp_db):
        # 2026-05-05 16:00 KST 분석 → 다음 09:00 KST 만료
        gen = _kst_unix(2026, 5, 5, 16, 0)
        nxt = ac._next_market_open_kst("korea", gen)
        assert nxt == _kst_unix(2026, 5, 6, 9, 0)

    def test_before_open_same_day(self, tmp_db):
        # 2026-05-06 03:00 KST 분석 (가상) → 같은 날 09:00 만료
        gen = _kst_unix(2026, 5, 6, 3, 0)
        nxt = ac._next_market_open_kst("korea", gen)
        assert nxt == _kst_unix(2026, 5, 6, 9, 0)

    def test_at_open_exact(self, tmp_db):
        # 09:00 정각이면 그 다음 영업일 09:00 (이미 만료)
        gen = _kst_unix(2026, 5, 6, 9, 0)
        nxt = ac._next_market_open_kst("korea", gen)
        assert nxt == _kst_unix(2026, 5, 7, 9, 0)


class TestNextMarketOpenUsKst:
    def test_us_standard_time_winter(self, tmp_db):
        # 2026-01-15 06:00 KST 분석 (겨울, 표준시 — UTC-5)
        # 미국 09:30 ET = KST 23:30 (당일)
        gen = _kst_unix(2026, 1, 15, 6, 0)
        nxt = ac._next_market_open_kst("us", gen)
        assert nxt == _kst_unix(2026, 1, 15, 23, 30)

    def test_us_daylight_time_summer(self, tmp_db):
        # 2026-07-15 06:00 KST 분석 (여름, 서머타임 — UTC-4)
        # 미국 09:30 ET = KST 22:30 (당일)
        gen = _kst_unix(2026, 7, 15, 6, 0)
        nxt = ac._next_market_open_kst("us", gen)
        assert nxt == _kst_unix(2026, 7, 15, 22, 30)

    def test_us_already_past_open_winter(self, tmp_db):
        # 2026-01-15 23:35 KST (겨울 시장 이미 시작) → 다음날 23:30
        gen = _kst_unix(2026, 1, 15, 23, 35)
        nxt = ac._next_market_open_kst("us", gen)
        assert nxt == _kst_unix(2026, 1, 16, 23, 30)

    def test_us_already_past_open_summer(self, tmp_db):
        # 2026-07-15 22:35 KST (여름 시장 이미 시작) → 다음날 22:30
        gen = _kst_unix(2026, 7, 15, 22, 35)
        nxt = ac._next_market_open_kst("us", gen)
        assert nxt == _kst_unix(2026, 7, 16, 22, 30)


class TestIsFreshKorea:
    def test_fresh_before_next_open(self, tmp_db):
        gen = _kst_unix(2026, 5, 5, 16, 0)
        now = _kst_unix(2026, 5, 6, 8, 59)
        row = {"market": "korea", "generated_at": gen}
        assert ac.is_fresh(row, now) is True

    def test_stale_after_next_open(self, tmp_db):
        gen = _kst_unix(2026, 5, 5, 16, 0)
        now = _kst_unix(2026, 5, 6, 9, 1)
        row = {"market": "korea", "generated_at": gen}
        assert ac.is_fresh(row, now) is False


class TestIsFreshUs:
    def test_fresh_winter(self, tmp_db):
        # 2026-01-15 06:00 KST 분석 → 23:30 까지 fresh
        gen = _kst_unix(2026, 1, 15, 6, 0)
        now = _kst_unix(2026, 1, 15, 22, 0)
        assert ac.is_fresh({"market": "us", "generated_at": gen}, now) is True

    def test_stale_winter(self, tmp_db):
        gen = _kst_unix(2026, 1, 15, 6, 0)
        now = _kst_unix(2026, 1, 15, 23, 31)
        assert ac.is_fresh({"market": "us", "generated_at": gen}, now) is False

    def test_fresh_summer(self, tmp_db):
        # 2026-07-15 06:00 KST → 22:30 까지 fresh
        gen = _kst_unix(2026, 7, 15, 6, 0)
        now = _kst_unix(2026, 7, 15, 22, 0)
        assert ac.is_fresh({"market": "us", "generated_at": gen}, now) is True

    def test_stale_summer(self, tmp_db):
        gen = _kst_unix(2026, 7, 15, 6, 0)
        now = _kst_unix(2026, 7, 15, 22, 31)
        assert ac.is_fresh({"market": "us", "generated_at": gen}, now) is False


class TestIsFreshAll:
    def test_all_fresh_when_every_symbol_fresh(self, tmp_db):
        ac.init_db()
        gen = _kst_unix(2026, 5, 5, 16, 0)
        now = _kst_unix(2026, 5, 5, 21, 0)  # 한국 16:00 분석 직후, 미국 22:30 마감 전
        # 한국·미국 종목 모두 직전 자동분석 시점에 분석됐다고 가정
        ac.put("AAPL", "us", "<p/>", "auto_cron")
        ac.put("005930.KS", "korea", "<p/>", "auto_cron")
        # generated_at 을 명시 시각으로 덮어쓰기 위해 직접 UPDATE
        with sqlite3.connect(tmp_db) as conn:
            conn.execute("UPDATE analysis_cache SET generated_at = ?", (gen,))
        all_row = {"market": "all", "generated_at": gen}
        # ALL 의 신선도는 종목별 row 가 모두 fresh 인지로 판단
        assert ac.is_fresh(all_row, now) is True

    def test_all_stale_when_any_symbol_stale(self, tmp_db):
        ac.init_db()
        fresh_gen = _kst_unix(2026, 5, 5, 16, 0)
        stale_gen = _kst_unix(2026, 5, 4, 16, 0)
        ac.put("AAPL", "us", "<p/>", "auto_cron")
        ac.put("005930.KS", "korea", "<p/>", "auto_cron")
        with sqlite3.connect(tmp_db) as conn:
            conn.execute("UPDATE analysis_cache SET generated_at = ? WHERE cache_key = 'AAPL'", (fresh_gen,))
            conn.execute("UPDATE analysis_cache SET generated_at = ? WHERE cache_key = '005930.KS'", (stale_gen,))
        now = _kst_unix(2026, 5, 6, 8, 30)
        all_row = {"market": "all", "generated_at": fresh_gen}
        assert ac.is_fresh(all_row, now) is False

    def test_all_with_no_symbol_rows_is_stale(self, tmp_db):
        ac.init_db()
        gen = _kst_unix(2026, 5, 5, 16, 0)
        now = _kst_unix(2026, 5, 5, 17, 0)
        all_row = {"market": "all", "generated_at": gen}
        assert ac.is_fresh(all_row, now) is False
