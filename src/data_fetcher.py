from __future__ import annotations

import time
import yfinance as yf
import pandas as pd
import re
from datetime import datetime, timedelta

from deep_translator import GoogleTranslator

_translator = GoogleTranslator(source="en", target="ko")


def _is_english(text: str) -> bool:
    if not text:
        return False
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    return ascii_chars / len(text) > 0.7


def _translate(text: str) -> str:
    if not text or not _is_english(text):
        return text
    try:
        return _translator.translate(text)
    except Exception:
        return text


def fetch_stock_data(symbol: str, period_days: int = 365, retries: int = 2) -> pd.DataFrame:
    """주가 데이터를 yfinance로 수집한다.

    Args:
        symbol: 종목 코드 (예: '005930.KS', 'AAPL')
        period_days: 수집할 과거 일수
        retries: 실패 시 재시도 횟수

    Returns:
        OHLCV 데이터프레임
    """
    end = datetime.now()
    start = end - timedelta(days=period_days)
    last_exc: Exception = ValueError(f"No data found for {symbol}")

    for attempt in range(retries + 1):
        try:
            ticker = yf.Ticker(symbol)
            df = ticker.history(start=start, end=end)
            if df.empty:
                raise ValueError(f"No data found for {symbol}")
            df.index = pd.to_datetime(df.index).tz_localize(None)
            return df
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(1)

    raise last_exc


def fetch_news(symbol: str, max_items: int = 10) -> list[dict]:
    """yfinance에서 종목 관련 뉴스를 수집한다.

    Returns:
        [{"title": ..., "link": ..., "publisher": ..., "published": ...}, ...]
    """
    try:
        ticker = yf.Ticker(symbol)
        news = ticker.news or []
        results = []
        for item in news[:max_items]:
            content = item.get("content", {})
            summary = content.get("summary", "")
            # HTML 태그 제거
            summary = re.sub(r"<[^>]+>", "", summary).strip()
            # 너무 길면 자르기
            if len(summary) > 200:
                summary = summary[:200].rsplit(" ", 1)[0] + "..."
            title = content.get("title", "")
            results.append({
                "title": _translate(title),
                "title_en": title,
                "link": content.get("canonicalUrl", {}).get("url", ""),
                "publisher": content.get("provider", {}).get("displayName", ""),
                "published": content.get("pubDate", ""),
                "summary": _translate(summary),
                "summary_en": summary,
            })
        return results
    except Exception as e:
        print(f"  [뉴스 수집 오류] {symbol}: {e}")
        return []


def fetch_multiple(stocks: list[dict], period_days: int = 365) -> dict[str, pd.DataFrame]:
    """여러 종목 데이터를 수집한다.

    Args:
        stocks: [{"symbol": "AAPL", "name": "Apple"}, ...]
        period_days: 수집할 과거 일수

    Returns:
        {symbol: DataFrame} 딕셔너리
    """
    results = {}
    for stock in stocks:
        symbol = stock["symbol"]
        try:
            results[symbol] = fetch_stock_data(symbol, period_days)
            print(f"  [OK] {stock['name']} ({symbol})")
        except Exception as e:
            print(f"  [FAIL] {stock['name']} ({symbol}): {e}")
    return results
