"""5 카테고리 보조지표 통합 entry.

Spec: docs/superpowers/specs/2026-05-10-pattern-indicators-design.md

Phase A: 이동평균 4상태 ✓
Phase B: 캔들 패턴 (TA-Lib 60+) ✓
Phase C: 차트 패턴 (자체) — 추후.
Phase D: 지지/저항 — 추후.
Phase E: 확률 경고 + summary 가중 다수결 — 추후.
"""
from __future__ import annotations

import pandas as pd

from src.pattern_candle import candle_summary, detect_candles
from src.pattern_ma import detect_ma_state


def detect_all_patterns(df: pd.DataFrame, market: str = "korea") -> dict:
    """5 카테고리 통합 detection.

    Returns:
        {
          "ma_state": {...},           # Phase A
          "candles": [...],            # Phase B
          "chart_patterns": [...],     # Phase C (현재 빈 list)
          "sr_levels": [...],          # Phase D
          "warning": None,             # Phase E
          "summary": {"signal": str, "score": int, "top_patterns": [str]},
        }
    """
    ma = detect_ma_state(df)
    candles = detect_candles(df, days=5)

    summary = _compute_summary(ma_state=ma, candles=candles)

    return {
        "ma_state": ma,
        "candles": candles,
        "chart_patterns": [],
        "sr_levels": [],
        "warning": None,
        "summary": summary,
    }


def _compute_summary(*, ma_state: dict, candles: list) -> dict:
    """가중 다수결 — ma:2, candle:1.

    Phase C/D/E 추가 시: chart:2, sr:1, warn:1 추가.
    """
    # Phase A: MA score
    ma_sig = ma_state.get("signal", "관망")
    ma_conf = float(ma_state.get("confidence", 0.0))
    ma_base = {"매수": 2, "매도": -2, "사지마": 0, "팔지마": 0, "관망": 0}.get(ma_sig, 0)
    ma_score = int(round(ma_base * (0.5 + ma_conf)))

    # Phase B: candle score
    cs = candle_summary(candles)
    candle_score = cs["score"]  # max ±2

    total_score = ma_score + candle_score
    if total_score > 0:
        signal = "매수"
    elif total_score < 0:
        signal = "매도"
    else:
        # 둘 다 0 면 ma 의 사지마/팔지마/관망 그대로
        signal = ma_sig if ma_sig in ("사지마", "팔지마") else "관망"

    # top patterns — ma label 의 첫 단어 + candle 상위 2개
    top_patterns = []
    ma_label = ma_state.get("label", "")
    if ma_sig in ("매수", "매도") and ma_label:
        top_patterns.append(ma_label.split(" ")[0])
    top_patterns.extend(cs.get("top_patterns", [])[:2])

    return {
        "signal": signal,
        "score": total_score,
        "top_patterns": top_patterns[:3],
    }
