"""leader_cache: SQLite 영속화 — 정량/LLM/사용자 수정본 분리 저장.

Spec §6: leaders 테이블 하나에 cond1/cond2 통과 여부, LLM 초안 (llm_*),
사용자 수정본 (user_*) 모두 저장. 표시 시 user_* 우선, NULL 이면 llm_* fallback.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

_DB_PATH = str(Path(__file__).parent.parent / "data" / "predictions.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS leaders (
    symbol              TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    market              TEXT NOT NULL,
    sector              TEXT,
    industry            TEXT,

    last_close          REAL NOT NULL,
    market_cap          INTEGER,
    market_cap_quintile INTEGER,
    near_high_pct       REAL,
    return_1y_pct       REAL,
    index_return_1y_pct REAL,
    rel_return_pp       REAL,
    trailing_eps        REAL,
    forward_eps         REAL,
    eps_growth_yoy      REAL,
    trailing_pe         REAL,
    pe_quintile         INTEGER,

    cond1_passed        BOOLEAN NOT NULL,
    cond2_passed        BOOLEAN NOT NULL,
    cond3_score         INTEGER,
    passed              BOOLEAN NOT NULL,

    llm_tam_narrative        TEXT,
    llm_narrative_expansion  TEXT,
    llm_bottleneck           TEXT,
    llm_moat                 TEXT,
    llm_raw_response         TEXT,
    llm_generated_at         INTEGER,
    llm_model                TEXT,
    llm_error                TEXT,

    user_tam_narrative       TEXT,
    user_narrative_expansion TEXT,
    user_bottleneck          TEXT,
    user_moat                TEXT,
    user_edited_at           INTEGER,
    user_edited_by           TEXT,

    status              TEXT NOT NULL DEFAULT 'active',
    is_stale            BOOLEAN NOT NULL DEFAULT 0,
    refreshed_at        INTEGER NOT NULL,
    created_at          INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_leaders_passed_status ON leaders(passed, status);
CREATE INDEX IF NOT EXISTS idx_leaders_market ON leaders(market);
"""

_LLM_FIELDS = ("tam_narrative", "narrative_expansion", "bottleneck", "moat")
_STALE_SECONDS = 7 * 24 * 60 * 60  # 7일


def init_db() -> None:
    Path(_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        conn.commit()
    logger.info("leader_cache DB 초기화 완료: %s", _DB_PATH)


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def upsert_quantitative(candidates: list[dict[str, Any]]) -> None:
    """정량 컬럼 + cond_passed + meta(refreshed_at, status='active') 갱신.

    LLM 컬럼과 user_* 는 건드리지 않음 (UPSERT 의 SET 절 명시).
    """
    if not candidates:
        return
    now = int(time.time())
    cols = [
        "symbol", "name", "market", "sector", "industry",
        "last_close", "market_cap", "market_cap_quintile",
        "near_high_pct", "return_1y_pct", "index_return_1y_pct", "rel_return_pp",
        "trailing_eps", "forward_eps", "eps_growth_yoy", "trailing_pe", "pe_quintile",
        "cond1_passed", "cond2_passed", "cond3_score", "passed",
    ]
    placeholders = ",".join("?" * (len(cols) + 3))  # +status,refreshed_at,created_at
    sql_cols = ",".join(cols) + ",status,refreshed_at,created_at"
    set_clause = ",".join(f"{c}=excluded.{c}" for c in cols) + (
        ",status='active',refreshed_at=excluded.refreshed_at"
    )
    sql = (
        f"INSERT INTO leaders({sql_cols}) VALUES ({placeholders}) "
        f"ON CONFLICT(symbol) DO UPDATE SET {set_clause}"
    )
    with _connect() as conn:
        for c in candidates:
            params = [c[k] for k in cols] + ["active", now, now]
            conn.execute(sql, params)
        conn.commit()


def list_active() -> list[sqlite3.Row]:
    with _connect() as conn:
        return list(conn.execute(
            "SELECT * FROM leaders WHERE passed=1 AND status='active' "
            "ORDER BY rel_return_pp DESC NULLS LAST"
        ))


def get(symbol: str) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM leaders WHERE symbol=?", (symbol,)
        ).fetchone()


def mark_dropped(symbols: list[str]) -> None:
    if not symbols:
        return
    placeholders = ",".join("?" * len(symbols))
    with _connect() as conn:
        conn.execute(
            f"UPDATE leaders SET status='dropped' WHERE symbol IN ({placeholders})",
            symbols,
        )
        conn.commit()


def upsert_llm(
    symbol: str,
    fields: dict[str, str],
    *,
    model: str,
    raw: str,
    error: str | None = None,
) -> None:
    """LLM 4필드 + 메타 갱신. user_* 는 건드리지 않음."""
    now = int(time.time())
    with _connect() as conn:
        conn.execute(
            "UPDATE leaders SET "
            "llm_tam_narrative=?, llm_narrative_expansion=?, "
            "llm_bottleneck=?, llm_moat=?, "
            "llm_raw_response=?, llm_generated_at=?, llm_model=?, llm_error=?, "
            "is_stale=0 "
            "WHERE symbol=?",
            (
                fields.get("tam_narrative"),
                fields.get("narrative_expansion"),
                fields.get("bottleneck"),
                fields.get("moat"),
                raw, now, model, error,
                symbol,
            ),
        )
        conn.commit()


def update_user_fields(symbol: str, fields: dict[str, str], user: str) -> None:
    """사용자 수정 — 4 필드 중 명시된 것만 user_* 컬럼 덮어쓰기."""
    allowed = set(_LLM_FIELDS)
    sets: list[str] = []
    params: list[Any] = []
    for k, v in fields.items():
        if k not in allowed:
            raise ValueError(f"invalid field: {k}")
        sets.append(f"user_{k}=?")
        params.append(v)
    if not sets:
        return
    sets.extend(["user_edited_at=?", "user_edited_by=?"])
    params.extend([int(time.time()), user, symbol])
    with _connect() as conn:
        conn.execute(
            f"UPDATE leaders SET {','.join(sets)} WHERE symbol=?", params,
        )
        conn.commit()


def recompute_stale() -> None:
    """llm_generated_at 이 7일 초과면 is_stale=1."""
    threshold = int(time.time()) - _STALE_SECONDS
    with _connect() as conn:
        conn.execute(
            "UPDATE leaders SET is_stale=1 "
            "WHERE llm_generated_at IS NOT NULL AND llm_generated_at < ?",
            (threshold,),
        )
        conn.commit()


def display_field(row: sqlite3.Row, name: str) -> str:
    """user_<name> 우선, NULL 이면 llm_<name> fallback, 둘 다 NULL 이면 '(분석 대기 중)'."""
    if name not in _LLM_FIELDS:
        raise ValueError(f"invalid field: {name}")
    user_val = row[f"user_{name}"]
    if user_val:
        return str(user_val)
    llm_val = row[f"llm_{name}"]
    if llm_val:
        return str(llm_val)
    return "(분석 대기 중)"


def diff_with_existing(symbols: list[str]) -> dict[str, list[str]]:
    """이번 cron 의 통과 종목 vs 기존 row 비교.

    Returns:
        {"new": [...], "stale": [...], "kept": [...], "dropped": [...]}
    """
    with _connect() as conn:
        existing = {
            r["symbol"]: r for r in conn.execute(
                "SELECT symbol, llm_generated_at, status FROM leaders"
            )
        }
    new_set = set(symbols)
    threshold = int(time.time()) - _STALE_SECONDS
    result: dict[str, list[str]] = {"new": [], "stale": [], "kept": [], "dropped": []}
    for sym in symbols:
        row = existing.get(sym)
        if row is None or row["llm_generated_at"] is None:
            result["new"].append(sym)
        elif row["llm_generated_at"] < threshold:
            result["stale"].append(sym)
        else:
            result["kept"].append(sym)
    for sym, row in existing.items():
        if sym not in new_set and row["status"] == "active":
            result["dropped"].append(sym)
    return result
