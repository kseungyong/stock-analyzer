"""src/technical_analysis.py 단위 테스트."""
import pandas as pd
import numpy as np
import pytest
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
