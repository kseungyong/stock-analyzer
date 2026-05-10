"""캔들 패턴 detector 테스트.

Spec: docs/superpowers/specs/2026-05-10-pattern-indicators-design.md §3.2
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.pattern_candle import candle_summary, detect_candles


def _df_random(n: int = 60) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    close = 10000 + np.cumsum(rng.normal(0, 50, n))
    return pd.DataFrame({
        "open": close * 0.999,
        "high": close * 1.005,
        "low": close * 0.995,
        "close": close,
        "volume": [100_000] * n,
    }, index=pd.date_range("2026-01-01", periods=n))


def test_returns_list():
    df = _df_random()
    result = detect_candles(df, days=5)
    assert isinstance(result, list)


def test_short_data_returns_empty():
    df = _df_random(n=5)
    result = detect_candles(df)
    assert result == []


def test_pattern_dict_shape():
    df = _df_random()
    result = detect_candles(df)
    for r in result:
        assert "name" in r
        assert "signal" in r
        assert "date" in r
        assert "code" in r
        assert r["signal"] in ("매수", "매도", "관망")


def test_summary_buy_majority():
    candles = [
        {"name": "망치", "signal": "매수", "date": "2026-05-08", "code": "CDLHAMMER"},
        {"name": "적삼병", "signal": "매수", "date": "2026-05-09", "code": "CDL3WHITESOLDIERS"},
        {"name": "도지", "signal": "관망", "date": "2026-05-10", "code": "CDLDOJI"},
    ]
    s = candle_summary(candles)
    assert s["signal"] == "매수"
    assert s["score"] == 2
    assert s["buy_count"] == 2
    assert s["sell_count"] == 0


def test_summary_sell_majority():
    candles = [
        {"name": "흑삼병", "signal": "매도", "date": "2026-05-08", "code": "CDL3BLACKCROWS"},
        {"name": "유성", "signal": "매도", "date": "2026-05-09", "code": "CDLSHOOTINGSTAR"},
    ]
    s = candle_summary(candles)
    assert s["signal"] == "매도"
    assert s["score"] == -2


def test_summary_empty():
    s = candle_summary([])
    assert s["signal"] == "관망"
    assert s["score"] == 0
