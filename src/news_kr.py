"""news_kr — Naver Finance 종목별 뉴스 크롤링.

기존 yfinance fetch_news 가 한국 종목 뉴스를 못 받는 문제를 해결.
data_fetcher.fetch_news 가 symbol suffix 로 dispatch.
"""
from __future__ import annotations

import logging
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_NAVER_BASE = "https://finance.naver.com"
_NAVER_NEWS_URL = _NAVER_BASE + "/item/news_news.naver?code={code}&page=1"
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
    """Naver Finance 종목 뉴스 list 페이지 크롤링.

    Returns:
        list of dict — 성공 (뉴스 0건이면 빈 list)
        None — HTTP 실패 또는 HTML 구조 변경 (parse 실패)
    """
    url = _NAVER_NEWS_URL.format(code=krx_code)
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_HTTP_TIMEOUT)
    except Exception as e:
        logger.warning("Naver fetch 실패 [%s]: %s", krx_code, e)
        return None

    if resp.status_code != 200:
        logger.warning("Naver HTTP %d [%s]", resp.status_code, krx_code)
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table", class_="type5")
    if table is None:
        logger.warning("Naver HTML 구조 변경 감지 [%s] — table.type5 없음", krx_code)
        return None

    tbody = table.find("tbody")
    if tbody is None:
        logger.warning("Naver HTML 구조 변경 감지 [%s] — tbody 없음", krx_code)
        return None

    items: list[dict] = []
    for tr in tbody.find_all("tr"):
        a = tr.find("td", class_="title")
        info = tr.find("td", class_="info")
        date = tr.find("td", class_="date")
        if a is None or info is None or date is None:
            continue
        link_tag = a.find("a")
        if link_tag is None:
            continue
        title = link_tag.get_text(strip=True)
        href = link_tag.get("href", "")
        link = urljoin(_NAVER_BASE, href)
        publisher = info.get_text(strip=True)
        # "2026.05.22 14:23" → "2026-05-22 14:23"
        published = date.get_text(strip=True).replace(".", "-", 2)
        items.append({
            "title": title,
            "title_en": "",       # Task 3 에서 채움
            "link": link,
            "publisher": publisher,
            "published": published,
            "summary": "",
            "summary_en": "",
        })
    return items
