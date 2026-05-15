"""pattern_popup.py 단위 테스트."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from src import pattern_popup as pp


@pytest.fixture(autouse=True)
def reset_cache():
    pp._chart_cache.clear()
    yield
    pp._chart_cache.clear()


def test_find_detection_chart_pattern_latest():
    """date 미지정 시 가장 최근 chart_pattern 반환."""
    pj = {
        "chart_patterns": [
            {"name": "더블바텀(W)", "to_date": "2026-04-01", "low1": {}, "low2": {}},
            {"name": "더블바텀(W)", "to_date": "2026-05-10", "low1": {}, "low2": {}},
            {"name": "더블탑(M)", "to_date": "2026-05-12", "high1": {}, "high2": {}},
        ],
        "candles": [],
    }
    d = pp.find_detection(pj, "더블바텀(W)", date=None)
    assert d is not None
    assert d["to_date"] == "2026-05-10"


def test_find_detection_candle_by_date():
    """date 지정 시 정확 매칭."""
    pj = {
        "chart_patterns": [],
        "candles": [
            {"name": "잉태형", "date": "2026-05-10", "signal": "매수"},
            {"name": "잉태형", "date": "2026-05-13", "signal": "매수"},
        ],
    }
    d = pp.find_detection(pj, "잉태형", date="2026-05-10")
    assert d is not None
    assert d["date"] == "2026-05-10"


def test_find_detection_returns_none_when_pattern_absent():
    pj = {"chart_patterns": [], "candles": []}
    assert pp.find_detection(pj, "더블바텀(W)", date=None) is None


def test_build_chart_caption_chart_pattern():
    detection = {
        "name": "더블바텀(W)",
        "low1": {"date": "2026-04-15", "price": 65000},
        "low2": {"date": "2026-05-08", "price": 65500},
        "neckline": 68000,
        "current": 70000,
        "breakout": True,
    }
    caption = pp._build_caption(detection)
    assert "65000" in caption or "65,000" in caption
    assert "넥라인" in caption or "68000" in caption or "68,000" in caption
    assert "돌파" in caption


def test_build_chart_caption_candle_pattern():
    detection = {"name": "잉태형", "date": "2026-05-13", "signal": "매수"}
    caption = pp._build_caption(detection)
    assert "2026-05-13" in caption
    assert "잉태형" in caption


def test_render_chart_with_mock_data():
    """매칭 데이터 + yfinance mock → base64 PNG 응답."""
    ohlc = pd.DataFrame({
        "Open":  [100.0] * 60,
        "High":  [110.0] * 60,
        "Low":   [90.0]  * 60,
        "Close": [105.0] * 60,
        "Volume": [1000] * 60,
    }, index=pd.date_range("2026-03-15", periods=60))
    detection = {
        "name": "더블바텀(W)",
        "low1": {"date": "2026-04-15", "price": 95},
        "low2": {"date": "2026-05-08", "price": 96},
        "neckline": 105,
        "current": 110,
        "breakout": True,
        "from_date": "2026-04-15",
        "to_date": "2026-05-08",
    }
    with patch.object(pp, "_fetch_ohlc", return_value=ohlc):
        result = pp.build_actual_chart("005930.KS", "더블바텀(W)", date=None, pattern_json={
            "chart_patterns": [detection],
            "candles": [],
        })
    assert result["chart_b64"] is not None
    assert len(result["chart_b64"]) > 100  # base64 PNG
    assert "caption" in result
    assert result["signal_at_detection"] == "매수"


def test_render_chart_returns_null_when_ohlc_empty():
    """OHLC fetch 실패 → null + caption."""
    detection = {"name": "잉태형", "date": "2026-05-13", "signal": "매수"}
    with patch.object(pp, "_fetch_ohlc", return_value=pd.DataFrame()):
        result = pp.build_actual_chart("005930.KS", "잉태형", date="2026-05-13", pattern_json={
            "chart_patterns": [],
            "candles": [detection],
        })
    assert result["chart_b64"] is None
    assert "차트 데이터 없음" in result["caption"]


def test_lru_cache_hit_on_second_call():
    """동일 (symbol, pattern, date) 두 번째 호출 시 _fetch_ohlc 미호출."""
    ohlc = pd.DataFrame({
        "Open": [100.0] * 60, "High": [110.0] * 60,
        "Low": [90.0] * 60, "Close": [105.0] * 60, "Volume": [1000] * 60,
    }, index=pd.date_range("2026-03-15", periods=60))
    detection = {"name": "잉태형", "date": "2026-05-13", "signal": "매수"}
    pj = {"chart_patterns": [], "candles": [detection]}

    mock = MagicMock(return_value=ohlc)
    with patch.object(pp, "_fetch_ohlc", mock):
        pp.build_actual_chart("005930.KS", "잉태형", date="2026-05-13", pattern_json=pj)
        pp.build_actual_chart("005930.KS", "잉태형", date="2026-05-13", pattern_json=pj)
    assert mock.call_count == 1  # 두 번째는 cache hit


def test_render_chart_with_tz_aware_index():
    """yfinance 실제 데이터는 tz-aware DatetimeIndex 반환 — 그 케이스 검증."""
    ohlc = pd.DataFrame({
        "Open":  [100.0] * 60,
        "High":  [110.0] * 60,
        "Low":   [90.0]  * 60,
        "Close": [105.0] * 60,
        "Volume": [1000] * 60,
    }, index=pd.date_range("2026-03-15", periods=60, tz="UTC"))
    detection = {
        "name": "더블바텀(W)",
        "low1": {"date": "2026-04-15", "price": 95},
        "low2": {"date": "2026-05-08", "price": 96},
        "neckline": 105,
        "current": 110,
        "breakout": True,
        "from_date": "2026-04-15",
        "to_date": "2026-05-08",
    }
    with patch.object(pp, "_fetch_ohlc", return_value=ohlc):
        result = pp.build_actual_chart("005930.KS", "더블바텀(W)", date=None, pattern_json={
            "chart_patterns": [detection],
            "candles": [],
        })
    # tz mismatch shouldn't raise; chart still renders
    assert result["chart_b64"] is not None, "tz-aware OHLC index must not crash chart rendering"
