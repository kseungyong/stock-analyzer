"""/api/pattern-popup/* 라우트 통합 테스트."""
from __future__ import annotations

import json
from unittest.mock import patch

import pandas as pd
import pytest

from src import pattern_metadata as pm
from src import pattern_popup as pp
from src.web_app import app


@pytest.fixture(autouse=True)
def reset_caches():
    pm.reset_cache()
    pp._chart_cache.clear()
    yield


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_textbook_returns_known_pattern(client):
    resp = client.get("/api/pattern-popup/textbook?pattern=더블바텀(W)")
    assert resp.status_code == 200
    j = resp.get_json()
    assert j["pattern"] == "더블바텀(W)"
    assert "<svg" in j["svg"]
    assert "<p" in j["description_html"]
    assert j["signal_typical"] == "매수"


def test_textbook_returns_404_for_unknown(client):
    resp = client.get("/api/pattern-popup/textbook?pattern=없는패턴XYZ")
    assert resp.status_code == 404


def test_textbook_missing_pattern_param(client):
    resp = client.get("/api/pattern-popup/textbook")
    assert resp.status_code == 400


def test_actual_with_mocked_data(client, monkeypatch):
    """analysis cache 에서 pattern_json 가져와서 actual chart 응답."""
    pj = {
        "chart_patterns": [{
            "name": "더블바텀(W)",
            "to_date": "2026-05-10",
            "from_date": "2026-04-15",
            "low1": {"date": "2026-04-15", "price": 95},
            "low2": {"date": "2026-05-08", "price": 96},
            "neckline": 105, "current": 110, "breakout": True,
        }],
        "candles": [],
    }
    ohlc = pd.DataFrame({
        "Open": [100.0]*60, "High": [110.0]*60,
        "Low": [90.0]*60, "Close": [105.0]*60, "Volume":[1000]*60,
    }, index=pd.date_range("2026-03-15", periods=60))

    # mock cache_row fetch + yfinance
    monkeypatch.setattr(pp, "_fetch_ohlc", lambda s: ohlc)
    monkeypatch.setattr(
        "src.web_app._fetch_pattern_json_for_symbol",
        lambda sym: pj,
    )

    resp = client.get("/api/pattern-popup/actual?symbol=005930.KS&pattern=더블바텀(W)")
    assert resp.status_code == 200
    j = resp.get_json()
    assert j["symbol"] == "005930.KS"
    assert j["chart_b64"] is not None
    assert "넥라인" in j["caption"] or "돌파" in j["caption"]
    assert "signal_at_detection" in j  # response key present
    assert j["signal_at_detection"] == "매수"  # double bottom with breakout=True


def test_actual_returns_null_chart_when_no_detection(client, monkeypatch):
    """검출 없음 → 200 + null."""
    monkeypatch.setattr(
        "src.web_app._fetch_pattern_json_for_symbol",
        lambda sym: {"chart_patterns": [], "candles": []},
    )
    resp = client.get("/api/pattern-popup/actual?symbol=005930.KS&pattern=더블바텀(W)")
    assert resp.status_code == 200
    j = resp.get_json()
    assert j["chart_b64"] is None


def test_actual_missing_required_params(client):
    resp = client.get("/api/pattern-popup/actual?symbol=005930.KS")  # pattern 누락
    assert resp.status_code == 400
    resp = client.get("/api/pattern-popup/actual?pattern=잉태형")  # symbol 누락
    assert resp.status_code == 400


def test_actual_returns_404_when_no_cache_row(client, monkeypatch):
    """analysis cache row 자체 없으면 404."""
    monkeypatch.setattr(
        "src.web_app._fetch_pattern_json_for_symbol",
        lambda sym: None,
    )
    resp = client.get("/api/pattern-popup/actual?symbol=UNKNOWN.KS&pattern=더블바텀(W)")
    assert resp.status_code == 404


def test_textbook_only_get_method(client):
    resp = client.post("/api/pattern-popup/textbook?pattern=더블바텀(W)")
    assert resp.status_code == 405


def test_actual_only_get_method(client):
    resp = client.post("/api/pattern-popup/actual?symbol=005930.KS&pattern=더블바텀(W)")
    assert resp.status_code == 405


def test_fetch_pattern_json_sql_uses_correct_column():
    """Regression: _fetch_pattern_json_for_symbol uses cache_key (not symbol).

    Mocked tests in this file patch the helper directly so the SQL string is never
    exercised. This test hits the real DB schema with a known-absent key — if SQL
    references a wrong column, sqlite3 raises 'no such column' which is swallowed
    by the broad except clause, but the warning log will contain the column error.
    """
    import logging
    from src.web_app import _fetch_pattern_json_for_symbol

    records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, r): records.append(r.getMessage())

    logger = logging.getLogger("src.web_app")
    h = _Capture()
    logger.addHandler(h)
    try:
        result = _fetch_pattern_json_for_symbol("NONEXISTENT_REGRESSION_KEY_XYZ")
        assert result is None
        for msg in records:
            assert "no such column" not in msg.lower(), f"SQL column mismatch: {msg}"
    finally:
        logger.removeHandler(h)


def test_textbook_triangle_variants(client):
    """3개 삼각형 변형 모두 매칭 (regression test for cross-task name mismatch)."""
    for pattern in ['상승 삼각형', '하락 삼각형', '삼각형 수렴 (대칭)']:
        resp = client.get('/api/pattern-popup/textbook?pattern=' + pattern)
        assert resp.status_code == 200, f'{pattern} should return 200, got {resp.status_code}'
        j = resp.get_json()
        assert j['pattern'] == pattern
        assert 'svg' in j and '<svg' in j['svg']
