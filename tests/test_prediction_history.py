"""src/prediction_history.py 단위 테스트."""
import sqlite3
import time
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


import pandas as pd


class TestBackfillInline:
    def _sample_df(self):
        """KST 거래일 인덱스의 df. backfill_inline의 df.index 매칭에 사용."""
        idx = pd.DatetimeIndex([
            "2026-04-30",  # 예측 기준일
            "2026-05-01",  # target_date — 상승 (51000 > 50000)
            "2026-05-02",  # target_date — 하락 (49000 < 50000)
        ])
        return pd.DataFrame({"Close": [50000.0, 51000.0, 49000.0]}, index=idx)

    def _target_date_unix(self, date_str: str) -> int:
        """YYYY-MM-DD → KST 자정 → UTC unix epoch."""
        ts = pd.Timestamp(date_str, tz="Asia/Seoul").normalize()
        return int(ts.tz_convert("UTC").timestamp())

    def test_evaluates_correct_up(self, tmp_db):
        ph.init_db()
        ph.insert_live(
            "AAPL",
            {"random_forest": {"direction": "상승", "confidence": 65.0}},
            base_close=50000.0,
            target_date=self._target_date_unix("2026-05-01"),
        )
        evaluated = ph.backfill_inline("AAPL", self._sample_df())
        assert evaluated == 1
        with sqlite3.connect(tmp_db) as conn:
            row = conn.execute(
                "SELECT hit, actual_close FROM predictions WHERE symbol='AAPL'"
            ).fetchone()
        assert row[0] == 1
        assert row[1] == 51000.0

    def test_evaluates_correct_down(self, tmp_db):
        ph.init_db()
        ph.insert_live(
            "AAPL",
            {"lightgbm": {"direction": "하락", "confidence": 60.0}},
            base_close=50000.0,
            target_date=self._target_date_unix("2026-05-02"),
        )
        ph.backfill_inline("AAPL", self._sample_df())
        with sqlite3.connect(tmp_db) as conn:
            row = conn.execute(
                "SELECT hit FROM predictions WHERE symbol='AAPL'"
            ).fetchone()
        assert row[0] == 1

    def test_evaluates_wrong_direction(self, tmp_db):
        ph.init_db()
        ph.insert_live(
            "AAPL",
            {"random_forest": {"direction": "하락", "confidence": 55.0}},
            base_close=50000.0,
            target_date=self._target_date_unix("2026-05-01"),
        )
        ph.backfill_inline("AAPL", self._sample_df())
        with sqlite3.connect(tmp_db) as conn:
            row = conn.execute(
                "SELECT hit FROM predictions WHERE symbol='AAPL'"
            ).fetchone()
        assert row[0] == 0

    def test_skips_unevaluatable(self, tmp_db):
        """target_date가 df 인덱스에 없으면 평가 불가 → 그대로 둠."""
        ph.init_db()
        ph.insert_live(
            "AAPL",
            {"random_forest": {"direction": "상승", "confidence": 65.0}},
            base_close=50000.0,
            target_date=self._target_date_unix("2026-12-31"),
        )
        evaluated = ph.backfill_inline("AAPL", self._sample_df())
        assert evaluated == 0
        with sqlite3.connect(tmp_db) as conn:
            row = conn.execute(
                "SELECT hit, actual_close FROM predictions WHERE symbol='AAPL'"
            ).fetchone()
        assert row[0] is None
        assert row[1] is None

    def test_already_evaluated_skipped(self, tmp_db):
        ph.init_db()
        ph.insert_live(
            "AAPL",
            {"random_forest": {"direction": "상승", "confidence": 65.0}},
            base_close=50000.0,
            target_date=self._target_date_unix("2026-05-01"),
        )
        ph.backfill_inline("AAPL", self._sample_df())
        evaluated = ph.backfill_inline("AAPL", self._sample_df())
        assert evaluated == 0

    def test_other_symbol_not_touched(self, tmp_db):
        ph.init_db()
        ph.insert_live(
            "MSFT",
            {"random_forest": {"direction": "상승", "confidence": 65.0}},
            base_close=50000.0,
            target_date=self._target_date_unix("2026-05-01"),
        )
        evaluated = ph.backfill_inline("AAPL", self._sample_df())
        assert evaluated == 0
        with sqlite3.connect(tmp_db) as conn:
            row = conn.execute(
                "SELECT hit FROM predictions WHERE symbol='MSFT'"
            ).fetchone()
        assert row[0] is None

    def test_atomic_rollback_on_error(self, tmp_db, monkeypatch):
        """executemany 도중 에러 발생 시 모든 업데이트 롤백되어야 함."""
        ph.init_db()
        # 두 개의 미평가 예측 삽입
        ph.insert_live(
            "AAPL",
            {"random_forest": {"direction": "상승", "confidence": 65.0},
             "lightgbm": {"direction": "상승", "confidence": 70.0}},
            base_close=50000.0,
            target_date=self._target_date_unix("2026-05-01"),
        )

        # sqlite3.Connection은 C 확장 타입이라 직접 패치 불가.
        # _connect()를 래핑해 executemany를 가로채는 프록시 연결을 반환.
        real_connect = ph._connect

        class _FailingConn:
            """실제 연결을 위임하되 UPDATE executemany는 예외를 던짐."""
            def __init__(self, inner):
                self._inner = inner

            def execute(self, sql, params=()):
                return self._inner.execute(sql, params)

            def executemany(self, sql, rows):
                if "UPDATE predictions" in sql:
                    raise RuntimeError("simulated mid-batch failure")
                return self._inner.executemany(sql, rows)

            def close(self):
                self._inner.close()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        def patched_connect():
            return _FailingConn(real_connect())

        monkeypatch.setattr(ph, "_connect", patched_connect)

        with pytest.raises(RuntimeError):
            ph.backfill_inline("AAPL", self._sample_df())

        # 롤백되어 actual_close는 여전히 NULL이어야 함
        with sqlite3.connect(tmp_db) as conn:
            rows = conn.execute(
                "SELECT actual_close, hit FROM predictions WHERE symbol='AAPL'"
            ).fetchall()
        for row in rows:
            assert row[0] is None  # actual_close
            assert row[1] is None  # hit


class TestBackfillAll:
    def _df_for(self, prices_by_date):
        idx = pd.DatetimeIndex(list(prices_by_date.keys()))
        return pd.DataFrame({"Close": list(prices_by_date.values())}, index=idx)

    def _target_date_unix(self, date_str):
        ts = pd.Timestamp(date_str, tz="Asia/Seoul").normalize()
        return int(ts.tz_convert("UTC").timestamp())

    def test_groups_by_symbol(self, tmp_db):
        ph.init_db()
        ph.insert_live("AAPL", {"random_forest": {"direction": "상승", "confidence": 65.0}},
                       50000.0, self._target_date_unix("2026-05-01"))
        ph.insert_live("MSFT", {"random_forest": {"direction": "하락", "confidence": 60.0}},
                       50000.0, self._target_date_unix("2026-05-01"))

        call_log = []

        def fetch_fn(symbol):
            call_log.append(symbol)
            return self._df_for({"2026-05-01": 51000.0 if symbol == "AAPL" else 49000.0})

        result = ph.backfill_all(fetch_fn=fetch_fn)
        assert result["evaluated"] == 2
        assert sorted(call_log) == ["AAPL", "MSFT"]

    def test_skips_when_no_unevaluated(self, tmp_db):
        ph.init_db()

        called = []
        def fetch_fn(symbol):
            called.append(symbol)
            return pd.DataFrame()

        result = ph.backfill_all(fetch_fn=fetch_fn)
        assert result["evaluated"] == 0
        assert called == []

    def test_partial_failure(self, tmp_db):
        ph.init_db()
        ph.insert_live("AAPL", {"random_forest": {"direction": "상승", "confidence": 65.0}},
                       50000.0, self._target_date_unix("2026-05-01"))
        ph.insert_live("BAD", {"random_forest": {"direction": "상승", "confidence": 65.0}},
                       50000.0, self._target_date_unix("2026-05-01"))

        def fetch_fn(symbol):
            if symbol == "BAD":
                raise RuntimeError("network")
            return self._df_for({"2026-05-01": 51000.0})

        result = ph.backfill_all(fetch_fn=fetch_fn)
        assert result["evaluated"] == 1
        assert "BAD" in result["failed_symbols"]

    def test_only_past_target_dates(self, tmp_db):
        """미래 target_date는 스킵."""
        ph.init_db()
        future = int(time.time()) + 3600 * 24 * 30  # 30일 후
        ph.insert_live("AAPL", {"random_forest": {"direction": "상승", "confidence": 65.0}},
                       50000.0, future)

        called = []
        def fetch_fn(symbol):
            called.append(symbol)
            return pd.DataFrame()

        result = ph.backfill_all(fetch_fn=fetch_fn)
        assert called == []
        assert result["evaluated"] == 0


class TestHitRateByModel:
    def _seed(self, symbol, model, direction, base, actual, target):
        ph.insert_live(
            symbol,
            {"random_forest" if model == "rf" else "lightgbm":
                {"direction": direction, "confidence": 65.0}},
            base_close=base,
            target_date=target,
        )
        with sqlite3.connect(ph._DB_PATH) as conn:
            hit = ph._compute_hit(direction, base, actual)
            conn.execute(
                """UPDATE predictions SET actual_close=?, hit=?, evaluated_at=?
                   WHERE symbol=? AND model=? AND target_date=?""",
                (actual, hit, int(time.time()), symbol, model, target),
            )

    def test_returns_hit_rate(self, tmp_db):
        ph.init_db()
        self._seed("AAPL", "rf", "상승", 100, 110, 1000001)
        self._seed("AAPL", "rf", "상승", 100, 105, 1000002)
        self._seed("AAPL", "rf", "하락", 100, 110, 1000003)
        result = ph.hit_rate_by_model("AAPL", source="live")
        assert result["rf"]["n"] == 3
        assert result["rf"]["hit_rate"] == pytest.approx(2 / 3, abs=1e-6)

    def test_omits_models_with_no_data(self, tmp_db):
        ph.init_db()
        self._seed("AAPL", "rf", "상승", 100, 110, 1000001)
        result = ph.hit_rate_by_model("AAPL", source="live")
        assert "rf" in result
        assert "lgbm" not in result

    def test_unevaluated_excluded(self, tmp_db):
        ph.init_db()
        ph.insert_live("AAPL", {"random_forest": {"direction": "상승", "confidence": 65.0}},
                       100, 1000001)
        result = ph.hit_rate_by_model("AAPL", source="live")
        assert result == {}

    def test_filters_by_source(self, tmp_db):
        ph.init_db()
        self._seed("AAPL", "rf", "상승", 100, 110, 1000001)
        result = ph.hit_rate_by_model("AAPL", source="backtest")
        assert "rf" not in result


class TestInsertBacktest:
    def test_inserts_backtest_rows(self, tmp_db):
        ph.init_db()
        rows = [
            {"symbol": "AAPL", "ts": 1000000, "target_date": 1000086400,
             "model": "rf", "direction": "상승", "confidence": 65.0,
             "base_close": 100.0, "actual_close": 105.0, "hit": 1,
             "evaluated_at": 1000172800},
            {"symbol": "AAPL", "ts": 1000000, "target_date": 1000086400,
             "model": "lgbm", "direction": "상승", "confidence": 70.0,
             "base_close": 100.0, "actual_close": 105.0, "hit": 1,
             "evaluated_at": 1000172800},
        ]
        ph.insert_backtest(rows, backtest_id="abc123")
        with sqlite3.connect(tmp_db) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM predictions WHERE source='backtest' AND backtest_id='abc123'"
            ).fetchone()[0]
        assert count == 2


class TestGetBacktestResults:
    def test_returns_summary(self, tmp_db):
        ph.init_db()
        rows = []
        for i, (rf_hit, lgbm_hit) in enumerate([(1, 0), (1, 1), (0, 0)]):
            rows.append({"symbol": "AAPL", "ts": 1000000 + i, "target_date": 1000086400 + i,
                         "model": "rf", "direction": "상승", "confidence": 65.0,
                         "base_close": 100.0, "actual_close": 105.0, "hit": rf_hit,
                         "evaluated_at": 1000172800 + i})
            rows.append({"symbol": "AAPL", "ts": 1000000 + i, "target_date": 1000086400 + i,
                         "model": "lgbm", "direction": "상승", "confidence": 70.0,
                         "base_close": 100.0, "actual_close": 105.0, "hit": lgbm_hit,
                         "evaluated_at": 1000172800 + i})
        ph.insert_backtest(rows, backtest_id="run42")
        result = ph.get_backtest_results("run42")
        assert result["summary"]["rf"]["hit_rate"] == pytest.approx(2 / 3, abs=1e-6)
        assert result["summary"]["lgbm"]["hit_rate"] == pytest.approx(1 / 3, abs=1e-6)
        assert len(result["rows"]) == 6


class TestListHistory:
    @pytest.fixture(autouse=True)
    def _freeze_time(self, monkeypatch):
        """테스트 데이터(1730000000 ~ 2024-10-27)가 90일 cutoff 안에 들어오도록
        time.time 을 그 직후로 고정. test_90_day_cutoff 는 자체 monkeypatch 로 덮어씀."""
        monkeypatch.setattr(time, "time", lambda: 1730086400)  # 2024-10-28

    def _insert_row(self, db_path, *, symbol, ts, target_date, model,
                    direction="상승", confidence=0.7, base_close=100.0,
                    actual_close=None, hit=None, source="live", backtest_id=None):
        """Helper: 직접 SQL insert (insert_live 의 모델 mapping 우회)."""
        import sqlite3
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """INSERT INTO predictions
                   (symbol, ts, target_date, model, direction, confidence,
                    actual_close, base_close, hit, source, backtest_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (symbol, ts, target_date, model, direction, confidence,
                 actual_close, base_close, hit, source, backtest_id),
            )

    def test_empty_db_returns_empty_list(self, tmp_db):
        ph.init_db()
        assert ph.list_history("AAPL", days=90) == []

    def test_pivots_5_models_into_one_row(self, tmp_db):
        ph.init_db()
        td = 1730000000  # 임의 unix epoch
        for model in ("rf", "lgbm", "lstm", "transformer", "ensemble"):
            self._insert_row(tmp_db, symbol="AAPL", ts=1729900000,
                             target_date=td, model=model,
                             direction="상승", confidence=0.7,
                             base_close=100.0, actual_close=105.0, hit=1)
        rows = ph.list_history("AAPL", days=90)
        assert len(rows) == 1
        assert rows[0]["target_date"] == td
        assert rows[0]["base_close"] == 100.0
        assert rows[0]["actual_close"] == 105.0
        assert rows[0]["ensemble_hit"] == 1
        assert set(rows[0]["models"].keys()) == {
            "rf", "lgbm", "lstm", "transformer", "ensemble",
        }
        for m_dict in rows[0]["models"].values():
            assert m_dict["direction"] == "상승"
            assert m_dict["confidence"] == 0.7
            assert m_dict["hit"] == 1

    def test_evaluated_and_pending_rows_mixed(self, tmp_db):
        ph.init_db()
        # 평가된 row (어제)
        self._insert_row(tmp_db, symbol="AAPL", ts=1729900000,
                         target_date=1730000000, model="ensemble",
                         actual_close=105.0, hit=1)
        # 평가 대기 row (오늘 — actual_close NULL)
        self._insert_row(tmp_db, symbol="AAPL", ts=1730000000,
                         target_date=1730086400, model="ensemble",
                         actual_close=None, hit=None)
        rows = ph.list_history("AAPL", days=90)
        assert len(rows) == 2
        # 시간순 내림차순 — 최신이 먼저
        assert rows[0]["target_date"] == 1730086400
        assert rows[0]["actual_close"] is None
        assert rows[0]["ensemble_hit"] is None
        assert rows[1]["actual_close"] == 105.0
        assert rows[1]["ensemble_hit"] == 1

    def test_90_day_cutoff(self, tmp_db, monkeypatch):
        import time
        ph.init_db()
        now = 1730000000
        monkeypatch.setattr(time, "time", lambda: now)
        old_td = now - 91 * 86400  # 91일 전 — 제외
        new_td = now - 89 * 86400  # 89일 전 — 포함
        self._insert_row(tmp_db, symbol="AAPL", ts=now, target_date=old_td,
                         model="ensemble", hit=1, actual_close=105.0)
        self._insert_row(tmp_db, symbol="AAPL", ts=now, target_date=new_td,
                         model="ensemble", hit=0, actual_close=95.0)
        rows = ph.list_history("AAPL", days=90)
        assert len(rows) == 1
        assert rows[0]["target_date"] == new_td

    def test_excludes_backtest_source(self, tmp_db):
        ph.init_db()
        self._insert_row(tmp_db, symbol="AAPL", ts=1729900000,
                         target_date=1730000000, model="ensemble",
                         source="live", hit=1, actual_close=105.0)
        self._insert_row(tmp_db, symbol="AAPL", ts=1729900000,
                         target_date=1730000000, model="ensemble",
                         source="backtest", backtest_id="bt1",
                         hit=0, actual_close=95.0)
        rows = ph.list_history("AAPL", days=90)
        assert len(rows) == 1
        # live row 의 hit=1 가 사용됨 (backtest 격리)
        assert rows[0]["ensemble_hit"] == 1

    def test_isolates_other_symbols(self, tmp_db):
        ph.init_db()
        self._insert_row(tmp_db, symbol="AAPL", ts=1729900000,
                         target_date=1730000000, model="ensemble",
                         hit=1, actual_close=105.0)
        self._insert_row(tmp_db, symbol="TSLA", ts=1729900000,
                         target_date=1730000000, model="ensemble",
                         hit=0, actual_close=200.0)
        rows = ph.list_history("AAPL", days=90)
        assert len(rows) == 1
        assert rows[0]["actual_close"] == 105.0
