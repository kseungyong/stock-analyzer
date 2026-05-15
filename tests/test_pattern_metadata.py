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
