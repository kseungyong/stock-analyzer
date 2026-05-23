"""dart_rules — DART 공시 critical event 분류 + 단일 case 규칙 기반 template.

Hybrid 전략:
- count == 0: empty marker
- count == 1: render_template (LLM 호출 X)
- count >= 2: caller 가 dart_llm.summarize_disclosures 호출
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

# tier 1 — 임계치 없이 무조건 critical
_TIER1_KEYS = (
    "capital_increase", "capital_decrease",
    "treasury_acquire", "treasury_dispose",
    "merger",
    "free_increase",
)

# tier 2 — 임계치 적용
_MAJOR_HOLDERS_MIN_DELTA_PP = 0.5      # 변동 비율 0.5%p 이상
_MAJOR_HOLDERS_MIN_HOLDING_PCT = 5.0   # 보유 비율 5% 이상
_EXEC_HOLDERS_MIN_QTY = 1000           # 변동 주식수 1000주 이상
_EXEC_HOLDERS_MIN_VALUE = 100_000_000  # 1억원

# 단일 case template — {type: (sentiment, trading_view, summary_template)}
_TEMPLATES = {
    "treasury_acquire": (
        "긍정", "매수 — 자사주 매입은 EPS 상승 + 회사 자신감 표명",
        "자기주식 취득 결정 — 주주환원 시그널",
    ),
    "treasury_dispose": (
        "부정", "매도 — 자사주 매도는 유통량 증가 + 주가 압박",
        "자기주식 처분 결정 — 유통량 증가 우려",
    ),
    "capital_increase": (
        "부정", "매도 — 신주 발행으로 기존 주주 지분 희석",
        "유상증자 결정 — 신주 발행에 따른 희석",
    ),
    "capital_decrease": (
        "부정", "매도 — 감자는 일반적으로 부정 시그널 (재무 악화 가능)",
        "감자 결정 — 자본 감소",
    ),
    "merger": (
        "중립", "관망 — 합병 효과 분석 필요 (시너지 vs 통합 비용)",
        "합병 결정 — 시너지/통합 비용 분석 필요",
    ),
    "free_increase": (
        "중립", "관망 — 무상증자는 주주 비례 신주 발행 (분할 효과)",
        "무상증자 결정 — 주주 비례 분할",
    ),
    "major_holders": (
        "중립", "관망 — 대량보유 변동, 보유자 의도 분석 필요",
        "대량보유 변동 (5%+)",
    ),
    "exec_holders": (
        "긍정", "매수 — 임원 자사주 매수는 회사 내부 자신감 시그널",
        "임원/주요주주 매수",
    ),
}


def _safe_float(s, default=0.0):
    try:
        return float(str(s).replace(",", ""))
    except (ValueError, TypeError):
        return default


def _safe_int(s, default=0):
    try:
        return int(float(str(s).replace(",", "")))
    except (ValueError, TypeError):
        return default


def classify_disclosures(disclosures: dict) -> dict:
    """critical event 분류.

    Returns:
        {
            "critical_events": [{"type", "tier", "raw"}, ...],
            "count": int,
            "should_call_llm": bool,   # count >= 2
        }
    """
    events: list[dict] = []

    for key in _TIER1_KEYS:
        for raw in disclosures.get(key) or []:
            events.append({"type": key, "tier": "high", "raw": raw})

    # tier 2 - major_holders
    for raw in disclosures.get("major_holders") or []:
        delta_pp = abs(_safe_float(raw.get("stkrt_irds", 0)))
        holding = _safe_float(raw.get("stkrt", 0))
        if delta_pp >= _MAJOR_HOLDERS_MIN_DELTA_PP and holding >= _MAJOR_HOLDERS_MIN_HOLDING_PCT:
            events.append({"type": "major_holders", "tier": "medium", "raw": raw})

    # tier 2 - exec_holders (qty>=1000 OR 거래금액>=1억)
    for raw in disclosures.get("exec_holders") or []:
        qty = abs(_safe_int(raw.get("stkqy", 0)))
        # DART elestock 필드: trd_amount (거래금액), unrl_uppr_amount, etc — 가장 흔한 trd_amount 사용
        value = abs(_safe_int(raw.get("trd_amount", 0)))
        if qty >= _EXEC_HOLDERS_MIN_QTY or value >= _EXEC_HOLDERS_MIN_VALUE:
            events.append({"type": "exec_holders", "tier": "medium", "raw": raw})

    return {
        "critical_events": events,
        "count": len(events),
        "should_call_llm": len(events) >= 2,
    }


def render_template(event: dict) -> dict:
    """단일 critical event 를 규칙 기반 요약 dict 로 변환.

    Returns: {summary, sentiment, key_events, trading_view, model, generated_at}
    """
    event_type = event["type"]
    raw = event.get("raw") or {}
    sentiment, trading_view, summary_template = _TEMPLATES.get(
        event_type, ("중립", "관망 — 규칙 미정의", "공시 발생"),
    )
    rcept_no = raw.get("rcept_no", "")
    key_event = f"[{rcept_no}] {summary_template}"
    return {
        "summary": summary_template,
        "sentiment": sentiment,
        "key_events": [key_event],
        "trading_view": trading_view,
        "model": "rule_based",
        "generated_at": int(time.time()),
    }
