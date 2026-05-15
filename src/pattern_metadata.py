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


def lookup(pattern_name: str) -> dict[str, Any] | None:
    """패턴 이름 → entry dict 또는 None."""
    data = load_metadata()
    return data.get(pattern_name)


def reset_cache() -> None:
    """테스트용 캐시 리셋."""
    global _cache
    with _cache_lock:
        _cache = None
