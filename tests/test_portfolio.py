"""portfolio 모듈 테스트 — DB 영속화 + JOIN 검증."""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from src import portfolio as p
from src import analysis_cache as ac


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch, tmp_path):
    """각 테스트마다 임시 DB — 모듈의 _DB_PATH 를 monkeypatch."""
    db_path = tmp_path / "test_predictions.db"
    monkeypatch.setattr(p, "_DB_PATH", db_path)
    monkeypatch.setattr(ac, "_DB_PATH", db_path)
    p.init_db()
    ac.init_db()
    yield


def test_add_new_holding_returns_true():
    new = p.add_holding("AAPL", 150.50, 10, "tech stock")
    assert new is True
    holdings = p.list_holdings()
    assert len(holdings) == 1
    assert holdings[0]["symbol"] == "AAPL"
    assert holdings[0]["avg_price"] == 150.50
    assert holdings[0]["qty"] == 10
    assert holdings[0]["notes"] == "tech stock"


def test_add_existing_returns_false_and_updates():
    p.add_holding("AAPL", 150.0, 10)
    new = p.add_holding("AAPL", 160.0, 20, "updated")
    assert new is False
    holdings = p.list_holdings()
    assert holdings[0]["avg_price"] == 160.0
    assert holdings[0]["qty"] == 20
    assert holdings[0]["notes"] == "updated"


def test_invalid_avg_price_raises():
    with pytest.raises(ValueError):
        p.add_holding("AAPL", 0, 10)
    with pytest.raises(ValueError):
        p.add_holding("AAPL", -10, 5)


def test_invalid_qty_raises():
    with pytest.raises(ValueError):
        p.add_holding("AAPL", 150.0, -5)


def test_remove_existing_returns_true():
    p.add_holding("AAPL", 150.0, 10)
    assert p.remove_holding("AAPL") is True
    assert p.list_holdings() == []


def test_remove_missing_returns_false():
    assert p.remove_holding("NONE") is False


def test_update_partial():
    p.add_holding("AAPL", 150.0, 10, "old note")
    assert p.update_holding("AAPL", avg_price=155.0) is True
    h = p.list_holdings()[0]
    assert h["avg_price"] == 155.0
    assert h["qty"] == 10  # 유지
    assert h["notes"] == "old note"


def test_update_missing_returns_false():
    assert p.update_holding("NONE", avg_price=100.0) is False


def test_count_holdings():
    assert p.count_holdings() == 0
    p.add_holding("AAPL", 150.0, 10)
    p.add_holding("MSFT", 300.0, 5)
    assert p.count_holdings() == 2


def test_list_with_pnl_no_analysis():
    """분석 캐시 없으면 last_close=None 등."""
    p.add_holding("AAPL", 150.0, 10)
    holdings = p.list_holdings_with_pnl()
    assert len(holdings) == 1
    h = holdings[0]
    assert h["symbol"] == "AAPL"
    assert h["avg_price"] == 150.0
    assert h["last_close"] is None
    assert h["signal_value"] is None


def test_list_with_pnl_join_with_cache():
    """analysis_cache UPSERT 후 JOIN 결과 확인."""
    p.add_holding("AAPL", 150.0, 10)
    ac.put(
        "AAPL", "us", "<p>x</p>", "manual",
        signal_value="매수", signal_score=3,
        last_close=160.0,
        pattern_signal="매수", pattern_score=8,
    )
    holdings = p.list_holdings_with_pnl()
    h = holdings[0]
    assert h["last_close"] == 160.0
    assert h["signal_value"] == "매수"
    assert h["signal_score"] == 3
    assert h["pattern_score"] == 8
