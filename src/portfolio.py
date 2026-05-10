"""포트폴리오 (보유 종목) 영속화 — 사용자별 격리.

Spec: docs/superpowers/specs/2026-05-10-portfolio-multiuser-design.md

테이블: portfolio((username, symbol) 복합 PK, avg_price, qty, added_at, notes)
JOIN with analysis_cache 로 last_close + 시그널 + 패턴 한 번에 lookup.
analysis_cache 는 사용자 무관 (분석 결과는 공유).
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

logger = logging.getLogger(__name__)

# analysis_cache 와 동일 DB (predictions.db)
_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "predictions.db"
_writer_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS portfolio (
    username   TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    avg_price  REAL NOT NULL,
    qty        INTEGER NOT NULL DEFAULT 0,
    added_at   INTEGER NOT NULL,
    notes      TEXT,
    PRIMARY KEY (username, symbol)
);
"""

# 마이그레이션 시 username 미보유 row 들을 이 사용자에게 할당
_LEGACY_OWNER = "admin"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _migrate_to_multiuser(conn: sqlite3.Connection) -> None:
    """기존 portfolio(symbol PK) → portfolio(username, symbol) 복합 PK.

    멱등: username 컬럼 이미 있으면 no-op.
    SQLite 는 PK 변경을 ALTER 로 못해서 rename → recreate → copy → drop 패턴.
    """
    cur = conn.execute("PRAGMA table_info(portfolio)")
    cols = {row[1] for row in cur.fetchall()}
    if not cols or "username" in cols:
        return  # 신규 (cols 비어있음 — 첫 실행) 또는 이미 마이그레이션됨

    logger.info("portfolio 다중 사용자 마이그레이션 시작 — owner=%s", _LEGACY_OWNER)
    conn.execute("BEGIN")
    try:
        conn.execute("ALTER TABLE portfolio RENAME TO portfolio_old")
        conn.executescript(_SCHEMA)
        conn.execute(
            """INSERT INTO portfolio(username, symbol, avg_price, qty, added_at, notes)
               SELECT ?, symbol, avg_price, qty, added_at, notes FROM portfolio_old""",
            (_LEGACY_OWNER,),
        )
        moved = conn.execute("SELECT COUNT(*) FROM portfolio").fetchone()[0]
        conn.execute("DROP TABLE portfolio_old")
        conn.execute("COMMIT")
        logger.info("마이그레이션 완료 — %d 종목을 %s 에 할당", moved, _LEGACY_OWNER)
    except Exception:
        conn.execute("ROLLBACK")
        raise


def init_db() -> None:
    """portfolio 테이블 생성 + 단일사용자→다중사용자 마이그레이션. 멱등."""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _writer_lock:
        with closing(_connect()) as conn:
            _migrate_to_multiuser(conn)
            conn.executescript(_SCHEMA)
    logger.info("portfolio DB 초기화 완료: %s", _DB_PATH)


def _validate(avg_price: float, qty: int) -> None:
    if avg_price is None or float(avg_price) <= 0:
        raise ValueError(f"avg_price must be positive, got {avg_price!r}")
    if qty is None or int(qty) < 0:
        raise ValueError(f"qty must be non-negative, got {qty!r}")


def add_holding(
    username: str, symbol: str, avg_price: float, qty: int,
    notes: str | None = None,
) -> bool:
    """추가 또는 갱신. 새로 추가됐으면 True, 기존 갱신이면 False."""
    _validate(avg_price, qty)
    now = int(time.time())
    with _writer_lock:
        with closing(_connect()) as conn:
            existing = conn.execute(
                "SELECT 1 FROM portfolio WHERE username = ? AND symbol = ?",
                (username, symbol),
            ).fetchone()
            conn.execute("BEGIN")
            try:
                conn.execute(
                    """INSERT INTO portfolio(username, symbol, avg_price, qty, added_at, notes)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(username, symbol) DO UPDATE SET
                         avg_price = excluded.avg_price,
                         qty       = excluded.qty,
                         notes     = excluded.notes""",
                    (username, symbol, float(avg_price), int(qty), now, notes),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
    return existing is None


def remove_holding(username: str, symbol: str) -> bool:
    """제거. 존재했으면 True, 없었으면 False."""
    with _writer_lock:
        with closing(_connect()) as conn:
            conn.execute("BEGIN")
            try:
                cur = conn.execute(
                    "DELETE FROM portfolio WHERE username = ? AND symbol = ?",
                    (username, symbol),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            return cur.rowcount > 0


def update_holding(
    username: str, symbol: str, *,
    avg_price: float | None = None,
    qty: int | None = None,
    notes: str | None = None,
) -> bool:
    """부분 업데이트. 존재했으면 True, 없었으면 False.

    다른 사용자 종목은 WHERE 절 미일치로 자동 noop → False.
    """
    sets: list[str] = []
    vals: list[object] = []
    if avg_price is not None:
        if float(avg_price) <= 0:
            raise ValueError(f"avg_price must be positive, got {avg_price!r}")
        sets.append("avg_price = ?")
        vals.append(float(avg_price))
    if qty is not None:
        if int(qty) < 0:
            raise ValueError(f"qty must be non-negative, got {qty!r}")
        sets.append("qty = ?")
        vals.append(int(qty))
    if notes is not None:
        sets.append("notes = ?")
        vals.append(notes)
    if not sets:
        return True
    vals.extend([username, symbol])
    with _writer_lock:
        with closing(_connect()) as conn:
            conn.execute("BEGIN")
            try:
                cur = conn.execute(
                    f"UPDATE portfolio SET {', '.join(sets)} "
                    f"WHERE username = ? AND symbol = ?",
                    vals,
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            return cur.rowcount > 0


def list_holdings(username: str) -> list[dict]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT symbol, avg_price, qty, added_at, notes "
            "FROM portfolio WHERE username = ? ORDER BY symbol",
            (username,),
        ).fetchall()
    return [
        {"symbol": r[0], "avg_price": float(r[1]), "qty": int(r[2]),
         "added_at": int(r[3]), "notes": r[4]}
        for r in rows
    ]


def count_holdings(username: str) -> int:
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM portfolio WHERE username = ?",
            (username,),
        ).fetchone()
    return int(row[0]) if row else 0


def list_holdings_with_pnl(username: str) -> list[dict]:
    """portfolio JOIN analysis_cache — 손익 + 시그널 + 패턴 한 번에.

    last_close NULL (분석 안 됨) 면 pnl_pct/pnl_abs NULL.
    """
    with closing(_connect()) as conn:
        rows = conn.execute(
            """SELECT p.symbol, p.avg_price, p.qty, p.added_at, p.notes,
                      c.market, c.last_close,
                      c.signal_value, c.signal_score,
                      c.bnf_signal_value, c.bnf_signal_score,
                      c.pattern_json, c.pattern_signal, c.pattern_score,
                      c.generated_at
               FROM portfolio p
               LEFT JOIN analysis_cache c ON p.symbol = c.cache_key
               WHERE p.username = ?
               ORDER BY p.symbol""",
            (username,),
        ).fetchall()
    result = []
    for r in rows:
        avg_price = float(r[1])
        qty = int(r[2])
        last_close = float(r[6]) if r[6] is not None else None
        pnl_pct = None
        pnl_abs = None
        if last_close is not None:
            pnl_pct = (last_close - avg_price) / avg_price * 100
            pnl_abs = (last_close - avg_price) * qty
        result.append({
            "symbol": r[0],
            "avg_price": avg_price,
            "qty": qty,
            "added_at": int(r[3]),
            "notes": r[4],
            "market": r[5],
            "last_close": last_close,
            "signal_value": r[7],
            "signal_score": r[8],
            "bnf_signal_value": r[9],
            "bnf_signal_score": r[10],
            "pattern_json": r[11],
            "pattern_signal": r[12],
            "pattern_score": r[13],
            "generated_at": r[14],
            "pnl_pct": pnl_pct,
            "pnl_abs": pnl_abs,
        })
    return result


def get_holding_with_pnl(username: str, symbol: str) -> dict | None:
    """단일 종목 — list_holdings_with_pnl 의 단일판."""
    for h in list_holdings_with_pnl(username):
        if h["symbol"] == symbol:
            return h
    return None
