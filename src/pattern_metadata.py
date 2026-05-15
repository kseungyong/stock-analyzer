"""Pattern badge modal 메타데이터 로더.

YAML 파일에서 패턴별 SVG + 설명 텍스트 로드. 시작 시 1회 로드, 메모리 캐시.
보안: SVG 안에 <script> 차단 (간이 XSS 가드).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_YAML_PATH = Path(__file__).parent / "data" / "pattern_metadata.yaml"
_SCRIPT_PATTERN = re.compile(r"<\s*script", re.IGNORECASE)

_cache: dict[str, Any] | None = None


def _parse_and_validate(yaml_text: str) -> dict[str, Any]:
    """YAML text → dict. SVG 에 <script> 있으면 ValueError."""
    data = yaml.safe_load(yaml_text) or {}
    if not isinstance(data, dict):
        raise ValueError(f"pattern_metadata.yaml: 루트는 dict 여야 함, got {type(data).__name__}")
    for name, entry in data.items():
        if not isinstance(entry, dict):
            raise ValueError(f"pattern_metadata.yaml: {name} 의 값이 dict 가 아님")
        svg = entry.get("svg", "")
        if _SCRIPT_PATTERN.search(svg):
            raise ValueError(
                f"pattern_metadata.yaml: {name} 의 svg 에 <script> 가 포함되어 있음 (XSS 위험)"
            )
        desc = entry.get("description_html", "")
        if _SCRIPT_PATTERN.search(desc):
            raise ValueError(
                f"pattern_metadata.yaml: {name} 의 description_html 에 <script> 가 포함되어 있음"
            )
    return data


def load_metadata() -> dict[str, Any]:
    """YAML 파일 로드. 1회 캐싱."""
    global _cache
    if _cache is None:
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
    _cache = None
