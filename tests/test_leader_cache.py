"""leader_cache: SQLite CRUD + display 헬퍼."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from src import leader_cache


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """예측 DB 를 tmp_path 로 이동, init_db 호출."""
    p = tmp_path / "predictions.db"
    monkeypatch.setattr(leader_cache, "_DB_PATH", str(p))
    leader_cache.init_db()
    return p


def test_init_db_creates_leaders_table(db_path: Path):
    with sqlite3.connect(str(db_path)) as conn:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )]
    assert "leaders" in names


def _sample_candidate(symbol: str = "005930.KS", passed: bool = True) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "name": "삼성전자",
        "market": "KOSPI",
        "sector": "Tech",
        "industry": "Semiconductors",
        "last_close": 70000.0,
        "market_cap": 400_000_000_000_000,
        "market_cap_quintile": 1,
        "near_high_pct": 0.92,
        "return_1y_pct": 0.45,
        "index_return_1y_pct": 0.15,
        "rel_return_pp": 0.30,
        "trailing_eps": 5000.0,
        "forward_eps": 6000.0,
        "eps_growth_yoy": 0.2,
        "revenue_growth_yoy": 0.18,
        "trailing_pe": 14.0,
        "pe_quintile": 3,
        "cond1_passed": passed,
        "cond2_passed": passed,
        "cond3_score": 3,
        "passed": passed,
    }


def test_upsert_quantitative_inserts_row(db_path: Path):
    leader_cache.upsert_quantitative([_sample_candidate()])
    rows = leader_cache.list_active()
    assert len(rows) == 1
    assert rows[0]["symbol"] == "005930.KS"
    assert rows[0]["passed"] == 1


def test_list_active_excludes_dropped(db_path: Path):
    leader_cache.upsert_quantitative([_sample_candidate()])
    leader_cache.mark_dropped(["005930.KS"])
    rows = leader_cache.list_active()
    assert rows == []


def test_get_returns_dropped_row(db_path: Path):
    leader_cache.upsert_quantitative([_sample_candidate()])
    leader_cache.mark_dropped(["005930.KS"])
    row = leader_cache.get("005930.KS")
    assert row is not None
    assert row["status"] == "dropped"


def test_upsert_llm_preserves_user_fields(db_path: Path):
    leader_cache.upsert_quantitative([_sample_candidate()])
    leader_cache.update_user_fields(
        "005930.KS",
        {"tam_narrative": "사용자 메모"},
        "sykim",
    )
    leader_cache.upsert_llm("005930.KS", {
        "tam_narrative": "LLM 메모",
        "narrative_expansion": "LLM 확장",
        "bottleneck": "LLM 병목",
        "moat": "LLM 해자",
    }, model="gemini-2.5-flash", raw="{...}")
    row = leader_cache.get("005930.KS")
    assert row["user_tam_narrative"] == "사용자 메모"
    assert row["llm_tam_narrative"] == "LLM 메모"


def test_display_field_prefers_user(db_path: Path):
    leader_cache.upsert_quantitative([_sample_candidate()])
    leader_cache.upsert_llm("005930.KS", {
        "tam_narrative": "LLM",
        "narrative_expansion": "LLM",
        "bottleneck": "LLM",
        "moat": "LLM",
    }, model="gemini-2.5-flash", raw="")
    leader_cache.update_user_fields(
        "005930.KS", {"tam_narrative": "USER"}, "sykim"
    )
    row = leader_cache.get("005930.KS")
    assert leader_cache.display_field(row, "tam_narrative") == "USER"
    assert leader_cache.display_field(row, "moat") == "LLM"


def test_recompute_stale_marks_old_llm(db_path: Path):
    leader_cache.upsert_quantitative([_sample_candidate()])
    # 8일 전 LLM 분석
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "UPDATE leaders SET llm_generated_at=? WHERE symbol=?",
            (int(time.time()) - 8 * 86400, "005930.KS"),
        )
        conn.commit()
    leader_cache.recompute_stale()
    row = leader_cache.get("005930.KS")
    assert row["is_stale"] == 1


def test_display_field_returns_pending_when_both_null(db_path: Path):
    """user_* AND llm_* 둘 다 NULL 이면 sentinel 반환."""
    leader_cache.upsert_quantitative([_sample_candidate()])
    row = leader_cache.get("005930.KS")
    assert leader_cache.display_field(row, "tam_narrative") == "(분석 대기 중)"
    assert leader_cache.display_field(row, "moat") == "(분석 대기 중)"


def test_display_field_returns_user_empty_string_not_fallback(db_path: Path):
    """사용자가 명시적으로 빈 문자열 저장 → 빈 문자열 반환 (LLM fallback 아님)."""
    leader_cache.upsert_quantitative([_sample_candidate()])
    leader_cache.upsert_llm("005930.KS", {
        "tam_narrative": "LLM",
        "narrative_expansion": "LLM",
        "bottleneck": "LLM",
        "moat": "LLM",
    }, model="gemini-2.5-flash", raw="")
    leader_cache.update_user_fields(
        "005930.KS", {"tam_narrative": ""}, "sykim"
    )
    row = leader_cache.get("005930.KS")
    # user_tam_narrative = "" → 사용자 의도 = '비움' → "" 반환
    assert leader_cache.display_field(row, "tam_narrative") == ""
    # moat 는 user_* 안 건드림 → llm fallback
    assert leader_cache.display_field(row, "moat") == "LLM"
