"""src/dart_llm.py 단위 테스트."""
from unittest.mock import MagicMock, patch
import pytest

from src import dart_llm


def _classified(count=2):
    return {
        "count": count,
        "should_call_llm": count >= 2,
        "critical_events": [
            {"type": "treasury_acquire", "tier": "high",
             "raw": {"rcept_no": "20260520000001", "aqpln_amount": "20000000000"}},
            {"type": "capital_increase", "tier": "high",
             "raw": {"rcept_no": "20260521000002", "nstk_ostk_qy": "1000000"}},
        ],
    }


class TestSummarizeDisclosures:
    @patch("src.dart_llm._get_model")
    def test_parses_gemini_json(self, mock_model_fn):
        mock_model = MagicMock()
        mock_resp = MagicMock()
        mock_resp.text = (
            '{"summary": "자기주식 취득 + 유상증자 동시 발생",'
            ' "sentiment": "중립",'
            ' "key_events": ["[20260520000001] 자기주식 200억", "[20260521000002] 유상증자"],'
            ' "trading_view": "관망 — 상반된 시그널"}'
        )
        mock_model.generate_content.return_value = mock_resp
        mock_model_fn.return_value = mock_model

        result = dart_llm.summarize_disclosures("005930.KS", "삼성전자", _classified())
        assert result is not None
        assert result["summary"].startswith("자기주식")
        assert result["sentiment"] == "중립"
        assert len(result["key_events"]) == 2
        assert "관망" in result["trading_view"]
        assert result["model"] == "gemini-2.5-flash"
        assert "generated_at" in result

    @patch("src.dart_llm._get_model")
    def test_validates_sentiment_enum(self, mock_model_fn):
        # Gemini 가 "긍정적" 같은 변형 반환 → "중립" fallback
        mock_model = MagicMock()
        mock_resp = MagicMock()
        mock_resp.text = (
            '{"summary": "...", "sentiment": "긍정적",'
            ' "key_events": ["[X] e1"], "trading_view": "매수 — 근거"}'
        )
        mock_model.generate_content.return_value = mock_resp
        mock_model_fn.return_value = mock_model

        result = dart_llm.summarize_disclosures("005930.KS", "삼성전자", _classified())
        assert result["sentiment"] == "중립"  # fallback

    @patch("src.dart_llm._get_model")
    def test_validates_trading_view_prefix(self, mock_model_fn):
        # trading_view 가 "강한매수" → "관망 — ..." fallback
        mock_model = MagicMock()
        mock_resp = MagicMock()
        mock_resp.text = (
            '{"summary": "...", "sentiment": "긍정",'
            ' "key_events": ["[X] e1"], "trading_view": "강한매수 — 근거"}'
        )
        mock_model.generate_content.return_value = mock_resp
        mock_model_fn.return_value = mock_model

        result = dart_llm.summarize_disclosures("005930.KS", "삼성전자", _classified())
        assert result["trading_view"].startswith("관망")
        assert "LLM 응답 형식 오류" in result["trading_view"]

    @patch("src.dart_llm._get_model")
    def test_parse_failure_falls_back_to_raw(self, mock_model_fn):
        mock_model = MagicMock()
        mock_resp = MagicMock()
        mock_resp.text = "this is not json at all"
        mock_model.generate_content.return_value = mock_resp
        mock_model_fn.return_value = mock_model

        result = dart_llm.summarize_disclosures("005930.KS", "삼성전자", _classified())
        assert result is not None
        assert result["sentiment"] == "중립"
        assert "관망" in result["trading_view"]

    @patch("src.dart_llm._get_model")
    def test_api_error_returns_none(self, mock_model_fn):
        mock_model = MagicMock()
        mock_model.generate_content.side_effect = Exception("API timeout")
        mock_model_fn.return_value = mock_model

        result = dart_llm.summarize_disclosures("005930.KS", "삼성전자", _classified())
        assert result is None
