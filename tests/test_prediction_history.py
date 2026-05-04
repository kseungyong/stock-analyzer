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

    def test_creates_required_indexes(self, tmp_db):
        ph.init_db()
        with sqlite3.connect(tmp_db) as conn:
            names = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()]
        assert 'idx_pred_live_unique' in names
        assert 'idx_pred_symbol_model' in names
        assert 'idx_pred_unevaluated' in names
        assert 'idx_pred_backtest_id' in names

    def test_live_unique_constraint_works(self, tmp_db):
        """idx_pred_live_unique partial index가 NULL backtest_id 중복을 차단해야 함."""
        ph.init_db()
        with sqlite3.connect(tmp_db) as conn:
            conn.execute(
                """INSERT INTO predictions
                   (symbol, ts, target_date, model, direction, confidence,
                    base_close, source, backtest_id)
                   VALUES ('AAPL', 1000, 2000, 'rf', '상승', 65.0, 100.0, 'live', NULL)"""
            )
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    """INSERT INTO predictions
                       (symbol, ts, target_date, model, direction, confidence,
                        base_close, source, backtest_id)
                       VALUES ('AAPL', 1001, 2000, 'rf', '하락', 70.0, 100.0, 'live', NULL)"""
                )
                conn.commit()


class TestInsertLive:
    def _sample_predictions(self):
        return {
            "prophet": {"predicted_price": 51000.0, "change_pct": 1.5, "range": [50000, 52000]},
            "random_forest": {"direction": "상승", "confidence": 65.0, "accuracy": 60.0},
            "lightgbm": {"direction": "상승", "confidence": 70.0, "accuracy": 62.0},
            "lstm": {"direction": "하락", "confidence": 55.0, "accuracy": 58.0},
            "transformer": {"direction": "상승", "confidence": 60.0, "accuracy": 59.0},
            "ensemble": {"direction": "상승", "confidence": 67.0, "vote_ratio": 0.67, "model_count": 4},
        }

    def test_inserts_five_models(self, tmp_db):
        ph.init_db()
        ph.insert_live(
            symbol="AAPL",
            predictions=self._sample_predictions(),
            base_close=50000.0,
            target_date=1714521600,
        )
        with sqlite3.connect(tmp_db) as conn:
            rows = conn.execute(
                "SELECT model FROM predictions WHERE symbol='AAPL' ORDER BY model"
            ).fetchall()
        assert sorted([r[0] for r in rows]) == ['ensemble', 'lgbm', 'lstm', 'rf', 'transformer']

    def test_skips_prophet(self, tmp_db):
        ph.init_db()
        ph.insert_live("AAPL", self._sample_predictions(), 50000.0, 1714521600)
        with sqlite3.connect(tmp_db) as conn:
            rows = conn.execute(
                "SELECT model FROM predictions WHERE model='prophet'"
            ).fetchall()
        assert rows == []

    def test_skips_models_with_error(self, tmp_db):
        ph.init_db()
        preds = self._sample_predictions()
        preds["lstm"] = {"error": "model failed"}
        ph.insert_live("AAPL", preds, 50000.0, 1714521600)
        with sqlite3.connect(tmp_db) as conn:
            rows = conn.execute(
                "SELECT model FROM predictions WHERE symbol='AAPL'"
            ).fetchall()
        models = [r[0] for r in rows]
        assert 'lstm' not in models
        assert 'rf' in models  # 다른 모델은 정상 저장

    def test_skips_models_with_data_insufficient(self, tmp_db):
        ph.init_db()
        preds = self._sample_predictions()
        preds["random_forest"] = {"direction": "데이터 부족", "confidence": 0.0}
        ph.insert_live("AAPL", preds, 50000.0, 1714521600)
        with sqlite3.connect(tmp_db) as conn:
            rows = conn.execute(
                "SELECT model FROM predictions WHERE symbol='AAPL' AND model='rf'"
            ).fetchall()
        assert rows == []  # "데이터 부족"은 저장 안 함

    def test_unique_collision_ignored(self, tmp_db):
        ph.init_db()
        preds = self._sample_predictions()
        ph.insert_live("AAPL", preds, 50000.0, 1714521600)
        # 같은 symbol/target_date/model로 두 번째 호출 — 첫 예측 보존
        preds["random_forest"]["direction"] = "하락"
        ph.insert_live("AAPL", preds, 50000.0, 1714521600)
        with sqlite3.connect(tmp_db) as conn:
            rows = conn.execute(
                "SELECT direction FROM predictions WHERE symbol='AAPL' AND model='rf'"
            ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "상승"  # 첫 예측 보존
