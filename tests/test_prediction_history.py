"""src/prediction_history.py 단위 테스트."""
import sqlite3
from pathlib import Path

import pytest

from src import prediction_history as ph


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """임시 DB 경로 사용. 모듈 전역 상태 격리."""
    db_path = tmp_path / "predictions.db"
    monkeypatch.setattr(ph, "_DB_PATH", db_path)
    yield db_path


class TestInitDb:
    def test_creates_db_file(self, tmp_db):
        assert not tmp_db.exists()
        ph.init_db()
        assert tmp_db.exists()

    def test_creates_parent_dir(self, tmp_path, monkeypatch):
        db = tmp_path / "deep" / "nested" / "predictions.db"
        monkeypatch.setattr(ph, "_DB_PATH", db)
        ph.init_db()
        assert db.exists()

    def test_creates_predictions_table(self, tmp_db):
        ph.init_db()
        with sqlite3.connect(tmp_db) as conn:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='predictions'"
            )
            assert cur.fetchone() is not None

    def test_idempotent(self, tmp_db):
        ph.init_db()
        ph.init_db()  # 두 번째 호출도 OK
        assert tmp_db.exists()

    def test_wal_enabled(self, tmp_db):
        ph.init_db()
        with sqlite3.connect(tmp_db) as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode.lower() == "wal"
