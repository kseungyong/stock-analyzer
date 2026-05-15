"""pattern_metadata.py 단위 테스트."""
from __future__ import annotations

import pytest

from src import pattern_metadata as pm


def setup_function():
    """각 테스트 전 캐시 리셋."""
    pm.reset_cache()


def test_load_returns_dict_with_known_keys():
    data = pm.load_metadata()
    assert isinstance(data, dict)
    # 초기 stub 에 들어 있는 키 (Task 3 에서 채워질 예정 — 일단 2개만)
    assert "더블바텀(W)" in data
    assert "잉태형" in data


def test_lookup_returns_entry_with_required_fields():
    entry = pm.lookup("더블바텀(W)")
    assert entry is not None
    assert "svg" in entry
    assert "description_html" in entry
    assert "signal_typical" in entry
    assert entry["signal_typical"] in {"매수", "매도", "관망", "varies"}


def test_lookup_unknown_pattern_returns_none():
    assert pm.lookup("존재하지않는패턴") is None


def test_svg_xss_guard_raises_on_script_tag():
    """SVG 안에 <script> 가 있으면 startup 시 ValueError."""
    malicious_yaml = """
"악성패턴":
  svg: '<svg><script>alert(1)</script></svg>'
  description_html: '<p>x</p>'
  signal_typical: '관망'
"""
    with pytest.raises(ValueError, match="script"):
        pm._parse_and_validate(malicious_yaml)


def test_svg_xss_guard_case_insensitive():
    """대소문자 무관 차단."""
    malicious_yaml = """
"악성패턴":
  svg: '<svg><SCRIPT>alert(1)</SCRIPT></svg>'
  description_html: '<p>x</p>'
  signal_typical: '관망'
"""
    with pytest.raises(ValueError):
        pm._parse_and_validate(malicious_yaml)


def test_svg_xss_guard_with_whitespace():
    """<  script (공백 삽입) 도 차단."""
    malicious_yaml = """
"악성패턴":
  svg: '<svg>< script >alert(1)</script></svg>'
  description_html: '<p>x</p>'
  signal_typical: '관망'
"""
    with pytest.raises(ValueError):
        pm._parse_and_validate(malicious_yaml)


def test_svg_xss_guard_blocks_event_attribute():
    malicious_yaml = """
"악성패턴":
  svg: '<svg onload="alert(1)"></svg>'
  description_html: '<p>x</p>'
  signal_typical: '관망'
"""
    with pytest.raises(ValueError, match="event handler"):
        pm._parse_and_validate(malicious_yaml)


def test_svg_xss_guard_blocks_foreignobject():
    malicious_yaml = """
"악성패턴":
  svg: '<svg><foreignObject><div>x</div></foreignObject></svg>'
  description_html: '<p>x</p>'
  signal_typical: '관망'
"""
    with pytest.raises(ValueError, match="foreignObject"):
        pm._parse_and_validate(malicious_yaml)


def test_description_html_xss_guard_blocks_event_attribute():
    malicious_yaml = """
"악성패턴":
  svg: '<svg></svg>'
  description_html: '<p onclick="alert(1)">x</p>'
  signal_typical: '관망'
"""
    with pytest.raises(ValueError, match="event handler"):
        pm._parse_and_validate(malicious_yaml)


def test_lookup_tier2_synthesizes_from_generic_template():
    """tier:2 entry → lookup() 가 generic template + entry metadata 합성."""
    pm.reset_cache()
    # Tier 1 6 templates 존재 확인
    assert set(pm._GENERIC_TEMPLATES.keys()) == {
        "bullish_reversal", "bearish_reversal",
        "bullish_continuation", "bearish_continuation",
        "neutral", "doji_variant",
    }
    # 실제 합성 — 타구리 는 bullish_reversal generic template + 매수 signal
    e = pm.lookup("타구리")
    assert e is not None
    assert "<svg" in e["svg"]  # template svg 들어 있음
    assert "타구리" in e["description_html"]  # entry name 합성됨
    assert e["signal_typical"] == "매수"
    assert e.get("tier") == 2
    # 도지 변종 — doji_variant template + 관망 signal
    e2 = pm.lookup("긴다리 도지")
    assert e2 is not None
    assert e2["signal_typical"] == "관망"
    assert e2.get("tier") == 2


def test_lookup_unknown_generic_template_returns_none():
    """잘못된 generic_template 키면 None + warning."""
    pm.reset_cache()
    # 직접 _cache 조작 (테스트 격리)
    pm._cache = {
        "x": {"tier": 2, "generic_template": "nonexistent", "signal_typical": "관망"}
    }
    try:
        assert pm.lookup("x") is None
    finally:
        pm.reset_cache()


def test_lookup_tier2_missing_generic_template_key_returns_none():
    """tier:2 인데 generic_template 키 자체가 없으면 None."""
    pm.reset_cache()
    pm._cache = {"x": {"tier": 2, "signal_typical": "관망"}}
    try:
        assert pm.lookup("x") is None
    finally:
        pm.reset_cache()
