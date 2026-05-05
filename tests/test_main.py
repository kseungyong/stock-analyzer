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


class TestAutoAnalyzeMarket:
    def test_processes_only_target_market(self, monkeypatch, tmp_path):
        import main
        from src import analysis_cache as ac

        # config 를 한국 1개 + 미국 1개로 stub
        fake_config = {
            "stocks": {
                "korea": [{"symbol": "005930.KS", "name": "삼성전자"}],
                "us":    [{"symbol": "AAPL", "name": "Apple"}],
            },
            "schedule": {"hour": 8, "minute": 30, "timezone": "Asia/Seoul"},
            "email": {},
        }
        monkeypatch.setattr(main, "load_config", lambda: fake_config)

        analyzed = []
        def fake_analyze(symbol, name):
            analyzed.append(symbol)
            return {"name": name, "symbol": symbol, "df": None,
                    "signal": None, "prediction": None, "news": [], "sentiment": None}
        monkeypatch.setattr(main, "analyze_stock", fake_analyze)
        monkeypatch.setattr("src.report_generator.generate_report", lambda a: "<p/>")

        puts = []
        monkeypatch.setattr(ac, "put", lambda **kw: puts.append(kw))

        main.auto_analyze_market("korea")
        assert analyzed == ["005930.KS"]
        assert len(puts) == 1
        assert puts[0]["cache_key"] == "005930.KS"
        assert puts[0]["market"] == "korea"
        assert puts[0]["source"] == "auto_cron"

    def test_skips_failed_symbols(self, monkeypatch):
        import main
        from src import analysis_cache as ac

        fake_config = {"stocks": {"us": [
            {"symbol": "BAD", "name": "Bad"},
            {"symbol": "GOOD", "name": "Good"},
        ]}, "schedule": {}, "email": {}}
        monkeypatch.setattr(main, "load_config", lambda: fake_config)

        def fake_analyze(symbol, name):
            if symbol == "BAD":
                return None  # fetch 실패 시뮬레이션
            return {"name": name, "symbol": symbol, "df": None,
                    "signal": None, "prediction": None, "news": [], "sentiment": None}
        monkeypatch.setattr(main, "analyze_stock", fake_analyze)
        monkeypatch.setattr("src.report_generator.generate_report", lambda a: "<p/>")
        puts = []
        monkeypatch.setattr(ac, "put", lambda **kw: puts.append(kw))

        main.auto_analyze_market("us")
        # GOOD 만 캐시
        assert [p["cache_key"] for p in puts] == ["GOOD"]


class TestDailyEmailJob:
    def test_skips_email_when_cache_empty(self, monkeypatch):
        import main
        from src import analysis_cache as ac

        monkeypatch.setattr(main, "load_config", lambda: {"email": {}})
        monkeypatch.setattr(ac, "list_symbols", lambda: [])

        sent = []
        monkeypatch.setattr(main, "send_report", lambda html, cfg: sent.append(html))
        main.daily_email_job()
        assert sent == []

    def test_sends_email_when_cache_has_rows(self, monkeypatch):
        import main
        from src import analysis_cache as ac

        monkeypatch.setattr(main, "load_config", lambda: {"email": {"recipients": ["a@b"]}})
        monkeypatch.setattr(ac, "list_symbols", lambda: [
            {"cache_key": "AAPL", "market": "us", "result_html": "<p/>",
             "generated_at": 1, "source": "auto_cron"}
        ])
        monkeypatch.setattr(ac, "is_fresh", lambda row, now: True)

        sent = []
        monkeypatch.setattr(main, "send_report", lambda html, cfg: sent.append(html))
        main.daily_email_job()
        assert len(sent) == 1
        assert "AAPL" in sent[0]


class TestAutoAnalyzeMarketSavesSignal:
    def test_signal_passed_to_cache_put(self, monkeypatch):
        import main
        from src import analysis_cache as ac

        fake_config = {"stocks": {
            "us": [{"symbol": "AAPL", "name": "Apple"}],
        }, "schedule": {}, "email": {}}
        monkeypatch.setattr(main, "load_config", lambda: fake_config)

        def fake_analyze(symbol, name):
            return {
                "name": name, "symbol": symbol,
                "df": None, "prediction": None, "news": [], "sentiment": None,
                "signal": {"signal": "매수", "score": 4},
            }

        monkeypatch.setattr(main, "analyze_stock", fake_analyze)
        monkeypatch.setattr("src.report_generator.generate_report",
                            lambda a: "<p/>")

        captured = []
        monkeypatch.setattr(ac, "put", lambda **kw: captured.append(kw))

        main.auto_analyze_market("us")

        assert len(captured) == 1
        assert captured[0]["signal_value"] == "매수"
        assert captured[0]["signal_score"] == 4
