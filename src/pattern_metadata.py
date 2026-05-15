"""Pattern badge modal 메타데이터 로더.

YAML 파일에서 패턴별 SVG + 설명 텍스트 로드. 시작 시 1회 로드, 메모리 캐시.
보안: SVG/HTML 안에 <script>, <foreignObject>, on*= 이벤트 핸들러 차단 (간이 XSS 가드).
"""
from __future__ import annotations

import logging
import re
import threading
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_YAML_PATH = Path(__file__).parent / "data" / "pattern_metadata.yaml"

_XSS_PATTERNS = [
    (re.compile(r"<\s*script", re.IGNORECASE), "<script> tag"),
    (re.compile(r"<\s*foreignObject", re.IGNORECASE), "<foreignObject> tag"),
    (re.compile(r"\son\w+\s*=", re.IGNORECASE), "event handler attribute (on*=)"),
]

_cache: dict[str, Any] | None = None
_cache_lock = threading.Lock()


def _parse_and_validate(yaml_text: str) -> dict[str, Any]:
    """YAML text → dict. SVG/description_html 에 XSS 패턴 있으면 ValueError."""
    data = yaml.safe_load(yaml_text) or {}
    if not isinstance(data, dict):
        raise ValueError(f"pattern_metadata.yaml: 루트는 dict 여야 함, got {type(data).__name__}")
    for name, entry in data.items():
        if not isinstance(entry, dict):
            raise ValueError(f"pattern_metadata.yaml: {name} 의 값이 dict 가 아님")
        svg = entry.get("svg", "")
        desc = entry.get("description_html", "")
        for pattern, label in _XSS_PATTERNS:
            if pattern.search(svg):
                raise ValueError(
                    f"pattern_metadata.yaml: {name} 의 svg 에 {label} 가 포함되어 있음 (XSS 위험)"
                )
            if pattern.search(desc):
                raise ValueError(
                    f"pattern_metadata.yaml: {name} 의 description_html 에 {label} 가 포함되어 있음"
                )
    return data


def load_metadata() -> dict[str, Any]:
    """YAML 파일 로드. 1회 캐싱 (thread-safe)."""
    global _cache
    if _cache is not None:
        return _cache
    with _cache_lock:
        if _cache is not None:  # double-check after acquiring lock
            return _cache
        if not _YAML_PATH.exists():
            raise FileNotFoundError(f"pattern_metadata.yaml not found at {_YAML_PATH}")
        with open(_YAML_PATH, "r", encoding="utf-8") as f:
            _cache = _parse_and_validate(f.read())
        logger.info("pattern_metadata 로드 완료: %d 패턴", len(_cache))
    return _cache


_GENERIC_TEMPLATES: dict[str, dict[str, str]] = {
    "bullish_reversal": {
        "svg": '''<svg viewBox="0 0 200 100" xmlns="http://www.w3.org/2000/svg">
            <rect x="50" y="25" width="14" height="50" fill="#DC2626" stroke="#7F1D1D"/>
            <line x1="57" y1="20" x2="57" y2="80" stroke="#7F1D1D"/>
            <rect x="90" y="35" width="14" height="40" fill="#fff" stroke="#16A34A" stroke-width="1.5"/>
            <line x1="97" y1="30" x2="97" y2="80" stroke="#16A34A"/>
            <path d="M 130 50 Q 150 30, 170 35" fill="none" stroke="#16A34A" stroke-width="2" stroke-dasharray="3,2"/>
            <polygon points="170,35 165,30 165,40" fill="#16A34A"/>
            <text x="40" y="95" font-size="7" fill="#666">하락</text>
            <text x="135" y="25" font-size="7" fill="#16A34A">매수 반전</text>
        </svg>''',
        "description_html": "<p><strong>유형:</strong> 매수 반전 패턴. 하락 추세 끝에서 매수 신호를 시사하는 봉 조합.</p>"
                          + "<p>구체 검출 조건은 talib 의 해당 CDL 코드 정의를 따름.</p>",
    },
    "bearish_reversal": {
        "svg": '''<svg viewBox="0 0 200 100" xmlns="http://www.w3.org/2000/svg">
            <rect x="50" y="25" width="14" height="50" fill="#fff" stroke="#16A34A" stroke-width="1.5"/>
            <line x1="57" y1="20" x2="57" y2="80" stroke="#16A34A"/>
            <rect x="90" y="35" width="14" height="40" fill="#DC2626" stroke="#7F1D1D"/>
            <line x1="97" y1="30" x2="97" y2="80" stroke="#7F1D1D"/>
            <path d="M 130 50 Q 150 70, 170 65" fill="none" stroke="#DC2626" stroke-width="2" stroke-dasharray="3,2"/>
            <polygon points="170,65 165,60 165,70" fill="#DC2626"/>
            <text x="40" y="95" font-size="7" fill="#666">상승</text>
            <text x="135" y="85" font-size="7" fill="#DC2626">매도 반전</text>
        </svg>''',
        "description_html": "<p><strong>유형:</strong> 매도 반전 패턴. 상승 추세 끝에서 매도 신호를 시사하는 봉 조합.</p>"
                          + "<p>구체 검출 조건은 talib 의 해당 CDL 코드 정의를 따름.</p>",
    },
    "bullish_continuation": {
        "svg": '''<svg viewBox="0 0 200 100" xmlns="http://www.w3.org/2000/svg">
            <rect x="30" y="55" width="12" height="25" fill="#fff" stroke="#16A34A" stroke-width="1.5"/>
            <rect x="60" y="45" width="12" height="20" fill="#DC2626" stroke="#7F1D1D"/>
            <rect x="90" y="50" width="12" height="15" fill="#DC2626" stroke="#7F1D1D"/>
            <rect x="120" y="35" width="12" height="30" fill="#fff" stroke="#16A34A" stroke-width="1.5"/>
            <rect x="150" y="20" width="12" height="35" fill="#fff" stroke="#16A34A" stroke-width="1.5"/>
            <text x="30" y="95" font-size="7" fill="#16A34A">매수 추세 지속</text>
        </svg>''',
        "description_html": "<p><strong>유형:</strong> 매수 추세 지속 패턴. 일시 조정 후 상승 추세 재개.</p>",
    },
    "bearish_continuation": {
        "svg": '''<svg viewBox="0 0 200 100" xmlns="http://www.w3.org/2000/svg">
            <rect x="30" y="20" width="12" height="25" fill="#DC2626" stroke="#7F1D1D"/>
            <rect x="60" y="30" width="12" height="20" fill="#fff" stroke="#16A34A" stroke-width="1.5"/>
            <rect x="90" y="35" width="12" height="15" fill="#fff" stroke="#16A34A" stroke-width="1.5"/>
            <rect x="120" y="40" width="12" height="30" fill="#DC2626" stroke="#7F1D1D"/>
            <rect x="150" y="50" width="12" height="35" fill="#DC2626" stroke="#7F1D1D"/>
            <text x="30" y="95" font-size="7" fill="#DC2626">매도 추세 지속</text>
        </svg>''',
        "description_html": "<p><strong>유형:</strong> 매도 추세 지속 패턴. 일시 반등 후 하락 추세 재개.</p>",
    },
    "neutral": {
        "svg": '''<svg viewBox="0 0 200 100" xmlns="http://www.w3.org/2000/svg">
            <rect x="80" y="40" width="14" height="20" fill="#fff" stroke="#666" stroke-width="1.5"/>
            <line x1="87" y1="35" x2="87" y2="65" stroke="#666"/>
            <rect x="106" y="40" width="14" height="20" fill="#DC2626" stroke="#7F1D1D"/>
            <line x1="113" y1="35" x2="113" y2="65" stroke="#7F1D1D"/>
            <text x="40" y="95" font-size="7" fill="#666">방향 불명 (관망)</text>
        </svg>''',
        "description_html": "<p><strong>유형:</strong> 방향성 불명. 다른 지표와 결합하여 판단 필요.</p>",
    },
    "doji_variant": {
        "svg": '''<svg viewBox="0 0 200 100" xmlns="http://www.w3.org/2000/svg">
            <line x1="100" y1="20" x2="100" y2="80" stroke="#333" stroke-width="1"/>
            <line x1="80" y1="50" x2="120" y2="50" stroke="#333" stroke-width="3"/>
            <text x="40" y="95" font-size="7" fill="#666">도지 변종 — 시가 ≈ 종가</text>
        </svg>''',
        "description_html": "<p><strong>유형:</strong> 도지 변종. 시가와 종가가 거의 같은 균형 봉 — 추세 정지 시사.</p>",
    },
}


def lookup(pattern_name: str) -> dict[str, Any] | None:
    """패턴 이름 → entry. Tier 2 면 generic template 합성."""
    data = load_metadata()
    entry = data.get(pattern_name)
    if entry is None:
        return None
    # Tier 2: generic_template 키만 있고 svg/description 없음 → 템플릿 합성
    if entry.get("tier") == 2:
        template_key = entry.get("generic_template")
        template = _GENERIC_TEMPLATES.get(template_key)
        if template is None:
            logger.warning("pattern %s: generic_template %s not found", pattern_name, template_key)
            return None
        return {
            "svg": template["svg"],
            "description_html": (
                f'<p><strong>{pattern_name}</strong> ({entry.get("description_short", "")})</p>'
                + template["description_html"]
            ),
            "signal_typical": entry.get("signal_typical", "관망"),
            "tier": 2,
        }
    return entry


def reset_cache() -> None:
    """테스트용 캐시 리셋."""
    global _cache
    with _cache_lock:
        _cache = None
