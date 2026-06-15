"""토스 holdings → portfolio 미러링.

toss_client(외부 I/O) 와 분리된 비즈니스 로직. 토스 API 없이 단위 테스트 가능.
"""
from __future__ import annotations

import logging
import os

from src import portfolio as portfolio_db

logger = logging.getLogger(__name__)


def _krx_listing() -> dict[str, str]:
    """{6자리코드: '.KS'|'.KQ'} 매핑. stock_search 캐시 재사용."""
    from src.stock_search import _load_krx_cache
    out: dict[str, str] = {}
    for item in _load_krx_cache():
        sym = item.get("symbol", "")  # 예: '005930.KS'
        if sym.endswith((".KS", ".KQ")) and len(sym) > 3:
            code, suffix = sym[:-3], sym[-3:]
            out[code] = suffix
    return out


def _to_sa_symbol(holding: dict) -> str | None:
    """토스 holding → stock-analyzer symbol. 변환 불가 시 None."""
    sym = str(holding.get("symbol", "")).strip()
    if not sym:
        return None
    country = str(holding.get("marketCountry", "")).strip().upper()
    if country == "US":
        return sym
    if country == "KR":
        suffix = _krx_listing().get(sym, ".KS")
        return f"{sym}{suffix}"
    logger.warning("알 수 없는 marketCountry=%s symbol=%s — skip", country, sym)
    return None
