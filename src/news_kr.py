"""news_kr — Naver Finance 종목별 뉴스 크롤링.

기존 yfinance fetch_news 가 한국 종목 뉴스를 못 받는 문제를 해결.
data_fetcher.fetch_news 가 symbol suffix 로 dispatch.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

_NAVER_NEWS_API = "https://m.stock.naver.com/api/news/stock/{code}"
_HTTP_TIMEOUT = 10
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9",
}


def _scrape_naver_finance(krx_code: str) -> list[dict] | None:
    """Naver Mobile API 로 종목 뉴스 조회.

    Returns:
        list of dict — 성공 (뉴스 0건이면 빈 list)
        None — HTTP 실패 또는 JSON 형식 변경 (parse 실패)
    """
    url = _NAVER_NEWS_API.format(code=krx_code)
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_HTTP_TIMEOUT)
    except Exception as e:
        logger.warning("Naver fetch 실패 [%s]: %s", krx_code, e)
        return None

    if resp.status_code != 200:
        logger.warning("Naver HTTP %d [%s]", resp.status_code, krx_code)
        return None

    try:
        groups = resp.json()
    except ValueError as e:
        logger.warning("Naver JSON parse 실패 [%s]: %s", krx_code, e)
        return None

    if not isinstance(groups, list):
        logger.warning("Naver JSON 구조 변경 감지 [%s] — list 아님", krx_code)
        return None

    items: list[dict] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        for raw in (group.get("items") or []):
            if not isinstance(raw, dict):
                continue
            title = (raw.get("titleFull") or raw.get("title") or "").strip()
            if not title:
                continue
            body = (raw.get("body") or "").strip()
            if len(body) > 200:
                body = body[:200].rsplit(" ", 1)[0] + "..."
            office = raw.get("officeName", "")
            office_id = raw.get("officeId", "")
            article_id = raw.get("articleId", "")
            link = raw.get("mobileNewsUrl") or (
                f"https://n.news.naver.com/mnews/article/{office_id}/{article_id}"
                if office_id and article_id else ""
            )
            dt = raw.get("datetime", "")
            published = (
                f"{dt[0:4]}-{dt[4:6]}-{dt[6:8]} {dt[8:10]}:{dt[10:12]}"
                if isinstance(dt, str) and len(dt) >= 12 else ""
            )
            items.append({
                "title": title,
                "title_en": "",
                "link": link,
                "publisher": office,
                "published": published,
                "summary": body,         # Mobile API 가 body 제공
                "summary_en": "",
            })
    return items


_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "news_cache"
_NEWS_CACHE_TTL = 3600  # 1h


def _cache_path(symbol: str) -> Path:
    return _CACHE_DIR / f"{symbol}.json"


def _cache_get(symbol: str) -> list[dict] | None:
    """캐시 hit 이면 items list, miss/만료/corrupt 이면 None.
    빈 list 도 정상 캐시 hit (성공+0건 시나리오)."""
    path = _cache_path(symbol)
    try:
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    fetched_at = payload.get("fetched_at", 0)
    if time.time() - fetched_at > _NEWS_CACHE_TTL:
        return None
    return payload.get("items", [])


def _cache_put(symbol: str, items: list[dict]) -> None:
    """atomic write: .tmp → os.replace → .json.
    호출자는 scrape 실패(None) 시 이 함수를 호출하지 않아야 한다 (Task 4 fetch_news_kr 책임)."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(symbol)
    tmp_path = path.with_suffix(".tmp")
    payload = {
        "fetched_at": int(time.time()),
        "symbol": symbol,
        "items": items,
    }
    tmp_path.write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    os.replace(tmp_path, path)


import random  # noqa: E402  (module-level appended after initial imports)

_KR_FINANCE_GLOSSARY = {
    "상한가": "Upper limit (+30%)",
    "하한가": "Lower limit (-30%)",
    "어닝 쇼크": "earnings miss",
    "어닝 서프라이즈": "earnings beat",
    "신고가": "new high",
    "신저가": "new low",
    "급등": "surge",
    "급락": "plunge",
    "감자": "capital reduction",
    "증자": "capital increase",
}

_TRANSLATE_SLEEP = 0.3
_TRANSLATE_BACKOFF = 10


def _preprocess_kr(text: str) -> str:
    """번역 직전 한국 금융 은어를 영어 표현으로 치환."""
    for kr, en in _KR_FINANCE_GLOSSARY.items():
        text = text.replace(kr, en)
    return text


def _translate_ko_to_en(text: str) -> str:
    """data_fetcher 의 cached translator 로 위임."""
    from src.data_fetcher import _translate_ko_to_en_cached
    return _translate_ko_to_en_cached(text)


def _translate_with_backoff(text: str) -> str:
    """번역 실패 시 한국어 원본 반환. 429/rate-limit 검출 시 backoff sleep."""
    if not text:
        return text
    preprocessed = _preprocess_kr(text)
    try:
        return _translate_ko_to_en(preprocessed)
    except Exception as e:
        msg = str(e)
        if "429" in msg or "rate" in msg.lower() or "too many" in msg.lower():
            logger.warning(
                "번역 429/rate-limit — %ds backoff 후 fallback", _TRANSLATE_BACKOFF,
            )
            time.sleep(_TRANSLATE_BACKOFF)
        else:
            logger.warning("번역 실패 (한국어 원본 유지): %s", e)
        return text


_SCRAPE_SLEEP_MIN = 0.5
_SCRAPE_SLEEP_MAX = 1.5


def _normalize_krx_code(symbol: str) -> str:
    """`.KS`/`.KQ` 제거 후 6자리 zero-padding."""
    return symbol.split(".")[0].zfill(6)


def fetch_news_kr(symbol: str, max_items: int = 10) -> list[dict]:
    """Naver Finance 종목 뉴스 fetch + 1h 캐시 + ko→en 번역.

    빈 list 반환 조건:
    - 실제 뉴스가 0건 (성공) — 캐시됨
    - HTTP/parse 실패 — 캐시 안 됨 (다음 호출에서 재시도)

    호출자가 캐시 vs 실패를 구별할 필요 없음 (둘 다 빈 list).
    """
    cached = _cache_get(symbol)
    if cached is not None:
        return cached[:max_items]

    krx_code = _normalize_krx_code(symbol)
    items = _scrape_naver_finance(krx_code)
    if items is None:
        # parse 실패 — 캐시 skip
        return []

    # 번역 (각 item title)
    for item in items:
        item["title_en"] = _translate_with_backoff(item["title"])
        time.sleep(_TRANSLATE_SLEEP)

    _cache_put(symbol, items)

    # 다음 종목 호출 전 jitter sleep (WAF 회피)
    time.sleep(random.uniform(_SCRAPE_SLEEP_MIN, _SCRAPE_SLEEP_MAX))

    return items[:max_items]
