"""캔들 패턴 detector — TA-Lib 의 60+ CDL 함수 활용.

Spec: docs/superpowers/specs/2026-05-10-pattern-indicators-design.md §3.2

TA-Lib 의 각 CDL* 함수는 다음 반환:
- 양수 (>0): bullish (강세) 시그널 — 일반적으로 매수 패턴
- 음수 (<0): bearish (약세) 시그널 — 일반적으로 매도 패턴
- 0: 패턴 없음

ENGULFING / HARAMI 등 양방향 패턴은 부호로 매수/매도 판별.
"""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

# 한국어 매핑 + 기본 시그널 방향. 사용자 분석 노트 (이미지 5, 7) 의 한국어 이름.
# "매수"/"매도" — TA-Lib 반환 부호와 무관하게 패턴 의미상 방향
# "varies" — 부호로 판별 (양→매수, 음→매도)
_NAME_MAP: dict[str, tuple[str, str]] = {
    "CDLDOJI": ("도지", "관망"),
    "CDLDOJISTAR": ("도지스타", "관망"),
    "CDLDRAGONFLYDOJI": ("잠자리 도지", "매수"),
    "CDLGRAVESTONEDOJI": ("묘비 도지", "매도"),
    "CDLLONGLEGGEDDOJI": ("긴다리 도지", "관망"),
    "CDLHAMMER": ("망치", "매수"),
    "CDLINVERTEDHAMMER": ("역망치", "매수"),
    "CDLHANGINGMAN": ("교수형", "매도"),
    "CDLSHOOTINGSTAR": ("유성", "매도"),
    "CDLENGULFING": ("장악형", "varies"),
    "CDLHARAMI": ("잉태형", "varies"),
    "CDLHARAMICROSS": ("잉태형 십자", "varies"),
    "CDLMORNINGSTAR": ("새벽의 샛별", "매수"),
    "CDLEVENINGSTAR": ("저녁별", "매도"),
    "CDLMORNINGDOJISTAR": ("새벽 도지스타", "매수"),
    "CDLEVENINGDOJISTAR": ("저녁 도지스타", "매도"),
    "CDL3WHITESOLDIERS": ("적삼병", "매수"),
    "CDL3BLACKCROWS": ("흑삼병", "매도"),
    "CDL3INSIDE": ("3봉 내부", "varies"),
    "CDL3OUTSIDE": ("3봉 외부", "varies"),
    "CDL3LINESTRIKE": ("3선 타격", "varies"),
    "CDL3STARSINSOUTH": ("남쪽 3성", "매수"),
    "CDLPIERCING": ("관통형", "매수"),
    "CDLDARKCLOUDCOVER": ("먹구름", "매도"),
    "CDLABANDONEDBABY": ("버려진 아이", "varies"),
    "CDL2CROWS": ("두 까마귀", "매도"),
    "CDLUPSIDEGAP2CROWS": ("상승갭 두 까마귀", "매도"),
    "CDLTAKURI": ("타구리", "매수"),
    "CDLSPINNINGTOP": ("팽이", "관망"),
    "CDLMARUBOZU": ("장대봉", "varies"),
    "CDLBELTHOLD": ("벨트홀드", "varies"),
    "CDLBREAKAWAY": ("이탈형", "varies"),
    "CDLCLOSINGMARUBOZU": ("종가 장대봉", "varies"),
    "CDLCONCEALBABYSWALL": ("아기벽 가림", "매수"),
    "CDLCOUNTERATTACK": ("반격선", "varies"),
    "CDLGAPSIDESIDEWHITE": ("사이드 갭 양봉", "매수"),
    "CDLHIGHWAVE": ("고파동", "관망"),
    "CDLHIKKAKE": ("히카케", "varies"),
    "CDLHIKKAKEMOD": ("수정 히카케", "varies"),
    "CDLHOMINGPIGEON": ("귀환 비둘기", "매수"),
    "CDLIDENTICAL3CROWS": ("동일 3까마귀", "매도"),
    "CDLINNECK": ("인넥", "매도"),
    "CDLKICKING": ("킥킹", "varies"),
    "CDLKICKINGBYLENGTH": ("킥킹 by 길이", "varies"),
    "CDLLADDERBOTTOM": ("사다리 바닥", "매수"),
    "CDLLONGLINE": ("장대선", "varies"),
    "CDLMATCHINGLOW": ("매칭 저점", "매수"),
    "CDLMATHOLD": ("매트홀드", "매수"),
    "CDLONNECK": ("온넥", "매도"),
    "CDLRICKSHAWMAN": ("인력거꾼", "관망"),
    "CDLRISEFALL3METHODS": ("상승/하락 삼법", "varies"),
    "CDLSEPARATINGLINES": ("분리선", "varies"),
    "CDLSHORTLINE": ("단봉", "관망"),
    "CDLSTALLEDPATTERN": ("정체 패턴", "매도"),
    "CDLSTICKSANDWICH": ("막대 샌드위치", "매수"),
    "CDLTASUKIGAP": ("타스키 갭", "varies"),
    "CDLTHRUSTING": ("끼움형", "매도"),
    "CDLTRISTAR": ("3성", "varies"),
    "CDLUNIQUE3RIVER": ("유니크 3강", "매수"),
    "CDLXSIDEGAP3METHODS": ("사이드 갭 3법", "varies"),
}


def detect_candles(df: pd.DataFrame, days: int = 5) -> list[dict[str, Any]]:
    """최근 N일의 캔들 패턴 list 반환.

    Args:
        df: OHLCV DataFrame (open/high/low/close 필요)
        days: 최근 며칠 검사 (default 5)

    Returns:
        list of {"name": str, "signal": "매수"|"매도"|"관망", "date": str, "code": str, "value": int}
    """
    if df is None or len(df) < 30:
        return []
    try:
        import talib
    except ImportError:
        logger.warning("TA-Lib 없음 — pattern_candle 비활성")
        return []

    open_ = df["open"].astype(float).values
    high = df["high"].astype(float).values
    low = df["low"].astype(float).values
    close = df["close"].astype(float).values

    # 인덱스 → 날짜 문자열 변환
    if isinstance(df.index, pd.DatetimeIndex):
        dates = [d.strftime("%Y-%m-%d") for d in df.index]
    else:
        dates = [str(d) for d in df.index]

    results: list[dict[str, Any]] = []
    recent_idxs = set(range(len(df) - days, len(df)))

    for code, (kor_name, default_signal) in _NAME_MAP.items():
        func = getattr(talib, code, None)
        if func is None:
            continue
        try:
            arr = func(open_, high, low, close)
        except Exception as e:
            logger.debug("CDL %s 호출 실패: %s", code, e)
            continue

        for idx in recent_idxs:
            val = int(arr[idx])
            if val == 0:
                continue
            # 시그널 결정
            if default_signal == "varies":
                signal = "매수" if val > 0 else "매도"
            else:
                # 부호와 default 일치 여부 — 일반적으로 양수면 default 그대로
                signal = default_signal
            results.append({
                "code": code,
                "name": kor_name,
                "signal": signal,
                "date": dates[idx],
                "value": val,
            })

    # 최신순 정렬 (date 내림차순)
    results.sort(key=lambda r: r["date"], reverse=True)
    return results


def candle_summary(candles: list[dict[str, Any]]) -> dict[str, Any]:
    """캔들 패턴 list → summary (signal + score + 카운트)."""
    buy_count = sum(1 for c in candles if c["signal"] == "매수")
    sell_count = sum(1 for c in candles if c["signal"] == "매도")
    if buy_count > sell_count:
        signal = "매수"
        score = min(2, buy_count - sell_count)
    elif sell_count > buy_count:
        signal = "매도"
        score = -min(2, sell_count - buy_count)
    else:
        signal = "관망"
        score = 0

    # top 패턴 — 매수면 매수 패턴, 매도면 매도 패턴
    top_patterns = []
    for c in candles:
        if signal == "매수" and c["signal"] == "매수":
            top_patterns.append(c["name"])
        elif signal == "매도" and c["signal"] == "매도":
            top_patterns.append(c["name"])
        if len(top_patterns) >= 2:
            break

    return {
        "signal": signal,
        "score": score,
        "buy_count": buy_count,
        "sell_count": sell_count,
        "top_patterns": top_patterns,
    }
