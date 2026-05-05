"""main.py 통합 테스트."""
from unittest.mock import patch, MagicMock
import pandas as pd
import pytest

import main


@pytest.fixture
def mock_df():
    idx = pd.date_range("2026-01-01", periods=120, freq="B")
    return pd.DataFrame({
        "Close": list(range(50000, 50000 + 120)),
        "Volume": [1000000] * 120,
    }, index=idx)


class TestAnalyzeStockHistoryIntegration:
    @patch("main.fetch_news", return_value=[])
    @patch("main.generate_signal", return_value={"signal": "buy"})
    @patch("main.compute_indicators", side_effect=lambda df: df)
    @patch("main.fetch_stock_data")
    @patch("main._engine")
    @patch("main.prediction_history")
    def test_calls_insert_live_and_backfill(
        self, ph_mock, engine_mock, fetch_mock, _ind, _sig, _news, mock_df
    ):
        fetch_mock.return_value = mock_df
        engine_mock.run.return_value = {
            "random_forest": {"direction": "상승", "confidence": 65.0, "accuracy": 60.0},
            "lightgbm": {"direction": "상승", "confidence": 70.0},
            "lstm": {"direction": "하락", "confidence": 55.0},
            "transformer": {"direction": "상승", "confidence": 60.0},
            "ensemble": {"direction": "상승", "confidence": 67.0},
        }
        result = main.analyze_stock("AAPL", "Apple")
        assert result is not None
        ph_mock.backfill_inline.assert_called_once()
        ph_mock.insert_live.assert_called_once()
        # insert_live의 첫 인자가 symbol
        args, kwargs = ph_mock.insert_live.call_args
        assert (kwargs.get("symbol") == "AAPL") or (args and args[0] == "AAPL")
