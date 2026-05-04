"""외인/기관 수급 스캐너

FinanceDataReader로 KRX 종목 모멘텀 스캔 (pykrx API 불안정 대체)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pandas as pd
import FinanceDataReader as fdr

logger = logging.getLogger(__name__)


def _get_trading_days(days: int = 20) -> tuple[str, str]:
    """최근 N 영업일 기준 시작/종료일 반환 (YYYY-MM-DD)."""
    end = datetime.now()
    # 주말 포함 여유있게 계산
    start = end - timedelta(days=int(days * 1.8))
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def scan_momentum(days: int = 20, top_n: int = 20, market: str = "KOSPI") -> list[dict]:
    """최근 N일간 주가 상승률 + 거래량 급증 상위 종목 스캔.

    Returns:
        list of {code, name, close, change_pct, volume_ratio, score}
    """
    try:
        listing = fdr.StockListing(market)
        codes = listing["Code"].tolist()[:200]  # 상위 200개 (속도)
        names = dict(zip(listing["Code"], listing["Name"]))
    except Exception as e:
        logger.error("종목 목록 조회 실패: %s", e)
        return []

    start_date, end_date = _get_trading_days(days)
    results = []

    for code in codes:
        try:
            df = fdr.DataReader(code, start_date, end_date)
            if df is None or len(df) < 5:
                continue

            # 상승률 (기간 전체)
            change_pct = (df["Close"].iloc[-1] - df["Close"].iloc[0]) / df["Close"].iloc[0] * 100

            # 최근 5일 거래량 vs 이전 거래량 비율
            recent_vol = df["Volume"].iloc[-5:].mean()
            prev_vol = df["Volume"].iloc[:-5].mean() if len(df) > 5 else recent_vol
            volume_ratio = recent_vol / prev_vol if prev_vol > 0 else 1.0

            # 종합 점수 (상승률 + 거래량 급증)
            score = change_pct * 0.6 + (volume_ratio - 1) * 20

            results.append({
                "code": code,
                "name": names.get(code, code),
                "close": int(df["Close"].iloc[-1]),
                "change_pct": round(change_pct, 2),
                "volume_ratio": round(volume_ratio, 2),
                "score": round(score, 2),
            })
        except Exception:
            continue

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top_n]


def scan_supply(days: int = 5, top_n: int = 20) -> list[dict]:
    """pykrx 대신 FDR 기반 수급 대체 스캔.
    
    거래량 급등 + 연속 상승 종목 = 외인/기관 매집 추정
    """
    start_date, end_date = _get_trading_days(days + 5)

    try:
        listing = fdr.StockListing("KOSPI")
        codes = listing["Code"].tolist()[:300]
        names = dict(zip(listing["Code"], listing["Name"]))
    except Exception as e:
        logger.error("종목 목록 조회 실패: %s", e)
        return []

    results = []

    for code in codes:
        try:
            df = fdr.DataReader(code, start_date, end_date)
            if df is None or len(df) < days + 3:
                continue

            recent = df.iloc[-days:]

            # 최근 N일 상승일 수
            up_days = (recent["Change"] > 0).sum()

            # 거래량 비율 (최근 N일 vs 이전)
            recent_vol = recent["Volume"].mean()
            prev_vol = df.iloc[:-days]["Volume"].mean()
            volume_ratio = recent_vol / prev_vol if prev_vol > 0 else 1.0

            # 기간 수익률
            period_return = (recent["Close"].iloc[-1] - recent["Close"].iloc[0]) / recent["Close"].iloc[0] * 100

            # 매집 점수: 연속상승 + 거래량 급증
            score = up_days * 10 + (volume_ratio - 1) * 30 + period_return

            if up_days >= days * 0.6 and volume_ratio >= 1.3:  # 조건 필터
                results.append({
                    "code": code,
                    "name": names.get(code, code),
                    "close": int(recent["Close"].iloc[-1]),
                    "change_pct": round(period_return, 2),
                    "volume_ratio": round(volume_ratio, 2),
                    "up_days": int(up_days),
                    "foreign_net": None,  # pykrx 불안정으로 N/A
                    "inst_net": None,
                    "total_net": None,
                    "score": round(score, 2),
                })
        except Exception:
            continue

    results.sort(key=lambda x: x["score"], reverse=True)
    if not results:
        logger.warning("조건에 맞는 종목이 없습니다 (volume_ratio>=1.3, up_days>=60%).")
    return results[:top_n]


def format_scan_result(results: list[dict], mode: str = "supply") -> str:
    """텍스트 테이블 형태로 포맷팅."""
    if not results:
        return "조건에 맞는 종목이 없습니다."

    if mode == "momentum":
        header = f"{'종목명':<14} {'코드':>7} {'현재가':>8} {'수익률':>7} {'거래량배율':>8} {'점수':>6}"
        sep = "-" * 56
        rows = [header, sep]
        for r in results:
            rows.append(
                f"{r['name']:<14} {r['code']:>7} {r['close']:>8,} {r['change_pct']:>+6.1f}% {r['volume_ratio']:>7.1f}x {r['score']:>6.1f}"
            )
    else:
        header = f"{'종목명':<14} {'코드':>7} {'현재가':>8} {'수익률':>7} {'거래량배율':>8} {'상승일':>5} {'점수':>6}"
        sep = "-" * 62
        rows = [header, sep]
        for r in results:
            rows.append(
                f"{r['name']:<14} {r['code']:>7} {r['close']:>8,} {r['change_pct']:>+6.1f}% {r['volume_ratio']:>7.1f}x {r['up_days']:>4}일 {r['score']:>6.1f}"
            )

    return "\n".join(rows)
