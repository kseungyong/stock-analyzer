"""확률 경고 — 차트 패턴 결과에서 이미지 6 의 7 패턴 추출.

Spec: docs/superpowers/specs/2026-05-10-pattern-indicators-design.md §3.5

이미지 6 의 매도/매수 경고 패턴 + 신뢰도:
| 패턴             | 시그널 | 신뢰도 | 액션         |
|-----------------|-------|-------|-------------|
| M형 (이중 천장)   | 매도   | 100% | 폭락 대비    |
| 하락 깃발형       | 매도   | 80%  | 신속히 매도  |
| 다이아몬드 천장   | 매도   | 65%  | 완만 하락    |
| 박스권 정리       | 관망   | 50%  | 위험 접근 금지 |
| W바닥 (이중 바닥) | 매수   | 65%  | 신속히 매수  |
| 상승 깃발형       | 매수   | 80%  | 신속히 매수  |
| 상승 쐐기형       | 매수   | 100% | 폭등 맞이    |

현재 detect_chart_patterns 가 잡는 것 중:
  더블바텀(W) → W바닥 65% / 신속히 매수
  더블탑(M)   → M형 100% / 폭락 대비
  나머지 4 패턴은 Phase C 에서 미구현 — 추후 보강.
"""
from __future__ import annotations

from typing import Any

# 이미지 6 의 패턴 → (신뢰도 %, 액션 라벨)
_WARN_MAP: dict[str, tuple[int, str, str]] = {
    # name → (confidence_pct, signal, action_label)
    "더블탑(M)": (100, "매도", "폭락 대비"),
    "M형 (이중 천장)": (100, "매도", "폭락 대비"),
    "하락 깃발형": (80, "매도", "신속히 매도"),
    "다이아몬드 천장": (65, "매도", "완만 하락"),
    "박스권 정리": (50, "관망", "위험 접근 금지"),
    "더블바텀(W)": (65, "매수", "신속히 매수"),
    "W바닥 (이중 바닥)": (65, "매수", "신속히 매수"),
    "상승 깃발형": (80, "매수", "신속히 매수"),
    "상승 쐐기형": (100, "매수", "폭등 맞이"),
}


def detect_warning(chart_patterns: list[dict[str, Any]]) -> dict[str, Any] | None:
    """차트 패턴에서 이미지 6 의 7 경고 패턴 추출 — 가장 강한 (높은 confidence) 1개 반환."""
    candidates: list[dict[str, Any]] = []
    for cp in chart_patterns:
        name = cp.get("name", "")
        if name in _WARN_MAP:
            pct, signal, action = _WARN_MAP[name]
            candidates.append({
                "pattern": name,
                "signal": signal,
                "confidence_pct": pct,
                "action": action,
                "label": f"{name} {pct}% → {action}",
            })
    if not candidates:
        return None
    # 가장 confidence_pct 높은 것
    candidates.sort(key=lambda c: c["confidence_pct"], reverse=True)
    return candidates[0]
