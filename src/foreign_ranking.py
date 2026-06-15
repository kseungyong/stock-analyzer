"""외인/기관/연기금 순매수 ranking 발굴 + universe push.

KIS API foreign-institution-total → 매일 30 종목 (합산 순매수 상위) →
SQLite 누적 (5일 누적 계산) → config/foreign_ranking.yaml (overlay) 에
source=foreign_ranking 으로 push. settings.yaml 은 건드리지 않으므로 매일 갱신이
git working tree 를 더럽히지 않는다 (src.universe 참고).

스케줄: 매일 16:00 KST (장 마감 30분 후), 후속 16:30 korea-analysis 가 자동 분석.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date as date_cls
from pathlib import Path

import yaml

from src.kis_client import KISClient

logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "predictions.db"
_SETTINGS_PATH = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
_SOURCE_TAG = "foreign_ranking"

# 투자자 키 → (KIS 필드 prefix, 표시명)
INVESTORS = {
    "foreign": ("frgn", "외인"),
    "institution": ("orgn", "기관"),
    "pension": ("fund", "연기금"),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS foreign_ranking_history (
    snap_date  TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    name       TEXT NOT NULL,
    frgn_qty   INTEGER NOT NULL,
    frgn_val   INTEGER NOT NULL,
    orgn_qty   INTEGER NOT NULL,
    orgn_val   INTEGER NOT NULL,
    fund_qty   INTEGER NOT NULL,
    fund_val   INTEGER NOT NULL,
    PRIMARY KEY (snap_date, symbol)
);
CREATE INDEX IF NOT EXISTS idx_fr_date ON foreign_ranking_history(snap_date);
"""

_db_lock = threading.Lock()


@contextmanager
def _db_conn():
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    try:
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _to_int(v) -> int:
    """KIS 응답값 (str) → int. 빈 값 / 음수 / 콤마 처리."""
    if v is None or v == "":
        return 0
    s = str(v).replace(",", "").strip()
    try:
        return int(s)
    except ValueError:
        try:
            return int(float(s))
        except ValueError:
            return 0


@dataclass
class RankingRow:
    symbol: str
    name: str
    frgn_qty: int
    frgn_val: int  # 백만원 단위 (KIS 응답)
    orgn_qty: int
    orgn_val: int
    fund_qty: int
    fund_val: int

    @classmethod
    def from_kis(cls, raw: dict) -> "RankingRow":
        return cls(
            symbol=raw.get("mksc_shrn_iscd", "").strip(),
            name=raw.get("hts_kor_isnm", "").strip(),
            frgn_qty=_to_int(raw.get("frgn_ntby_qty")),
            frgn_val=_to_int(raw.get("frgn_ntby_tr_pbmn")),
            orgn_qty=_to_int(raw.get("orgn_ntby_qty")),
            orgn_val=_to_int(raw.get("orgn_ntby_tr_pbmn")),
            fund_qty=_to_int(raw.get("fund_ntby_qty")),
            fund_val=_to_int(raw.get("fund_ntby_tr_pbmn")),
        )


def fetch_today(client: KISClient | None = None) -> list[RankingRow]:
    """KIS 호출 1회 → 합산 순매수 상위 30 종목 (외인+기관 양쪽 정보 동시)."""
    if client is None:
        with KISClient() as c:
            raw = c.fetch_foreign_institution_total(sort_code="0")
    else:
        raw = client.fetch_foreign_institution_total(sort_code="0")
    rows = [RankingRow.from_kis(r) for r in raw if r.get("mksc_shrn_iscd")]
    logger.info("KIS foreign-institution-total — %d rows", len(rows))
    return rows


def save_snapshot(snap: date_cls, rows: list[RankingRow]) -> int:
    """일자별 ranking 저장. (snap_date, symbol) UPSERT. 반환: 저장된 row 수."""
    with _db_lock, _db_conn() as conn:
        date_str = snap.isoformat()
        for r in rows:
            conn.execute(
                "INSERT INTO foreign_ranking_history "
                "(snap_date, symbol, name, frgn_qty, frgn_val, "
                " orgn_qty, orgn_val, fund_qty, fund_val) "
                "VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(snap_date, symbol) DO UPDATE SET "
                "  name=excluded.name, "
                "  frgn_qty=excluded.frgn_qty, frgn_val=excluded.frgn_val, "
                "  orgn_qty=excluded.orgn_qty, orgn_val=excluded.orgn_val, "
                "  fund_qty=excluded.fund_qty, fund_val=excluded.fund_val",
                (date_str, r.symbol, r.name,
                 r.frgn_qty, r.frgn_val, r.orgn_qty, r.orgn_val,
                 r.fund_qty, r.fund_val),
            )
    return len(rows)


def top_n_by_investor(
    snap: date_cls,
    investor: str,
    *,
    period_days: int = 1,
    n: int = 10,
) -> list[dict]:
    """투자자별 top N. period_days=1 일별, period_days=5 5일 누적.

    Returns list of {symbol, name, qty, val} sorted by val desc.
    """
    if investor not in INVESTORS:
        raise ValueError(f"unknown investor: {investor}")
    prefix, _label = INVESTORS[investor]
    qty_col, val_col = f"{prefix}_qty", f"{prefix}_val"

    from datetime import timedelta
    start = (snap - timedelta(days=period_days - 1)).isoformat()
    end = snap.isoformat()

    with _db_conn() as conn:
        cur = conn.execute(
            f"SELECT symbol, MAX(name) AS name, "
            f"       SUM({qty_col}) AS qty, SUM({val_col}) AS val "
            f"FROM foreign_ranking_history "
            f"WHERE snap_date BETWEEN ? AND ? "
            f"GROUP BY symbol "
            f"HAVING val > 0 "
            f"ORDER BY val DESC LIMIT ?",
            (start, end, n),
        )
        return [{"symbol": s, "name": nm, "qty": int(q or 0), "val": int(v or 0)}
                for s, nm, q, v in cur.fetchall()]


def compute_union_top(snap: date_cls, n: int = 10) -> list[tuple[str, str]]:
    """3 투자자 × 2 기간 = 6 ranking 의 union → (symbol, name) 리스트.

    중복 제거 후 최대 60 → 실측 ~20-40 종목 추정.
    """
    seen: dict[str, str] = {}  # symbol → name
    for investor in INVESTORS:
        for period in (1, 5):
            for row in top_n_by_investor(snap, investor, period_days=period, n=n):
                seen.setdefault(row["symbol"], row["name"])
    return list(seen.items())


def push_to_overlay(symbols: list[tuple[str, str]]) -> tuple[int, int]:
    """config/foreign_ranking.yaml (overlay) 에 source=foreign_ranking 으로 push.

    overlay 는 매번 전체 교체. settings.yaml (사용자 등록) 은 읽기 전용으로만 참조하여
    이미 사용자가 직접 등록한 symbol 은 overlay 에서 제외 (중복 방지).
    settings.yaml 자체는 수정하지 않는다.

    Returns: (이전 overlay 종목 수, 새 overlay 종목 수)
    """
    from src import universe

    user_symbols: set[str] = set()
    if _SETTINGS_PATH.exists():
        cfg = yaml.safe_load(_SETTINGS_PATH.read_text(encoding="utf-8")) or {}
        user_symbols = {
            s["symbol"] for s in cfg.get("stocks", {}).get("korea", [])
        }

    removed = len(universe.load_overlay().get("korea", []))

    entries: list[dict] = []
    for symbol_6, name in symbols:
        ks_symbol = f"{symbol_6}.KS"  # KIS 응답은 6자리, stock-analyzer 는 .KS suffix
        if ks_symbol in user_symbols:
            # 이미 사용자가 직접 등록 — overlay 에 중복 추가 안 함
            continue
        entries.append({
            "symbol": ks_symbol,
            "name": name,
            "source": _SOURCE_TAG,
        })

    universe.write_overlay({"korea": entries})
    added = len(entries)
    logger.info("foreign_ranking overlay push — removed=%d added=%d", removed, added)
    return removed, added


def run_daily(snap: date_cls | None = None) -> dict:
    """전체 흐름: fetch → save → compute union → push universe.

    launchd 16:00 잡의 entry point. main.py 의 `foreign-ranking` 서브커맨드에서 호출.
    """
    snap = snap or date_cls.today()
    rows = fetch_today()
    saved = save_snapshot(snap, rows)
    union = compute_union_top(snap, n=10)
    removed, added = push_to_overlay(union)
    return {
        "snap_date": snap.isoformat(),
        "fetched": len(rows),
        "saved": saved,
        "union_count": len(union),
        "removed": removed,
        "added": added,
    }
