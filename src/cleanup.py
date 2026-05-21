"""cleanup — 관심 가치 떨어진 종목 자동 정리.

조건 (모두 AND):
1. composite < -5 (Tech + BNF + Pattern×0.5)
2. 최근 7일 (5+ row) 모두 조건 1 만족
3. ETF/인덱스 아님
4. portfolio 보유 중 아님
5. settings.yaml 에 pinned 또는 note 없음
"""
from __future__ import annotations

import logging

from src import composite_history

logger = logging.getLogger(__name__)

_ETF_PREFIXES = ("KODEX", "TIGER", "ARIRANG", "KBSTAR", "HANARO")
_ETF_SYMBOLS = {
    "SPY", "QQQ", "VTI", "VOO", "IWM",
    "KORU", "QLD", "TECL", "USD", "SOXL", "TQQQ",
}

_COMPOSITE_THRESHOLD = -5.0
_MIN_HISTORY_ROWS = 5
_SAFETY_LIMIT = 10


def is_etf(symbol: str, name: str) -> bool:
    """ETF/인덱스 식별. symbol 화이트리스트 또는 name prefix 매칭."""
    sym_upper = symbol.upper()
    if sym_upper in _ETF_SYMBOLS:
        return True
    name_upper = name.upper()
    return any(name_upper.startswith(p) for p in _ETF_PREFIXES)


def should_remove(
    symbol: str,
    name: str,
    history_rows: list[tuple[int, float]],
    is_held: bool,
    is_pinned_or_noted: bool,
) -> bool:
    """5개 조건 AND 판정. True면 삭제 후보."""
    if is_etf(symbol, name):
        return False
    if is_held:
        return False
    if is_pinned_or_noted:
        return False
    if len(history_rows) < _MIN_HISTORY_ROWS:
        return False
    return all(composite < _COMPOSITE_THRESHOLD for _, composite in history_rows)


def find_candidates(config: dict, held_symbols: set[str]) -> list[dict]:
    """settings.yaml 의 모든 종목을 검사하여 삭제 후보 list 반환.

    Args:
        config: yaml.safe_load 된 settings.yaml dict
        held_symbols: portfolio 보유 종목 symbol set

    Returns:
        [{"symbol", "name", "market", "composite_avg", "days"}, ...]
    """
    candidates = []
    stocks_by_market = config.get("stocks", {})
    for market, group in stocks_by_market.items():
        for stock in group:
            symbol = stock["symbol"]
            name = stock["name"]
            is_held = symbol in held_symbols
            is_pinned_or_noted = bool(
                stock.get("pinned") or stock.get("note")
            )
            history_rows = composite_history.recent(symbol, days=7)
            if not should_remove(
                symbol, name, history_rows, is_held, is_pinned_or_noted,
            ):
                continue
            composites = [c for _, c in history_rows]
            candidates.append({
                "symbol": symbol,
                "name": name,
                "market": market,
                "composite_avg": sum(composites) / len(composites),
                "days": len(history_rows),
            })
    return candidates
