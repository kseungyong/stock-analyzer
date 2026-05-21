"""src/composite_history.py 단위 테스트."""
import time
import pytest
from src import composite_history as ch


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """매 테스트마다 임시 DB 파일 사용."""
    db = tmp_path / "test_predictions.db"
    monkeypatch.setattr(ch, "_DB_PATH", db)
    ch.init_db()
    yield


class TestCompositeHistory:
    def test_init_db_idempotent(self):
        ch.init_db()
        ch.init_db()  # 2회 호출도 OK

    def test_insert_and_recent(self):
        ch.insert("AAPL", -3.5)
        rows = ch.recent("AAPL", days=7)
        assert len(rows) == 1
        recorded_at, composite = rows[0]
        assert composite == pytest.approx(-3.5)
        assert recorded_at > int(time.time()) - 10

    def test_recent_returns_newest_first(self):
        now = int(time.time())
        ch.insert("AAPL", -1.0, recorded_at=now - 86400 * 3)
        ch.insert("AAPL", -2.0, recorded_at=now - 86400)
        ch.insert("AAPL", -3.0, recorded_at=now)
        rows = ch.recent("AAPL", days=7)
        assert [r[1] for r in rows] == [-3.0, -2.0, -1.0]

    def test_recent_respects_days_window(self):
        now = int(time.time())
        ch.insert("AAPL", -1.0, recorded_at=now - 86400 * 10)  # 10일 전 (창 밖)
        ch.insert("AAPL", -2.0, recorded_at=now - 86400 * 3)   # 3일 전 (창 안)
        rows = ch.recent("AAPL", days=7)
        assert len(rows) == 1
        assert rows[0][1] == pytest.approx(-2.0)

    def test_recent_empty_symbol(self):
        assert ch.recent("UNKNOWN", days=7) == []

    def test_insert_explicit_timestamp(self):
        ch.insert("AAPL", 1.5, recorded_at=1700000000)
        rows = ch.recent("AAPL", days=365 * 10)
        assert rows[0] == (1700000000, 1.5)

    def test_insert_same_timestamp_replaces(self):
        """PRIMARY KEY (symbol, recorded_at) — 동일 키는 덮어쓴다."""
        ch.insert("AAPL", -5.0, recorded_at=1700000000)
        ch.insert("AAPL", -6.0, recorded_at=1700000000)
        rows = ch.recent("AAPL", days=365 * 10)
        assert len(rows) == 1
        assert rows[0][1] == pytest.approx(-6.0)

    def test_purge_old_removes_only_old(self):
        now = int(time.time())
        ch.insert("AAPL", -1.0, recorded_at=now - 86400 * 100)  # 100일 전 — 삭제
        ch.insert("AAPL", -2.0, recorded_at=now - 86400 * 30)   # 30일 전 — 유지
        deleted = ch.purge_old(days=90)
        assert deleted == 1
        rows = ch.recent("AAPL", days=365)
        assert len(rows) == 1
        assert rows[0][1] == pytest.approx(-2.0)

    def test_purge_old_returns_zero_when_nothing_old(self):
        ch.insert("AAPL", -1.0)
        assert ch.purge_old(days=90) == 0
