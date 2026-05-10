"""차트 패턴 detector — find_peaks 기반 자체 구현.

Spec: docs/superpowers/specs/2026-05-10-pattern-indicators-design.md §3.3

감지 패턴:
- 반전 매수: 더블바텀(W), 역헤드앤숄더, 하락쐐기 (반등)
- 반전 매도: 더블탑(M), 헤드앤숄더, 상승쐐기 (반락)
- 횡보 (대기): 박스권, 삼각형 수렴 (대칭)
- 추세 지속: 상승삼각형, 하락삼각형, 상승플래그, 하락플래그

알고리즘:
1. scipy.signal.find_peaks 로 high (저항) / low (지지) 피벗 감지
2. 각 패턴 별 기하학적 매칭 (높이 비율 / 거리 / 추세선 등)
3. 신뢰도 = 매칭 강도 (0.5~0.95)
"""
from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

try:
    from scipy.signal import find_peaks
except ImportError:
    find_peaks = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# 분석 윈도 — 최근 N일 (영업일)
_WINDOW = 120
# 피벗 감지 — 최소 거리 (영업일)
_MIN_DISTANCE = 5
# 두 저점/고점 가격 일치 허용 오차 (%)
_PRICE_TOLERANCE = 0.03


def detect_chart_patterns(df: pd.DataFrame) -> list[dict[str, Any]]:
    """OHLCV → 차트 패턴 list (구간 날짜/가격 포함)."""
    if df is None or len(df) < _WINDOW or find_peaks is None:
        return []

    recent = df.iloc[-_WINDOW:].reset_index(drop=False)
    high = recent["high"].astype(float).values
    low = recent["low"].astype(float).values
    close = recent["close"].astype(float).values

    if isinstance(df.index, pd.DatetimeIndex):
        dates = [d.strftime("%Y-%m-%d") for d in df.index[-_WINDOW:]]
    else:
        dates = [str(d) for d in df.index[-_WINDOW:]]

    high_idx, _ = find_peaks(high, distance=_MIN_DISTANCE)
    low_idx, _ = find_peaks(-low, distance=_MIN_DISTANCE)

    detectors = [
        _detect_double_bottom,
        _detect_double_top,
        _detect_head_shoulders_inverse,
        _detect_head_shoulders,
        _detect_triangle,
    ]

    results: list[dict[str, Any]] = []
    for detector in detectors:
        try:
            r = detector(high, low, close, high_idx, low_idx, dates)
            if r is not None:
                results.append(r)
        except Exception as e:
            logger.debug("%s 실패: %s", detector.__name__, e)

    results.sort(key=lambda r: r.get("confidence", 0), reverse=True)
    return results


def _is_close(a: float, b: float, tolerance: float = _PRICE_TOLERANCE) -> bool:
    """두 가격이 tolerance % 이내."""
    return abs(a - b) / max(abs(a), abs(b), 1.0) < tolerance


def _detect_double_bottom(
    high, low, close, high_idx, low_idx, dates,
) -> dict[str, Any] | None:
    """더블바텀 (W) — 두 저점 + 사이 고점 + 넥라인 돌파 여부."""
    if len(low_idx) < 2:
        return None
    i1, i2 = low_idx[-2], low_idx[-1]
    if i2 - i1 < _MIN_DISTANCE:
        return None
    p1, p2 = low[i1], low[i2]
    if not _is_close(p1, p2):
        return None
    between_high = high[i1:i2].max() if i2 > i1 else 0
    if between_high <= max(p1, p2):
        return None
    last_close = close[-1]
    breakout = last_close > between_high
    return {
        "name": "더블바텀(W)",
        "signal": "매수",
        "confidence": 0.65 if breakout else 0.5,
        "from_date": dates[i1],
        "to_date": dates[i2],
        "duration_days": int(i2 - i1),
        "low1": {"date": dates[i1], "price": round(float(p1), 2)},
        "low2": {"date": dates[i2], "price": round(float(p2), 2)},
        "neckline": round(float(between_high), 2),
        "current": round(float(last_close), 2),
        "breakout": breakout,
        "details": (
            f"저점1 {dates[i1]} {p1:.0f} → 저점2 {dates[i2]} {p2:.0f} "
            f"(넥라인 {between_high:.0f}, 현재 {last_close:.0f}"
            f"{' — 넥라인 돌파!' if breakout else ' — 넥라인 미돌파'})"
        ),
    }


def _detect_double_top(
    high, low, close, high_idx, low_idx, dates,
) -> dict[str, Any] | None:
    """더블탑 (M) — 두 고점 + 사이 저점."""
    if len(high_idx) < 2:
        return None
    i1, i2 = high_idx[-2], high_idx[-1]
    if i2 - i1 < _MIN_DISTANCE:
        return None
    p1, p2 = high[i1], high[i2]
    if not _is_close(p1, p2):
        return None
    between_low = low[i1:i2].min() if i2 > i1 else 1e9
    if between_low >= min(p1, p2):
        return None
    last_close = close[-1]
    breakdown = last_close < between_low
    return {
        "name": "더블탑(M)",
        "signal": "매도",
        "confidence": 0.65 if breakdown else 0.5,
        "from_date": dates[i1],
        "to_date": dates[i2],
        "duration_days": int(i2 - i1),
        "high1": {"date": dates[i1], "price": round(float(p1), 2)},
        "high2": {"date": dates[i2], "price": round(float(p2), 2)},
        "neckline": round(float(between_low), 2),
        "current": round(float(last_close), 2),
        "breakdown": breakdown,
        "details": (
            f"고점1 {dates[i1]} {p1:.0f} → 고점2 {dates[i2]} {p2:.0f} "
            f"(넥라인 {between_low:.0f}, 현재 {last_close:.0f}"
            f"{' — 넥라인 이탈!' if breakdown else ' — 넥라인 유지'})"
        ),
    }


def _detect_head_shoulders_inverse(
    high, low, close, high_idx, low_idx, dates,
) -> dict[str, Any] | None:
    """역헤드앤숄더 — 3개 저점."""
    if len(low_idx) < 3:
        return None
    l, h, r = low_idx[-3:]
    if not (l < h < r):
        return None
    pl, ph, pr = low[l], low[h], low[r]
    if not (ph < pl and ph < pr and _is_close(pl, pr, tolerance=0.05)):
        return None
    return {
        "name": "역헤드앤숄더",
        "signal": "매수",
        "confidence": 0.7,
        "from_date": dates[l],
        "to_date": dates[r],
        "duration_days": int(r - l),
        "left_shoulder": {"date": dates[l], "price": round(float(pl), 2)},
        "head": {"date": dates[h], "price": round(float(ph), 2)},
        "right_shoulder": {"date": dates[r], "price": round(float(pr), 2)},
        "details": (
            f"좌어깨 {dates[l]} {pl:.0f} / 헤드 {dates[h]} {ph:.0f} / "
            f"우어깨 {dates[r]} {pr:.0f}"
        ),
    }


def _detect_head_shoulders(
    high, low, close, high_idx, low_idx, dates,
) -> dict[str, Any] | None:
    """헤드앤숄더 — 3개 고점."""
    if len(high_idx) < 3:
        return None
    l, h, r = high_idx[-3:]
    if not (l < h < r):
        return None
    pl, ph, pr = high[l], high[h], high[r]
    if not (ph > pl and ph > pr and _is_close(pl, pr, tolerance=0.05)):
        return None
    return {
        "name": "헤드앤숄더",
        "signal": "매도",
        "confidence": 0.7,
        "from_date": dates[l],
        "to_date": dates[r],
        "duration_days": int(r - l),
        "left_shoulder": {"date": dates[l], "price": round(float(pl), 2)},
        "head": {"date": dates[h], "price": round(float(ph), 2)},
        "right_shoulder": {"date": dates[r], "price": round(float(pr), 2)},
        "details": (
            f"좌어깨 {dates[l]} {pl:.0f} / 헤드 {dates[h]} {ph:.0f} / "
            f"우어깨 {dates[r]} {pr:.0f}"
        ),
    }


def _detect_triangle(
    high, low, close, high_idx, low_idx, dates,
) -> dict[str, Any] | None:
    """삼각형 패턴 — 최근 N개 high 추세 + low 추세로 분류."""
    if len(high_idx) < 3 or len(low_idx) < 3:
        return None
    recent_h = high_idx[-3:]
    recent_l = low_idx[-3:]
    h_slope = np.polyfit(recent_h, high[recent_h], 1)[0]
    l_slope = np.polyfit(recent_l, low[recent_l], 1)[0]

    avg_price = float(close[-1])
    h_slope_pct = h_slope / avg_price * 100
    l_slope_pct = l_slope / avg_price * 100

    start_idx = int(min(recent_h[0], recent_l[0]))
    end_idx = int(max(recent_h[-1], recent_l[-1]))
    base = {
        "from_date": dates[start_idx],
        "to_date": dates[end_idx],
        "duration_days": int(end_idx - start_idx),
        "high_slope_pct_per_day": round(float(h_slope_pct), 3),
        "low_slope_pct_per_day": round(float(l_slope_pct), 3),
    }

    if abs(h_slope_pct) < 0.05 and l_slope_pct > 0.05:
        return {**base, "name": "상승 삼각형", "signal": "매수", "confidence": 0.6,
                "details": f"{dates[start_idx]} ~ {dates[end_idx]}: 저항 평평 / 지지 +{l_slope_pct:.2f}%/일"}
    if abs(l_slope_pct) < 0.05 and h_slope_pct < -0.05:
        return {**base, "name": "하락 삼각형", "signal": "매도", "confidence": 0.6,
                "details": f"{dates[start_idx]} ~ {dates[end_idx]}: 지지 평평 / 저항 {h_slope_pct:.2f}%/일"}
    if h_slope_pct < -0.05 and l_slope_pct > 0.05:
        return {**base, "name": "삼각형 수렴 (대칭)", "signal": "관망", "confidence": 0.5,
                "details": (f"{dates[start_idx]} ~ {dates[end_idx]}: 저항 {h_slope_pct:.2f}%/일 / "
                            f"지지 +{l_slope_pct:.2f}%/일 — 돌파 방향 대기")}
    return None
