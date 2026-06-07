"""src/universe.py 단위 테스트 — foreign_ranking overlay 머지/분리."""
from __future__ import annotations

import copy

import pytest

from src import universe


@pytest.fixture
def _tmp_overlay(tmp_path, monkeypatch):
    overlay_path = tmp_path / "foreign_ranking.yaml"
    monkeypatch.setattr(universe, "OVERLAY_PATH", overlay_path)
    yield overlay_path


def _base_config():
    return {
        "stocks": {
            "korea": [
                {"symbol": "005930.KS", "name": "삼성전자"},
                {"symbol": "000660.KS", "name": "SK하이닉스"},
            ],
            "us": [{"symbol": "AAPL", "name": "Apple"}],
        },
    }


def test_apply_overlay_no_file_is_noop(_tmp_overlay):
    cfg = _base_config()
    out = universe.apply_overlay(copy.deepcopy(cfg))
    assert out["stocks"]["korea"] == cfg["stocks"]["korea"]


def test_apply_overlay_merges_korea(_tmp_overlay):
    universe.write_overlay({"korea": [
        {"symbol": "034220.KS", "name": "LG디스플레이", "source": "foreign_ranking"},
    ]})
    out = universe.apply_overlay(_base_config())
    symbols = {s["symbol"] for s in out["stocks"]["korea"]}
    assert symbols == {"005930.KS", "000660.KS", "034220.KS"}


def test_apply_overlay_dedupes_user_priority(_tmp_overlay):
    # overlay 가 사용자 등록 symbol 과 겹치면 추가하지 않음 (중복 방지)
    universe.write_overlay({"korea": [
        {"symbol": "005930.KS", "name": "삼성전자(overlay)", "source": "foreign_ranking"},
    ]})
    out = universe.apply_overlay(_base_config())
    korea = out["stocks"]["korea"]
    assert len(korea) == 2  # 추가 없음
    samsung = next(s for s in korea if s["symbol"] == "005930.KS")
    assert samsung["name"] == "삼성전자"  # 사용자 등록 우선, source 마커 없음
    assert "source" not in samsung


def test_strip_overlay_removes_tagged_entries():
    cfg = _base_config()
    cfg["stocks"]["korea"].append(
        {"symbol": "034220.KS", "name": "LG디스플레이", "source": "foreign_ranking"}
    )
    out = universe.strip_overlay(cfg)
    symbols = {s["symbol"] for s in out["stocks"]["korea"]}
    assert symbols == {"005930.KS", "000660.KS"}
    # 원본은 변경되지 않음 (deep copy)
    assert len(cfg["stocks"]["korea"]) == 3


def test_apply_strip_roundtrip(_tmp_overlay):
    universe.write_overlay({"korea": [
        {"symbol": "034220.KS", "name": "LG디스플레이", "source": "foreign_ranking"},
    ]})
    cfg = _base_config()
    merged = universe.apply_overlay(copy.deepcopy(cfg))
    stripped = universe.strip_overlay(merged)
    assert {s["symbol"] for s in stripped["stocks"]["korea"]} == {
        "005930.KS", "000660.KS",
    }
