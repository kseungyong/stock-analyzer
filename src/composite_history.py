"""composite_history — 종목별 일일 composite score 시계열 저장.

cleanup 모듈이 '7일 연속 < -5' 같은 지속성 판정에 사용.
"""
from __future__ import annotations

import logging
import sqlite3
import time
import threading
from contextlib import closing
from pathlib import Path

logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "predictions.db"
_writer_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS composite_history (
    symbol       TEXT NOT NULL,
    recorded_at  INTEGER NOT NULL,
    composite    REAL NOT NULL,
    PRIMARY KEY (symbol, recorded_at)
);

CREATE INDEX IF NOT EXISTS idx_ch_symbol_date
    ON composite_history(symbol, recorded_at DESC);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    """스키마 적용. 멱등."""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _writer_lock:
        with closing(_connect()) as conn:
            conn.executescript(_SCHEMA)
    logger.info("composite_history DB 초기화 완료: %s", _DB_PATH)


def insert(symbol: str, composite: float, recorded_at: int | None = None) -> None:
    """recorded_at 생략 시 현재 시각. 동일 (symbol, recorded_at) 은 덮어쓴다."""
    ts = recorded_at if recorded_at is not None else int(time.time())
    with _writer_lock:
        with closing(_connect()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO composite_history "
                "(symbol, recorded_at, composite) VALUES (?, ?, ?)",
                (symbol, ts, float(composite)),
            )
            conn.commit()


def recent(symbol: str, days: int = 7) -> list[tuple[int, float]]:
    """최근 N일간 (recorded_at, composite) 리스트. 최신순."""
    cutoff = int(time.time()) - days * 86400
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT recorded_at, composite FROM composite_history "
            "WHERE symbol = ? AND recorded_at >= ? "
            "ORDER BY recorded_at DESC",
            (symbol, cutoff),
        ).fetchall()
    return [(int(r[0]), float(r[1])) for r in rows]


def purge_old(days: int = 90) -> int:
    """N일 이전 row 삭제. 삭제된 row 수 반환."""
    cutoff = int(time.time()) - days * 86400
    with _writer_lock:
        with closing(_connect()) as conn:
            cur = conn.execute(
                "DELETE FROM composite_history WHERE recorded_at < ?",
                (cutoff,),
            )
            conn.commit()
            return cur.rowcount
