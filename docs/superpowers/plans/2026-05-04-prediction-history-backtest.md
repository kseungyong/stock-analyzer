# 예측 이력 DB + 백테스트 리포트 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 모든 ML 모델 예측을 SQLite에 영속화하고, 다음 영업일 종가로 자동 평가해 누적 hit rate를 리포트에 노출하며, 종목별 RF+LGBM 6개월 walk-forward 백테스트를 제공한다.

**Architecture:** `src/prediction_history.py` (SQLite 영속화·집계, `_writer_lock`으로 직렬화) + `src/backtest.py` (walk-forward) 신규 모듈. `analyze_stock`/`report_generator`/`web_app`/`scheduler`에 통합. 백테스트는 글로벌 lock으로 동시 실행 1개 제한.

**Tech Stack:** Python 3.9 / sqlite3 (표준) / pandas / Flask / APScheduler / pytest

**Spec:** `docs/superpowers/specs/2026-05-04-prediction-history-backtest-design.md`

---

## File Structure

| 파일 | 역할 |
|------|------|
| `src/prediction_history.py` (신규) | SQLite 영속화·백필·집계. 단일 책임. |
| `src/backtest.py` (신규) | RF+LGBM walk-forward 시뮬레이션. |
| `main.py` (수정) | `analyze_stock`에 insert_live + backfill_inline 호출. extra_jobs로 cron 등록. |
| `src/report_generator.py` (수정) | ML 섹션에 hit rate 표 추가. |
| `src/web_app.py` (수정) | `/backtest/<symbol>` POST 라우트, `_backtest_lock`, 결과 렌더, 폼. |
| `src/scheduler.py` (수정) | `start_scheduler`에 `extra_jobs` 파라미터 추가. |
| `tests/test_prediction_history.py` (신규) | DB 모듈 단위 테스트. |
| `tests/test_backtest.py` (신규) | walk_forward 단위 + 통합 테스트. |
| `tests/test_web_app.py` (수정) | `/backtest` 라우트 테스트. |
| `data/predictions.db` (런타임 생성, gitignore 추가) | SQLite 파일. |

**모델 식별자 매핑** (run_prediction 출력 → DB `model` 컬럼):
- `random_forest` → `'rf'`
- `lightgbm` → `'lgbm'`
- `lstm` → `'lstm'`
- `transformer` → `'transformer'`
- `ensemble` → `'ensemble'`

---

## Task 1: prediction_history 스켈레톤 + init_db

**Files:**
- Create: `src/prediction_history.py`
- Create: `tests/test_prediction_history.py`
- Modify: `.gitignore` (data/predictions.db 추가)

- [ ] **Step 1: 실패 테스트 작성**

새 파일 `tests/test_prediction_history.py`:

```python
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
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `python3 -m pytest tests/test_prediction_history.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: 모듈 작성**

새 파일 `src/prediction_history.py`:

```python
"""예측 이력 SQLite 영속화 + 백필/집계."""
from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "predictions.db"
_writer_lock = threading.Lock()  # 동일 프로세스 내 쓰기 직렬화

_TRACKED_MODELS = ('rf', 'lgbm', 'lstm', 'transformer', 'ensemble')

_SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT NOT NULL,
    ts            INTEGER NOT NULL,
    target_date   INTEGER NOT NULL,
    model         TEXT NOT NULL,
    direction     TEXT NOT NULL,
    confidence    REAL NOT NULL,
    actual_close  REAL,
    base_close    REAL NOT NULL,
    hit           INTEGER,
    evaluated_at  INTEGER,
    source        TEXT NOT NULL DEFAULT 'live',
    backtest_id   TEXT,
    UNIQUE(symbol, target_date, model, source, backtest_id)
);

CREATE INDEX IF NOT EXISTS idx_pred_symbol_model
    ON predictions(symbol, model, source);
CREATE INDEX IF NOT EXISTS idx_pred_unevaluated
    ON predictions(symbol, target_date) WHERE actual_close IS NULL;
"""


def _connect() -> sqlite3.Connection:
    """DB 연결 + PRAGMA 설정."""
    conn = sqlite3.connect(_DB_PATH, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """첫 호출 시 DB 파일과 부모 디렉토리 생성, 스키마 적용. 멱등."""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _writer_lock:
        with _connect() as conn:
            conn.executescript(_SCHEMA)
    logger.info("predictions DB 초기화 완료: %s", _DB_PATH)
```

- [ ] **Step 4: gitignore 추가**

`.gitignore`에 추가:

```
data/
```

- [ ] **Step 5: 테스트 통과 확인**

Run: `python3 -m pytest tests/test_prediction_history.py -v`
Expected: PASS (5 tests)

- [ ] **Step 6: 커밋**

```bash
git add src/prediction_history.py tests/test_prediction_history.py .gitignore
git commit -m "feat(prediction_history): SQLite 스키마 + init_db 멱등 초기화"
```

---

## Task 2: insert_live + UNIQUE 충돌 처리

**Files:**
- Modify: `src/prediction_history.py`
- Modify: `tests/test_prediction_history.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_prediction_history.py`에 추가:

```python
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
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `python3 -m pytest tests/test_prediction_history.py::TestInsertLive -v`
Expected: FAIL — `insert_live` 미정의

- [ ] **Step 3: 구현 추가**

`src/prediction_history.py` 끝에 추가 (import time도 상단에 추가):

```python
import time

# 모델 식별자 매핑: run_prediction 출력 키 → DB model 컬럼
_MODEL_KEY_MAP = {
    "random_forest": "rf",
    "lightgbm": "lgbm",
    "lstm": "lstm",
    "transformer": "transformer",
    "ensemble": "ensemble",
}


def insert_live(
    symbol: str,
    predictions: dict,
    base_close: float,
    target_date: int,
) -> None:
    """live 예측 5개 모델을 일괄 저장. UNIQUE 충돌 시 INSERT OR IGNORE."""
    now_unix = int(time.time())
    rows = []
    for src_key, db_model in _MODEL_KEY_MAP.items():
        pred = predictions.get(src_key)
        if not pred or "error" in pred:
            continue
        direction = pred.get("direction")
        if direction not in ("상승", "하락"):
            continue  # "데이터 부족" 등은 스킵
        confidence = float(pred.get("confidence", 0.0))
        rows.append((
            symbol, now_unix, target_date, db_model,
            direction, confidence, base_close, "live", None,
        ))

    if not rows:
        return

    with _writer_lock:
        with _connect() as conn:
            conn.executemany(
                """INSERT OR IGNORE INTO predictions
                   (symbol, ts, target_date, model, direction, confidence,
                    base_close, source, backtest_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `python3 -m pytest tests/test_prediction_history.py -v`
Expected: PASS (10 tests = 5 init + 5 insert_live)

- [ ] **Step 5: 커밋**

```bash
git add src/prediction_history.py tests/test_prediction_history.py
git commit -m "feat(prediction_history): insert_live — 모델별 정규화·UNIQUE 충돌 처리"
```

---

## Task 3: backfill_inline + hit 계산

**Files:**
- Modify: `src/prediction_history.py`
- Modify: `tests/test_prediction_history.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_prediction_history.py`에 추가:

```python
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
        # "상승" 예측 + 다음날 실제 상승 → hit=1
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
        assert row[0] == 1  # hit
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
        assert row[0] == 1  # 하락 예측 + 실제 하락 → hit

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
        assert row[0] == 0  # 하락 예측 + 실제 상승 → miss

    def test_skips_unevaluatable(self, tmp_db):
        """target_date가 df 인덱스에 없으면 평가 불가 → 그대로 둠."""
        ph.init_db()
        ph.insert_live(
            "AAPL",
            {"random_forest": {"direction": "상승", "confidence": 65.0}},
            base_close=50000.0,
            target_date=self._target_date_unix("2026-12-31"),  # df에 없음
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
        ph.backfill_inline("AAPL", self._sample_df())  # 1차 평가
        evaluated = ph.backfill_inline("AAPL", self._sample_df())  # 2차 호출
        assert evaluated == 0  # 이미 평가된 건 다시 안 함

    def test_other_symbol_not_touched(self, tmp_db):
        ph.init_db()
        ph.insert_live(
            "MSFT",  # 다른 심볼
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
        assert row[0] is None  # MSFT는 그대로
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `python3 -m pytest tests/test_prediction_history.py::TestBackfillInline -v`
Expected: FAIL — `backfill_inline` 미정의

- [ ] **Step 3: 구현 추가**

`src/prediction_history.py`에 추가 (import pandas as pd 상단에):

```python
import pandas as pd


def _df_to_target_close_map(df: pd.DataFrame) -> dict[int, float]:
    """df의 (KST 자정 UTC unix epoch → close) 매핑.
    
    yfinance/FDR이 반환한 거래일 인덱스를 KST 자정 기준으로 정규화한다.
    """
    if df.empty:
        return {}
    idx = df.index
    # tz-naive이면 KST로 가정, tz-aware이면 KST로 변환
    if idx.tz is None:
        idx = idx.tz_localize("Asia/Seoul", nonexistent="shift_forward", ambiguous="raise")
    else:
        idx = idx.tz_convert("Asia/Seoul")
    idx = idx.normalize()  # 자정으로 절단
    epochs = idx.tz_convert("UTC").astype("int64") // 10**9  # nanosecond → second
    return dict(zip(epochs.tolist(), df["Close"].astype(float).tolist()))


def _compute_hit(direction: str, base_close: float, actual_close: float) -> int:
    """방향 예측 vs 실제 종가 비교. 변동 없음은 보수적으로 0(miss)."""
    if direction == "상승":
        return 1 if actual_close > base_close else 0
    if direction == "하락":
        return 1 if actual_close < base_close else 0
    return 0


def backfill_inline(symbol: str, df: pd.DataFrame) -> int:
    """인라인 백필: df에 있는 날짜의 미평가 예측 actual_close 채움.
    
    Returns: 평가된 행 수.
    """
    target_close_map = _df_to_target_close_map(df)
    if not target_close_map:
        return 0

    with _writer_lock:
        with _connect() as conn:
            cur = conn.execute(
                """SELECT id, target_date, direction, base_close
                   FROM predictions
                   WHERE symbol = ? AND actual_close IS NULL""",
                (symbol,),
            )
            updates = []
            now_unix = int(time.time())
            for row_id, target_date, direction, base_close in cur.fetchall():
                actual_close = target_close_map.get(target_date)
                if actual_close is None:
                    continue
                hit = _compute_hit(direction, base_close, actual_close)
                updates.append((actual_close, hit, now_unix, row_id))

            if updates:
                conn.executemany(
                    """UPDATE predictions
                       SET actual_close = ?, hit = ?, evaluated_at = ?
                       WHERE id = ?""",
                    updates,
                )
            return len(updates)
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `python3 -m pytest tests/test_prediction_history.py -v`
Expected: PASS (16 tests)

- [ ] **Step 5: 커밋**

```bash
git add src/prediction_history.py tests/test_prediction_history.py
git commit -m "feat(prediction_history): backfill_inline + hit 계산 (KST 정규화)"
```

---

## Task 4: backfill_all (cron용)

**Files:**
- Modify: `src/prediction_history.py`
- Modify: `tests/test_prediction_history.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_prediction_history.py`에 추가:

```python
class TestBackfillAll:
    def _df_for(self, prices_by_date):
        idx = pd.DatetimeIndex(list(prices_by_date.keys()))
        return pd.DataFrame({"Close": list(prices_by_date.values())}, index=idx)

    def _target_date_unix(self, date_str):
        ts = pd.Timestamp(date_str, tz="Asia/Seoul").normalize()
        return int(ts.tz_convert("UTC").timestamp())

    def test_groups_by_symbol(self, tmp_db):
        ph.init_db()
        # 두 심볼 각각 1개 예측
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
        # 평가 가능한 예측 없음

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
        assert result["evaluated"] == 1  # AAPL만 성공
        assert "BAD" in result["failed_symbols"]

    def test_only_past_target_dates(self, tmp_db):
        """미래 target_date는 스킵 (아직 평가할 종가 없음)."""
        ph.init_db()
        future = int(time.time()) + 3600 * 24 * 30  # 30일 후
        ph.insert_live("AAPL", {"random_forest": {"direction": "상승", "confidence": 65.0}},
                       50000.0, future)

        called = []
        def fetch_fn(symbol):
            called.append(symbol)
            return pd.DataFrame()

        result = ph.backfill_all(fetch_fn=fetch_fn)
        assert called == []  # 미래 예측 → fetch 호출 안 함
        assert result["evaluated"] == 0
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `python3 -m pytest tests/test_prediction_history.py::TestBackfillAll -v`
Expected: FAIL — `backfill_all` 미정의

- [ ] **Step 3: 구현 추가**

`src/prediction_history.py`에 추가:

```python
def backfill_all(fetch_fn) -> dict:
    """cron용 전체 백필 — 미평가 + target_date < now 인 예측을 심볼별 일괄 평가.
    
    Args:
        fetch_fn: callable(symbol) -> pd.DataFrame. 외부 API 의존성 주입.
    
    Returns: {'evaluated': N, 'failed_symbols': [...]}
    """
    now_unix = int(time.time())
    with _connect() as conn:
        cur = conn.execute(
            """SELECT DISTINCT symbol FROM predictions
               WHERE actual_close IS NULL AND target_date < ?""",
            (now_unix,),
        )
        symbols = [r[0] for r in cur.fetchall()]

    total_evaluated = 0
    failed = []
    for symbol in symbols:
        try:
            df = fetch_fn(symbol)
        except Exception as e:
            logger.warning("backfill_all fetch 실패: %s — %s", symbol, e)
            failed.append(symbol)
            continue
        try:
            count = backfill_inline(symbol, df)
            total_evaluated += count
        except Exception as e:
            logger.warning("backfill_all 평가 실패: %s — %s", symbol, e)
            failed.append(symbol)

    return {"evaluated": total_evaluated, "failed_symbols": failed}
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `python3 -m pytest tests/test_prediction_history.py -v`
Expected: PASS (20 tests)

- [ ] **Step 5: 커밋**

```bash
git add src/prediction_history.py tests/test_prediction_history.py
git commit -m "feat(prediction_history): backfill_all — 심볼별 일괄 cron 백필"
```

---

## Task 5: hit_rate_by_model + insert_backtest + get_backtest_results

**Files:**
- Modify: `src/prediction_history.py`
- Modify: `tests/test_prediction_history.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_prediction_history.py`에 추가:

```python
class TestHitRateByModel:
    def _seed(self, symbol, model, direction, base, actual, target):
        ph.insert_live(
            symbol,
            {"random_forest" if model == "rf" else "lightgbm":
                {"direction": direction, "confidence": 65.0}},
            base_close=base,
            target_date=target,
        )
        # 직접 actual_close 갱신
        with sqlite3.connect(ph._DB_PATH) as conn:
            hit = ph._compute_hit(direction, base, actual)
            conn.execute(
                """UPDATE predictions SET actual_close=?, hit=?, evaluated_at=?
                   WHERE symbol=? AND model=? AND target_date=?""",
                (actual, hit, int(time.time()), symbol, model, target),
            )

    def test_returns_hit_rate(self, tmp_db):
        ph.init_db()
        # rf: 2승 1패
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
        assert "lgbm" not in result  # n=0 → 누락

    def test_unevaluated_excluded(self, tmp_db):
        ph.init_db()
        ph.insert_live("AAPL", {"random_forest": {"direction": "상승", "confidence": 65.0}},
                       100, 1000001)  # 미평가
        result = ph.hit_rate_by_model("AAPL", source="live")
        assert result == {}  # 평가된 행 없음

    def test_filters_by_source(self, tmp_db):
        ph.init_db()
        self._seed("AAPL", "rf", "상승", 100, 110, 1000001)
        result = ph.hit_rate_by_model("AAPL", source="backtest")
        assert "rf" not in result  # backtest 소스에는 없음


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
        # rf: 2승 1패, lgbm: 1승 2패
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
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `python3 -m pytest tests/test_prediction_history.py -v`
Expected: FAIL — `hit_rate_by_model`, `insert_backtest`, `get_backtest_results` 미정의

- [ ] **Step 3: 구현 추가**

`src/prediction_history.py`에 추가:

```python
def insert_backtest(rows: list[dict], backtest_id: str) -> None:
    """백테스트 walk-forward 결과 일괄 저장 (단일 트랜잭션)."""
    if not rows:
        return
    tuples = [
        (r["symbol"], r["ts"], r["target_date"], r["model"],
         r["direction"], r["confidence"], r["actual_close"],
         r["base_close"], r["hit"], r["evaluated_at"], "backtest", backtest_id)
        for r in rows
    ]
    with _writer_lock:
        with _connect() as conn:
            conn.executemany(
                """INSERT OR IGNORE INTO predictions
                   (symbol, ts, target_date, model, direction, confidence,
                    actual_close, base_close, hit, evaluated_at, source, backtest_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                tuples,
            )


def hit_rate_by_model(
    symbol: str,
    source: str = "live",
    backtest_id: str | None = None,
) -> dict:
    """심볼의 모델별 hit rate. 평가된(hit IS NOT NULL) 행 기준."""
    sql = """SELECT model, COUNT(*) AS n, AVG(CAST(hit AS REAL)) AS rate
             FROM predictions
             WHERE symbol = ? AND source = ? AND hit IS NOT NULL"""
    params: list = [symbol, source]
    if backtest_id is not None:
        sql += " AND backtest_id = ?"
        params.append(backtest_id)
    sql += " GROUP BY model HAVING n > 0"

    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()

    return {
        model: {"hit_rate": float(rate), "n": int(n)}
        for model, n, rate in rows
    }


def get_backtest_results(backtest_id: str) -> dict:
    """백테스트 1회분 결과: 모델별 hit rate + walk-forward 행들."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT symbol, ts, target_date, model, direction, confidence,
                      actual_close, base_close, hit, evaluated_at
               FROM predictions
               WHERE source = 'backtest' AND backtest_id = ?
               ORDER BY target_date, model""",
            (backtest_id,),
        ).fetchall()

    if not rows:
        return {"backtest_id": backtest_id, "summary": {}, "rows": []}

    symbol = rows[0][0]
    summary = hit_rate_by_model(symbol, source="backtest", backtest_id=backtest_id)
    return {
        "backtest_id": backtest_id,
        "summary": summary,
        "rows": [
            {
                "symbol": r[0], "ts": r[1], "target_date": r[2], "model": r[3],
                "direction": r[4], "confidence": r[5], "actual_close": r[6],
                "base_close": r[7], "hit": r[8], "evaluated_at": r[9],
            }
            for r in rows
        ],
    }
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `python3 -m pytest tests/test_prediction_history.py -v`
Expected: PASS (28 tests)

- [ ] **Step 5: 커밋**

```bash
git add src/prediction_history.py tests/test_prediction_history.py
git commit -m "feat(prediction_history): hit_rate_by_model·insert_backtest·get_backtest_results"
```

---

## Task 6: analyze_stock 통합

**Files:**
- Modify: `main.py`
- Modify: `tests/test_main.py` (없으면 생성)

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_main.py` 끝에 추가 (파일 없으면 새로 생성, 기본 import 포함):

```python
"""main.py 통합 테스트."""
from unittest.mock import patch, MagicMock
import pandas as pd
import pytest

import main


@pytest.fixture
def mock_df():
    idx = pd.date_range("2026-01-01", periods=120, freq="B")
    return pd.DataFrame({
        "Close": range(50000, 50000 + 120),
        "Volume": [1000000] * 120,
    }, index=idx)


class TestAnalyzeStockHistoryIntegration:
    @patch("main.fetch_news", return_value=[])
    @patch("main.compute_indicators", side_effect=lambda df: df)
    @patch("main.fetch_stock_data")
    @patch("main._engine")
    @patch("main.prediction_history")
    def test_calls_insert_live_and_backfill(
        self, ph_mock, engine_mock, fetch_mock, _ind, _news, mock_df
    ):
        fetch_mock.return_value = mock_df
        engine_mock.run.return_value = {
            "random_forest": {"direction": "상승", "confidence": 65.0, "accuracy": 60.0},
            "lightgbm": {"direction": "상승", "confidence": 70.0},
            "lstm": {"direction": "하락", "confidence": 55.0},
            "transformer": {"direction": "상승", "confidence": 60.0},
            "ensemble": {"direction": "상승", "confidence": 67.0},
        }
        result = main.analyze_stock("AAPL", "Apple")
        assert result is not None
        ph_mock.backfill_inline.assert_called_once()
        ph_mock.insert_live.assert_called_once()
        # insert_live 호출 인자 검증
        args, kwargs = ph_mock.insert_live.call_args
        assert kwargs.get("symbol") == "AAPL" or args[0] == "AAPL"
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `python3 -m pytest tests/test_main.py -v`
Expected: FAIL — `main.prediction_history` 미정의 또는 호출 안 됨

- [ ] **Step 3: 구현 추가**

`main.py` 상단 import에 추가:

```python
from src import prediction_history
```

`analyze_stock` 함수 내부 수정 (현재 70-92행):

```python
def analyze_stock(symbol: str, name: str) -> dict | None:
    """단일 종목 분석을 수행한다."""
    symbol = sanitize_stock_symbol(symbol)
    if not validate_stock_symbol(symbol):
        logger.error("유효하지 않은 심볼: %s", symbol)
        return None

    try:
        df = fetch_stock_data(symbol)
        df = compute_indicators(df)

        # 인라인 백필 (즉시성 보조 — cron이 메인 메커니즘)
        try:
            prediction_history.backfill_inline(symbol, df)
        except Exception as e:
            logger.warning("backfill_inline 실패 (분석은 계속): %s", e)

        signal = generate_signal(df)

        from src.ml_predictor import analyze_sentiment
        with ThreadPoolExecutor(max_workers=2) as ex:
            fut_pred = ex.submit(_engine.run, df, symbol)
            fut_news = ex.submit(fetch_news, symbol)
            prediction = fut_pred.result()
            news = fut_news.result()

        # live 예측 저장
        try:
            last_close = float(df["Close"].iloc[-1])
            target_date = _next_business_day_unix(df.index[-1])
            prediction_history.insert_live(symbol, prediction, last_close, target_date)
        except Exception as e:
            logger.warning("insert_live 실패 (분석 결과는 정상 반환): %s", e)

        sentiment = analyze_sentiment(news)

        return {
            "name": name,
            "symbol": symbol,
            "df": df,
            "signal": signal,
            "prediction": prediction,
            "news": news,
            "sentiment": sentiment,
        }
    except Exception as e:
        logger.error("분석 실패 — %s (%s): %s", name, symbol, e)
        return None
```

`main.py`에 헬퍼 추가 (load_config 위 적절한 위치):

```python
def _next_business_day_unix(last_index) -> int:
    """df 마지막 인덱스 → 다음 영업일 KST 자정 → UTC unix epoch."""
    ts = pd.Timestamp(last_index)
    if ts.tz is None:
        ts = ts.tz_localize("Asia/Seoul", nonexistent="shift_forward", ambiguous="raise")
    else:
        ts = ts.tz_convert("Asia/Seoul")
    next_bday = (ts + pd.tseries.offsets.BDay(1)).normalize()
    return int(next_bday.tz_convert("UTC").timestamp())
```

`main.py` 상단 import에 추가:

```python
import pandas as pd
```

또한 `main.py`의 `__main__` 또는 첫 실행 진입점에서 `prediction_history.init_db()` 호출 보장. `load_config()` 직후 추가:

```python
def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


# 모듈 로드 시점에 1회 — DB 파일/스키마 보장
prediction_history.init_db()
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `python3 -m pytest tests/test_main.py -v`
Expected: PASS (1 test)

전체 회귀: `python3 -m pytest tests/test_prediction_history.py tests/test_main.py -v 2>&1 | tail -5`
Expected: 29 tests pass

- [ ] **Step 5: 커밋**

```bash
git add main.py tests/test_main.py
git commit -m "feat(main): analyze_stock에 insert_live·backfill_inline 통합"
```

---

## Task 7: report_generator hit rate 섹션

**Files:**
- Modify: `src/report_generator.py`
- Modify: `tests/test_report_generator.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_report_generator.py` 끝에 추가 (파일 있다고 가정 — 없으면 import 보강):

```python
from unittest.mock import patch
import pandas as pd
from src.report_generator import generate_report


class TestHitRateSection:
    def _analysis(self, symbol="AAPL"):
        idx = pd.date_range("2026-01-01", periods=30, freq="B")
        return {
            "name": "Apple",
            "symbol": symbol,
            "df": pd.DataFrame({"Close": range(100, 130)}, index=idx),
            "signal": {"signal": "관망", "score": 0, "reasons": []},
            "prediction": {
                "prophet": None,
                "random_forest": {"direction": "상승", "confidence": 65.0},
                "lightgbm": {"direction": "상승", "confidence": 70.0},
                "lstm": {"direction": "하락", "confidence": 55.0},
                "transformer": {"direction": "상승", "confidence": 60.0},
                "ensemble": {"direction": "상승", "confidence": 67.0},
            },
            "news": [],
            "sentiment": {"label": "뉴스 없음", "score": 0.0, "details": []},
        }

    @patch("src.report_generator.prediction_history.hit_rate_by_model")
    def test_renders_hit_rate_when_data_exists(self, mock_hr):
        mock_hr.return_value = {
            "rf": {"hit_rate": 0.62, "n": 21},
            "lgbm": {"hit_rate": 0.67, "n": 21},
        }
        html = generate_report([self._analysis()])
        assert "62.0%" in html or "62%" in html
        assert "21" in html  # n 표시

    @patch("src.report_generator.prediction_history.hit_rate_by_model")
    def test_omits_section_when_no_data(self, mock_hr):
        mock_hr.return_value = {}
        html = generate_report([self._analysis()])
        # hit rate 섹션 헤더가 표시되지 않아야 함 (구체 헤더 텍스트로 확인)
        assert "누적 적중률" not in html

    @patch("src.report_generator.prediction_history.hit_rate_by_model")
    def test_marks_low_n_as_insufficient(self, mock_hr):
        mock_hr.return_value = {"rf": {"hit_rate": 0.6, "n": 5}}  # n < 10
        html = generate_report([self._analysis()])
        assert "데이터 부족" in html
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `python3 -m pytest tests/test_report_generator.py::TestHitRateSection -v`
Expected: FAIL — 모듈 attribute 또는 출력 누락

- [ ] **Step 3: 구현 추가**

`src/report_generator.py` 상단에 추가:

```python
from src import prediction_history
```

`generate_report` 함수 안에서 ML 예측 섹션을 렌더하는 부분을 찾아, 그 끝에 다음 섹션 추가 (실제 함수 구조에 맞춰 통합):

```python
def _render_hit_rate_section(symbol: str) -> str:
    """모델별 누적 hit rate를 표 형태로 렌더링. 데이터 없으면 빈 문자열."""
    rates = prediction_history.hit_rate_by_model(symbol, source="live")
    if not rates:
        return ""

    rows = []
    model_label = {
        "rf": "RandomForest", "lgbm": "LightGBM",
        "lstm": "LSTM", "transformer": "Transformer", "ensemble": "Ensemble",
    }
    for model_key in ("rf", "lgbm", "lstm", "transformer", "ensemble"):
        info = rates.get(model_key)
        if not info:
            continue
        rate_pct = info["hit_rate"] * 100
        n = info["n"]
        if n < 10:
            display = f'<span style="color:#999;">데이터 부족 (n={n})</span>'
        else:
            display = f"{rate_pct:.1f}% (n={n})"
        rows.append(f"<tr><td>{model_label[model_key]}</td><td>{display}</td></tr>")

    if not rows:
        return ""

    return f"""
    <h4 style="margin-top:16px;">📊 누적 적중률 (live tracking)</h4>
    <table style="width:auto; font-size:0.9em;">
      <thead><tr><th>모델</th><th>Hit Rate</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
    """
```

그리고 ML 예측 섹션을 그리는 곳에 호출 추가:

```python
# 기존 ML 섹션 렌더 후
ml_html += _render_hit_rate_section(analysis["symbol"])
```

(정확한 통합 지점은 `report_generator.py`의 ML 섹션 함수 구조를 확인 후 찾아 적용한다.)

- [ ] **Step 4: 테스트 통과 확인**

Run: `python3 -m pytest tests/test_report_generator.py::TestHitRateSection -v`
Expected: PASS (3 tests)

전체 회귀: `python3 -m pytest tests/test_report_generator.py -v 2>&1 | tail -5`
Expected: 모두 PASS

- [ ] **Step 5: 커밋**

```bash
git add src/report_generator.py tests/test_report_generator.py
git commit -m "feat(report): ML 섹션에 모델별 누적 hit rate 표 추가"
```

---

## Task 8: backtest 모듈 (walk_forward)

**Files:**
- Create: `src/backtest.py`
- Create: `tests/test_backtest.py`

- [ ] **Step 1: 실패 테스트 작성**

새 파일 `tests/test_backtest.py`:

```python
"""src/backtest.py 단위 테스트."""
import numpy as np
import pandas as pd
import pytest

from src import backtest as bt


def _make_df(n=200, seed=0):
    rng = np.random.default_rng(seed)
    close = 50000 + np.cumsum(rng.normal(0, 500, n))
    volume = rng.integers(1_000_000, 5_000_000, n).astype(float)
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    df = pd.DataFrame({"Close": close, "Volume": volume}, index=idx)
    df["MA5"] = df["Close"].rolling(5).mean()
    df["MA20"] = df["Close"].rolling(20).mean()
    df["RSI"] = 50.0
    df["MACD"] = 0.0
    df["MACD_Hist"] = 0.0
    df["BB_Upper"] = df["Close"] * 1.02
    df["BB_Lower"] = df["Close"] * 0.98
    df["Volume_Ratio"] = 1.0
    df["Stoch_K"] = 50.0
    df["Stoch_D"] = 50.0
    df["ATR_pct"] = 1.5
    df["OBV_Change"] = 0.0
    df["Williams_R"] = -50.0
    df["CCI"] = 0.0
    df["Return_1d"] = df["Close"].pct_change(1)
    df["Return_5d"] = df["Close"].pct_change(5)
    df["Return_20d"] = df["Close"].pct_change(20)
    return df


class TestWalkForward:
    def test_insufficient_data_returns_error(self):
        df = _make_df(n=30)  # 30 + 126 보다 작음
        result = bt.walk_forward("AAPL", df, days=126)
        assert result["error"] == "데이터 부족"
        assert result["rows"] == []
        assert result["summary"] == {}

    def test_returns_summary_with_models(self):
        df = _make_df(n=200)
        result = bt.walk_forward("AAPL", df, days=30)
        assert "rf" in result["summary"]
        assert "lgbm" in result["summary"]
        assert "ensemble" in result["summary"]
        assert result["backtest_id"]
        assert len(result["rows"]) > 0

    def test_each_row_has_required_fields(self):
        df = _make_df(n=200)
        result = bt.walk_forward("AAPL", df, days=10)
        required = {"symbol", "ts", "target_date", "model", "direction",
                    "confidence", "actual_close", "base_close", "hit", "evaluated_at"}
        for row in result["rows"]:
            assert required.issubset(row.keys())

    def test_hit_calculated_correctly(self):
        df = _make_df(n=200)
        result = bt.walk_forward("AAPL", df, days=10)
        for row in result["rows"]:
            if row["direction"] == "상승":
                assert row["hit"] == (1 if row["actual_close"] > row["base_close"] else 0)
            elif row["direction"] == "하락":
                assert row["hit"] == (1 if row["actual_close"] < row["base_close"] else 0)
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `python3 -m pytest tests/test_backtest.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: 구현 작성**

새 파일 `src/backtest.py`:

```python
"""RF + LGBM walk-forward 백테스트."""
from __future__ import annotations

import logging
import time
import uuid

import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from lightgbm import LGBMClassifier

from src.ml_predictor import _CLF_FEATURES, _prepare_clf_data

logger = logging.getLogger(__name__)

_MIN_TRAIN_ROWS = 30  # _prepare_clf_data와 동일


def _ensemble_vote(rf_dir: str, rf_conf: float, lgbm_dir: str, lgbm_conf: float) -> tuple[str, float]:
    """RF + LGBM voting. 같은 방향이면 평균 confidence, 다르면 더 높은 쪽."""
    if rf_dir == lgbm_dir:
        return rf_dir, (rf_conf + lgbm_conf) / 2
    return (rf_dir, rf_conf) if rf_conf >= lgbm_conf else (lgbm_dir, lgbm_conf)


def _hit(direction: str, base: float, actual: float) -> int:
    if direction == "상승":
        return 1 if actual > base else 0
    if direction == "하락":
        return 1 if actual < base else 0
    return 0


def _index_to_unix(ts: pd.Timestamp) -> int:
    """KST 자정 → UTC unix epoch."""
    if ts.tz is None:
        ts = ts.tz_localize("Asia/Seoul", nonexistent="shift_forward", ambiguous="raise")
    return int(ts.normalize().tz_convert("UTC").timestamp())


def walk_forward(symbol: str, df: pd.DataFrame, days: int = 126) -> dict:
    """RF + LGBM + 둘의 voting ensemble을 과거 N영업일 walk-forward.
    
    Returns:
        {'backtest_id': uuid8, 'rows': [...], 'summary': {model: {hit_rate, n}}}
        데이터 부족 시: {'error': '데이터 부족', 'backtest_id': None, 'rows': [], 'summary': {}}
    """
    # 인덱스 정렬
    df = df.sort_index()
    
    if len(df) < _MIN_TRAIN_ROWS + days + 1:
        return {"backtest_id": None, "rows": [], "summary": {}, "error": "데이터 부족"}

    backtest_id = uuid.uuid4().hex[:8]
    rows: list[dict] = []
    now_unix = int(time.time())

    # walk-forward: t = 마지막 days 영업일에서 학습→예측
    n = len(df)
    start_t = n - days - 1  # t=start_t에서 예측, t+1에서 평가
    end_t = n - 1  # t+1이 인덱스 마지막을 넘지 않도록

    for t in range(start_t, end_t):
        train_df = df.iloc[: t + 1].dropna(subset=_CLF_FEATURES)
        if len(train_df) < _MIN_TRAIN_ROWS:
            continue
        prepared = _prepare_clf_data(train_df)
        if prepared is None:
            continue
        X_train, _, y_train, _, _ = prepared

        try:
            rf = RandomForestClassifier(n_estimators=100, random_state=42)
            rf.fit(X_train, y_train)
        except Exception as e:
            logger.warning("RF fit 실패 (t=%d): %s", t, e)
            continue
        try:
            lgbm = LGBMClassifier(n_estimators=100, random_state=42, verbosity=-1)
            lgbm.fit(X_train, y_train)
        except Exception as e:
            logger.warning("LGBM fit 실패 (t=%d): %s", t, e)
            continue

        # 예측: df.iloc[t]의 features로 다음날 방향 예측
        row_t = df.iloc[t]
        if row_t[_CLF_FEATURES].isna().any():
            continue
        x_t = row_t[_CLF_FEATURES].values.reshape(1, -1)

        rf_pred = rf.predict(x_t)[0]
        rf_conf = float(rf.predict_proba(x_t)[0].max() * 100)
        rf_dir = "상승" if rf_pred == 1 else "하락"

        lgbm_pred = lgbm.predict(x_t)[0]
        lgbm_conf = float(lgbm.predict_proba(x_t)[0].max() * 100)
        lgbm_dir = "상승" if lgbm_pred == 1 else "하락"

        ens_dir, ens_conf = _ensemble_vote(rf_dir, rf_conf, lgbm_dir, lgbm_conf)

        base_close = float(df.iloc[t]["Close"])
        actual_close = float(df.iloc[t + 1]["Close"])
        ts_unix = _index_to_unix(df.index[t])
        target_unix = _index_to_unix(df.index[t + 1])

        for model, direction, confidence in [
            ("rf", rf_dir, rf_conf),
            ("lgbm", lgbm_dir, lgbm_conf),
            ("ensemble", ens_dir, ens_conf),
        ]:
            rows.append({
                "symbol": symbol,
                "ts": ts_unix,
                "target_date": target_unix,
                "model": model,
                "direction": direction,
                "confidence": confidence,
                "base_close": base_close,
                "actual_close": actual_close,
                "hit": _hit(direction, base_close, actual_close),
                "evaluated_at": now_unix,
            })

    summary: dict = {}
    for model in ("rf", "lgbm", "ensemble"):
        model_rows = [r for r in rows if r["model"] == model]
        if not model_rows:
            continue
        n_total = len(model_rows)
        hits = sum(r["hit"] for r in model_rows)
        summary[model] = {"hit_rate": hits / n_total, "n": n_total}

    return {"backtest_id": backtest_id, "rows": rows, "summary": summary}
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `python3 -m pytest tests/test_backtest.py -v`
Expected: PASS (4 tests). 시간이 걸릴 수 있음 (실제 RF/LGBM 학습 수십 회).

- [ ] **Step 5: 커밋**

```bash
git add src/backtest.py tests/test_backtest.py
git commit -m "feat(backtest): walk_forward — RF+LGBM+voting 6개월 시뮬레이션"
```

---

## Task 9: web_app `/backtest` 라우트 + lock + 폼

**Files:**
- Modify: `src/web_app.py`
- Modify: `tests/test_web_app.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_web_app.py` 끝에 추가:

```python
class TestBacktest:
    def test_csrf_missing_returns_403(self, client):
        resp = client.post("/backtest/AAPL")
        assert resp.status_code == 403

    def test_invalid_symbol_returns_400(self, client):
        resp = _post(client, "/backtest/<bad>", {})
        assert resp.status_code == 400

    @patch("src.web_app._run_backtest_bg")
    def test_valid_request_redirects_to_job(self, run_mock, client):
        resp = _post(client, "/backtest/AAPL", {})
        assert resp.status_code == 303
        assert "/jobs/" in resp.headers["Location"]
        run_mock.assert_called_once()

    @patch("src.web_app._run_backtest_bg")
    def test_concurrent_request_returns_error(self, run_mock, client):
        import src.web_app as wa
        # 첫 요청: lock 획득 시뮬레이션 (수동으로 lock 점유)
        wa._backtest_lock.acquire()
        try:
            resp = _post(client, "/backtest/AAPL", {})
            assert resp.status_code == 303
            assert "error=" in resp.headers["Location"]
        finally:
            wa._backtest_lock.release()
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `python3 -m pytest tests/test_web_app.py::TestBacktest -v`
Expected: FAIL — `/backtest` 라우트 없음

- [ ] **Step 3: 구현 추가**

`src/web_app.py` 상단 import 추가:

```python
from src import prediction_history
from src import backtest as bt
```

모듈 전역 (다른 lock들 근처):

```python
_backtest_lock = threading.Lock()
```

`api_stocks_search` 라우트 다음에 새 라우트 추가:

```python
@app.route("/backtest/<path:symbol>", methods=["POST"])
def start_backtest(symbol: str):
    """백테스트 실행 트리거. 동시 1개로 제한."""
    _csrf_validate()
    symbol = sanitize_stock_symbol(symbol)
    if not validate_stock_symbol(symbol):
        abort(400)

    if not _backtest_lock.acquire(blocking=False):
        return redirect(
            url_for("index", error="다른 백테스트가 실행 중입니다. 잠시 후 다시 시도하세요."),
            code=303,
        )

    job_id = uuid.uuid4().hex[:8]
    backtest_id = uuid.uuid4().hex[:8]
    with _jobs_lock:
        _jobs[job_id] = {
            "status": "running",
            "symbol": symbol,
            "name": f"{symbol} 백테스트",
            "result_html": None,
            "error": None,
            "started_at": datetime.now().strftime("%H:%M:%S"),
        }

    threading.Thread(
        target=_run_backtest_bg,
        args=(job_id, symbol, backtest_id),
        daemon=True,
    ).start()
    return redirect(f"/jobs/{job_id}", code=303)


def _run_backtest_bg(job_id: str, symbol: str, backtest_id: str) -> None:
    """백그라운드 백테스트 실행. 성공/실패 모두 _backtest_lock 해제."""
    logger.info("백테스트 시작: job_id=%s symbol=%s backtest_id=%s",
                job_id, symbol, backtest_id)
    try:
        from src.data_fetcher import fetch_stock_data
        from src.technical_analysis import compute_indicators

        df = fetch_stock_data(symbol)
        df = compute_indicators(df)
        result = bt.walk_forward(symbol, df, days=126)

        if result.get("error"):
            _jobs_set(job_id, status="error", error=result["error"])
            return

        prediction_history.insert_backtest(result["rows"], backtest_id)
        html = _render_backtest_report(symbol, result)
        _jobs_set(job_id, status="done", result_html=html)
        logger.info("백테스트 완료: job_id=%s", job_id)
    except Exception as e:
        logger.exception("백테스트 실패: %s", e)
        _jobs_set(job_id, status="error", error=str(e))
    finally:
        _backtest_lock.release()
        _trim_jobs()


def _render_backtest_report(symbol: str, result: dict) -> str:
    """백테스트 결과 HTML 렌더."""
    summary = result["summary"]
    rows_html = []
    model_label = {"rf": "RandomForest", "lgbm": "LightGBM", "ensemble": "Ensemble (RF+LGBM)"}
    for model in ("rf", "lgbm", "ensemble"):
        info = summary.get(model)
        if not info:
            continue
        pct = info["hit_rate"] * 100
        rows_html.append(
            f"<tr><td>{model_label[model]}</td>"
            f"<td>{pct:.1f}%</td>"
            f"<td>{info['n']}</td></tr>"
        )
    return f"""
    <h2>{escape(symbol)} 백테스트 결과 (6개월 walk-forward)</h2>
    <table style="margin:16px 0;">
      <thead><tr><th>모델</th><th>Hit Rate</th><th>평가 횟수</th></tr></thead>
      <tbody>{''.join(rows_html)}</tbody>
    </table>
    <p style="color:#666; font-size:0.9em;">
      backtest_id: <code>{escape(result['backtest_id'])}</code><br>
      ⚠️ 백테스트 결과는 과거 데이터 기반이며, 미래 수익을 보장하지 않습니다.
    </p>
    """
```

또한 `/jobs/<job_id>` (분석 리포트 결과 페이지)에 백테스트 폼 추가. `job_detail` 뷰의 분석 결과 표시 부분에 다음을 추가:

```python
# job["status"] == "done"이고 분석 결과(백테스트 아닌)일 때만 폼 표시
if job["status"] == "done" and not job["name"].endswith("백테스트"):
    backtest_form = f"""
    <form method="post" action="/backtest/{escape(job['symbol'])}" style="margin:24px 0;">
      {_csrf_input()}
      <button type="submit" class="btn btn-amber">
        🔬 백테스트 실행 (RF+LGBM, 6개월 walk-forward)
      </button>
    </form>
    """
    body += backtest_form  # 또는 결과 HTML 후에 concat
```

(정확한 위치는 `job_detail` 함수 구조 확인 후 결정)

- [ ] **Step 4: 테스트 통과 확인**

Run: `python3 -m pytest tests/test_web_app.py::TestBacktest -v`
Expected: PASS (4 tests)

- [ ] **Step 5: 커밋**

```bash
git add src/web_app.py tests/test_web_app.py
git commit -m "feat(web): /backtest 라우트·동시 실행 lock·결과 렌더링"
```

---

## Task 10: scheduler 18:00 KST cron

**Files:**
- Modify: `src/scheduler.py`
- Modify: `main.py`
- Modify: `tests/test_scheduler.py` (없으면 생성)

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_scheduler.py`:

```python
"""src/scheduler.py 테스트."""
from unittest.mock import MagicMock, patch
import pytest

from src.scheduler import start_scheduler


class TestExtraJobs:
    @patch("src.scheduler.BlockingScheduler")
    def test_extra_jobs_added(self, sched_cls):
        instance = MagicMock()
        sched_cls.return_value = instance

        def main_job():
            pass

        def backfill_job():
            pass

        from apscheduler.triggers.cron import CronTrigger
        extra = {
            "backfill_daily": {
                "func": backfill_job,
                "trigger": CronTrigger(hour=18, minute=0),
                "name": "Daily Backfill",
            }
        }

        start_scheduler(main_job, {"hour": 8, "minute": 30}, extra_jobs=extra)

        # 첫 호출: daily_report
        # 두 번째 호출: backfill_daily
        assert instance.add_job.call_count == 2
        ids = [call.kwargs.get("id") for call in instance.add_job.call_args_list]
        assert "daily_report" in ids
        assert "backfill_daily" in ids
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `python3 -m pytest tests/test_scheduler.py -v`
Expected: FAIL — `extra_jobs` 미지원

- [ ] **Step 3: 구현 추가**

`src/scheduler.py` 교체:

```python
import logging
from collections.abc import Callable
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

logger = logging.getLogger(__name__)


def start_scheduler(
    job_func: Callable[[], None],
    config: dict,
    extra_jobs: dict | None = None,
) -> None:
    """APScheduler로 매일 지정 시간에 job_func을 실행한다.

    Args:
        job_func: 메인 일일 작업 함수
        config: schedule 설정 (hour, minute, timezone)
        extra_jobs: 추가 작업 dict, 형식:
            {job_id: {"func": callable, "trigger": Trigger, "name": str(optional)}}
    """
    tz = pytz.timezone(config.get("timezone", "Asia/Seoul"))
    scheduler = BlockingScheduler(timezone=tz)

    trigger = CronTrigger(
        hour=config.get("hour", 8),
        minute=config.get("minute", 30),
        timezone=tz,
    )
    scheduler.add_job(job_func, trigger, id="daily_report", name="Daily Stock Report")

    if extra_jobs:
        for job_id, job in extra_jobs.items():
            scheduler.add_job(
                job["func"],
                job["trigger"],
                id=job_id,
                name=job.get("name", job_id),
            )

    logger.info(
        "스케줄러 시작 — daily_report 매일 %d:%02d (%s) + extra_jobs %d개. Ctrl+C로 종료.",
        config.get("hour", 8), config.get("minute", 30),
        config.get("timezone", "Asia/Seoul"),
        len(extra_jobs) if extra_jobs else 0,
    )

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("스케줄러 종료됨.")
```

`main.py`의 `start_scheduler` 호출부 수정:

```python
    if args.start_scheduler:
        from apscheduler.triggers.cron import CronTrigger
        extra_jobs = {
            "backfill_daily": {
                "func": lambda: prediction_history.backfill_all(fetch_fn=fetch_stock_data),
                "trigger": CronTrigger(
                    hour=18, minute=0, timezone="Asia/Seoul"
                ),
                "name": "Daily Prediction Backfill",
            }
        }
        start_scheduler(daily_job, config["schedule"], extra_jobs=extra_jobs)
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `python3 -m pytest tests/test_scheduler.py -v`
Expected: PASS (1 test)

- [ ] **Step 5: 커밋**

```bash
git add src/scheduler.py main.py tests/test_scheduler.py
git commit -m "feat(scheduler): extra_jobs 지원·매일 18:00 KST 백필 cron 추가"
```

---

## Task 11: 최종 회귀 + 수동 검증

- [ ] **Step 1: 전체 테스트 스위트**

Run: `python3 -m pytest -v 2>&1 | tail -10`
Expected: 모두 PASS

- [ ] **Step 2: lint**

Run: `python3 -m py_compile src/prediction_history.py src/backtest.py src/web_app.py src/scheduler.py main.py src/report_generator.py`
Expected: 오류 없음

- [ ] **Step 3: 수동 검증 — DB 초기화**

```bash
rm -f data/predictions.db
python3 -c "from src import prediction_history; prediction_history.init_db()"
ls -la data/predictions.db
```
Expected: 파일 생성됨.

- [ ] **Step 4: 수동 검증 — 분석 → DB 저장 확인**

```bash
python3 main.py --symbol AAPL
sqlite3 data/predictions.db "SELECT model, direction, confidence FROM predictions WHERE symbol='AAPL';"
```
Expected: rf/lgbm/lstm/transformer/ensemble 5개 행 (오류 없는 모델만).

- [ ] **Step 5: 수동 검증 — 백테스트 실행**

```bash
python3 main.py --web --port 8080 &
SERVER_PID=$!
sleep 3
# 브라우저에서 종목 분석 → 결과 페이지 → "백테스트 실행" 버튼 클릭
# 또는 curl로 직접:
# curl -X POST -d "csrf_token=..." http://localhost:8080/backtest/AAPL
sleep 30  # walk-forward 완료 대기
sqlite3 data/predictions.db "SELECT model, COUNT(*), AVG(hit) FROM predictions WHERE source='backtest' AND symbol='AAPL' GROUP BY model;"
kill $SERVER_PID
```
Expected: rf/lgbm/ensemble 3개 모델 각 ~120행 + hit 비율.

- [ ] **Step 6: git status 클린**

Run: `git status`
Expected: clean.

---

## 자체 검증 체크리스트 (Spec 매핑)

| Spec 요구 사항 | 구현 위치 |
|---------------|-----------|
| SQLite 스키마 (UTC unix epoch) | Task 1 |
| WAL 모드 | Task 1 |
| `_writer_lock` 직렬화 | Task 1 (모든 쓰기에 적용) |
| `init_db` 멱등 | Task 1 |
| `insert_live` UNIQUE OR IGNORE | Task 2 |
| 모델 매핑 (random_forest → rf 등) | Task 2 (`_MODEL_KEY_MAP`) |
| 에러 모델 스킵 | Task 2 |
| `backfill_inline` + KST 정규화 | Task 3 (`_df_to_target_close_map`) |
| hit 계산 (변동 없음 → 0) | Task 3 (`_compute_hit`) |
| `backfill_all` cron용 | Task 4 |
| symbol별 그룹화 + 부분 실패 격리 | Task 4 |
| `hit_rate_by_model` source 필터 | Task 5 |
| `insert_backtest` 일괄 | Task 5 |
| `get_backtest_results` | Task 5 |
| `analyze_stock` 통합 (DB 장애 → UX 차단 X) | Task 6 |
| `_next_business_day_unix` 헬퍼 | Task 6 |
| 리포트 hit rate 표 (n<10 → 데이터 부족) | Task 7 |
| `walk_forward` RF+LGBM+voting | Task 8 |
| Voting ensemble 규칙 (같은 방향 평균, 다르면 높은 confidence) | Task 8 (`_ensemble_vote`) |
| `_backtest_lock` 글로벌 1개 제한 | Task 9 |
| `/backtest/<symbol>` POST + CSRF | Task 9 |
| 백테스트 폼 분석 리포트에 추가 | Task 9 |
| 스케줄러 extra_jobs (18:00 KST 백필) | Task 10 |
| 전체 회귀 + 수동 E2E | Task 11 |
