import pytest
import src.toss_sync as ts


@pytest.fixture
def _krx_stub(monkeypatch):
    # _load_krx_cache 가 주는 형식: symbol 에 suffix 가 이미 붙어있음
    monkeypatch.setattr(ts, "_krx_listing", lambda: {
        "005930": ".KS",   # KOSPI
        "035720": ".KQ",   # KOSDAQ (가정)
    })


def test_us_symbol_passthrough(_krx_stub):
    assert ts._to_sa_symbol({"symbol": "AAPL", "marketCountry": "US"}) == "AAPL"


def test_kr_kospi_suffix(_krx_stub):
    assert ts._to_sa_symbol({"symbol": "005930", "marketCountry": "KR"}) == "005930.KS"


def test_kr_kosdaq_suffix(_krx_stub):
    assert ts._to_sa_symbol({"symbol": "035720", "marketCountry": "KR"}) == "035720.KQ"


def test_kr_unknown_code_defaults_ks(_krx_stub):
    # listing 에 없는 코드 → .KS 기본
    assert ts._to_sa_symbol({"symbol": "999999", "marketCountry": "KR"}) == "999999.KS"


def test_unconvertible_returns_none(_krx_stub):
    assert ts._to_sa_symbol({"symbol": "", "marketCountry": "KR"}) is None


# --- 미러링 + 안전장치 -------------------------------------------------------

@pytest.fixture
def _pf(tmp_path, monkeypatch):
    """임시 DB 로 portfolio 격리 (test_portfolio.py 패턴 동일)."""
    from src import portfolio as pf
    monkeypatch.setattr(pf, "_DB_PATH", tmp_path / "p.db")
    pf.init_db()
    return pf


def _h(symbol, country, qty, avg):
    return {"symbol": symbol, "marketCountry": country, "quantity": str(qty),
            "averagePurchasePrice": str(avg)}


def test_mirror_adds_and_updates(_pf, _krx_stub):
    _pf.add_holding("admin", "000660.KS", 100000.0, 5)  # 기존, 토스에 없음 → 제거
    res = ts.mirror_to_portfolio("admin", [
        _h("005930", "KR", 10, 70000),
        _h("AAPL", "US", 1.5, 190.5),
    ])
    syms = {r["symbol"]: r for r in _pf.list_holdings("admin")}
    assert "005930.KS" in syms and syms["005930.KS"]["qty"] == 10
    assert "AAPL" in syms and syms["AAPL"]["qty"] == 1.5
    assert "000660.KS" not in syms          # 토스에 없으니 제거
    assert res["added"] == 2 and res["removed"] == 1


def test_mirror_skips_zero_and_negative(_pf, _krx_stub):
    res = ts.mirror_to_portfolio("admin", [
        _h("005930", "KR", 0, 70000),     # qty 0 → skip
        _h("000660", "KR", 5, -1),        # avg<=0 → skip
    ])
    assert _pf.list_holdings("admin") == []
    assert res["skipped"] == 2


def test_mirror_50pct_delete_guard_aborts(_pf, _krx_stub, monkeypatch):
    for s in ("005930.KS", "000660.KS", "035720.KQ", "AAPL"):
        _pf.add_holding("admin", s, 1000.0, 1)
    monkeypatch.delenv("TOSS_SYNC_FORCE", raising=False)
    # 토스가 1종목만 → 3/4=75% 삭제 → 가드 abort
    with pytest.raises(ts.SyncAborted, match="50%"):
        ts.mirror_to_portfolio("admin", [_h("005930", "KR", 1, 1000)])
    # 포트폴리오 무변경
    assert len(_pf.list_holdings("admin")) == 4


def test_mirror_force_bypasses_guard(_pf, _krx_stub, monkeypatch):
    for s in ("005930.KS", "000660.KS", "035720.KQ", "AAPL"):
        _pf.add_holding("admin", s, 1000.0, 1)
    monkeypatch.setenv("TOSS_SYNC_FORCE", "1")
    res = ts.mirror_to_portfolio("admin", [_h("005930", "KR", 1, 1000)])
    assert res["removed"] == 3
