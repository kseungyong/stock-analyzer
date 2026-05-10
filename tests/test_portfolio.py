"""portfolio 모듈 테스트 — 사용자별 격리 + JOIN 검증."""
from __future__ import annotations

import sqlite3
import time

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


# --- 기본 CRUD (사용자: admin) -----------------------------------------------

def test_add_new_holding_returns_true():
    new = p.add_holding("admin", "AAPL", 150.50, 10, "tech stock")
    assert new is True
    holdings = p.list_holdings("admin")
    assert len(holdings) == 1
    assert holdings[0]["symbol"] == "AAPL"
    assert holdings[0]["avg_price"] == 150.50
    assert holdings[0]["qty"] == 10
    assert holdings[0]["notes"] == "tech stock"


def test_add_existing_returns_false_and_updates():
    p.add_holding("admin", "AAPL", 150.0, 10)
    new = p.add_holding("admin", "AAPL", 160.0, 20, "updated")
    assert new is False
    holdings = p.list_holdings("admin")
    assert holdings[0]["avg_price"] == 160.0
    assert holdings[0]["qty"] == 20
    assert holdings[0]["notes"] == "updated"


def test_invalid_avg_price_raises():
    with pytest.raises(ValueError):
        p.add_holding("admin", "AAPL", 0, 10)
    with pytest.raises(ValueError):
        p.add_holding("admin", "AAPL", -10, 5)


def test_invalid_qty_raises():
    with pytest.raises(ValueError):
        p.add_holding("admin", "AAPL", 150.0, -5)


def test_remove_existing_returns_true():
    p.add_holding("admin", "AAPL", 150.0, 10)
    assert p.remove_holding("admin", "AAPL") is True
    assert p.list_holdings("admin") == []


def test_remove_missing_returns_false():
    assert p.remove_holding("admin", "NONE") is False


def test_update_partial():
    p.add_holding("admin", "AAPL", 150.0, 10, "old note")
    assert p.update_holding("admin", "AAPL", avg_price=155.0) is True
    h = p.list_holdings("admin")[0]
    assert h["avg_price"] == 155.0
    assert h["qty"] == 10
    assert h["notes"] == "old note"


def test_update_missing_returns_false():
    assert p.update_holding("admin", "NONE", avg_price=100.0) is False


def test_count_holdings():
    assert p.count_holdings("admin") == 0
    p.add_holding("admin", "AAPL", 150.0, 10)
    p.add_holding("admin", "MSFT", 300.0, 5)
    assert p.count_holdings("admin") == 2


def test_list_with_pnl_no_analysis():
    p.add_holding("admin", "AAPL", 150.0, 10)
    holdings = p.list_holdings_with_pnl("admin")
    assert len(holdings) == 1
    h = holdings[0]
    assert h["symbol"] == "AAPL"
    assert h["avg_price"] == 150.0
    assert h["last_close"] is None
    assert h["signal_value"] is None


def test_list_with_pnl_join_with_cache():
    p.add_holding("admin", "AAPL", 150.0, 10)
    ac.put(
        "AAPL", "us", "<p>x</p>", "manual",
        signal_value="매수", signal_score=3,
        last_close=160.0,
        pattern_signal="매수", pattern_score=8,
    )
    holdings = p.list_holdings_with_pnl("admin")
    h = holdings[0]
    assert h["last_close"] == 160.0
    assert h["signal_value"] == "매수"
    assert h["signal_score"] == 3
    assert h["pattern_score"] == 8


# --- 사용자 격리 -----------------------------------------------------------

def test_user_a_holding_invisible_to_user_b():
    p.add_holding("admin", "AAPL", 150.0, 10)
    assert len(p.list_holdings("admin")) == 1
    assert p.list_holdings("shnoh") == []
    assert p.count_holdings("admin") == 1
    assert p.count_holdings("shnoh") == 0


def test_user_a_cannot_remove_user_b_holding():
    p.add_holding("admin", "AAPL", 150.0, 10)
    assert p.remove_holding("shnoh", "AAPL") is False
    assert len(p.list_holdings("admin")) == 1


def test_user_a_cannot_update_user_b_holding():
    p.add_holding("admin", "AAPL", 150.0, 10)
    assert p.update_holding("shnoh", "AAPL", avg_price=999.0) is False
    assert p.list_holdings("admin")[0]["avg_price"] == 150.0


def test_same_symbol_different_users_independent():
    p.add_holding("admin", "AAPL", 150.0, 10, "admin note")
    p.add_holding("shnoh", "AAPL", 200.0, 5, "shnoh note")
    admin_h = p.list_holdings("admin")[0]
    shnoh_h = p.list_holdings("shnoh")[0]
    assert admin_h["avg_price"] == 150.0 and admin_h["qty"] == 10
    assert shnoh_h["avg_price"] == 200.0 and shnoh_h["qty"] == 5
    # analysis_cache 공유 → 같은 last_close, 다른 PnL
    ac.put("AAPL", "us", "<p>x</p>", "manual", last_close=180.0)
    admin_pnl = p.get_holding_with_pnl("admin", "AAPL")
    shnoh_pnl = p.get_holding_with_pnl("shnoh", "AAPL")
    assert admin_pnl["last_close"] == 180.0
    assert shnoh_pnl["last_close"] == 180.0
    assert abs(admin_pnl["pnl_pct"] - 20.0) < 0.01
    assert abs(shnoh_pnl["pnl_pct"] - (-10.0)) < 0.01


# --- 마이그레이션 (구 스키마 → 신 스키마) -----------------------------------

def test_migrate_legacy_assigns_to_admin(tmp_path, monkeypatch):
    """구 스키마 (symbol PK) DB 에 데이터 → init_db() → 모두 admin 소유."""
    db_path = tmp_path / "legacy.db"
    monkeypatch.setattr(p, "_DB_PATH", db_path)
    monkeypatch.setattr(ac, "_DB_PATH", db_path)
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE portfolio (
            symbol     TEXT PRIMARY KEY,
            avg_price  REAL NOT NULL,
            qty        INTEGER NOT NULL DEFAULT 0,
            added_at   INTEGER NOT NULL,
            notes      TEXT
        );
    """)
    now = int(time.time())
    conn.executemany(
        "INSERT INTO portfolio(symbol, avg_price, qty, added_at, notes) "
        "VALUES (?, ?, ?, ?, ?)",
        [("AAPL", 150.0, 10, now, "legacy note"),
         ("MSFT", 300.0, 5, now, None)],
    )
    conn.commit()
    conn.close()

    p.init_db()
    ac.init_db()

    admin_h = p.list_holdings("admin")
    assert len(admin_h) == 2
    syms = {h["symbol"] for h in admin_h}
    assert syms == {"AAPL", "MSFT"}
    aapl = next(h for h in admin_h if h["symbol"] == "AAPL")
    assert aapl["avg_price"] == 150.0
    assert aapl["qty"] == 10
    assert aapl["notes"] == "legacy note"
    assert p.list_holdings("shnoh") == []


def test_migration_idempotent(tmp_path, monkeypatch):
    """init_db() 여러 번 호출해도 데이터 유실 없음."""
    db_path = tmp_path / "idempotent.db"
    monkeypatch.setattr(p, "_DB_PATH", db_path)
    monkeypatch.setattr(ac, "_DB_PATH", db_path)
    p.init_db()
    ac.init_db()
    p.add_holding("admin", "AAPL", 150.0, 10)
    p.init_db()
    p.init_db()
    assert len(p.list_holdings("admin")) == 1
    assert p.list_holdings("admin")[0]["avg_price"] == 150.0
