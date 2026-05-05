"""분석 결과 캐시 — 종목별 + 전체 분석 SQLite 영속화."""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "predictions.db"
_writer_lock = threading.Lock()

_KST = ZoneInfo("Asia/Seoul")
_NY = ZoneInfo("America/New_York")
_ONE_DAY = timedelta(days=1)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS analysis_cache (
    cache_key      TEXT PRIMARY KEY,
    market         TEXT NOT NULL,
    result_html    TEXT NOT NULL,
    generated_at   INTEGER NOT NULL,
    source         TEXT NOT NULL,
    signal_value   TEXT,
    signal_score   INTEGER
);

CREATE INDEX IF NOT EXISTS idx_analysis_cache_market
    ON analysis_cache(market);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    """첫 호출 시 DB 파일과 부모 디렉토리 생성, 스키마 적용. 멱등."""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _writer_lock:
        with closing(_connect()) as conn:
            conn.executescript(_SCHEMA)
            _migrate(conn)
    logger.info("analysis_cache DB 초기화 완료: %s", _DB_PATH)


def _migrate(conn: sqlite3.Connection) -> None:
    """기존 DB 에 누락된 컬럼을 추가하는 멱등 마이그레이션.

    PRAGMA table_info 로 컬럼 존재 확인 후 조건부 ALTER. SQLite 의 ALTER TABLE
    ADD COLUMN 은 IF NOT EXISTS 미지원이라 명시적 체크 필요.
    """
    cur = conn.execute("PRAGMA table_info(analysis_cache)")
    cols = {row[1] for row in cur.fetchall()}
    if "signal_value" not in cols:
        conn.execute("ALTER TABLE analysis_cache ADD COLUMN signal_value TEXT")
    if "signal_score" not in cols:
        conn.execute("ALTER TABLE analysis_cache ADD COLUMN signal_score INTEGER")


def put(
    cache_key: str,
    market: str,
    result_html: str,
    source: str,
    *,
    signal_value: str | None = None,
    signal_score: int | None = None,
) -> None:
    """analysis_cache UPSERT. 같은 cache_key 존재 시 덮어쓴다.

    signal_value/signal_score 가 None 이면 NULL 저장 — UPSERT 시 기존 값을 NULL 로
    덮어쓰는 효과 (호출자가 명시적으로 전달해야 보존).
    """
    now_unix = int(time.time())
    with _writer_lock:
        with closing(_connect()) as conn:
            conn.execute("BEGIN")
            try:
                conn.execute(
                    """INSERT INTO analysis_cache
                       (cache_key, market, result_html, generated_at, source,
                        signal_value, signal_score)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(cache_key) DO UPDATE SET
                         market       = excluded.market,
                         result_html  = excluded.result_html,
                         generated_at = excluded.generated_at,
                         source       = excluded.source,
                         signal_value = excluded.signal_value,
                         signal_score = excluded.signal_score""",
                    (cache_key, market, result_html, now_unix, source,
                     signal_value, signal_score),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise


def get(cache_key: str) -> dict | None:
    """cache_key 의 row 를 dict 로 반환. 없으면 None."""
    with closing(_connect()) as conn:
        row = conn.execute(
            """SELECT cache_key, market, result_html, generated_at, source,
                      signal_value, signal_score
               FROM analysis_cache WHERE cache_key = ?""",
            (cache_key,),
        ).fetchone()
    if row is None:
        return None
    return {
        "cache_key": row[0],
        "market": row[1],
        "result_html": row[2],
        "generated_at": row[3],
        "source": row[4],
        "signal_value": row[5],
        "signal_score": row[6],
    }


def list_symbols() -> list[dict]:
    """종목별 row 만 (market != 'all') market·cache_key 순으로 반환."""
    with closing(_connect()) as conn:
        rows = conn.execute(
            """SELECT cache_key, market, result_html, generated_at, source,
                      signal_value, signal_score
               FROM analysis_cache
               WHERE market != 'all'
               ORDER BY market, cache_key"""
        ).fetchall()
    return [
        {
            "cache_key": r[0],
            "market": r[1],
            "result_html": r[2],
            "generated_at": r[3],
            "source": r[4],
            "signal_value": r[5],
            "signal_score": r[6],
        }
        for r in rows
    ]


def _next_market_open_kst(market: str, generated_at_unix: int) -> int:
    """generated_at 이후 다음 시장 시작 시각의 unix epoch (UTC) 를 반환.

    market='korea' → 한국시간 09:00 (KOSPI 정규장 시작)
    market='us'    → 미국 동부 09:30 → KST 환산 (서머타임 자동 처리)
    """
    gen_dt = datetime.fromtimestamp(generated_at_unix, tz=_KST)

    if market == "korea":
        candidate = gen_dt.replace(hour=9, minute=0, second=0, microsecond=0)
        if candidate <= gen_dt:
            candidate = candidate + _ONE_DAY
        return int(candidate.timestamp())

    if market == "us":
        # NY 09:30 을 두 번 후보로 검사 (gen_dt 와 같은 NY 날짜, 다음 NY 날짜)
        gen_ny = gen_dt.astimezone(_NY)
        candidate_ny = gen_ny.replace(hour=9, minute=30, second=0, microsecond=0)
        if candidate_ny <= gen_ny:
            candidate_ny = candidate_ny + _ONE_DAY
        return int(candidate_ny.astimezone(_KST).timestamp())

    raise ValueError(f"Unknown market: {market}")


def is_fresh(row: dict, now_unix: int) -> bool:
    """row 가 만료 전인지 판단.

    market='korea'/'us' → _next_market_open_kst 와 비교.
    market='all'        → 모든 종목 row 가 fresh 일 때만 True.
    """
    market = row["market"]
    if market in ("korea", "us"):
        return now_unix < _next_market_open_kst(market, row["generated_at"])

    if market == "all":
        symbol_rows = list_symbols()
        if not symbol_rows:
            return False
        return all(is_fresh(r, now_unix) for r in symbol_rows)

    raise ValueError(f"Unknown market: {market}")
