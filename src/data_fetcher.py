from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
import yfinance as yf
import FinanceDataReader as fdr
from pykrx import stock as pykrx_stock
import pandas as pd
import re
from datetime import datetime, timedelta

from deep_translator import GoogleTranslator
from src.news_kr import fetch_news_kr
from src.toss_client import TossClient

logger = logging.getLogger(__name__)

_translator = GoogleTranslator(source="en", target="ko")
_translator_ko_en = GoogleTranslator(source="ko", target="en")


@lru_cache(maxsize=512)
def _translate_ko_to_en_cached(text: str) -> str:
    """한국어 → 영어 번역. 결과 캐싱."""
    return _translator_ko_en.translate(text)


def _is_english(text: str) -> bool:
    if not text:
        return False
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    return ascii_chars / len(text) > 0.7


@lru_cache(maxsize=512)
def _translate_cached(text: str) -> str:
    """번역 결과를 캐싱한다. 동일 텍스트 반복 API 호출을 방지한다."""
    return _translator.translate(text)


def _translate(text: str) -> str:
    if not text or not _is_english(text):
        return text
    try:
        return _translate_cached(text)
    except Exception:
        return text


def _to_krx_code(symbol: str) -> str:
    """yfinance 스타일 심볼을 KRX 종목코드로 변환한다. (예: '005930.KS' → '005930')"""
    return symbol.split(".")[0]


def _candles_to_df(candles: list[dict]) -> pd.DataFrame:
    """토스 candle 리스트 → yfinance 스타일 DataFrame (Open/High/Low/Close/Volume).

    index: timestamp → tz-naive, 날짜 normalize, 오름차순 정렬.
    빈 입력은 ValueError (fetch_stock_data 가 폴백 트리거).
    """
    if not candles:
        raise ValueError("토스 candles 비어있음")
    idx = pd.to_datetime([c["timestamp"] for c in candles])
    data = {
        "Open": [float(c["openPrice"]) for c in candles],
        "High": [float(c["highPrice"]) for c in candles],
        "Low": [float(c["lowPrice"]) for c in candles],
        "Close": [float(c["closePrice"]) for c in candles],
        "Volume": [float(c["volume"]) for c in candles],
    }
    df = pd.DataFrame(data, index=idx)
    # tz-aware(+09:00) → tz-naive + 날짜 normalize (시각 제거, 일봉이므로 날짜만 유효)
    df.index = df.index.tz_localize(None).normalize()
    return df.sort_index()


def _required_count(period_days: int) -> int:
    """period_days(캘린더 일수) → 필요한 거래일 봉 수 추정. 영업일 비율 ~0.69 에 여유."""
    return int(period_days * 0.75) + 10


def _fetch_with_toss(symbol: str, period_days: int) -> pd.DataFrame:
    """토스 candles 로 일봉 수집 → DataFrame. .KS/.KQ 는 6자리 코드로 변환."""
    toss_symbol = _to_krx_code(symbol) if symbol.endswith((".KS", ".KQ")) else symbol
    count = _required_count(period_days)
    with TossClient() as client:
        candles = client.fetch_candles(toss_symbol, interval="1d", count=count)
    return _candles_to_df(candles)   # 빈 candles 면 ValueError


def _fetch_with_fdr(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    """FinanceDataReader로 주가 데이터를 수집한다."""
    fdr_symbol = _to_krx_code(symbol) if symbol.endswith((".KS", ".KQ")) else symbol
    df = fdr.DataReader(fdr_symbol, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
    if df.empty:
        raise ValueError(f"No data found via FDR for {symbol}")
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df


def fetch_stock_data(symbol: str, period_days: int = 365, retries: int = 2) -> pd.DataFrame:
    """주가 데이터를 토스 candles 로 수집하고, 실패 시 FinanceDataReader 로 폴백한다.

    Args:
        symbol: 종목 코드 (예: '005930.KS', 'AAPL')
        period_days: 수집할 과거 일수
        retries: 토스 실패 시 재시도 횟수

    Returns:
        OHLCV 데이터프레임 (Open/High/Low/Close/Volume, tz-naive 오름차순 index)
    """
    end = datetime.now()
    start = end - timedelta(days=period_days)

    # 1차: 토스 candles 시도
    for attempt in range(retries + 1):
        try:
            df = _fetch_with_toss(symbol, period_days)
            if df.empty:
                raise ValueError(f"토스 데이터 없음 {symbol}")
            logger.info("데이터 수집 완료 [토스]: %s", symbol)
            return df
        except Exception as exc:
            if "TOSS_CLIENT" in str(exc):
                # 자격증명 미설정 — 재시도 무의미, 즉시 FDR 폴백 (CI/키 없는 환경)
                logger.warning("토스 자격증명 미설정 [%s] — FDR 폴백", symbol)
                break
            if attempt < retries:
                time.sleep(1)
            else:
                logger.warning("토스 실패 [%s]: %s — FinanceDataReader로 폴백", symbol, exc)

    # 2차: FinanceDataReader 폴백
    try:
        df = _fetch_with_fdr(symbol, start, end)
        logger.info("데이터 수집 완료 [FinanceDataReader]: %s", symbol)
        return df
    except Exception as fdr_exc:
        raise ValueError(
            f"토스 및 FinanceDataReader 모두 실패 [{symbol}]: {fdr_exc}"
        ) from fdr_exc


def fetch_institutional_data(symbol: str, period_days: int = 90) -> pd.DataFrame:
    """pykrx를 사용해 외국인/기관 순매수 데이터를 수집한다.

    Args:
        symbol: 종목 코드 (예: '005930.KS' 또는 '005930')
        period_days: 수집할 과거 일수

    Returns:
        외국인·기관 순매수 데이터프레임
    """
    krx_code = _to_krx_code(symbol)
    end = datetime.now()
    start = end - timedelta(days=period_days)
    start_str = start.strftime("%Y%m%d")
    end_str = end.strftime("%Y%m%d")

    try:
        df = pykrx_stock.get_market_trading_volume_by_date(
            start_str, end_str, krx_code
        )
        if df.empty:
            logger.warning("외인/기관 데이터 없음: %s", symbol)
            return pd.DataFrame()
        df.index = pd.to_datetime(df.index).tz_localize(None)
        logger.info("외인/기관 데이터 수집 완료 [pykrx]: %s (%d일)", symbol, period_days)
        return df
    except Exception as e:
        logger.warning("외인/기관 데이터 수집 실패 [%s]: %s", symbol, e)
        return pd.DataFrame()


def fetch_news(symbol: str, max_items: int = 10) -> list[dict]:
    """종목 관련 뉴스 수집. 한국 종목(.KS/.KQ)은 Naver Finance, 그 외는 yfinance.

    Returns:
        [{"title", "title_en", "link", "publisher", "published",
          "summary", "summary_en"}, ...]
    """
    if symbol.endswith((".KS", ".KQ")):
        return fetch_news_kr(symbol, max_items=max_items)

    try:
        ticker = yf.Ticker(symbol)
        news = ticker.news or []
        results = []
        for item in news[:max_items]:
            content = item.get("content", {})
            summary = content.get("summary", "")
            summary = re.sub(r"<[^>]+>", "", summary).strip()
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
        logger.warning("뉴스 수집 오류 [%s]: %s", symbol, e)
        return []


def fetch_multiple(stocks: list[dict], period_days: int = 365) -> dict[str, pd.DataFrame]:
    """여러 종목 데이터를 병렬로 수집한다.

    Args:
        stocks: [{"symbol": "AAPL", "name": "Apple"}, ...]
        period_days: 수집할 과거 일수

    Returns:
        {symbol: DataFrame} 딕셔너리
    """
    results = {}

    def _fetch(stock: dict) -> tuple[str, pd.DataFrame | None]:
        symbol = stock["symbol"]
        try:
            df = fetch_stock_data(symbol, period_days)
            logger.info("데이터 수집 완료: %s (%s)", stock["name"], symbol)
            return symbol, df
        except Exception as e:
            logger.error("데이터 수집 실패: %s (%s): %s", stock["name"], symbol, e)
            return symbol, None

    with ThreadPoolExecutor(max_workers=min(len(stocks), 8)) as executor:
        futures = {executor.submit(_fetch, stock): stock for stock in stocks}
        for future in as_completed(futures):
            symbol, df = future.result()
            if df is not None:
                results[symbol] = df

    return results
