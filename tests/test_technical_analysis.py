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


class TestStageLabel:
    """_stage_label — KST 기준 시장 운영 단계 판별."""

    def _kst(self, year, month, day, hour, minute):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("Asia/Seoul"))

    def test_korea_market_open_at_open(self):
        from src.technical_analysis import _stage_label
        # 월요일 09:00 KST
        assert _stage_label(self._kst(2026, 5, 18, 9, 0), "korea") == "market_open"

    def test_korea_market_open_at_close(self):
        from src.technical_analysis import _stage_label
        # 월요일 15:30 KST (경계 — 포함)
        assert _stage_label(self._kst(2026, 5, 18, 15, 30), "korea") == "market_open"

    def test_korea_before_open(self):
        from src.technical_analysis import _stage_label
        # 월요일 08:59 KST
        assert _stage_label(self._kst(2026, 5, 18, 8, 59), "korea") == "before_open"

    def test_korea_after_close(self):
        from src.technical_analysis import _stage_label
        # 월요일 15:31 KST
        assert _stage_label(self._kst(2026, 5, 18, 15, 31), "korea") == "after_close"

    def test_kosdaq_uses_korea_hours(self):
        from src.technical_analysis import _stage_label
        # 월요일 10:00 KST
        assert _stage_label(self._kst(2026, 5, 18, 10, 0), "kosdaq") == "market_open"

    def test_us_market_open_late_night(self):
        from src.technical_analysis import _stage_label
        # 월요일 23:00 KST
        assert _stage_label(self._kst(2026, 5, 18, 23, 0), "us") == "market_open"

    def test_us_market_open_early_morning(self):
        from src.technical_analysis import _stage_label
        # 화요일 03:00 KST
        assert _stage_label(self._kst(2026, 5, 19, 3, 0), "us") == "market_open"

    def test_us_after_close(self):
        from src.technical_analysis import _stage_label
        # 화요일 07:00 KST
        assert _stage_label(self._kst(2026, 5, 19, 7, 0), "us") == "after_close"

    def test_us_before_open(self):
        from src.technical_analysis import _stage_label
        # 월요일 12:00 KST (장 시작 전, 한국 시간 기준 점심)
        assert _stage_label(self._kst(2026, 5, 18, 12, 0), "us") == "before_open"

    def test_weekend_saturday(self):
        from src.technical_analysis import _stage_label
        # 토요일 14:00 KST
        assert _stage_label(self._kst(2026, 5, 23, 14, 0), "korea") == "weekend"

    def test_weekend_sunday(self):
        from src.technical_analysis import _stage_label
        # 일요일 14:00 KST
        assert _stage_label(self._kst(2026, 5, 24, 14, 0), "us") == "weekend"


class TestComputeRelativePerformance:
    """compute_relative_performance — 종목 vs 시장 인덱스 등락률 계산."""

    def _stock_df(self, prev: float, last: float):
        """Close 2개만 있는 최소 fixture."""
        idx = pd.date_range("2026-05-15", periods=2, freq="B")
        return pd.DataFrame({
            "Close": [prev, last],
            "Open":  [prev, last],
            "High":  [prev, last],
            "Low":   [prev, last],
            "Volume": [1, 1],
        }, index=idx)

    def test_basic_positive_alpha(self, monkeypatch):
        """종목 +2%, 인덱스 +1% -> 알파 +1pp"""
        from src import technical_analysis as ta_mod
        stock = self._stock_df(100.0, 102.0)   # +2%
        index = self._stock_df(1000.0, 1010.0)  # +1%
        monkeypatch.setattr(ta_mod, "fetch_market_df", lambda mk: index)

        result = ta_mod.compute_relative_performance(stock, "005930.KS")

        assert result is not None
        assert result["index_name"] == "KOSPI"
        assert result["stock_pct"] == pytest.approx(2.0, abs=1e-6)
        assert result["index_pct"] == pytest.approx(1.0, abs=1e-6)
        assert result["alpha_pp"]  == pytest.approx(1.0, abs=1e-6)
        assert "as_of" in result
        assert "stage" in result

    def test_negative_alpha_us(self, monkeypatch):
        """종목 -1%, S&P +0.5% -> 알파 -1.5pp, US 매핑"""
        from src import technical_analysis as ta_mod
        stock = self._stock_df(200.0, 198.0)    # -1%
        index = self._stock_df(5000.0, 5025.0)  # +0.5%
        monkeypatch.setattr(ta_mod, "fetch_market_df", lambda mk: index)

        result = ta_mod.compute_relative_performance(stock, "AAPL")

        assert result["index_name"] == "S&P 500"
        assert result["stock_pct"] == pytest.approx(-1.0, abs=1e-6)
        assert result["index_pct"] == pytest.approx(0.5, abs=1e-6)
        assert result["alpha_pp"]  == pytest.approx(-1.5, abs=1e-6)

    def test_short_df_returns_none(self, monkeypatch):
        """len(df) < 2 -> None (신규상장 등)"""
        from src import technical_analysis as ta_mod
        idx = pd.date_range("2026-05-18", periods=1, freq="B")
        stock = pd.DataFrame({"Close": [100.0]}, index=idx)
        monkeypatch.setattr(ta_mod, "fetch_market_df", lambda mk: self._stock_df(1000, 1010))

        assert ta_mod.compute_relative_performance(stock, "005930.KS") is None

    def test_index_fetch_fail_returns_none(self, monkeypatch):
        """fetch_market_df가 None 반환 -> None"""
        from src import technical_analysis as ta_mod
        stock = self._stock_df(100.0, 102.0)
        monkeypatch.setattr(ta_mod, "fetch_market_df", lambda mk: None)

        assert ta_mod.compute_relative_performance(stock, "005930.KS") is None

    def test_short_index_df_returns_none(self, monkeypatch):
        """인덱스 df도 len < 2면 None"""
        from src import technical_analysis as ta_mod
        stock = self._stock_df(100.0, 102.0)
        short_idx = pd.DataFrame({"Close": [1000.0]},
                                  index=pd.date_range("2026-05-18", periods=1, freq="B"))
        monkeypatch.setattr(ta_mod, "fetch_market_df", lambda mk: short_idx)

        assert ta_mod.compute_relative_performance(stock, "005930.KS") is None

    def test_zero_prev_close_returns_none(self, monkeypatch):
        """전일 종가 0 -> div-by-zero 방어, None"""
        from src import technical_analysis as ta_mod
        stock = self._stock_df(0.0, 1.0)
        index = self._stock_df(1000.0, 1010.0)
        monkeypatch.setattr(ta_mod, "fetch_market_df", lambda mk: index)

        assert ta_mod.compute_relative_performance(stock, "005930.KS") is None

    def test_zero_prev_index_returns_none(self, monkeypatch):
        """인덱스 전일 종가 0 -> None"""
        from src import technical_analysis as ta_mod
        stock = self._stock_df(100.0, 102.0)
        index = self._stock_df(0.0, 1.0)
        monkeypatch.setattr(ta_mod, "fetch_market_df", lambda mk: index)

        assert ta_mod.compute_relative_performance(stock, "005930.KS") is None
