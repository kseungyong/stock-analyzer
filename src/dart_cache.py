"""dart_cache — DART corp_codes + disclosures + dart_summaries DB layer.

기존 data/predictions.db 재사용 (analysis_cache 등과 같은 파일).
"""
from __future__ import annotations

import json as _json
import logging
import sqlite3
import threading
import time
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

CREATE TABLE IF NOT EXISTS disclosures (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    corp_code       TEXT NOT NULL,
    stock_code      TEXT NOT NULL,
    disclosure_type TEXT NOT NULL,
    rcept_no        TEXT,
    rcept_dt        TEXT,
    raw_json        TEXT NOT NULL,
    fetched_at      INTEGER NOT NULL,
    UNIQUE(corp_code, disclosure_type, rcept_no)
);
CREATE INDEX IF NOT EXISTS idx_disclosures_stock ON disclosures(stock_code, rcept_dt DESC);

CREATE TABLE IF NOT EXISTS dart_summaries (
    symbol           TEXT PRIMARY KEY,
    summary_json     TEXT NOT NULL,
    sentiment        TEXT,
    critical_count   INTEGER NOT NULL,
    generated_at     INTEGER NOT NULL,
    model            TEXT,
    source           TEXT NOT NULL
);
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


# ---------------------------------------------------------------------------
# disclosures
# ---------------------------------------------------------------------------

def insert_disclosures(
    stock_code: str, corp_code: str, disclosure_type: str,
    rows: list[dict], fetched_at: int | None = None,
) -> int:
    """INSERT OR IGNORE — UNIQUE 위반 (동일 rcept_no) 은 silent skip."""
    if not rows:
        return 0
    ts = fetched_at if fetched_at is not None else int(time.time())
    with _writer_lock:
        with closing(_connect()) as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO disclosures "
                "(corp_code, stock_code, disclosure_type, rcept_no, rcept_dt, raw_json, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(corp_code, stock_code, disclosure_type,
                  r.get("rcept_no") or "", r.get("rcept_dt") or "",
                  r.get("raw_json") if isinstance(r.get("raw_json"), str)
                    else _json.dumps(r, ensure_ascii=False),
                  ts) for r in rows],
            )
            conn.commit()
    return len(rows)


def count_disclosures(stock_code: str) -> int:
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM disclosures WHERE stock_code = ?",
            (stock_code,),
        ).fetchone()
    return row[0] if row else 0


def purge_old(days: int = 14) -> int:
    """fetched_at < now - days*86400 인 row 삭제. 반환: 삭제 row 수."""
    cutoff = int(time.time()) - days * 86400
    with _writer_lock:
        with closing(_connect()) as conn:
            cur = conn.execute(
                "DELETE FROM disclosures WHERE fetched_at < ?", (cutoff,),
            )
            conn.commit()
            return cur.rowcount


# ---------------------------------------------------------------------------
# dart_summaries
# ---------------------------------------------------------------------------

def upsert_summary(
    symbol: str, summary_json: str, sentiment: str | None,
    critical_count: int, model: str | None, source: str,
) -> None:
    """INSERT ... ON CONFLICT(symbol) DO UPDATE — atomic."""
    now = int(time.time())
    with _writer_lock:
        with closing(_connect()) as conn:
            conn.execute(
                "INSERT INTO dart_summaries "
                "(symbol, summary_json, sentiment, critical_count, generated_at, model, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(symbol) DO UPDATE SET "
                "  summary_json = excluded.summary_json, "
                "  sentiment = excluded.sentiment, "
                "  critical_count = excluded.critical_count, "
                "  generated_at = excluded.generated_at, "
                "  model = excluded.model, "
                "  source = excluded.source",
                (symbol, summary_json, sentiment, critical_count, now, model, source),
            )
            conn.commit()


from contextlib import contextmanager


@contextmanager
def transaction():
    """단일 connection + 명시적 BEGIN/COMMIT 으로 batch upsert.

    사용 예:
        with dart_cache.transaction() as conn:
            for symbol, ... in pending:
                dart_cache.upsert_summary_within_tx(conn, symbol, ...)
    """
    with _writer_lock:
        conn = _connect()
        conn.execute("BEGIN")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def upsert_summary_within_tx(
    conn, symbol: str, summary_json: str, sentiment: str | None,
    critical_count: int, model: str | None, source: str,
) -> None:
    """transaction() 안에서 호출. 자동 commit 없음 (caller가 처리)."""
    now = int(time.time())
    conn.execute(
        "INSERT INTO dart_summaries "
        "(symbol, summary_json, sentiment, critical_count, generated_at, model, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(symbol) DO UPDATE SET "
        "  summary_json = excluded.summary_json, "
        "  sentiment = excluded.sentiment, "
        "  critical_count = excluded.critical_count, "
        "  generated_at = excluded.generated_at, "
        "  model = excluded.model, "
        "  source = excluded.source",
        (symbol, summary_json, sentiment, critical_count, now, model, source),
    )


def get_summary(symbol: str) -> dict | None:
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT symbol, summary_json, sentiment, critical_count, "
            "generated_at, model, source FROM dart_summaries WHERE symbol = ?",
            (symbol,),
        ).fetchone()
    if not row:
        return None
    return {
        "symbol": row[0], "summary_json": row[1], "sentiment": row[2],
        "critical_count": row[3], "generated_at": row[4],
        "model": row[5], "source": row[6],
    }


def list_summaries() -> dict[str, dict]:
    """{symbol: row_dict} — web/report 가 한 번에 fetch."""
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT symbol, summary_json, sentiment, critical_count, "
            "generated_at, model, source FROM dart_summaries"
        ).fetchall()
    return {
        r[0]: {
            "symbol": r[0], "summary_json": r[1], "sentiment": r[2],
            "critical_count": r[3], "generated_at": r[4],
            "model": r[5], "source": r[6],
        }
        for r in rows
    }
