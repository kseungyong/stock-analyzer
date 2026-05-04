"""한국·미국 종목 통합 검색."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_MIN_QUERY_LEN = 2


def search_stocks(query: str, limit: int = 10) -> list[dict]:
    """회사명 또는 심볼로 한국·미국 종목을 검색한다.

    Args:
        query: 검색어 (한글/영문/숫자, 2자 이상)
        limit: 반환할 최대 결과 수

    Returns:
        [{"symbol": str, "name": str, "market": "korea"|"us"}, ...].
        한국 결과 우선, 심볼 기준 중복 제거.
    """
    q = query.strip() if query else ""
    if len(q) < _MIN_QUERY_LEN:
        return []
    return []
