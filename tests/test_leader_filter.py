"""leader_filter: 정량 hard filter (1·2·3번)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import math
import pandas as pd
import pytest

from src import leader_filter


def _make_universe_yaml(tmp_path: Path) -> Path:
    """auto-trader 형식 minimal yaml 생성 (etf 섹션 포함 — filter 가 제외해야)."""
    p = tmp_path / "universe.yaml"
    p.write_text("""kospi200:
  - "005930"  # 삼성전자
  - "000660"  # SK하이닉스
kosdaq150:
  - "247540"  # 에코프로비엠
etf:
  - "069500"  # KODEX 200
""", encoding="utf-8")
    return p


def test_load_universe_excludes_etf(tmp_path: Path):
    p = _make_universe_yaml(tmp_path)
    syms = leader_filter.load_universe(str(p))
    assert ("005930.KS", "KOSPI") in syms
    assert ("000660.KS", "KOSPI") in syms
    assert ("247540.KQ", "KOSDAQ") in syms
    # ETF must be excluded
    assert all(not s[0].startswith("069500") for s in syms)


def test_load_universe_missing_section_ok(tmp_path: Path):
    p = tmp_path / "u.yaml"
    p.write_text("kospi200:\n  - \"005930\"\n", encoding="utf-8")
    syms = leader_filter.load_universe(str(p))
    assert syms == [("005930.KS", "KOSPI")]


def test_compute_index_return_uses_first_last_close(monkeypatch: pytest.MonkeyPatch):
    """^KS11 의 1년 수익률 = (마지막 종가 / 첫 종가) - 1."""
    fake_hist = pd.DataFrame(
        {"Close": [3000.0, 3500.0]},
        index=pd.to_datetime(["2025-05-15", "2026-05-15"]),
    )
    fake_ticker = MagicMock()
    fake_ticker.history.return_value = fake_hist
    monkeypatch.setattr(leader_filter.yf, "Ticker", lambda s: fake_ticker)
    r = leader_filter.compute_index_return("^KS11")
    assert math.isclose(r, 500.0 / 3000.0, rel_tol=1e-6)


def test_compute_index_return_returns_none_when_empty(monkeypatch: pytest.MonkeyPatch):
    fake_ticker = MagicMock()
    fake_ticker.history.return_value = pd.DataFrame()
    monkeypatch.setattr(leader_filter.yf, "Ticker", lambda s: fake_ticker)
    assert leader_filter.compute_index_return("^KS11") is None


def _fake_ticker(info: dict, hist_closes: list[float]) -> MagicMock:
    t = MagicMock()
    t.info = info
    t.history.return_value = pd.DataFrame(
        {"Close": hist_closes, "High": [c * 1.1 for c in hist_closes]},
        index=pd.date_range("2025-05-15", periods=len(hist_closes), freq="D"),
    )
    return t


def test_evaluate_passes_when_all_3_conds_met():
    info = {
        "longName": "삼성전자", "sector": "Tech", "industry": "Semi",
        "marketCap": 400_000_000_000_000,
        "trailingEps": 5000.0, "forwardEps": 6000.0,
        "earningsGrowth": 0.2, "revenueGrowth": 0.18,
        "trailingPE": 14.0,
    }
    closes = [60000.0] * 252 + [80000.0]  # 1년 +33%
    cand = leader_filter._evaluate_single(
        symbol="005930.KS", market="KOSPI",
        ticker=_fake_ticker(info, closes),
        index_return_1y=0.10,
        market_cap_quintile=1,
        pe_quintile=3,
    )
    assert cand is not None
    assert cand.cond1_passed is True
    assert cand.cond2_passed is True
    assert cand.passed is True


def test_evaluate_fails_cond1_when_below_high():
    info = {
        "longName": "X", "marketCap": 1e14, "trailingEps": 100.0,
        "forwardEps": 110.0, "trailingPE": 12.0,
    }
    closes = [50.0] + [60.0] * 250 + [80.0]
    t = _fake_ticker(info, closes)
    t.history.return_value["High"] = [100.0] * len(closes)
    cand = leader_filter._evaluate_single(
        symbol="X.KS", market="KOSPI", ticker=t,
        index_return_1y=0.10, market_cap_quintile=1, pe_quintile=3,
    )
    assert cand.cond1_passed is False
    assert cand.passed is False


def test_evaluate_fails_cond1_when_smaller_than_market_plus_20pp():
    info = {"longName": "X", "marketCap": 1e14, "trailingEps": 100.0,
            "forwardEps": 110.0, "trailingPE": 12.0}
    closes = [100.0] * 252 + [125.0]
    t = _fake_ticker(info, closes)
    t.history.return_value["High"] = [130.0] * len(closes)
    cand = leader_filter._evaluate_single(
        symbol="X.KS", market="KOSPI", ticker=t,
        index_return_1y=0.10,
        market_cap_quintile=1, pe_quintile=3,
    )
    assert cand.cond1_passed is False


def test_evaluate_fails_cond1_when_market_cap_below_top20():
    info = {"longName": "X", "marketCap": 1e11, "trailingEps": 100.0,
            "forwardEps": 110.0, "trailingPE": 12.0}
    closes = [100.0] * 252 + [200.0]
    t = _fake_ticker(info, closes)
    t.history.return_value["High"] = [200.0] * len(closes)
    cand = leader_filter._evaluate_single(
        symbol="X.KS", market="KOSPI", ticker=t,
        index_return_1y=0.10, market_cap_quintile=3, pe_quintile=3,
    )
    assert cand.cond1_passed is False


def test_evaluate_passes_cond2_with_forward_growth_only():
    info = {"longName": "X", "marketCap": 1e14,
            "trailingEps": -100.0, "forwardEps": 50.0,
            "trailingPE": -10.0}
    closes = [100.0] * 252 + [200.0]
    t = _fake_ticker(info, closes)
    t.history.return_value["High"] = [200.0] * len(closes)
    cand = leader_filter._evaluate_single(
        symbol="X.KS", market="KOSPI", ticker=t,
        index_return_1y=0.10, market_cap_quintile=1, pe_quintile=5,
    )
    assert cand.cond2_passed is True
    assert cand.passed is True


def test_evaluate_ignores_cond3_pe():
    """PER 높아도 1·2번 만족하면 통과 (사용자 요구사항: PER 무관)."""
    info = {"longName": "X", "marketCap": 1e14, "trailingEps": 100.0,
            "forwardEps": 110.0, "trailingPE": 200.0}
    closes = [100.0] * 252 + [200.0]
    t = _fake_ticker(info, closes)
    t.history.return_value["High"] = [200.0] * len(closes)
    cand = leader_filter._evaluate_single(
        symbol="X.KS", market="KOSPI", ticker=t,
        index_return_1y=0.10, market_cap_quintile=1, pe_quintile=5,
    )
    assert cand.passed is True
    assert cand.pe_quintile == 5


def test_run_filter_assigns_market_cap_quintile_globally(monkeypatch: pytest.MonkeyPatch):
    """모집단: universe 전체 단일 컷오프 (시장 분리 X)."""
    universe = [
        ("A.KS", "KOSPI"), ("B.KS", "KOSPI"),
        ("C.KQ", "KOSDAQ"), ("D.KQ", "KOSDAQ"),
    ]
    fundamentals = {
        "A.KS": {"marketCap": 400e12, "trailingEps": 100, "forwardEps": 110,
                 "trailingPE": 14, "longName": "A"},
        "B.KS": {"marketCap": 100e12, "trailingEps": 100, "forwardEps": 110,
                 "trailingPE": 14, "longName": "B"},
        "C.KQ": {"marketCap": 50e12, "trailingEps": 100, "forwardEps": 110,
                 "trailingPE": 14, "longName": "C"},
        "D.KQ": {"marketCap": 10e12, "trailingEps": 100, "forwardEps": 110,
                 "trailingPE": 14, "longName": "D"},
    }
    closes_pass = [100.0] * 252 + [200.0]

    def mk(sym):
        t = MagicMock()
        t.info = fundamentals[sym]
        t.history.return_value = pd.DataFrame(
            {"Close": closes_pass, "High": [200.0] * 253},
            index=pd.date_range("2025-05-15", periods=253, freq="D"),
        )
        return t

    monkeypatch.setattr(leader_filter.yf, "Ticker", mk)
    monkeypatch.setattr(leader_filter, "compute_index_return", lambda s: 0.10)

    cands = leader_filter.run_filter(universe)
    by_sym = {c.symbol: c for c in cands}
    assert by_sym["A.KS"].market_cap_quintile == 1
    assert by_sym["D.KQ"].market_cap_quintile == 5 or by_sym["D.KQ"].market_cap_quintile == 4
    passed_syms = {c.symbol for c in cands if c.passed}
    assert passed_syms == {"A.KS"}
