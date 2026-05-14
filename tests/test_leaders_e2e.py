"""leaders-refresh cron 흐름 end-to-end (fake yfinance + fake Gemini)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest


@pytest.fixture
def patched_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """leader_cache._DB_PATH + universe yaml + yfinance + Gemini 모두 patch."""
    from src import leader_cache, leader_filter, leader_llm

    db = tmp_path / "predictions.db"
    monkeypatch.setattr(leader_cache, "_DB_PATH", str(db))
    leader_cache.init_db()

    universe = tmp_path / "universe.yaml"
    universe.write_text(
        'kospi200:\n  - "005930"\n  - "000660"\nkosdaq150:\n  - "247540"\netf:\n  - "069500"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("AUTO_TRADER_UNIVERSE_PATH", str(universe))

    # fake yfinance
    def fake_ticker(sym):
        t = MagicMock()
        info_by_sym = {
            "005930.KS": {"longName": "삼성전자", "sector": "Tech", "industry": "Semi",
                          "marketCap": 400e12, "trailingEps": 5000.0,
                          "forwardEps": 6000.0, "trailingPE": 14.0,
                          "earningsGrowth": 0.2, "revenueGrowth": 0.18},
            "000660.KS": {"longName": "SK하이닉스", "sector": "Tech", "industry": "Semi",
                          "marketCap": 100e12, "trailingEps": 3000.0,
                          "forwardEps": 4000.0, "trailingPE": 12.0,
                          "earningsGrowth": 0.3, "revenueGrowth": 0.25},
            "247540.KQ": {"longName": "에코프로비엠", "sector": "Materials",
                          "industry": "Battery", "marketCap": 10e12,
                          "trailingEps": -100.0, "forwardEps": 200.0,
                          "trailingPE": -50.0},
        }
        if sym in ("^KS11", "^KQ11"):
            t.history.return_value = pd.DataFrame(
                {"Close": [3000.0] * 252 + [3300.0]},
                index=pd.date_range("2025-05-15", periods=253, freq="D"),
            )
            return t
        t.info = info_by_sym.get(sym, {})
        closes = [100.0] * 252 + [200.0]  # +100%
        t.history.return_value = pd.DataFrame(
            {"Close": closes, "High": [200.0] * 253},
            index=pd.date_range("2025-05-15", periods=253, freq="D"),
        )
        return t

    monkeypatch.setattr(leader_filter.yf, "Ticker", fake_ticker)

    # fake Gemini
    payload = json.dumps({
        "tam_narrative": "T", "narrative_expansion": "N",
        "bottleneck": "B", "moat": "M",
    })
    fake_model = MagicMock()
    fake_resp = MagicMock()
    fake_resp.text = payload
    fake_model.generate_content.return_value = fake_resp
    monkeypatch.setattr(leader_llm, "_get_model", lambda: fake_model)
    monkeypatch.setattr(leader_llm, "_daily_count", lambda: 0)

    # Use MagicMock for _increment_daily_count so we can assert call count
    mock_increment = MagicMock()
    monkeypatch.setattr(leader_llm, "_increment_daily_count", mock_increment)

    return {"db": db, "universe": universe, "mock_increment": mock_increment}


def test_leaders_refresh_e2e(patched_runtime, monkeypatch: pytest.MonkeyPatch):
    """e2e: load universe → filter → LLM analyze → cache upsert."""
    import main
    rc = main.leaders_refresh()
    assert rc == 0

    from src import leader_cache
    rows = leader_cache.list_active()
    # 삼성전자 (시총 1위, +100% > KOSPI +10% +20%p) 통과해야
    syms = {r["symbol"] for r in rows}
    assert "005930.KS" in syms
    samsung = next(r for r in rows if r["symbol"] == "005930.KS")
    assert samsung["llm_tam_narrative"] == "T"
    assert samsung["llm_moat"] == "M"

    # _increment_daily_count must be called once per successful LLM call
    mock_increment = patched_runtime["mock_increment"]
    assert mock_increment.call_count >= 1


def test_leaders_refresh_e2e_llm_failure(patched_runtime, monkeypatch: pytest.MonkeyPatch):
    """LLM analyze_one 이 success=False 를 반환할 때: cache에 error 필드 채워지고 cron 은 정상 종료."""
    from src import leader_llm, leader_cache
    from src.leader_llm import LLMResult

    # 삼성전자는 LLM 실패, 다른 종목은 정상
    original_analyze = leader_llm.analyze_one

    def failing_analyze_one(inputs):
        if inputs.get("symbol") == "005930.KS":
            return LLMResult(fields={}, raw="error_raw", error="api_error")
        return original_analyze(inputs)

    monkeypatch.setattr(leader_llm, "analyze_one", failing_analyze_one)

    import main
    rc = main.leaders_refresh()
    assert rc == 0  # cron must not crash

    rows = leader_cache.list_active()
    syms = {r["symbol"] for r in rows}
    # 삼성전자는 filter 통과해야 (정량 결과는 있어야)
    assert "005930.KS" in syms

    samsung = next(r for r in rows if r["symbol"] == "005930.KS")
    # LLM 실패 → llm_error 채워지고 tam_narrative 없어야
    assert samsung["llm_error"] == "api_error"
    assert samsung["llm_tam_narrative"] is None
