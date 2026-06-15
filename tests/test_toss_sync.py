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
