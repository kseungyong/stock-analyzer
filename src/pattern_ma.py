"""이동평균 4상태 패턴 — 사 / 팔아 / 사지마 / 팔지마.

Spec: docs/superpowers/specs/2026-05-10-pattern-indicators-design.md §3.1

분석 노트 (사용자 제공) 의 4 상태:
  - 매수 (사!): 골든크로스 + 장기선 우상향
  - 사지마: 골든크로스이지만 장기선 하향 (false breakout 위험)
  - 매도 (팔아!): 데드크로스 + 장기선 하향
  - 팔지마: 데드크로스이지만 장기선 우상향 (단기 조정)
"""
from __future__ import annotations

import pandas as pd

# 단기/중기/장기 SMA 윈도 (사용자 노트 기준 단기 5/10, 중기 50/100, 장기 50/200)
_SHORT = 5
_MID = 50
_LONG = 200
# 장기선 기울기 측정 윈도 (영업일 기준 약 1개월)
_SLOPE_WINDOW = 20
# 기울기 임계값 — 1개월간 0.1% 이상 = 의미 있는 추세
_SLOPE_THRESHOLD = 0.001


def detect_ma_state(df: pd.DataFrame) -> dict:
    """OHLCV → 이동평균 4상태.

    Args:
        df: pandas DataFrame with 'close' column. 최소 200일 필요.

    Returns:
        {"signal": "매수"|"매도"|"사지마"|"팔지마"|"관망",
         "label": str (한국어 설명),
         "confidence": float (0.0 ~ 1.0),
         "ma": {"sma5": float, "sma50": float, "sma200": float}}
    """
    if df is None or len(df) < _LONG:
        return {
            "signal": "관망",
            "label": "데이터 부족 (200일 미만)",
            "confidence": 0.0,
            "ma": {},
        }

    close = df["close"].astype(float)
    sma_short = close.rolling(_SHORT).mean()
    sma_mid = close.rolling(_MID).mean()
    sma_long = close.rolling(_LONG).mean()

    last_short = float(sma_short.iloc[-1])
    last_mid = float(sma_mid.iloc[-1])
    last_long = float(sma_long.iloc[-1])
    ma_summary = {"sma5": last_short, "sma50": last_mid, "sma200": last_long}

    # 단기-중기 cross 감지 (5 vs 50)
    diff = sma_short - sma_mid
    cross_up = diff.iloc[-2] < 0 <= diff.iloc[-1]
    cross_down = diff.iloc[-2] > 0 >= diff.iloc[-1]

    # 장기선 (200) 기울기 — 최근 20일 변화율
    slope = (sma_long.iloc[-1] - sma_long.iloc[-_SLOPE_WINDOW]) / sma_long.iloc[-_SLOPE_WINDOW]
    uptrend = slope > _SLOPE_THRESHOLD
    downtrend = slope < -_SLOPE_THRESHOLD

    # 4 상태 매핑 (cross 시점)
    if cross_up and uptrend:
        return {
            "signal": "매수",
            "label": f"골든크로스 + 장기선 우상향 (200일 +{slope*100:.2f}%)",
            "confidence": min(1.0, 0.5 + abs(slope) * 50),
            "ma": ma_summary,
        }
    if cross_up and downtrend:
        return {
            "signal": "사지마",
            "label": f"골든크로스이지만 장기선 하향 ({slope*100:.2f}%) — false breakout 위험",
            "confidence": 0.4,
            "ma": ma_summary,
        }
    if cross_down and downtrend:
        return {
            "signal": "매도",
            "label": f"데드크로스 + 장기선 하향 ({slope*100:.2f}%)",
            "confidence": min(1.0, 0.5 + abs(slope) * 50),
            "ma": ma_summary,
        }
    if cross_down and uptrend:
        return {
            "signal": "팔지마",
            "label": f"데드크로스이지만 장기선 우상향 (+{slope*100:.2f}%) — 단기 조정 가능",
            "confidence": 0.4,
            "ma": ma_summary,
        }

    # cross 없으면 — 추세 + 단기/중기 위치 기반 약한 시그널
    if uptrend and last_short > last_mid:
        return {
            "signal": "매수",
            "label": f"단기 > 중기, 장기 우상향 ({slope*100:.2f}%)",
            "confidence": 0.55,
            "ma": ma_summary,
        }
    if downtrend and last_short < last_mid:
        return {
            "signal": "매도",
            "label": f"단기 < 중기, 장기 하향 ({slope*100:.2f}%)",
            "confidence": 0.55,
            "ma": ma_summary,
        }
    return {
        "signal": "관망",
        "label": "추세 미확정 (이동평균선 혼재)",
        "confidence": 0.3,
        "ma": ma_summary,
    }
