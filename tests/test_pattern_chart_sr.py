"""차트 패턴 + 지지/저항 + 확률 경고 + 통합 summary 테스트."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from src.pattern_chart import detect_chart_patterns
from src.pattern_indicators import detect_all_patterns
from src.pattern_sr import detect_support_resistance
from src.pattern_warn import detect_warning


def _df_random(n: int = 150, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 10000 + np.cumsum(rng.normal(0, 80, n))
    return pd.DataFrame({
        "open": close * 0.999,
        "high": close * 1.01,
        "low": close * 0.99,
        "close": close,
        "volume": [100_000] * n,
    }, index=pd.date_range("2026-01-01", periods=n))


def _df_double_bottom() -> pd.DataFrame:
    """두 저점이 비슷한 가격 + 사이 고점 + 마지막 close 가 중간 고점 위 (돌파)."""
    n = 150
    base = list(np.linspace(11000, 10000, 50))  # 첫 하락
    base += list(np.linspace(10000, 10500, 30))  # 첫 저점 근처 → 반등
    base += list(np.linspace(10500, 10050, 30))  # 두 번째 하락 (저점 비슷)
    base += list(np.linspace(10050, 11500, 40))  # 돌파 후 상승
    close = np.array(base)
    return pd.DataFrame({
        "open": close * 0.999,
        "high": close * 1.01,
        "low": close * 0.99,
        "close": close,
        "volume": [100_000] * n,
    }, index=pd.date_range("2026-01-01", periods=n))


def test_chart_random_returns_list():
    df = _df_random()
    result = detect_chart_patterns(df)
    assert isinstance(result, list)


def test_chart_short_data_returns_empty():
    df = _df_random(n=50)
    result = detect_chart_patterns(df)
    assert result == []


def test_chart_pattern_dict_shape():
    df = _df_random()
    for r in detect_chart_patterns(df):
        assert "name" in r
        assert "signal" in r
        assert "confidence" in r
        assert r["signal"] in ("매수", "매도", "관망")
        assert 0.0 <= r["confidence"] <= 1.0


def test_sr_returns_list():
    df = _df_random()
    result = detect_support_resistance(df)
    assert isinstance(result, list)
    for r in result:
        assert "price" in r
        assert "type" in r
        assert r["type"] in ("지지", "저항")
        assert r["touches"] >= 2


def test_sr_short_data_returns_empty():
    df = _df_random(n=50)
    assert detect_support_resistance(df) == []


def test_warning_with_double_top():
    chart = [{"name": "더블탑(M)", "signal": "매도", "confidence": 0.65}]
    w = detect_warning(chart)
    assert w is not None
    assert w["signal"] == "매도"
    assert w["confidence_pct"] == 100
    assert "폭락 대비" in w["action"]


def test_warning_no_match_returns_none():
    chart = [{"name": "삼각형 수렴 (대칭)", "signal": "관망", "confidence": 0.5}]
    assert detect_warning(chart) is None


def test_chart_pattern_json_serializable():
    """detect_chart_patterns 결과는 stdlib json.dumps 로 직렬화 가능해야 함.

    이전 버그: numpy 비교 결과 (numpy.bool_) 가 'breakout'/'breakdown' 필드에
    그대로 들어가 analysis_cache.put 에서 'Object of type bool is not JSON
    serializable' 발생 → cache miss.
    """
    df = _df_double_bottom()
    patterns = detect_chart_patterns(df)
    assert patterns, "double bottom fixture 가 패턴을 발견해야 한다"
    json.dumps(patterns)  # numpy.bool_ 가 끼어 있으면 TypeError


def test_summary_integration_random():
    """랜덤 데이터 — 통합 summary 형식 정상."""
    df = _df_random()
    result = detect_all_patterns(df)
    assert "ma_state" in result
    assert "candles" in result
    assert "chart_patterns" in result
    assert "sr_levels" in result
    assert "warning" in result
    assert "summary" in result
    summary = result["summary"]
    assert summary["signal"] in ("매수", "매도", "관망", "사지마", "팔지마")
    assert isinstance(summary["score"], int)
    assert isinstance(summary["top_patterns"], list)
