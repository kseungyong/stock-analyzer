"""dart_llm — Gemini 2.5 Flash 기반 DART 공시 종합 해석 (hybrid 복수 case 전용).

count >= 2 일 때만 호출. count == 0/1 은 dart_rules 가 처리.
"""
from __future__ import annotations

import json
import logging
import os
import time

import google.generativeai as genai  # type: ignore

logger = logging.getLogger(__name__)

_MODEL_NAME = "gemini-2.5-flash"
_TIMEOUT_S = 30

_VALID_SENTIMENT = ("긍정", "부정", "중립")
_VALID_TRADING_PREFIX = ("매수", "매도", "관망")

_SYSTEM_INSTRUCTION = (
    "당신은 한국 주식 시장의 공시 분석 전문가다. 입력으로 받은 critical events 를 "
    "사실 기반으로 종합 해석한다. 출력은 반드시 strict JSON, 다른 텍스트 금지. "
    "key_events 의 각 항목은 [rcept_no] 형식으로 사실 인용 필수. 추정/과장 금지."
)

_PROMPT_TEMPLATE = """종목: {name} ({symbol})

최근 30일 주요 공시 (분류된 critical events):
{events_json}

다음 규칙을 엄격히 지켜:
1. key_events 의 각 항목은 입력에 있는 rcept_no 를 [접수번호] 형식으로 시작.
2. sentiment 는 "긍정" / "부정" / "중립" 중 하나만 (정확한 enum).
3. trading_view 는 "매수" / "매도" / "관망" 중 하나로 시작하고, " — " 다음에 1줄 근거.

응답은 아래 JSON 형식만:

{{
  "summary": "2-3문장 종합 해석 (여러 공시의 상호 영향)",
  "sentiment": "긍정 또는 부정 또는 중립",
  "key_events": ["[rcept_no] 사실 인용 1", "[rcept_no] 사실 인용 2"],
  "trading_view": "매수|매도|관망 — 1줄 근거"
}}
"""


def _get_model():  # noqa: ANN202
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY 환경변수 없음")
    genai.configure(api_key=api_key)
    return genai.GenerativeModel(
        model_name=_MODEL_NAME,
        system_instruction=_SYSTEM_INSTRUCTION,
    )


def _validate_and_normalize(parsed: dict) -> dict:
    """sentiment / trading_view enum 검증 + fallback."""
    sentiment = parsed.get("sentiment", "")
    if sentiment not in _VALID_SENTIMENT:
        logger.warning("dart_llm sentiment enum 위반: %r → 중립", sentiment)
        sentiment = "중립"

    trading_view = (parsed.get("trading_view") or "").strip()
    if not any(trading_view.startswith(p) for p in _VALID_TRADING_PREFIX):
        logger.warning("dart_llm trading_view prefix 위반: %r → 관망 fallback", trading_view)
        trading_view = "관망 — LLM 응답 형식 오류"

    return {
        "summary": str(parsed.get("summary", ""))[:1000],
        "sentiment": sentiment,
        "key_events": [str(e) for e in (parsed.get("key_events") or [])][:5],
        "trading_view": trading_view,
        "model": _MODEL_NAME,
        "generated_at": int(time.time()),
    }


def summarize_disclosures(symbol: str, name: str, classified: dict) -> dict | None:
    """복수 critical events 종합 요약. count < 2 면 호출 금지.

    Returns:
        dict — 정상 또는 parse 실패 시 fallback
        None — API 호출 실패 (timeout/429/etc)
    """
    if not classified.get("should_call_llm"):
        logger.warning("dart_llm: should_call_llm=False — caller 가 잘못 호출")
        return None

    events_json = json.dumps(
        classified["critical_events"], ensure_ascii=False, indent=2,
    )[:4000]  # 토큰 절약
    prompt = _PROMPT_TEMPLATE.format(
        name=name, symbol=symbol, events_json=events_json,
    )
    gen_cfg = {
        "temperature": 0.3,
        # 4096 — critical event 多 종목 (SK하이닉스 7건 등) 응답 잘림 방지 (leader_llm 동일)
        "max_output_tokens": 4096,
        "response_mime_type": "application/json",
    }

    try:
        model = _get_model()
        resp = model.generate_content(
            prompt,
            generation_config=gen_cfg,
            request_options={"timeout": _TIMEOUT_S},
        )
        raw = getattr(resp, "text", "") or ""
    except Exception as e:
        logger.warning("dart_llm API 호출 실패: %s", e)
        return None

    try:
        parsed = json.loads(raw.strip())
    except json.JSONDecodeError:
        logger.warning("dart_llm JSON parse 실패 (len=%d) — fallback", len(raw))
        return {
            "summary": "LLM 응답 파싱 실패 — DART 공시 원본 직접 확인 권장",
            "sentiment": "중립",
            "key_events": [],
            "trading_view": "관망 — LLM 응답 형식 오류",
            "model": _MODEL_NAME,
            "generated_at": int(time.time()),
        }

    return _validate_and_normalize(parsed)
