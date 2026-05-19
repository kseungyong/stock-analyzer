"""5 카테고리 보조지표 통합 entry.

Spec: docs/superpowers/specs/2026-05-10-pattern-indicators-design.md

Phase A: 이동평균 4상태 ✓
Phase B: 캔들 패턴 (TA-Lib 60+) ✓
Phase C: 차트 패턴 (자체 — find_peaks) ✓
Phase D: 지지/저항 (자체 — 피벗 cluster) ✓
Phase E: 확률 경고 + summary 가중 다수결 ✓
"""
from __future__ import annotations

import pandas as pd

from src.pattern_candle import candle_summary, detect_candles
from src.pattern_chart import detect_chart_patterns
from src.pattern_ma import detect_ma_state
from src.pattern_sr import detect_support_resistance
from src.pattern_warn import detect_warning


def detect_all_patterns(df: pd.DataFrame, market: str = "korea") -> dict:
    """5 카테고리 통합 detection.

    Returns:
        {
          "ma_state": {...},           # Phase A
          "candles": [...],            # Phase B
          "chart_patterns": [...],     # Phase C
          "sr_levels": [...],          # Phase D
          "warning": {...} | None,     # Phase E
          "summary": {"signal": str, "score": int, "top_patterns": [str]},
        }
    """
    ma = detect_ma_state(df)
    candles = detect_candles(df, days=5)
    chart_patterns = detect_chart_patterns(df)
    sr_levels = detect_support_resistance(df)
    warning = detect_warning(chart_patterns)

    summary = _compute_summary(
        ma_state=ma,
        candles=candles,
        chart_patterns=chart_patterns,
        warning=warning,
    )

    return {
        "ma_state": ma,
        "candles": candles,
        "chart_patterns": chart_patterns,
        "sr_levels": sr_levels,
        "warning": warning,
        "summary": summary,
    }


def _compute_summary(
    *, ma_state: dict, candles: list, chart_patterns: list, warning: dict | None,
) -> dict:
    """가중 다수결 — ma:2, candle:1, chart:2, warning:2."""
    # MA (max ±3)
    ma_sig = ma_state.get("signal", "관망")
    ma_conf = float(ma_state.get("confidence", 0.0))
    ma_base = {"매수": 2, "매도": -2, "사지마": 0, "팔지마": 0, "관망": 0}.get(ma_sig, 0)
    ma_score = int(round(ma_base * (0.5 + ma_conf)))

    # Candles (max ±2)
    cs = candle_summary(candles)
    candle_score = cs["score"]

    # Chart patterns (각 conf 합산, max ±4)
    chart_score = 0
    for cp in chart_patterns:
        sig = cp.get("signal", "관망")
        conf = float(cp.get("confidence", 0.0))
        if sig == "매수":
            chart_score += int(round(conf * 2))
        elif sig == "매도":
            chart_score -= int(round(conf * 2))
    chart_score = max(-4, min(4, chart_score))

    # Warning (강한 시그널 — max ±2)
    warn_score = 0
    if warning:
        wsig = warning.get("signal", "관망")
        wpct = warning.get("confidence_pct", 0)
        if wsig == "매수":
            warn_score = int(round(wpct / 50))
        elif wsig == "매도":
            warn_score = -int(round(wpct / 50))

    total_score = ma_score + candle_score + chart_score + warn_score

    if total_score >= 2:
        signal = "매수"
    elif total_score <= -2:
        signal = "매도"
    elif total_score > 0:
        signal = "약매수"
    elif total_score < 0:
        signal = "약매도"
    else:
        signal = ma_sig if ma_sig in ("사지마", "팔지마") else "관망"

    top_patterns: list[str] = []
    # ma_state.name 우선 사용 (메타데이터 lookup 가능한 명시적 패턴명).
    # 예전엔 label.split(" ")[0] 으로 첫 단어를 잘라 썼는데 "단기 > 중기..."
    # 같은 label 의 첫 단어 "단기" 가 패턴명으로 잘못 노출됐음.
    ma_name = ma_state.get("name") or ma_state.get("label", "").split(" ")[0]
    if ma_sig in ("매수", "매도") and ma_name:
        top_patterns.append(ma_name)
    if warning:
        top_patterns.append(warning.get("pattern", ""))
    for cp in chart_patterns[:1]:
        if cp.get("signal") != "관망":
            top_patterns.append(cp.get("name", ""))
    top_patterns.extend(cs.get("top_patterns", [])[:2])

    seen: set[str] = set()
    unique_tops: list[str] = []
    for p in top_patterns:
        if p and p not in seen:
            unique_tops.append(p)
            seen.add(p)

    return {
        "signal": signal,
        "score": total_score,
        "top_patterns": unique_tops[:3],
    }
