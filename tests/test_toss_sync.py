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


def _h(symbol, country, qty, avg, name=""):
    return {"symbol": symbol, "marketCountry": country, "quantity": str(qty),
            "averagePurchasePrice": str(avg), "name": name}


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


def test_mirror_stores_toss_name(_pf, _krx_stub):
    # 토스 holding 의 name 필드가 portfolio 에 저장돼야 함 (229200 등 종목명 공백 방지).
    ts.mirror_to_portfolio("admin", [
        _h("229200", "KR", 3, 12000, name="KODEX 코스닥150"),
        _h("AAPL", "US", 1.5, 190.5, name="Apple Inc."),
    ])
    syms = {r["symbol"]: r for r in _pf.list_holdings("admin")}
    assert syms["229200.KS"]["name"] == "KODEX 코스닥150"
    assert syms["AAPL"]["name"] == "Apple Inc."


def test_mirror_missing_name_stores_empty(_pf, _krx_stub):
    # name 필드 없는 holding 도 깨지지 않고 빈 문자열로 저장.
    ts.mirror_to_portfolio("admin", [
        {"symbol": "005930", "marketCountry": "KR",
         "quantity": "10", "averagePurchasePrice": "70000"},
    ])
    h = _pf.list_holdings("admin")[0]
    assert h["name"] == ""


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


# --- Fix B: _to_float 비유한수 차단 -----------------------------------------

def test_to_float_rejects_nan_inf():
    assert ts._to_float("nan") is None
    assert ts._to_float("inf") is None
    assert ts._to_float("-inf") is None
    # 정상 값은 통과
    assert ts._to_float("1,234.5") == 1234.5


def test_mirror_skips_nan_qty(_pf, _krx_stub):
    # nan qty 종목은 _to_float→None 으로 skip 되어 DB 에 저장 안 됨
    res = ts.mirror_to_portfolio("admin", [
        _h("005930", "KR", "nan", 70000),
        _h("AAPL", "US", 1.5, 190.5),
    ])
    syms = {r["symbol"] for r in _pf.list_holdings("admin")}
    assert "005930.KS" not in syms
    assert "AAPL" in syms
    assert res["skipped"] == 1 and res["added"] == 1


# --- Fix A: apply 부분실패 격리 ----------------------------------------------

def test_mirror_isolates_add_failure(_pf, _krx_stub, monkeypatch):
    # 특정 symbol 에서 add_holding 이 예외 → 그 종목만 failed, 나머지는 정상 added
    real_add = _pf.add_holding

    def flaky_add(username, sym, avg, qty, name=None):
        if sym == "005930.KS":
            raise RuntimeError("database is locked")
        return real_add(username, sym, avg, qty, name=name)

    monkeypatch.setattr(ts.portfolio_db, "add_holding", flaky_add)
    res = ts.mirror_to_portfolio("admin", [
        _h("005930", "KR", 10, 70000),   # 쓰기 실패
        _h("AAPL", "US", 1.5, 190.5),    # 정상
    ])
    syms = {r["symbol"] for r in _pf.list_holdings("admin")}
    assert "005930.KS" not in syms      # 실패한 종목은 미저장
    assert "AAPL" in syms               # 나머지는 진행
    assert res["failed"] == 1 and res["added"] == 1


# --- run_sync 오케스트레이션 (client stub) -----------------------------------

class _FakeClient:
    def __init__(self, accounts, holdings):
        self._accounts, self._holdings = accounts, holdings
    def __enter__(self): return self
    def __exit__(self, *a): return None
    def fetch_accounts(self): return self._accounts
    def fetch_holdings(self, seq): return self._holdings


def test_run_sync_dry_run_no_db_change(_pf, _krx_stub, monkeypatch):
    fake = _FakeClient([{"accountNo": "1", "accountSeq": 7, "accountType": "BROKERAGE"}],
                       [_h("005930", "KR", 10, 70000)])
    monkeypatch.setattr(ts, "TossClient", lambda: fake)
    res = ts.run_sync("admin", dry_run=True)
    assert res["target_count"] == 1
    assert _pf.list_holdings("admin") == []   # dry_run → DB 무변경
    assert _pf.get_last_sync("admin", "toss") is None   # dry_run → 시각 미기록


def test_run_sync_aborts_on_empty_accounts(_pf, _krx_stub, monkeypatch):
    fake = _FakeClient([], [])
    monkeypatch.setattr(ts, "TossClient", lambda: fake)
    with pytest.raises(ts.SyncAborted, match="계좌"):
        ts.run_sync("admin")


def test_run_sync_applies(_pf, _krx_stub, monkeypatch):
    fake = _FakeClient([{"accountNo": "1", "accountSeq": 7, "accountType": "BROKERAGE"}],
                       [_h("005930", "KR", 10, 70000)])
    monkeypatch.setattr(ts, "TossClient", lambda: fake)
    res = ts.run_sync("admin")
    assert res["added"] == 1
    assert {r["symbol"] for r in _pf.list_holdings("admin")} == {"005930.KS"}
    assert _pf.get_last_sync("admin", "toss") is not None   # 성공 → 시각 기록
