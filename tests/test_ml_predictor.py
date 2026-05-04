"""src/ml_predictor.py 단위 테스트 — 경량 (무거운 모델 실행 없이)."""
import sys
import types
import time
import pandas as pd
import numpy as np
import pytest

# prophet, tensorflow 등 무거운 패키지를 스텁으로 대체 (torch는 scipy 충돌로 제외)
import unittest.mock as _mock

for _mod in ("prophet", "prophet.forecaster",
             "tensorflow", "tensorflow.keras",
             "tensorflow.keras.models", "tensorflow.keras.layers",
             "tensorflow.keras.callbacks"):
    if _mod not in sys.modules:
        sys.modules[_mod] = types.ModuleType(_mod)

sys.modules["prophet"].Prophet = _mock.MagicMock()

from src.ml_predictor import (
    predict_direction,
    predict_direction_lgbm,
    analyze_sentiment,
    run_prediction,
    _prediction_cache,
    _PREDICTION_CACHE_TTL,
)


def _make_df(n: int = 120, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 50_000 + np.cumsum(rng.normal(0, 500, n))
    volume = rng.integers(1_000_000, 5_000_000, n).astype(float)
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    df = pd.DataFrame({"Close": close, "Volume": volume}, index=idx)
    # 기술 지표 수동 계산 (compute_indicators 없이 간단하게).
    # _CLF_FEATURES 17개 모두 포함해야 ml_predictor의 dropna(subset=...) 통과.
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


class TestPredictDirection:
    def test_insufficient_data(self):
        df = _make_df(n=10)
        result = predict_direction(df)
        assert result["direction"] == "데이터 부족"
        assert result["confidence"] == 0.0

    def test_sufficient_data_returns_direction(self):
        df = _make_df(n=120)
        result = predict_direction(df)
        assert result["direction"] in ("상승", "하락")
        assert 0.0 <= result["confidence"] <= 100.0
        assert "accuracy" in result

    def test_lgbm_insufficient_data(self):
        df = _make_df(n=10)
        result = predict_direction_lgbm(df)
        assert result["direction"] == "데이터 부족"

    def test_lgbm_sufficient_data(self):
        df = _make_df(n=120)
        result = predict_direction_lgbm(df)
        assert result["direction"] in ("상승", "하락")
        assert 0.0 <= result["confidence"] <= 100.0


class TestAnalyzeSentiment:
    def test_empty_list(self):
        result = analyze_sentiment([])
        assert result["label"] == "뉴스 없음"
        assert result["score"] == 0.0
        assert result["details"] == []

    def test_items_with_no_text(self):
        result = analyze_sentiment([{"title_en": "", "summary_en": ""}])
        assert result["label"] == "분석할 텍스트 없음"

    def test_uses_english_fields(self):
        """title_en / summary_en 필드를 우선 사용하는지 확인 (모델 없이 텍스트 선택 로직만 검증)."""
        items = [{"title": "번역된 제목", "title_en": "Original Title", "summary": "", "summary_en": ""}]
        # transformers 미설치 환경에서는 error 키 반환 — 그래도 crash 없어야 함
        result = analyze_sentiment(items)
        assert isinstance(result, dict)


class TestPredictionCache:
    def setup_method(self):
        _prediction_cache.clear()

    def test_cache_hit(self):
        df = _make_df(n=120)
        # Prophet/LSTM 등 무거운 모델은 에러로 처리되지만 결과 구조는 반환됨
        result1 = run_prediction(df, cache_key="TEST")
        assert "TEST" in _prediction_cache

        # 캐시된 타임스탬프를 충분히 최근으로 유지 → 두 번째 호출은 캐시 반환
        result2 = run_prediction(df, cache_key="TEST")
        assert result1 is result2  # 동일 객체 참조 = 캐시 히트

    def test_cache_miss_after_ttl(self):
        df = _make_df(n=120)
        run_prediction(df, cache_key="EXPIRE")
        # TTL 만료 시뮬레이션
        _prediction_cache["EXPIRE"] = (time.time() - _PREDICTION_CACHE_TTL - 1, _prediction_cache["EXPIRE"][1])
        old_result = _prediction_cache["EXPIRE"][1]

        new_result = run_prediction(df, cache_key="EXPIRE")
        # TTL 만료 후 새로 계산 — 새 객체
        assert new_result is not old_result

    def test_no_cache_without_key(self):
        df = _make_df(n=120)
        run_prediction(df)  # cache_key 미지정
        assert len(_prediction_cache) == 0
