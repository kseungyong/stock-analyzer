"""leaders Flask 라우트 6 테스트 (Spec 2026-05-15, Task 6)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from src import leader_cache


# ── fixture ──────────────────────────────────────────────────────────────────

_SAMPLE_ROW = {
    "symbol": "005930.KS",
    "name": "삼성전자",
    "market": "KOSPI",
    "sector": "Tech",
    "industry": "Semi",
    "last_close": 70000.0,
    "market_cap": int(400e12),
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
    "cond1_passed": True,
    "cond2_passed": True,
    "cond3_score": 3,
    "passed": True,
}

_CSRF_TOKEN = "test-csrf-token"


@pytest.fixture
def app_with_leader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """web_app + leader_cache DB 를 tmp_path 로 격리.

    conftest.py 가 ENABLE_BASIC_AUTH 를 pop 하므로 인증 게이트는 OFF.
    CSRF 검증은 monkeypatch 로 no-op 처리.
    """
    db = tmp_path / "predictions.db"
    monkeypatch.setattr(leader_cache, "_DB_PATH", str(db))
    leader_cache.init_db()

    # sample row upsert
    leader_cache.upsert_quantitative([_SAMPLE_ROW])
    leader_cache.upsert_llm(
        "005930.KS",
        {
            "tam_narrative": "T",
            "narrative_expansion": "N",
            "bottleneck": "B",
            "moat": "M",
        },
        model="gemini-2.5-flash",
        raw="{}",
    )

    import src.web_app as wa

    wa.app.config["TESTING"] = True

    with wa.app.test_client() as client:
        with client.session_transaction() as sess:
            sess["csrf_token"] = _CSRF_TOKEN
        yield client


def _post(client, path: str, data: dict):
    """CSRF 토큰을 포함한 POST 헬퍼."""
    return client.post(path, data={**data, "csrf_token": _CSRF_TOKEN})


# ── 1. GET /leaders 목록 ──────────────────────────────────────────────────────

class TestGetLeaders:
    def test_returns_200(self, app_with_leader):
        r = app_with_leader.get("/leaders")
        assert r.status_code == 200

    def test_shows_active_passed_stock(self, app_with_leader):
        r = app_with_leader.get("/leaders")
        body = r.data.decode()
        assert "삼성전자" in body
        assert "005930.KS" in body


# ── 2. GET /leaders/<symbol> 상세 ─────────────────────────────────────────────

class TestGetLeaderDetail:
    def test_returns_200_with_all_5_axes(self, app_with_leader):
        r = app_with_leader.get("/leaders/005930.KS")
        assert r.status_code == 200
        body = r.data.decode()
        # 5축 조건 표시 확인
        assert "삼성전자" in body
        # LLM 4필드
        assert "T" in body   # tam_narrative
        assert "N" in body   # narrative_expansion
        assert "B" in body   # bottleneck
        assert "M" in body   # moat

    def test_returns_404_for_missing_symbol(self, app_with_leader):
        r = app_with_leader.get("/leaders/999999.KS")
        assert r.status_code == 404

    def test_detail_contains_cond_flags(self, app_with_leader):
        r = app_with_leader.get("/leaders/005930.KS")
        body = r.data.decode()
        # cond1 pass / cond2 pass 표시
        assert "통과" in body or "pass" in body.lower()


# ── 3. POST /leaders/<symbol>/notes ──────────────────────────────────────────

class TestPostNotes:
    def test_saves_user_notes_and_redirects(self, app_with_leader):
        r = _post(
            app_with_leader,
            "/leaders/005930.KS/notes",
            {"user_notes": "나의 투자 메모"},
        )
        assert r.status_code in (302, 303)

        row = leader_cache.get("005930.KS")
        assert row["user_notes"] == "나의 투자 메모"

    def test_notes_without_csrf_returns_403(self, app_with_leader):
        r = app_with_leader.post(
            "/leaders/005930.KS/notes",
            data={"user_notes": "hack"},
        )
        assert r.status_code == 403

    def test_notes_preserves_llm_fields(self, app_with_leader):
        """user_notes 저장이 llm_* 필드를 건드리지 않음."""
        _post(
            app_with_leader,
            "/leaders/005930.KS/notes",
            {"user_notes": "메모 저장"},
        )
        row = leader_cache.get("005930.KS")
        assert row["llm_tam_narrative"] == "T"
        assert row["llm_moat"] == "M"


# ── 4. POST /leaders/<symbol>/refresh-llm ────────────────────────────────────

class TestPostRefreshLlm:
    def test_calls_analyze_one_and_upserts_llm(self, app_with_leader, monkeypatch):
        """refresh-llm 이 analyze_one → upsert_llm 호출하고 user_* 보존."""
        from src import leader_llm

        fake_result = leader_llm.LLMResult(
            fields={
                "tam_narrative": "NEW_LLM",
                "narrative_expansion": "NE",
                "bottleneck": "NB",
                "moat": "NM",
            },
            raw="{}",
            error=None,
        )

        # user 수정본 먼저 기록
        leader_cache.update_user_fields(
            "005930.KS", {"tam_narrative": "USER_EDIT"}, "tester"
        )

        monkeypatch.setattr(leader_llm, "analyze_one", lambda inputs: fake_result)

        r = _post(app_with_leader, "/leaders/005930.KS/refresh-llm", {})
        assert r.status_code in (302, 303)

        row = leader_cache.get("005930.KS")
        assert row["llm_tam_narrative"] == "NEW_LLM"
        # user_* 보존 확인
        assert row["user_tam_narrative"] == "USER_EDIT"

    def test_returns_429_when_daily_limit_exhausted(self, app_with_leader, monkeypatch):
        """daily limit 초과 시 429 반환."""
        from src import leader_llm

        over_limit_result = leader_llm.LLMResult(fields={}, raw="", error="over_limit")
        monkeypatch.setattr(leader_llm, "analyze_one", lambda inputs: over_limit_result)

        r = _post(app_with_leader, "/leaders/005930.KS/refresh-llm", {})
        assert r.status_code == 429

    def test_refresh_llm_missing_symbol_returns_404(self, app_with_leader, monkeypatch):
        from src import leader_llm

        fake_result = leader_llm.LLMResult(
            fields={"tam_narrative": "X", "narrative_expansion": "X",
                    "bottleneck": "X", "moat": "X"},
            raw="{}",
            error=None,
        )
        monkeypatch.setattr(leader_llm, "analyze_one", lambda inputs: fake_result)

        r = _post(app_with_leader, "/leaders/999999.KS/refresh-llm", {})
        assert r.status_code == 404
