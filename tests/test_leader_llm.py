"""leader_llm: Gemini 2.5 Flash wrapper + retry + daily limit."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from src import leader_llm


@pytest.fixture
def fake_genai(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """google.generativeai 를 통째로 mock 으로 교체."""
    mock = MagicMock()
    monkeypatch.setattr(leader_llm, "genai", mock)
    monkeypatch.setattr(leader_llm, "_get_model", lambda: mock.model)
    monkeypatch.setattr(leader_llm, "_daily_count", lambda: 0)
    monkeypatch.setattr(leader_llm, "_increment_daily_count", lambda: None)
    return mock


def _input(symbol: str = "005930.KS") -> dict:
    return {
        "symbol": symbol, "name": "삼성전자", "market": "KOSPI",
        "sector": "Tech", "industry": "Semi",
        "market_cap": 400_000_000_000_000,
        "return_1y_pct": 0.45, "rel_return_pp": 0.30,
        "trailing_eps": 5000.0, "forward_eps": 6000.0,
        "revenue_growth_pct": 0.18, "trailing_pe": 14.0,
    }


def test_analyze_one_returns_parsed_json(fake_genai: MagicMock):
    payload = {
        "tam_narrative": "글로벌 반도체 TAM 1조 달러",
        "narrative_expansion": "GPU→메모리→전력 확장",
        "bottleneck": "HBM 생산 capa",
        "moat": "EUV 노광 노하우",
    }
    resp = MagicMock()
    resp.text = json.dumps(payload, ensure_ascii=False)
    fake_genai.model.generate_content.return_value = resp

    result = leader_llm.analyze_one(_input())
    assert result.fields == payload
    assert result.error is None
    assert result.raw == resp.text


def test_analyze_one_retries_on_exception_once(fake_genai: MagicMock, monkeypatch: pytest.MonkeyPatch):
    """첫 호출 실패 → backoff → 두 번째 성공."""
    payload = {"tam_narrative": "x", "narrative_expansion": "x",
               "bottleneck": "x", "moat": "x"}
    success_resp = MagicMock()
    success_resp.text = json.dumps(payload)
    fake_genai.model.generate_content.side_effect = [
        Exception("503 Service Unavailable"),
        success_resp,
    ]
    sleeps: list[float] = []
    monkeypatch.setattr(leader_llm.time, "sleep", lambda s: sleeps.append(s))

    result = leader_llm.analyze_one(_input())
    assert result.error is None
    assert fake_genai.model.generate_content.call_count == 2
    assert 2 in sleeps


def test_analyze_one_marks_parse_failed_on_non_json(fake_genai: MagicMock):
    resp = MagicMock()
    resp.text = "이것은 JSON 이 아님"
    fake_genai.model.generate_content.return_value = resp
    result = leader_llm.analyze_one(_input())
    assert result.error == "parse_failed"
    assert result.raw == "이것은 JSON 이 아님"


def test_analyze_one_respects_daily_limit(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(leader_llm, "_daily_count", lambda: 999)
    monkeypatch.setenv("LEADER_LLM_DAILY_LIMIT", "20")
    result = leader_llm.analyze_one(_input())
    assert result.error == "over_limit"


def test_analyze_one_returns_api_error_after_two_failures(fake_genai: MagicMock, monkeypatch: pytest.MonkeyPatch):
    """양쪽 시도 모두 실패 → error='api_error', 호출 count = 2."""
    fake_genai.model.generate_content.side_effect = Exception("429 quota exceeded")
    monkeypatch.setattr(leader_llm.time, "sleep", lambda s: None)
    result = leader_llm.analyze_one(_input())
    assert result.error == "api_error"
    assert fake_genai.model.generate_content.call_count == 2
    # 원본 예외 정보는 raw 에 보존되어 디버그 가능
    assert "429" in result.raw or "quota" in result.raw


def test_analyze_one_strips_markdown_fence(fake_genai: MagicMock):
    """Gemini 가 strict JSON 무시하고 ```json...``` 으로 감싸면 strip 후 파싱."""
    payload = {"tam_narrative": "T", "narrative_expansion": "N",
               "bottleneck": "B", "moat": "M"}
    resp = MagicMock()
    resp.text = f"```json\n{json.dumps(payload)}\n```"
    fake_genai.model.generate_content.return_value = resp

    result = leader_llm.analyze_one(_input())
    assert result.error is None
    assert result.fields == payload
