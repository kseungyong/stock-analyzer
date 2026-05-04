"""예측 이력 SQLite 영속화 + 백필/집계."""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import closing
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

-- 라이브 예측은 backtest_id=NULL이라 위 UNIQUE 제약을 우회 → 별도 partial unique index로 보호
CREATE UNIQUE INDEX IF NOT EXISTS idx_pred_live_unique
    ON predictions(symbol, target_date, model)
    WHERE source = 'live';

CREATE INDEX IF NOT EXISTS idx_pred_symbol_model
    ON predictions(symbol, model, source);
CREATE INDEX IF NOT EXISTS idx_pred_unevaluated
    ON predictions(symbol, target_date) WHERE actual_close IS NULL;
CREATE INDEX IF NOT EXISTS idx_pred_backtest_id
    ON predictions(backtest_id) WHERE backtest_id IS NOT NULL;
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
        with closing(_connect()) as conn:
            conn.executescript(_SCHEMA)
    logger.info("predictions DB 초기화 완료: %s", _DB_PATH)


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
        with closing(_connect()) as conn:
            conn.executemany(
                """INSERT OR IGNORE INTO predictions
                   (symbol, ts, target_date, model, direction, confidence,
                    base_close, source, backtest_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
