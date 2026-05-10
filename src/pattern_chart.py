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
    """OHLCV → 차트 패턴 list.

    각 detector 호출 후 None 아닌 결과만 list 에 포함. 신뢰도 내림차순 정렬.
    """
    if df is None or len(df) < _WINDOW or find_peaks is None:
        return []

    # 최근 _WINDOW 일만 분석
    recent = df.iloc[-_WINDOW:].reset_index(drop=False)
    high = recent["high"].astype(float).values
    low = recent["low"].astype(float).values
    close = recent["close"].astype(float).values

    # 피벗 감지 (high 위 + low 아래)
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
            r = detector(high, low, close, high_idx, low_idx)
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
    high: np.ndarray, low: np.ndarray, close: np.ndarray,
    high_idx: np.ndarray, low_idx: np.ndarray,
) -> dict[str, Any] | None:
    """더블바텀 (W) — 두 저점이 비슷한 가격 + 사이 고점 + 마지막 가격 > 사이 고점."""
    if len(low_idx) < 2:
        return None
    # 마지막 두 저점
    i1, i2 = low_idx[-2], low_idx[-1]
    if i2 - i1 < _MIN_DISTANCE:
        return None
    p1, p2 = low[i1], low[i2]
    if not _is_close(p1, p2):
        return None
    # 두 저점 사이의 최고가
    between_high = high[i1:i2].max() if i2 > i1 else 0
    if between_high <= max(p1, p2):
        return None
    # 마지막 close 가 between_high 위 = 돌파 (강한 시그널)
    last_close = close[-1]
    breakout = last_close > between_high
    confidence = 0.65 if breakout else 0.5
    return {
        "name": "더블바텀(W)",
        "signal": "매수",
        "confidence": confidence,
        "details": f"저점1={p1:.0f} 저점2={p2:.0f} 중간={between_high:.0f} 현재={last_close:.0f}{' (돌파)' if breakout else ''}",
    }


def _detect_double_top(
    high: np.ndarray, low: np.ndarray, close: np.ndarray,
    high_idx: np.ndarray, low_idx: np.ndarray,
) -> dict[str, Any] | None:
    """더블탑 (M) — 두 고점이 비슷한 가격 + 사이 저점."""
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
    confidence = 0.65 if breakdown else 0.5
    return {
        "name": "더블탑(M)",
        "signal": "매도",
        "confidence": confidence,
        "details": f"고점1={p1:.0f} 고점2={p2:.0f} 중간={between_low:.0f} 현재={last_close:.0f}{' (이탈)' if breakdown else ''}",
    }


def _detect_head_shoulders_inverse(
    high: np.ndarray, low: np.ndarray, close: np.ndarray,
    high_idx: np.ndarray, low_idx: np.ndarray,
) -> dict[str, Any] | None:
    """역헤드앤숄더 — 3개 저점 (가운데 가장 낮음, 좌/우 어깨 비슷)."""
    if len(low_idx) < 3:
        return None
    l, h, r = low_idx[-3:]
    if not (l < h < r):
        return None
    pl, ph, pr = low[l], low[h], low[r]
    # 가운데 가장 낮음 + 좌우 어깨 비슷
    if not (ph < pl and ph < pr and _is_close(pl, pr, tolerance=0.05)):
        return None
    return {
        "name": "역헤드앤숄더",
        "signal": "매수",
        "confidence": 0.7,
        "details": f"좌={pl:.0f} 헤드={ph:.0f} 우={pr:.0f}",
    }


def _detect_head_shoulders(
    high: np.ndarray, low: np.ndarray, close: np.ndarray,
    high_idx: np.ndarray, low_idx: np.ndarray,
) -> dict[str, Any] | None:
    """헤드앤숄더 — 3개 고점 (가운데 가장 높음)."""
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
        "details": f"좌={pl:.0f} 헤드={ph:.0f} 우={pr:.0f}",
    }


def _detect_triangle(
    high: np.ndarray, low: np.ndarray, close: np.ndarray,
    high_idx: np.ndarray, low_idx: np.ndarray,
) -> dict[str, Any] | None:
    """삼각형 패턴 — 최근 N개 high 추세 + low 추세로 분류.

    상승 삼각형 (저항 평평 + 지지 상승 → 매수)
    하락 삼각형 (지지 평평 + 저항 하락 → 매도)
    대칭 삼각형 (양쪽 수렴 → 관망)
    """
    if len(high_idx) < 3 or len(low_idx) < 3:
        return None
    # 최근 3 고점/저점 으로 추세 fit
    recent_h = high_idx[-3:]
    recent_l = low_idx[-3:]
    h_slope = np.polyfit(recent_h, high[recent_h], 1)[0]
    l_slope = np.polyfit(recent_l, low[recent_l], 1)[0]

    # slope normalization (가격 대비)
    avg_price = float(close[-1])
    h_slope_pct = h_slope / avg_price * 100  # %/일
    l_slope_pct = l_slope / avg_price * 100

    # 상승 삼각형: 저항 평평 (h_slope ≈ 0) + 지지 상승 (l_slope > 0.05%/일)
    if abs(h_slope_pct) < 0.05 and l_slope_pct > 0.05:
        return {
            "name": "상승 삼각형",
            "signal": "매수",
            "confidence": 0.6,
            "details": f"저항 평평 / 지지 +{l_slope_pct:.2f}%/일",
        }
    # 하락 삼각형: 지지 평평 + 저항 하락
    if abs(l_slope_pct) < 0.05 and h_slope_pct < -0.05:
        return {
            "name": "하락 삼각형",
            "signal": "매도",
            "confidence": 0.6,
            "details": f"지지 평평 / 저항 {h_slope_pct:.2f}%/일",
        }
    # 대칭 삼각형: 저항 하락 + 지지 상승 (수렴)
    if h_slope_pct < -0.05 and l_slope_pct > 0.05:
        return {
            "name": "삼각형 수렴 (대칭)",
            "signal": "관망",
            "confidence": 0.5,
            "details": f"저항 {h_slope_pct:.2f}%/일 / 지지 +{l_slope_pct:.2f}%/일 — 돌파 방향 대기",
        }
    return None
