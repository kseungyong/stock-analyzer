"""leaders Flask 라우트 테스트 (Spec 2026-05-15, Task 6 — spec 복원)."""
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
    CSRF 검증은 세션 토큰으로 처리.
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
        assert "통과" in body or "pass" in body.lower()

    def test_detail_has_edit_form_with_4_textareas(self, app_with_leader):
        """상세 페이지에 4개 user_* textarea 와 /edit form action 이 있어야 함."""
        r = app_with_leader.get("/leaders/005930.KS")
        body = r.data.decode()
        assert "/leaders/005930.KS/edit" in body
        assert 'id="user-tam_narrative"' in body
        assert 'id="user-narrative_expansion"' in body
        assert 'id="user-bottleneck"' in body
        assert 'id="user-moat"' in body
        # label for 연결 확인 (a11y)
        assert 'for="user-tam_narrative"' in body
        assert 'for="user-moat"' in body

    def test_detail_refresh_action_points_to_refresh_not_refresh_llm(self, app_with_leader):
        """refresh 버튼은 /refresh 이어야 하며 /refresh-llm 이 아니어야 함."""
        r = app_with_leader.get("/leaders/005930.KS")
        body = r.data.decode()
        assert "/leaders/005930.KS/refresh" in body
        assert "refresh-llm" not in body

    def test_detail_no_user_notes_field(self, app_with_leader):
        """spec 에 없는 user_notes textarea 가 없어야 함."""
        r = app_with_leader.get("/leaders/005930.KS")
        body = r.data.decode()
        assert 'name="user_notes"' not in body
        assert "/notes" not in body


# ── 3. POST /leaders/<symbol>/edit ───────────────────────────────────────────

class TestPostEdit:
    def test_happy_path_all_4_fields_redirects_and_saves(self, app_with_leader):
        """4 필드 전부 POST → 302/303 redirect, DB user_* 갱신 확인."""
        r = _post(
            app_with_leader,
            "/leaders/005930.KS/edit",
            {
                "user_tam_narrative": "나의 TAM 분석",
                "user_narrative_expansion": "내러티브 확장",
                "user_bottleneck": "병목 분석",
                "user_moat": "해자 분석",
            },
        )
        assert r.status_code in (302, 303)

        row = leader_cache.get("005930.KS")
        assert row["user_tam_narrative"] == "나의 TAM 분석"
        assert row["user_narrative_expansion"] == "내러티브 확장"
        assert row["user_bottleneck"] == "병목 분석"
        assert row["user_moat"] == "해자 분석"
        assert row["user_edited_at"] is not None
        assert row["user_edited_by"] is not None

    def test_partial_update_only_submitted_fields_changed(self, app_with_leader):
        """2 필드만 POST → 해당 2 필드만 갱신, 나머지 user_* 는 이전 값 유지."""
        # 먼저 전체 기록
        leader_cache.update_user_fields(
            "005930.KS",
            {"tam_narrative": "ORIG_TAM", "moat": "ORIG_MOAT"},
            "tester",
        )

        r = _post(
            app_with_leader,
            "/leaders/005930.KS/edit",
            {"user_tam_narrative": "UPDATED_TAM"},  # moat 은 보내지 않음
        )
        assert r.status_code in (302, 303)

        row = leader_cache.get("005930.KS")
        assert row["user_tam_narrative"] == "UPDATED_TAM"
        assert row["user_moat"] == "ORIG_MOAT"  # 변경 없음

    def test_empty_field_skipped_not_updated(self, app_with_leader):
        """빈 문자열 필드는 DB 갱신하지 않음 (기존 값 보존)."""
        leader_cache.update_user_fields(
            "005930.KS", {"tam_narrative": "EXISTING"}, "tester"
        )

        # user_tam_narrative 를 빈 문자열로 제출
        r = _post(
            app_with_leader,
            "/leaders/005930.KS/edit",
            {"user_tam_narrative": ""},
        )
        assert r.status_code in (302, 303)

        row = leader_cache.get("005930.KS")
        # 빈 값은 skip → 기존 "EXISTING" 보존
        assert row["user_tam_narrative"] == "EXISTING"

    def test_edit_without_csrf_returns_403(self, app_with_leader):
        r = app_with_leader.post(
            "/leaders/005930.KS/edit",
            data={"user_tam_narrative": "hack"},
        )
        assert r.status_code == 403

    def test_edit_missing_symbol_returns_404(self, app_with_leader):
        r = _post(
            app_with_leader,
            "/leaders/999999.KS/edit",
            {"user_tam_narrative": "test"},
        )
        assert r.status_code == 404

    def test_edit_preserves_llm_fields(self, app_with_leader):
        """user_* 저장이 llm_* 필드를 건드리지 않음."""
        _post(
            app_with_leader,
            "/leaders/005930.KS/edit",
            {"user_tam_narrative": "사용자 메모"},
        )
        row = leader_cache.get("005930.KS")
        assert row["llm_tam_narrative"] == "T"
        assert row["llm_moat"] == "M"


# ── 4. POST /leaders/<symbol>/refresh ────────────────────────────────────────

class TestPostRefresh:
    def test_calls_analyze_one_and_upserts_llm(self, app_with_leader, monkeypatch):
        """refresh 가 analyze_one → upsert_llm 호출하고 user_* 보존."""
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

        r = _post(app_with_leader, "/leaders/005930.KS/refresh", {})
        assert r.status_code in (302, 303)

        row = leader_cache.get("005930.KS")
        assert row["llm_tam_narrative"] == "NEW_LLM"
        # user_* 보존 확인
        assert row["user_tam_narrative"] == "USER_EDIT"

    def test_daily_limit_returns_redirect_not_json(self, app_with_leader, monkeypatch):
        """daily limit 초과 시 JSON 이 아닌 redirect 반환 (spec §C.2)."""
        from src import leader_llm

        over_limit_result = leader_llm.LLMResult(fields={}, raw="", error="over_limit")
        monkeypatch.setattr(leader_llm, "analyze_one", lambda inputs: over_limit_result)

        r = _post(app_with_leader, "/leaders/005930.KS/refresh", {})
        # 302/303 redirect (flash + redirect, not 429 JSON)
        assert r.status_code in (302, 303)
        # Content-Type 이 JSON 이 아님
        content_type = r.content_type or ""
        assert "application/json" not in content_type

    def test_refresh_missing_symbol_returns_404(self, app_with_leader, monkeypatch):
        from src import leader_llm

        fake_result = leader_llm.LLMResult(
            fields={"tam_narrative": "X", "narrative_expansion": "X",
                    "bottleneck": "X", "moat": "X"},
            raw="{}",
            error=None,
        )
        monkeypatch.setattr(leader_llm, "analyze_one", lambda inputs: fake_result)

        r = _post(app_with_leader, "/leaders/999999.KS/refresh", {})
        assert r.status_code == 404

    def test_refresh_without_csrf_returns_403(self, app_with_leader, monkeypatch):
        from src import leader_llm

        fake_result = leader_llm.LLMResult(fields={}, raw="{}", error=None)
        monkeypatch.setattr(leader_llm, "analyze_one", lambda inputs: fake_result)

        r = app_with_leader.post("/leaders/005930.KS/refresh", data={})
        assert r.status_code == 403
