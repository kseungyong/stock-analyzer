"""src/report_generator.py 단위 테스트."""
from __future__ import annotations

import base64
import sys
import types
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

# matplotlib 미설치 환경 대응
def _mock_matplotlib():
    mpl = types.ModuleType("matplotlib")
    mpl.use = MagicMock()
    mpl.rcParams = {}

    pyplot = types.ModuleType("matplotlib.pyplot")
    fig_mock = MagicMock()
    ax_mock = MagicMock()
    ax_mock.xaxis = MagicMock()
    ax_mock.xaxis.set_major_formatter = MagicMock()
    fig_mock.savefig = MagicMock()
    pyplot.subplots = MagicMock(return_value=(fig_mock, [ax_mock, ax_mock, ax_mock]))
    pyplot.rcParams = {}
    pyplot.close = MagicMock()
    pyplot.tight_layout = MagicMock()
    pyplot.switch_backend = MagicMock()

    dates_mod = types.ModuleType("matplotlib.dates")
    dates_mod.DateFormatter = MagicMock(return_value=MagicMock())

    fm_mod = types.ModuleType("matplotlib.font_manager")
    fm_mod.fontManager = MagicMock()
    fm_mod.fontManager.ttflist = []

    mpl.pyplot = pyplot
    mpl.dates = dates_mod
    mpl.font_manager = fm_mod

    sys.modules.setdefault("matplotlib", mpl)
    sys.modules.setdefault("matplotlib.pyplot", pyplot)
    sys.modules.setdefault("matplotlib.dates", dates_mod)
    sys.modules.setdefault("matplotlib.font_manager", fm_mod)

    # fig.savefig가 빈 PNG를 buf에 쓰도록 패치
    import io
    _EMPTY_PNG = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )

    def _fake_savefig(buf, **kwargs):
        buf.write(_EMPTY_PNG)

    fig_mock.savefig.side_effect = _fake_savefig


_mock_matplotlib()

from src.report_generator import _signal_color, generate_report  # noqa: E402


@pytest.fixture(autouse=True)
def _patch_hit_rate(request):
    """hit_rate_by_model이 실제 DB를 사용하지 않도록 기본 패치. TestHitRateSection은 자체 패치를 사용."""
    if request.node.cls is not None and request.node.cls.__name__ == "TestHitRateSection":
        yield  # TestHitRateSection은 각 테스트에서 직접 패치
        return
    with patch("src.report_generator.prediction_history.hit_rate_by_model", return_value={}):
        yield


def _make_df(rows: int = 60) -> pd.DataFrame:
    idx = pd.date_range("2023-01-01", periods=rows)
    return pd.DataFrame(
        {
            "Close": [100.0 + i for i in range(rows)],
            "Open": [99.0 + i for i in range(rows)],
            "High": [101.0 + i for i in range(rows)],
            "Low": [98.0 + i for i in range(rows)],
            "Volume": [1_000_000] * rows,
            "MA5": [100.0 + i for i in range(rows)],
            "MA20": [100.0 + i for i in range(rows)],
            "MA60": [100.0 + i for i in range(rows)],
            "RSI": [50.0] * rows,
            "MACD": [0.5] * rows,
            "MACD_Signal": [0.4] * rows,
            "MACD_Hist": [0.1] * rows,
            "BB_Upper": [105.0 + i for i in range(rows)],
            "BB_Lower": [95.0 + i for i in range(rows)],
        },
        index=idx,
    )


def _make_analysis(signal: str = "매수") -> dict:
    return {
        "name": "Apple",
        "symbol": "AAPL",
        "df": _make_df(),
        "signal": {
            "signal": signal,
            "close": 150.0,
            "rsi": 50.0,
            "score": 1,
            "reasons": ["RSI 중립"],
            "indicators": [
                {"name": "RSI", "value": "50.0", "comment": "중립"},
            ],
        },
        "prediction": {
            "prophet": {"predicted_price": 150.0, "change_pct": 2.5, "range": [145.0, 155.0]},
            "random_forest": {"direction": "상승", "confidence": 65.0},
            "lightgbm": {"direction": "상승", "confidence": 70.0},
            "lstm": {"direction": "상승", "confidence": 60.0},
            "transformer": {"direction": "상승", "confidence": 68.0},
            "disclaimer": "참고용",
        },
    }


class TestSignalColor:
    def test_buy_color(self):
        assert _signal_color("매수") == "#28a745"

    def test_sell_color(self):
        assert _signal_color("매도") == "#dc3545"

    def test_hold_color(self):
        assert _signal_color("관망") == "#6c757d"

    def test_unknown_color(self):
        assert _signal_color("unknown") == "#6c757d"


class TestGenerateReport:
    def test_returns_string(self):
        html = generate_report([_make_analysis()])
        assert isinstance(html, str)

    def test_contains_doctype(self):
        html = generate_report([_make_analysis()])
        assert "<!DOCTYPE html>" in html or "<!doctype html>" in html.lower()

    def test_contains_stock_name(self):
        html = generate_report([_make_analysis()])
        assert "Apple" in html

    def test_contains_symbol(self):
        html = generate_report([_make_analysis()])
        assert "AAPL" in html

    def test_contains_signal(self):
        html = generate_report([_make_analysis("매도")])
        assert "매도" in html

    def test_contains_prophet_price(self):
        html = generate_report([_make_analysis()])
        assert "150" in html

    def test_multiple_analyses(self):
        second = {**_make_analysis(), "name": "NVIDIA", "symbol": "NVDA"}
        html = generate_report([_make_analysis(), second])
        assert "Apple" in html
        assert "NVIDIA" in html

    def test_empty_analyses(self):
        html = generate_report([])
        assert isinstance(html, str)

    def test_lstm_error_handled(self):
        analysis = _make_analysis()
        analysis["prediction"]["lstm"] = {"error": "데이터 부족"}
        html = generate_report([analysis])
        assert "예측 불가" in html

    def test_xss_escaped_in_name(self):
        analysis = _make_analysis()
        analysis["name"] = "<script>alert(1)</script>"
        html = generate_report([analysis])
        assert "<script>alert(1)</script>" not in html


from unittest.mock import patch


class TestHitRateSection:
    def _analysis(self, symbol="AAPL"):
        idx = pd.date_range("2026-01-01", periods=30, freq="B")
        df = pd.DataFrame({
            "Close": list(range(100, 130)),
            "Open": list(range(100, 130)),
            "High": [c+2 for c in range(100, 130)],
            "Low": [c-2 for c in range(100, 130)],
            "Volume": [1000000] * 30,
            "RSI": [50.0] * 30,
            "MACD": [0.0] * 30,
            "MACD_Signal": [0.0] * 30,
            "MACD_Hist": [0.0] * 30,
            "MA5": list(range(100, 130)),
            "MA20": list(range(100, 130)),
            "BB_Upper": [c*1.02 for c in range(100, 130)],
            "BB_Lower": [c*0.98 for c in range(100, 130)],
        }, index=idx)
        return {
            "name": "Apple",
            "symbol": symbol,
            "df": df,
            "signal": {"signal": "관망", "score": 0, "close": 129, "rsi": 50, "reasons": [], "indicators": []},
            "prediction": {
                "prophet": None,
                "random_forest": {"direction": "상승", "confidence": 65.0},
                "lightgbm": {"direction": "상승", "confidence": 70.0},
                "lstm": {"direction": "하락", "confidence": 55.0},
                "transformer": {"direction": "상승", "confidence": 60.0},
                "ensemble": {"direction": "상승", "confidence": 67.0},
            },
            "news": [],
            "sentiment": {"label": "뉴스 없음", "score": 0.0, "details": []},
        }

    @patch("src.report_generator.prediction_history.hit_rate_by_model")
    def test_renders_hit_rate_when_data_exists(self, mock_hr):
        mock_hr.return_value = {
            "rf": {"hit_rate": 0.62, "n": 21},
            "lgbm": {"hit_rate": 0.67, "n": 21},
        }
        from src.report_generator import generate_report
        html_out = generate_report([self._analysis()])
        assert "62.0%" in html_out
        assert "21" in html_out

    @patch("src.report_generator.prediction_history.hit_rate_by_model")
    def test_omits_section_when_no_data(self, mock_hr):
        mock_hr.return_value = {}
        from src.report_generator import generate_report
        html_out = generate_report([self._analysis()])
        assert "누적 적중률" not in html_out

    @patch("src.report_generator.prediction_history.hit_rate_by_model")
    def test_marks_low_n_as_insufficient(self, mock_hr):
        mock_hr.return_value = {"rf": {"hit_rate": 0.6, "n": 5}}
        from src.report_generator import generate_report
        html_out = generate_report([self._analysis()])
        assert "데이터 부족" in html_out
