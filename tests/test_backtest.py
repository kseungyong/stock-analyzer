"""src/backtest.py 단위 테스트."""
import numpy as np
import pandas as pd
import pytest

from src import backtest as bt


def _make_df(n=200, seed=0):
    rng = np.random.default_rng(seed)
    close = 50000 + np.cumsum(rng.normal(0, 500, n))
    volume = rng.integers(1_000_000, 5_000_000, n).astype(float)
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    df = pd.DataFrame({"Close": close, "Volume": volume}, index=idx)
    df["MA5"] = df["Close"].rolling(5).mean()
    df["MA20"] = df["Close"].rolling(20).mean()
    df["RSI"] = 50.0
    df["MACD"] = 0.0
    df["MACD_Hist"] = 0.0
    df["BB_Upper"] = df["Close"] * 1.02
    df["BB_Lower"] = df["Close"] * 0.98
    df["Volume_Ratio"] = 1.0
    df["Stoch_K"] = 50.0
    df["Stoch_D"] = 50.0
    df["ATR_pct"] = 1.5
    df["OBV_Change"] = 0.0
    df["Williams_R"] = -50.0
    df["CCI"] = 0.0
    df["Return_1d"] = df["Close"].pct_change(1)
    df["Return_5d"] = df["Close"].pct_change(5)
    df["Return_20d"] = df["Close"].pct_change(20)
    return df


class TestWalkForward:
    def test_insufficient_data_returns_error(self):
        df = _make_df(n=30)
        result = bt.walk_forward("AAPL", df, days=126)
        assert result["error"] == "데이터 부족"
        assert result["rows"] == []
        assert result["summary"] == {}

    def test_returns_summary_with_models(self):
        df = _make_df(n=200)
        result = bt.walk_forward("AAPL", df, days=30)
        assert "rf" in result["summary"]
        assert "lgbm" in result["summary"]
        assert "ensemble" in result["summary"]
        assert result["backtest_id"]
        assert len(result["rows"]) > 0

    def test_each_row_has_required_fields(self):
        df = _make_df(n=200)
        result = bt.walk_forward("AAPL", df, days=10)
        required = {"symbol", "ts", "target_date", "model", "direction",
                    "confidence", "actual_close", "base_close", "hit", "evaluated_at"}
        for row in result["rows"]:
            assert required.issubset(row.keys())

    def test_hit_calculated_correctly(self):
        df = _make_df(n=200)
        result = bt.walk_forward("AAPL", df, days=10)
        for row in result["rows"]:
            if row["direction"] == "상승":
                assert row["hit"] == (1 if row["actual_close"] > row["base_close"] else 0)
            elif row["direction"] == "하락":
                assert row["hit"] == (1 if row["actual_close"] < row["base_close"] else 0)
