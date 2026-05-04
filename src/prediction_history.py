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
