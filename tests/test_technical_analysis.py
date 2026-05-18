"""src/technical_analysis.py 단위 테스트."""
import time
import pandas as pd
import numpy as np
import pytest
from src import technical_analysis as ta_mod
from src.technical_analysis import compute_indicators, generate_signal


def _make_df(n: int = 120, seed: int = 42) -> pd.DataFrame:
    """테스트용 OHLCV 데이터프레임을 생성한다."""
    rng = np.random.default_rng(seed)
    close = 50_000 + np.cumsum(rng.normal(0, 500, n))
    volume = rng.integers(100_000, 1_000_000, n)
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    return pd.DataFrame({
        "Open": close * 0.99,
        "High": close * 1.01,
        "Low": close * 0.98,
        "Close": close,
        "Volume": volume,
    }, index=idx)


class TestComputeIndicators:
    def setup_method(self):
        self.df = compute_indicators(_make_df())

    def test_moving_averages_exist(self):
        for col in ["MA5", "MA20", "MA60"]:
            assert col in self.df.columns

    def test_rsi_range(self):
        rsi = self.df["RSI"].dropna()
        assert (rsi >= 0).all() and (rsi <= 100).all()

    def test_macd_columns_exist(self):
        for col in ["MACD", "MACD_Signal", "MACD_Hist"]:
            assert col in self.df.columns

    def test_bollinger_bands_order(self):
        valid = self.df.dropna(subset=["BB_Lower", "BB_Middle", "BB_Upper"])
        assert (valid["BB_Lower"] <= valid["BB_Middle"]).all()
        assert (valid["BB_Middle"] <= valid["BB_Upper"]).all()

    def test_volume_ratio_positive(self):
        ratio = self.df["Volume_Ratio"].dropna()
        assert (ratio > 0).all()


class TestGenerateSignal:
    def setup_method(self):
        self.df = compute_indicators(_make_df())

    def test_signal_is_valid(self):
        sig = generate_signal(self.df)
        assert sig["signal"] in ("매수", "매도", "관망")

    def test_required_keys(self):
        sig = generate_signal(self.df)
        for key in ("signal", "score", "reasons", "indicators", "rsi", "close"):
            assert key in sig

    def test_rsi_value_reasonable(self):
        sig = generate_signal(self.df)
        assert 0 <= sig["rsi"] <= 100

    def test_indicators_include_volume(self):
        sig = generate_signal(self.df)
        names = [ind["name"] for ind in sig["indicators"]]
        assert "거래량" in names


@pytest.fixture(autouse=True)
def _clear_market_cache():
    """각 테스트 시작 시 _market_cache 비움 (모듈 변수 격리)."""
    ta_mod._market_cache.clear()
    yield
    ta_mod._market_cache.clear()


def _fake_df():
    """간단한 OHLCV df — compute_indicators 가 작동할 정도의 길이."""
    import numpy as np
    n = 100
    idx = pd.date_range("2026-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "Open":  np.linspace(100, 110, n),
        "High":  np.linspace(102, 112, n),
        "Low":   np.linspace(98, 108, n),
        "Close": np.linspace(100, 110, n),
        "Volume": [1_000_000] * n,
    }, index=idx)


class TestFetchMarketDf:
    def test_korea_fetches_kospi_index(self, monkeypatch):
        captured = []
        def fake_fetch(symbol):
            captured.append(symbol)
            return _fake_df()
        monkeypatch.setattr("src.data_fetcher.fetch_stock_data", fake_fetch)
        result = ta_mod.fetch_market_df("korea")
        assert result is not None
        assert captured == ["^KS11"]

    def test_us_fetches_sp500_index(self, monkeypatch):
        captured = []
        def fake_fetch(symbol):
            captured.append(symbol)
            return _fake_df()
        monkeypatch.setattr("src.data_fetcher.fetch_stock_data", fake_fetch)
        result = ta_mod.fetch_market_df("us")
        assert result is not None
        assert captured == ["^GSPC"]

    def test_fetch_failure_returns_none(self, monkeypatch):
        def fake_fetch(symbol):
            raise RuntimeError("network down")
        monkeypatch.setattr("src.data_fetcher.fetch_stock_data", fake_fetch)
        result = ta_mod.fetch_market_df("us")
        assert result is None

    def test_ttl_cache_hits_only_one_fetch(self, monkeypatch):
        captured = []
        def fake_fetch(symbol):
            captured.append(symbol)
            return _fake_df()
        monkeypatch.setattr("src.data_fetcher.fetch_stock_data", fake_fetch)
        # 첫 호출 — fetch
        r1 = ta_mod.fetch_market_df("us")
        # 두 번째 호출 — 캐시 hit
        r2 = ta_mod.fetch_market_df("us")
        assert len(captured) == 1
        assert r1 is r2  # 같은 객체 (캐시)


def _build_df_for_disparity(disparity_pct: float, rsi: float = 50.0,
                             volume_ratio: float = 1.0, green: bool = True):
    """MA20 이격율 + RSI + 거래량 비율 + 양/음봉을 의도된 값으로 가지는 df 생성."""
    import numpy as np
    n = 100
    base = 100.0
    # 마지막 close 만 base*(1+disparity_pct/100), MA20 ≈ base
    closes = [base] * (n - 1) + [base * (1 + disparity_pct / 100)]
    # 양/음봉
    last_open = closes[-1] - 1 if green else closes[-1] + 1
    opens = closes.copy()
    opens[-1] = last_open
    # 거래량
    volumes = [1_000_000] * (n - 1) + [int(1_000_000 * volume_ratio)]
    idx = pd.date_range("2026-01-01", periods=n, freq="D")
    df = pd.DataFrame({
        "Open": opens, "High": [c + 2 for c in closes],
        "Low": [c - 2 for c in closes], "Close": closes, "Volume": volumes,
    }, index=idx)
    df = ta_mod.compute_indicators(df)
    # RSI 강제 (덮어쓰기 — compute_indicators 결과 위에)
    df.loc[df.index[-1], "RSI"] = rsi
    return df


class TestGenerateBnfSignal:
    def test_strong_oversold_buy(self):
        df = _build_df_for_disparity(disparity_pct=-12, rsi=25)
        result = ta_mod.generate_bnf_signal(df)
        assert result["signal"] == "매수"
        assert result["score"] >= 2

    def test_strong_overbought_sell(self):
        df = _build_df_for_disparity(disparity_pct=+12, rsi=75)
        result = ta_mod.generate_bnf_signal(df)
        assert result["signal"] == "매도"
        assert result["score"] <= -2

    def test_neutral_hold(self):
        df = _build_df_for_disparity(disparity_pct=0, rsi=50)
        result = ta_mod.generate_bnf_signal(df)
        assert result["signal"] == "관망"
        assert -2 < result["score"] < 2

    def test_panic_volume_with_red_candle_adds_buy_point(self):
        df = _build_df_for_disparity(disparity_pct=-8, rsi=50,
                                      volume_ratio=2.5, green=False)
        result = ta_mod.generate_bnf_signal(df)
        assert "거래량" in " ".join(result["reasons"])
        assert result["score"] >= 1

    def test_volume_surge_no_buy_on_green(self):
        df = _build_df_for_disparity(disparity_pct=0, rsi=50,
                                      volume_ratio=2.5, green=True)
        result = ta_mod.generate_bnf_signal(df)
        assert all("거래량" not in r for r in result["reasons"])

    def test_market_panic_amplifies_buy(self):
        stock_df = _build_df_for_disparity(disparity_pct=-11, rsi=50)
        market_df = _build_df_for_disparity(disparity_pct=-4, rsi=50)
        result = ta_mod.generate_bnf_signal(stock_df, market_df=market_df)
        assert result["market_disparity"] is not None
        assert result["market_disparity"] < -3
        assert result["score"] >= 3

    def test_market_overheat_amplifies_sell(self):
        stock_df = _build_df_for_disparity(disparity_pct=+8, rsi=50)
        market_df = _build_df_for_disparity(disparity_pct=+6, rsi=50)
        result = ta_mod.generate_bnf_signal(stock_df, market_df=market_df)
        assert result["market_disparity"] > 5
        assert result["score"] <= -2

    def test_market_df_none_gives_no_market_score(self):
        stock_df = _build_df_for_disparity(disparity_pct=-11, rsi=50)
        result = ta_mod.generate_bnf_signal(stock_df, market_df=None)
        assert result["market_disparity"] is None
        # disparity -11% +2, RSI 50 +0
        assert result["score"] == 2


class TestResolveIndexMarket:
    def test_kospi_suffix_ks(self):
        from src.technical_analysis import resolve_index_market
        assert resolve_index_market("005930.KS") == ("KOSPI", "korea")

    def test_kosdaq_suffix_kq(self):
        from src.technical_analysis import resolve_index_market
        assert resolve_index_market("247540.KQ") == ("KOSDAQ", "kosdaq")

    def test_us_no_suffix(self):
        from src.technical_analysis import resolve_index_market
        assert resolve_index_market("AAPL") == ("S&P 500", "us")

    def test_us_with_dot_but_not_kr(self):
        from src.technical_analysis import resolve_index_market
        # BRK.B 같은 미국 심볼 — KR suffix 아니면 US
        assert resolve_index_market("BRK.B") == ("S&P 500", "us")

    def test_kosdaq_market_key_resolves_via_fetch_market_df(self):
        """_MARKET_INDEX['kosdaq']가 ^KQ11로 매핑되는지 확인."""
        from src.technical_analysis import _MARKET_INDEX
        assert _MARKET_INDEX.get("kosdaq") == "^KQ11"
