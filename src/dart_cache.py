"""dart_cache — DART corp_codes + disclosures + dart_summaries DB layer.

기존 data/predictions.db 재사용 (analysis_cache 등과 같은 파일).
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "predictions.db"
_writer_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS corp_codes (
    corp_code   TEXT PRIMARY KEY,
    corp_name   TEXT NOT NULL,
    stock_code  TEXT,
    modify_date TEXT
);
CREATE INDEX IF NOT EXISTS idx_corp_codes_stock_code ON corp_codes(stock_code);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _writer_lock:
        with closing(_connect()) as conn:
            conn.executescript(_SCHEMA)
    logger.info("dart_cache DB 초기화 완료: %s", _DB_PATH)


def upsert_corp_codes(rows: list[dict]) -> int:
    """INSERT OR REPLACE — 동일 corp_code 는 덮어쓴다. 반환: 처리된 row 수."""
    if not rows:
        return 0
    with _writer_lock:
        with closing(_connect()) as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO corp_codes "
                "(corp_code, corp_name, stock_code, modify_date) VALUES (?, ?, ?, ?)",
                [(r["corp_code"], r["corp_name"], r["stock_code"], r["modify_date"])
                 for r in rows],
            )
            conn.commit()
    return len(rows)


def get_corp_code_by_stock(stock_code: str) -> str | None:
    """주식 종목코드 → DART corp_code. 없으면 None."""
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT corp_code FROM corp_codes WHERE stock_code = ? LIMIT 1",
            (stock_code,),
        ).fetchone()
    return row[0] if row else None


def corp_codes_last_modify_date() -> str | None:
    """corp_codes 테이블의 최대 modify_date. 없으면 None (=재다운로드 필요)."""
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT MAX(modify_date) FROM corp_codes"
        ).fetchone()
    return row[0] if row and row[0] else None
