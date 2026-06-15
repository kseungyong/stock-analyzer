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


def test_add_holding_preserves_fractional_qty():
    # 미국 fractional shares — 1.5 주가 1.0 으로 절삭되면 안 됨.
    p.add_holding("admin", "AAPL", avg_price=190.5, qty=1.5)
    rows = p.list_holdings("admin")
    assert len(rows) == 1
    assert rows[0]["qty"] == 1.5   # int 절삭되면 1.0 으로 실패


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


def test_update_holding_preserves_fractional_qty():
    # 미국 fractional shares — update_holding 으로 1.5 주 갱신 시 절삭 금지.
    p.add_holding("admin", "AAPL", 190.5, 10)
    assert p.update_holding("admin", "AAPL", qty=1.5) is True
    rows = p.list_holdings("admin")
    assert rows[0]["qty"] == 1.5   # int 절삭되면 1.0 으로 실패
    h = p.get_holding_with_pnl("admin", "AAPL")
    assert h["qty"] == 1.5


def test_adjust_preserves_fractional_qty():
    # record_adjust 로 소수점 보유수량 조정 — portfolio.qty 는 float 보존.
    # (audit tx 의 qty 는 정수 절삭이 의도된 동작 — 여기선 검증 대상 아님)
    p.add_holding("admin", "AAPL", 190.5, 10)
    assert p.record_adjust("admin", "AAPL", qty=2.5) is True
    rows = p.list_holdings("admin")
    assert rows[0]["qty"] == 2.5   # int 절삭되면 2.0 으로 실패
    h = p.get_holding_with_pnl("admin", "AAPL")
    assert h["qty"] == 2.5


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


# --- Transactions: record_buy / record_sell / record_adjust / list ---------

def test_buy_first_time_creates_holding():
    new_state = p.record_buy("admin", "AAPL", 150.0, 10, "초기 매수")
    assert new_state == {"avg_price": 150.0, "qty": 10}
    h = p.list_holdings("admin")[0]
    assert h["symbol"] == "AAPL"
    assert h["avg_price"] == 150.0
    assert h["qty"] == 10
    txs = p.list_transactions("admin")
    assert len(txs) == 1
    assert txs[0]["side"] == "BUY"
    assert txs[0]["price"] == 150.0
    assert txs[0]["qty"] == 10


def test_buy_recomputes_weighted_avg():
    """10주 @ 100원 → 추가 10주 @ 200원 → 평균 150원, 20주."""
    p.record_buy("admin", "AAPL", 100.0, 10)
    p.record_buy("admin", "AAPL", 200.0, 10)
    h = p.list_holdings("admin")[0]
    assert h["avg_price"] == 150.0
    assert h["qty"] == 20


def test_buy_recomputes_weighted_avg_uneven():
    """5주 @ 100원 + 15주 @ 200원 → (500 + 3000)/20 = 175원."""
    p.record_buy("admin", "AAPL", 100.0, 5)
    p.record_buy("admin", "AAPL", 200.0, 15)
    h = p.list_holdings("admin")[0]
    assert h["avg_price"] == 175.0
    assert h["qty"] == 20
    txs = p.list_transactions("admin", "AAPL")
    assert len(txs) == 2


def test_buy_invalid_price_raises():
    with pytest.raises(ValueError):
        p.record_buy("admin", "AAPL", 0, 10)
    with pytest.raises(ValueError):
        p.record_buy("admin", "AAPL", -10, 10)


def test_buy_invalid_qty_raises():
    with pytest.raises(ValueError):
        p.record_buy("admin", "AAPL", 150.0, 0)
    with pytest.raises(ValueError):
        p.record_buy("admin", "AAPL", 150.0, -5)


def test_sell_partial_keeps_avg():
    """매도 시 평균가 유지 (cost basis)."""
    p.record_buy("admin", "AAPL", 150.0, 10)
    new_state = p.record_sell("admin", "AAPL", 200.0, 3)
    assert new_state == {"avg_price": 150.0, "qty": 7}
    h = p.list_holdings("admin")[0]
    assert h["avg_price"] == 150.0
    assert h["qty"] == 7


def test_sell_full_deletes_holding():
    """전량 매도 → portfolio row 삭제, transactions 유지."""
    p.record_buy("admin", "AAPL", 150.0, 10)
    result = p.record_sell("admin", "AAPL", 200.0, 10)
    assert result is None
    assert p.list_holdings("admin") == []
    # transactions 는 BUY + SELL 모두 보존
    txs = p.list_transactions("admin", "AAPL")
    assert len(txs) == 2
    assert {t["side"] for t in txs} == {"BUY", "SELL"}


def test_sell_exceeds_holding_raises():
    p.record_buy("admin", "AAPL", 150.0, 10)
    with pytest.raises(ValueError):
        p.record_sell("admin", "AAPL", 200.0, 11)


def test_sell_missing_holding_raises():
    with pytest.raises(ValueError):
        p.record_sell("admin", "AAPL", 200.0, 5)


def test_adjust_records_audit_transaction():
    p.add_holding("admin", "AAPL", 150.0, 10)  # 초기 add 는 tx 안 남김 (legacy)
    ok = p.record_adjust("admin", "AAPL", avg_price=160.0)
    assert ok is True
    h = p.list_holdings("admin")[0]
    assert h["avg_price"] == 160.0
    txs = p.list_transactions("admin", "AAPL")
    assert any(t["side"] == "ADJUST" for t in txs)


def test_list_transactions_filters_by_symbol():
    p.record_buy("admin", "AAPL", 100.0, 10)
    p.record_buy("admin", "MSFT", 300.0, 5)
    p.record_buy("admin", "AAPL", 200.0, 5)
    aapl = p.list_transactions("admin", "AAPL")
    msft = p.list_transactions("admin", "MSFT")
    all_ = p.list_transactions("admin")
    assert len(aapl) == 2
    assert len(msft) == 1
    assert len(all_) == 3


def test_list_transactions_user_isolation():
    p.record_buy("admin", "AAPL", 100.0, 10)
    p.record_buy("shnoh", "AAPL", 200.0, 5)
    admin_txs = p.list_transactions("admin")
    shnoh_txs = p.list_transactions("shnoh")
    assert len(admin_txs) == 1 and admin_txs[0]["price"] == 100.0
    assert len(shnoh_txs) == 1 and shnoh_txs[0]["price"] == 200.0


def test_seed_creates_buy_tx_for_existing_holdings(tmp_path, monkeypatch):
    """init_db 이전에 portfolio 에 보유분 있고 transactions 비어있으면 seed."""
    db_path = tmp_path / "seed.db"
    monkeypatch.setattr(p, "_DB_PATH", db_path)
    monkeypatch.setattr(ac, "_DB_PATH", db_path)
    # 신 스키마로 portfolio 만 만들고 데이터 삽입 (transactions 테이블 없이)
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE portfolio (
            username TEXT NOT NULL, symbol TEXT NOT NULL,
            avg_price REAL NOT NULL, qty INTEGER NOT NULL,
            added_at INTEGER NOT NULL, notes TEXT,
            PRIMARY KEY (username, symbol)
        );
    """)
    conn.execute(
        "INSERT INTO portfolio VALUES ('admin', 'AAPL', 150.0, 10, 1000, NULL)")
    conn.execute(
        "INSERT INTO portfolio VALUES ('admin', 'MSFT', 300.0, 5, 1000, NULL)")
    conn.commit()
    conn.close()
    p.init_db()
    txs = p.list_transactions("admin")
    assert len(txs) == 2
    syms = {t["symbol"] for t in txs}
    assert syms == {"AAPL", "MSFT"}
    for t in txs:
        assert t["side"] == "BUY"
        assert "seed" in t["notes"].lower() or "초기" in t["notes"]


def test_seed_idempotent_does_not_duplicate(tmp_path, monkeypatch):
    """init_db() 두 번 호출해도 transactions 중복 생성 안 됨."""
    db_path = tmp_path / "seed_idem.db"
    monkeypatch.setattr(p, "_DB_PATH", db_path)
    monkeypatch.setattr(ac, "_DB_PATH", db_path)
    p.init_db()
    ac.init_db()
    p.add_holding("admin", "AAPL", 150.0, 10)
    p.init_db()
    p.init_db()
    # add_holding 은 tx 안 남기지만 seed 도 안 돌아야 함 (이미 transactions 테이블 존재)
    # 단, transactions 가 비어있다면 seed 가 BUY 하나 생성 → 멱등성 두 번째 init 에서 변동 없음
    txs1 = p.list_transactions("admin")
    p.init_db()
    txs2 = p.list_transactions("admin")
    assert len(txs1) == len(txs2)


# --- sync meta (마지막 동기화 시각) ------------------------------------------

def test_get_last_sync_none_when_absent():
    assert p.get_last_sync("admin", "toss") is None


def test_record_and_get_sync():
    p.record_sync("admin", "toss", ts=1700000000)
    assert p.get_last_sync("admin", "toss") == 1700000000


def test_record_sync_default_ts_is_now():
    before = int(time.time())
    p.record_sync("admin", "toss")
    after = int(time.time())
    got = p.get_last_sync("admin", "toss")
    assert got is not None and before <= got <= after


def test_record_sync_upsert_overwrites():
    p.record_sync("admin", "toss", ts=1000)
    p.record_sync("admin", "toss", ts=2000)
    assert p.get_last_sync("admin", "toss") == 2000


def test_sync_meta_isolated_by_username():
    p.record_sync("admin", "toss", ts=1000)
    p.record_sync("shnoh", "toss", ts=2000)
    assert p.get_last_sync("admin", "toss") == 1000
    assert p.get_last_sync("shnoh", "toss") == 2000


def test_sync_meta_isolated_by_source():
    p.record_sync("admin", "toss", ts=1000)
    p.record_sync("admin", "kis", ts=2000)
    assert p.get_last_sync("admin", "toss") == 1000
    assert p.get_last_sync("admin", "kis") == 2000
