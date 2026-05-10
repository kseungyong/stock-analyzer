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
    cache_key         TEXT PRIMARY KEY,
    market            TEXT NOT NULL,
    result_html       TEXT NOT NULL,
    generated_at      INTEGER NOT NULL,
    source            TEXT NOT NULL,
    signal_value      TEXT,
    signal_score      INTEGER,
    bnf_signal_value  TEXT,
    bnf_signal_score  INTEGER,
    pattern_json      TEXT,
    pattern_signal    TEXT,
    pattern_score     INTEGER,
    last_close        REAL
);

CREATE INDEX IF NOT EXISTS idx_analysis_cache_market
    ON analysis_cache(market);
"""


def _connect() -> sqlite3.Connection:
    # isolation_level=DEFERRED (기본) — BEGIN/COMMIT 정상 동작 + WAL 일관성 보장.
    # 이전 isolation_level=None (autocommit) 은 gunicorn worker + scheduler cron
    # 동시 write 시 transaction boundary 부재로 race 위험.
    conn = sqlite3.connect(_DB_PATH)
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
    if "bnf_signal_value" not in cols:
        conn.execute("ALTER TABLE analysis_cache ADD COLUMN bnf_signal_value TEXT")
    if "bnf_signal_score" not in cols:
        conn.execute("ALTER TABLE analysis_cache ADD COLUMN bnf_signal_score INTEGER")
    # Phase A — pattern indicators (이동평균 4상태 + Phase B/C/D/E full payload)
    if "pattern_json" not in cols:
        conn.execute("ALTER TABLE analysis_cache ADD COLUMN pattern_json TEXT")
    if "pattern_signal" not in cols:
        conn.execute("ALTER TABLE analysis_cache ADD COLUMN pattern_signal TEXT")
    if "pattern_score" not in cols:
        conn.execute("ALTER TABLE analysis_cache ADD COLUMN pattern_score INTEGER")
    # Portfolio (last_close — 손익 계산용)
    if "last_close" not in cols:
        conn.execute("ALTER TABLE analysis_cache ADD COLUMN last_close REAL")


def put(
    cache_key: str,
    market: str,
    result_html: str,
    source: str,
    *,
    signal_value: str | None = None,
    signal_score: int | None = None,
    bnf_signal_value: str | None = None,
    bnf_signal_score: int | None = None,
    pattern_json: str | None = None,
    pattern_signal: str | None = None,
    pattern_score: int | None = None,
    last_close: float | None = None,
) -> None:
    """analysis_cache UPSERT. 같은 cache_key 존재 시 덮어쓴다."""
    now_unix = int(time.time())
    with _writer_lock:
        with closing(_connect()) as conn:
            conn.execute("BEGIN")
            try:
                conn.execute(
                    """INSERT INTO analysis_cache
                       (cache_key, market, result_html, generated_at, source,
                        signal_value, signal_score,
                        bnf_signal_value, bnf_signal_score,
                        pattern_json, pattern_signal, pattern_score,
                        last_close)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(cache_key) DO UPDATE SET
                         market           = excluded.market,
                         result_html      = excluded.result_html,
                         generated_at     = excluded.generated_at,
                         source           = excluded.source,
                         signal_value     = excluded.signal_value,
                         signal_score     = excluded.signal_score,
                         bnf_signal_value = excluded.bnf_signal_value,
                         bnf_signal_score = excluded.bnf_signal_score,
                         pattern_json     = excluded.pattern_json,
                         pattern_signal   = excluded.pattern_signal,
                         pattern_score    = excluded.pattern_score,
                         last_close       = excluded.last_close""",
                    (cache_key, market, result_html, now_unix, source,
                     signal_value, signal_score,
                     bnf_signal_value, bnf_signal_score,
                     pattern_json, pattern_signal, pattern_score,
                     last_close),
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
                      signal_value, signal_score,
                      bnf_signal_value, bnf_signal_score,
                      pattern_json, pattern_signal, pattern_score,
                      last_close
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
        "bnf_signal_value": row[7],
        "bnf_signal_score": row[8],
        "pattern_json": row[9] if len(row) > 9 else None,
        "pattern_signal": row[10] if len(row) > 10 else None,
        "pattern_score": row[11] if len(row) > 11 else None,
        "last_close": row[12] if len(row) > 12 else None,
    }


def list_symbols() -> list[dict]:
    """종목별 row 만 (market != 'all') market·cache_key 순으로 반환."""
    with closing(_connect()) as conn:
        rows = conn.execute(
            """SELECT cache_key, market, result_html, generated_at, source,
                      signal_value, signal_score,
                      bnf_signal_value, bnf_signal_score
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
            "bnf_signal_value": r[7],
            "bnf_signal_score": r[8],
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
