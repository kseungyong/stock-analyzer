"""leader_llm: Gemini 2.5 Flash 기반 정성 분석 wrapper.

Spec §4.2: 종목당 1회 호출 → strict JSON 4필드 (tam_narrative, narrative_expansion,
bottleneck, moat). retry 1회 + 2초 backoff. daily limit cap.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import google.generativeai as genai  # type: ignore

logger = logging.getLogger(__name__)

_MODEL_NAME = "gemini-2.5-flash"
_TIMEOUT_S = 30
_RETRY_BACKOFF_S = 2
_DAILY_COUNT_FILE = Path(__file__).parent.parent / "data" / ".leader_llm_count"

_SYSTEM_INSTRUCTION = (
    "당신은 주식 시장의 주도주 분석 전문가다. 입력으로 받은 한국 종목에 대해 "
    "4가지 정성 조건을 산출한다. 출력은 반드시 strict JSON, 다른 텍스트 금지. "
    "데이터 부족 시 추정 금지 — '데이터 부족' 명시. 마케팅 어조 금지, 사실 기반 "
    "분석만."
)

_PROMPT_TEMPLATE = """종목: {name} ({symbol})
시장: {market}, 섹터: {sector}, 산업: {industry}
시가총액: {market_cap:,}원, 1년 수익률: {return_1y_pct:.1%}, 시장지수 대비 +{rel_return_pp:.1%}p
trailing EPS: {trailing_eps}, forward EPS: {forward_eps}, 매출 성장률: {revenue_growth_pct:.1%}
trailing PE: {trailing_pe}

아래 4가지를 분석해 JSON 으로만 응답:

{{
  "tam_narrative": "이 회사가 속한 글로벌 산업의 TAM 규모와 성장 동인. 3~5문장.",
  "narrative_expansion": "이 회사 이야기가 인접 섹터로 확장 가능한가 (예: GPU→전력→메모리). 2~3문장.",
  "bottleneck": "산업 밸류체인 내 반드시 거쳐야 하는 구간을 점유하는가. 2~3문장.",
  "moat": "그 구간 내 경쟁자 진입 장벽 (기술/특허/규모/네트워크). 2~3문장."
}}
"""


@dataclass
class LLMResult:
    fields: dict[str, str]      # tam_narrative / narrative_expansion / bottleneck / moat
    raw: str                    # 원본 응답 (디버그)
    error: str | None           # None=성공, 'parse_failed'/'timeout'/'rate_limit'/'over_limit' etc


def _get_model():  # noqa: ANN202 — Gemini 객체 타입 안정 X
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY 환경변수 없음")
    genai.configure(api_key=api_key)
    return genai.GenerativeModel(
        model_name=_MODEL_NAME,
        system_instruction=_SYSTEM_INSTRUCTION,
    )


def _daily_count() -> int:
    if not _DAILY_COUNT_FILE.exists():
        return 0
    try:
        date, n = _DAILY_COUNT_FILE.read_text().strip().split(":")
    except ValueError:
        return 0
    if date != time.strftime("%Y-%m-%d"):
        return 0
    return int(n)


def _increment_daily_count() -> None:
    today = time.strftime("%Y-%m-%d")
    n = _daily_count() + 1
    _DAILY_COUNT_FILE.parent.mkdir(parents=True, exist_ok=True)
    _DAILY_COUNT_FILE.write_text(f"{today}:{n}")


def _daily_limit() -> int:
    return int(os.environ.get("LEADER_LLM_DAILY_LIMIT", "20"))


def _format_eps(v: float | None) -> str:
    return f"{v:.0f}" if v is not None else "데이터 없음"


def analyze_one(inputs: dict[str, Any]) -> LLMResult:
    """단일 종목 → Gemini 호출 → LLMResult."""
    if _daily_count() >= _daily_limit():
        logger.warning(
            "LEADER_LLM_DAILY_LIMIT (%d) 초과 — %s skip",
            _daily_limit(), inputs.get("symbol"),
        )
        return LLMResult(fields={}, raw="", error="over_limit")

    prompt = _PROMPT_TEMPLATE.format(
        name=inputs["name"],
        symbol=inputs["symbol"],
        market=inputs.get("market", ""),
        sector=inputs.get("sector") or "데이터 없음",
        industry=inputs.get("industry") or "데이터 없음",
        market_cap=int(inputs.get("market_cap") or 0),
        return_1y_pct=float(inputs.get("return_1y_pct") or 0.0),
        rel_return_pp=float(inputs.get("rel_return_pp") or 0.0),
        trailing_eps=_format_eps(inputs.get("trailing_eps")),
        forward_eps=_format_eps(inputs.get("forward_eps")),
        revenue_growth_pct=float(inputs.get("revenue_growth_pct") or 0.0),
        trailing_pe=_format_eps(inputs.get("trailing_pe")),
    )

    model = _get_model()
    gen_cfg = {
        "temperature": 0.3,
        "max_output_tokens": 1024,
        "response_mime_type": "application/json",
    }
    last_err: str | None = None
    last_raw = ""
    for attempt in range(2):
        try:
            resp = model.generate_content(
                prompt,
                generation_config=gen_cfg,
                request_options={"timeout": _TIMEOUT_S},
            )
            last_raw = getattr(resp, "text", "") or ""
            _increment_daily_count()
            try:
                fields = json.loads(last_raw)
            except json.JSONDecodeError:
                return LLMResult(fields={}, raw=last_raw, error="parse_failed")
            required = {"tam_narrative", "narrative_expansion", "bottleneck", "moat"}
            if not required.issubset(fields):
                return LLMResult(fields=fields, raw=last_raw, error="missing_fields")
            return LLMResult(
                fields={k: str(fields[k]) for k in required}, raw=last_raw, error=None
            )
        except Exception as e:
            last_err = str(e)
            logger.warning(
                "Gemini 호출 실패 (attempt %d/2) %s: %s", attempt + 1, inputs.get("symbol"), e
            )
            if attempt == 0:
                time.sleep(_RETRY_BACKOFF_S)
    return LLMResult(fields={}, raw=last_raw, error=last_err or "unknown")
