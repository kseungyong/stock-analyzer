"""이동평균 4상태 detector 테스트.

Spec: docs/superpowers/specs/2026-05-10-pattern-indicators-design.md §3.1
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.pattern_ma import detect_ma_state


def _df_with_close(close_array: np.ndarray) -> pd.DataFrame:
    return pd.DataFrame({
        "close": close_array,
        "open": close_array * 0.999,
        "high": close_array * 1.005,
        "low": close_array * 0.995,
        "volume": [100_000] * len(close_array),
    })


def test_data_insufficient_returns_observe():
    df = _df_with_close(np.array([10000.0] * 100))
    result = detect_ma_state(df)
    assert result["signal"] == "관망"
    assert "데이터 부족" in result["label"]


def test_steady_uptrend_returns_buy():
    """200일 우상향 추세 — 단기 > 중기, 장기 우상향."""
    n = 250
    close = np.linspace(10000, 13000, n)
    result = detect_ma_state(_df_with_close(close))
    assert result["signal"] == "매수"
    assert result["confidence"] >= 0.5


def test_steady_downtrend_returns_sell():
    n = 250
    close = np.linspace(13000, 10000, n)
    result = detect_ma_state(_df_with_close(close))
    assert result["signal"] == "매도"


def test_flat_returns_observe():
    n = 250
    close = np.array([10000.0] * n)
    result = detect_ma_state(_df_with_close(close))
    assert result["signal"] == "관망"


def test_returns_ma_values():
    n = 250
    close = np.linspace(10000, 12000, n)
    result = detect_ma_state(_df_with_close(close))
    assert "ma" in result
    assert "sma5" in result["ma"]
    assert "sma50" in result["ma"]
    assert "sma200" in result["ma"]
    assert result["ma"]["sma5"] > 0
