"""한국·미국 종목 통합 검색."""
from __future__ import annotations

import logging
import threading
import time

import pandas as pd

logger = logging.getLogger(__name__)

_MIN_QUERY_LEN = 2
_KRX_TTL_SECONDS = 24 * 3600

_krx_cache: dict = {"loaded_at": None, "data": []}
_krx_lock = threading.Lock()


def _fetch_krx_listing() -> pd.DataFrame:
    """FinanceDataReader에서 전체 KRX 종목 목록을 받는다 (외부 호출 격리용)."""
    import FinanceDataReader as fdr
    return fdr.StockListing("KRX")


def _load_krx_cache() -> list[dict]:
    """KRX 캐시를 TTL 내면 재사용, 만료되면 새로 받는다.

    Returns:
        [{"symbol": "005930.KS", "name": "삼성전자", "market": "korea"}, ...]
        — 실패 시 빈 리스트.
    """
    now = time.time()
    loaded_at = _krx_cache["loaded_at"]
    if loaded_at is not None and (now - loaded_at) < _KRX_TTL_SECONDS:
        return list(_krx_cache["data"])

    with _krx_lock:
        loaded_at = _krx_cache["loaded_at"]
        if loaded_at is not None and (now - loaded_at) < _KRX_TTL_SECONDS:
            return list(_krx_cache["data"])
        try:
            df = _fetch_krx_listing()
            data = []
            for _, row in df.iterrows():
                code = str(row.get("Code", "")).strip()
                name = str(row.get("Name", "")).strip()
                market = str(row.get("Market", "")).strip()
                if not code or not name:
                    continue
                suffix = ".KQ" if "KOSDAQ" in market.upper() else ".KS"
                data.append({"symbol": f"{code}{suffix}", "name": name, "market": "korea"})
            _krx_cache["data"] = data
            _krx_cache["loaded_at"] = now
            logger.info("KRX 캐시 로드 완료: %d 종목", len(data))
            return list(data)
        except Exception as e:
            logger.warning("KRX 캐시 로드 실패: %s", e)
            return []


def _search_kr(query: str, limit: int) -> list[dict]:
    """KRX 캐시에서 종목명 substring 또는 심볼 prefix 매칭."""
    data = _load_krx_cache()
    if not data:
        return []
    q_lower = query.lower()
    results = []
    for item in data:
        name = item["name"]
        symbol = item["symbol"]
        code = symbol.split(".")[0]
        if q_lower in name.lower() or code.upper().startswith(query.upper()):
            results.append(item)
            if len(results) >= limit:
                break
    return results


def _search_us(query: str, limit: int) -> list[dict]:
    """미국 종목 검색 (Task 4에서 구현)."""
    return []


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
    kr = _search_kr(q, limit)
    us = _search_us(q, limit)
    seen = set()
    combined = []
    for item in (*kr, *us):
        if item["symbol"] in seen:
            continue
        seen.add(item["symbol"])
        combined.append(item)
        if len(combined) >= limit:
            break
    return combined
