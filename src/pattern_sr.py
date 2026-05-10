"""지지/저항 수평선 자동 감지.

Spec: docs/superpowers/specs/2026-05-10-pattern-indicators-design.md §3.4

알고리즘:
1. find_peaks 로 high (저항 후보) + low (지지 후보) 피벗 추출
2. 가격 cluster (±0.5%)
3. cluster 의 touch 수 ≥ 2 → 의미 있는 수평선
4. 현재가 비교 → 위 = 저항, 아래 = 지지
5. 현재가 ±20% 범위 내 만 반환 (관련 있는 라인)
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

_WINDOW = 120
_MIN_DISTANCE = 5
_CLUSTER_TOLERANCE = 0.005  # 0.5%
_MIN_TOUCHES = 2
_RANGE_PCT = 0.20  # 현재가 ±20% 범위만


def detect_support_resistance(df: pd.DataFrame) -> list[dict[str, Any]]:
    """OHLCV → 지지/저항 수평선 list.

    Returns:
        list of {"price": float, "type": "지지"|"저항", "touches": int, "distance_pct": float}
    """
    if df is None or len(df) < _WINDOW or find_peaks is None:
        return []

    recent = df.iloc[-_WINDOW:]
    high = recent["high"].astype(float).values
    low = recent["low"].astype(float).values
    close = float(recent["close"].iloc[-1])

    # 피벗 가격들
    high_idx, _ = find_peaks(high, distance=_MIN_DISTANCE)
    low_idx, _ = find_peaks(-low, distance=_MIN_DISTANCE)
    pivot_prices = list(high[high_idx]) + list(low[low_idx])
    if not pivot_prices:
        return []

    # Cluster — 가격 정렬 후 ±0.5% 범위로 그룹화
    clusters = _cluster_prices(sorted(pivot_prices))

    # touch 수 ≥ 2 + 현재가 ±20% 범위
    range_low = close * (1 - _RANGE_PCT)
    range_high = close * (1 + _RANGE_PCT)
    levels = []
    for cluster_prices in clusters:
        if len(cluster_prices) < _MIN_TOUCHES:
            continue
        avg_price = float(np.mean(cluster_prices))
        if not (range_low <= avg_price <= range_high):
            continue
        levels.append({
            "price": round(avg_price, 2),
            "type": "저항" if avg_price > close else "지지",
            "touches": len(cluster_prices),
            "distance_pct": round((avg_price - close) / close * 100, 2),
        })

    # 현재가에 가까운 순
    levels.sort(key=lambda r: abs(r["distance_pct"]))
    return levels[:6]  # 너무 많으면 6개로 제한


def _cluster_prices(sorted_prices: list[float]) -> list[list[float]]:
    """정렬된 가격 list → cluster (인접 ±tolerance% 그룹)."""
    if not sorted_prices:
        return []
    clusters: list[list[float]] = [[sorted_prices[0]]]
    for p in sorted_prices[1:]:
        last_avg = float(np.mean(clusters[-1]))
        if abs(p - last_avg) / max(last_avg, 1.0) <= _CLUSTER_TOLERANCE:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    return clusters
