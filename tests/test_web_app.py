"""src/web_app.py Flask 라우트 테스트."""
import pytest
import yaml
import tempfile
from pathlib import Path
from unittest.mock import patch

# 테스트용 설정 파일을 임시 경로로 교체
_MINIMAL_CONFIG = {
    "stocks": {"us": [{"symbol": "AAPL", "name": "Apple"}], "korea": []},
    "email": {"sender": "", "password": "", "recipients": [], "smtp_server": "smtp.gmail.com", "smtp_port": 587},
    "schedule": {"hour": 8, "minute": 30, "timezone": "Asia/Seoul"},
}


@pytest.fixture
def config_file(tmp_path):
    """임시 settings.yaml을 생성하고 경로를 반환한다."""
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(yaml.dump(_MINIMAL_CONFIG, allow_unicode=True), encoding="utf-8")
    return cfg


_CSRF_TOKEN = "test-csrf-token"


@pytest.fixture
def client(config_file, monkeypatch):
    """Flask 테스트 클라이언트 — CONFIG_PATH를 임시 파일로 교체하고 CSRF 토큰을 주입한다."""
    import src.web_app as wa
    monkeypatch.setattr(wa, "CONFIG_PATH", config_file)
    wa.app.config["TESTING"] = True
    wa._jobs.clear()
    # 이전 테스트에서 lock이 해제되지 않았을 경우를 대비해 초기화
    if wa._backtest_lock.locked():
        try:
            wa._backtest_lock.release()
        except RuntimeError:
            pass
    with wa.app.test_client() as c:
        with c.session_transaction() as sess:
            sess["csrf_token"] = _CSRF_TOKEN
        yield c


def _post(client, path, data: dict):
    """CSRF 토큰을 포함한 POST 요청 헬퍼."""
    return client.post(path, data={**data, "csrf_token": _CSRF_TOKEN})


class TestIndex:
    def test_returns_200(self, client):
        resp = client.get("/")
        assert resp.status_code == 200

    def test_shows_stock(self, client):
        resp = client.get("/")
        assert b"AAPL" in resp.data

    def test_error_banner(self, client):
        resp = client.get("/?error=테스트오류")
        assert "테스트오류".encode() in resp.data


class TestStocksAdd:
    def test_add_valid_us_symbol(self, client, config_file):
        resp = _post(client, "/stocks/add", {"symbol": "TSLA", "name": "Tesla", "market": "us"})
        assert resp.status_code == 303
        config = yaml.safe_load(config_file.read_text())
        symbols = [s["symbol"] for s in config["stocks"]["us"]]
        assert "TSLA" in symbols

    def test_add_duplicate_redirects_with_error(self, client):
        resp = _post(client, "/stocks/add", {"symbol": "AAPL", "name": "Apple", "market": "us"})
        assert resp.status_code == 303
        assert b"error=" in resp.headers["Location"].encode()

    def test_add_invalid_symbol_redirects_with_error(self, client):
        resp = _post(client, "/stocks/add", {"symbol": "<script>", "name": "hack", "market": "us"})
        assert resp.status_code == 303
        assert b"error=" in resp.headers["Location"].encode()

    def test_add_empty_symbol_redirects_with_error(self, client):
        resp = _post(client, "/stocks/add", {"symbol": "", "name": "NoName", "market": "us"})
        assert resp.status_code == 303

    def test_korea_symbol_appends_ks(self, client, config_file):
        _post(client, "/stocks/add", {"symbol": "005930", "name": "삼성전자", "market": "korea"})
        config = yaml.safe_load(config_file.read_text())
        symbols = [s["symbol"] for s in config["stocks"].get("korea", [])]
        assert "005930.KS" in symbols

    def test_csrf_missing_returns_403(self, client):
        resp = client.post("/stocks/add", data={"symbol": "TSLA", "name": "Tesla", "market": "us"})
        assert resp.status_code == 403


class TestStocksDelete:
    def test_delete_existing(self, client, config_file):
        resp = _post(client, "/stocks/delete", {"symbol": "AAPL"})
        assert resp.status_code == 303
        config = yaml.safe_load(config_file.read_text())
        symbols = [s["symbol"] for s in config["stocks"]["us"]]
        assert "AAPL" not in symbols

    def test_csrf_missing_returns_403(self, client):
        resp = client.post("/stocks/delete", data={"symbol": "AAPL"})
        assert resp.status_code == 403


class TestAnalyzeAll:
    def test_csrf_missing_returns_403(self, client):
        resp = client.post("/analyze-all")
        assert resp.status_code == 403


class TestJobs:
    def test_jobs_list_empty(self, client):
        resp = client.get("/jobs")
        assert resp.status_code == 200
        assert "작업 없음".encode() in resp.data

    def test_job_detail_not_found(self, client):
        resp = client.get("/jobs/nonexistent")
        assert resp.status_code == 200
        assert "찾을 수 없습니다".encode() in resp.data

    def test_api_job_not_found(self, client):
        resp = client.get("/api/jobs/nonexistent")
        assert resp.status_code == 404

    def test_job_download_not_found(self, client):
        resp = client.get("/jobs/nonexistent/download")
        assert resp.status_code == 200
        assert "다운로드할 리포트".encode() in resp.data


class TestApiStocksSearch:
    def test_returns_results(self, client):
        with patch("src.web_app.search_stocks") as m:
            m.return_value = [{"symbol": "AAPL", "name": "Apple Inc.", "market": "us"}]
            resp = client.get("/api/stocks/search?q=apple")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data == [{"symbol": "AAPL", "name": "Apple Inc.", "market": "us"}]
            m.assert_called_once_with("apple", limit=10)

    def test_short_query_returns_empty(self, client):
        with patch("src.web_app.search_stocks") as m:
            resp = client.get("/api/stocks/search?q=a")
            assert resp.status_code == 200
            assert resp.get_json() == []
            m.assert_not_called()

    def test_invalid_chars_returns_empty(self, client):
        with patch("src.web_app.search_stocks") as m:
            resp = client.get("/api/stocks/search?q=DROP%20TABLE%3B")
            assert resp.status_code == 200
            assert resp.get_json() == []
            m.assert_not_called()

    def test_too_long_query_returns_empty(self, client):
        with patch("src.web_app.search_stocks") as m:
            resp = client.get("/api/stocks/search?q=" + "a" * 51)
            assert resp.status_code == 200
            assert resp.get_json() == []
            m.assert_not_called()

    def test_missing_q_returns_empty(self, client):
        resp = client.get("/api/stocks/search")
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_search_exception_returns_empty(self, client):
        with patch("src.web_app.search_stocks", side_effect=RuntimeError("boom")) as m:
            resp = client.get("/api/stocks/search?q=apple")
            assert resp.status_code == 200
            assert resp.get_json() == []
            m.assert_called_once_with("apple", limit=10)

    def test_korean_query(self, client):
        with patch("src.web_app.search_stocks") as m:
            m.return_value = [{"symbol": "005930.KS", "name": "삼성전자", "market": "korea"}]
            resp = client.get("/api/stocks/search?q=삼성")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data == [{"symbol": "005930.KS", "name": "삼성전자", "market": "korea"}]
            # 한글이 unicode-escape되지 않고 그대로 응답에 포함되어야 함
            assert "삼성전자".encode("utf-8") in resp.data
            m.assert_called_once_with("삼성", limit=10)


class TestBacktest:
    def test_csrf_missing_returns_403(self, client):
        resp = client.post("/backtest/AAPL")
        assert resp.status_code == 403

    def test_invalid_symbol_returns_400(self, client):
        resp = _post(client, "/backtest/<bad>", {})
        assert resp.status_code == 400

    @patch("src.web_app._run_backtest_bg")
    def test_valid_request_redirects_to_job(self, run_mock, client):
        resp = _post(client, "/backtest/AAPL", {})
        assert resp.status_code == 303
        assert "/jobs/" in resp.headers["Location"]
        run_mock.assert_called_once()

    @patch("src.web_app._run_backtest_bg")
    def test_concurrent_request_returns_error(self, run_mock, client):
        import src.web_app as wa
        wa._backtest_lock.acquire()
        try:
            resp = _post(client, "/backtest/AAPL", {})
            assert resp.status_code == 303
            assert "error=" in resp.headers["Location"]
        finally:
            wa._backtest_lock.release()


class TestAnalyzePost:
    def test_post_with_return_to_jobs_redirects_to_jobs(self, client, monkeypatch):
        # _run_analysis_bg 를 즉시 종료 stub 으로 교체
        import src.web_app as wa
        monkeypatch.setattr(wa, "_run_analysis_bg", lambda *a, **k: None)
        resp = _post(client, "/analyze/AAPL", {"return_to": "jobs"})
        assert resp.status_code == 303
        assert resp.headers["Location"].startswith("/jobs/")

    def test_post_with_return_to_stock_redirects_to_stock(self, client, monkeypatch):
        import src.web_app as wa
        monkeypatch.setattr(wa, "_run_analysis_bg", lambda *a, **k: None)
        resp = _post(client, "/analyze/AAPL", {"return_to": "stock"})
        assert resp.status_code == 303
        loc = resp.headers["Location"]
        assert loc.startswith("/stock/AAPL?job=")

    def test_post_default_return_to_is_jobs(self, client, monkeypatch):
        import src.web_app as wa
        monkeypatch.setattr(wa, "_run_analysis_bg", lambda *a, **k: None)
        resp = _post(client, "/analyze/AAPL", {})
        assert resp.status_code == 303
        assert resp.headers["Location"].startswith("/jobs/")

    def test_post_without_csrf_returns_403(self, client):
        resp = client.post("/analyze/AAPL", data={"return_to": "jobs"})
        assert resp.status_code == 403

    def test_get_method_not_allowed(self, client):
        resp = client.get("/analyze/AAPL")
        assert resp.status_code == 405


class TestAnalyzeBgCachePut:
    def test_successful_analysis_puts_cache(self, client, monkeypatch, tmp_path):
        """_run_analysis_bg 성공 → analysis_cache.put 호출 확인."""
        import src.web_app as wa
        from src import analysis_cache as ac

        captured = {}

        def fake_analyze_stock(symbol, name, market=None):
            return {"name": name, "symbol": symbol, "df": None,
                    "signal": None, "prediction": None, "news": [], "sentiment": None}

        def fake_generate_report(analyses):
            return "<p>fake report</p>"

        def fake_put(cache_key, market, result_html, source, *,
                     signal_value=None, signal_score=None,
                     bnf_signal_value=None, bnf_signal_score=None):
            captured["cache_key"] = cache_key
            captured["market"] = market
            captured["result_html"] = result_html
            captured["source"] = source

        monkeypatch.setattr("main.analyze_stock", fake_analyze_stock)
        monkeypatch.setattr("src.report_generator.generate_report", fake_generate_report)
        monkeypatch.setattr(ac, "put", fake_put)

        # config 의 AAPL → market="us" 로 결정
        wa._jobs.clear()
        job_id = "testjob1"
        wa._jobs[job_id] = {
            "status": "running", "symbol": "AAPL", "name": "Apple",
            "result_html": None, "error": None,
            "started_at": "00:00:00",
        }
        wa._run_analysis_bg(job_id, "AAPL", "Apple")

        assert captured["cache_key"] == "AAPL"
        assert captured["market"] == "us"
        assert captured["result_html"] == "<p>fake report</p>"
        assert captured["source"] == "manual"

    def test_failed_analysis_does_not_put_cache(self, client, monkeypatch):
        import src.web_app as wa
        from src import analysis_cache as ac

        called = {"put": False}
        monkeypatch.setattr(ac, "put", lambda *a, **k: called.__setitem__("put", True))
        monkeypatch.setattr("main.analyze_stock", lambda s, n, market=None: None)

        wa._jobs.clear()
        wa._jobs["job2"] = {
            "status": "running", "symbol": "AAPL", "name": "Apple",
            "result_html": None, "error": None, "started_at": "00:00:00",
        }
        wa._run_analysis_bg("job2", "AAPL", "Apple")
        assert called["put"] is False


class TestStockGet:
    def test_cache_hit_shows_result_html(self, client):
        from src import analysis_cache as ac
        ac.init_db()
        ac.put("AAPL", "us", "<p>cached body</p>", "auto_cron")
        resp = client.get("/stock/AAPL")
        assert resp.status_code == 200
        assert b"cached body" in resp.data

    def test_cache_hit_shows_meta_bar(self, client):
        from src import analysis_cache as ac
        ac.init_db()
        ac.put("AAPL", "us", "<p>cached</p>", "auto_cron")
        resp = client.get("/stock/AAPL")
        # 메타바: "분석 시각" 텍스트 포함
        assert "분석 시각".encode() in resp.data
        # 재분석 폼: return_to=stock
        assert b'name="return_to" value="stock"' in resp.data

    def test_cache_miss_shows_start_button(self, client):
        from src import analysis_cache as ac
        ac.init_db()
        # 캐시 비어있음 — 그러나 이전 테스트에서 잔여 row 가 있을 수 있어 명시적으로 정리
        import sqlite3
        with sqlite3.connect(ac._DB_PATH) as conn:
            conn.execute("DELETE FROM analysis_cache WHERE cache_key = 'AAPL'")
        resp = client.get("/stock/AAPL")
        assert resp.status_code == 200
        assert "분석 이력".encode() in resp.data or "분석 시작".encode() in resp.data
        assert b'name="return_to"' in resp.data  # 폼 존재

    def test_invalid_symbol_returns_400_or_404(self, client):
        resp = client.get("/stock/<script>")
        assert resp.status_code in (400, 404)


class TestStockInlinePolling:
    def test_running_job_renders_overlay_and_polling_script(self, client):
        import src.web_app as wa
        from src import analysis_cache as ac
        ac.init_db()
        ac.put("AAPL", "us", "<p>old</p>", "auto_cron")

        wa._jobs.clear()
        wa._jobs["abc12345"] = {
            "status": "running", "symbol": "AAPL", "name": "Apple",
            "result_html": None, "error": None, "started_at": "16:00:00",
        }

        resp = client.get("/stock/AAPL?job=abc12345")
        assert resp.status_code == 200
        # 오버레이
        assert "재분석 중".encode() in resp.data or "분석 진행 중".encode() in resp.data
        # 폴링 JS — jobId 가 const 로 삽입되고 fetch 가 /api/jobs/ 호출
        assert b'const jobId = "abc12345"' in resp.data
        assert b"/api/jobs/" in resp.data
        # 기존 캐시는 흐리게 보여줌
        assert b"old" in resp.data

    def test_completed_job_redirects_to_clean_url(self, client):
        import src.web_app as wa
        wa._jobs.clear()
        wa._jobs["done1234"] = {
            "status": "done", "symbol": "AAPL", "name": "Apple",
            "result_html": "<p>x</p>", "error": None, "started_at": "16:00:00",
        }
        resp = client.get("/stock/AAPL?job=done1234")
        assert resp.status_code == 303
        assert resp.headers["Location"] == "/stock/AAPL"

    def test_unknown_job_id_redirects(self, client):
        resp = client.get("/stock/AAPL?job=unknown1")
        assert resp.status_code == 303
        assert resp.headers["Location"] == "/stock/AAPL"


class TestStockAll:
    def test_cache_hit(self, client):
        from src import analysis_cache as ac
        ac.init_db()
        ac.put("ALL", "all", "<p>full digest</p>", "manual")
        resp = client.get("/stock/all")
        assert resp.status_code == 200
        assert b"full digest" in resp.data

    def test_cache_miss_shows_start_button(self, client):
        from src import analysis_cache as ac
        ac.init_db()
        # 명시적으로 ALL row 삭제
        import sqlite3
        with sqlite3.connect(ac._DB_PATH) as conn:
            conn.execute("DELETE FROM analysis_cache WHERE cache_key = 'ALL'")
        resp = client.get("/stock/all")
        assert resp.status_code == 200
        assert "전체 분석".encode() in resp.data


class TestAnalyzeAllUpsert:
    def _setup(self, monkeypatch):
        """공통 stub — 2종목 (한국 1, 미국 1)."""
        import src.web_app as wa
        from src import analysis_cache as ac

        fake_analyses = [
            {"symbol": "AAPL", "name": "Apple"},
            {"symbol": "005930.KS", "name": "삼성전자"},
        ]
        fake_config = {"stocks": {
            "us":    [{"symbol": "AAPL", "name": "Apple"}],
            "korea": [{"symbol": "005930.KS", "name": "삼성전자"}],
        }}
        monkeypatch.setattr("main.collect_analyses", lambda cfg: fake_analyses)
        monkeypatch.setattr("main.load_config", lambda: fake_config)

        captured: list[tuple] = []
        monkeypatch.setattr(
            ac, "put",
            lambda *a, **k: captured.append((a, k)),
        )

        # generate_report 는 종목 수에 따라 다른 HTML 반환
        def fake_gen(items):
            if len(items) == 1:
                return f"<p>single:{items[0]['symbol']}</p>"
            return f"<p>digest:{len(items)}</p>"
        monkeypatch.setattr("src.report_generator.generate_report", fake_gen)

        wa._jobs.clear()
        wa._jobs["full1"] = {
            "status": "running", "symbol": "ALL", "name": "전체 종목",
            "result_html": None, "error": None, "started_at": "16:00:00",
        }
        return wa, captured

    def test_full_analysis_puts_all_cache(self, client, monkeypatch):
        wa, captured = self._setup(monkeypatch)
        wa._run_full_analysis_bg("full1")
        # put("ALL", "all", "<p>digest:2</p>", source="manual")
        all_calls = [c for c in captured if c[0][0] == "ALL"]
        assert len(all_calls) == 1
        args, kwargs = all_calls[0]
        assert args == ("ALL", "all", "<p>digest:2</p>")
        assert kwargs.get("source") == "manual"

    def test_full_analysis_puts_per_symbol_cache(self, client, monkeypatch):
        """일괄 분석이 종목별 row 도 함께 UPSERT — 카드 신선도 갱신용."""
        wa, captured = self._setup(monkeypatch)
        wa._run_full_analysis_bg("full1")

        # 종목별 put 호출 — AAPL (us), 005930.KS (korea) 각 1회
        symbol_calls = {c[0][0]: c[0] for c in captured if c[0][0] != "ALL"}
        assert "AAPL" in symbol_calls
        assert symbol_calls["AAPL"] == ("AAPL", "us", "<p>single:AAPL</p>")
        assert "005930.KS" in symbol_calls
        assert symbol_calls["005930.KS"] == (
            "005930.KS", "korea", "<p>single:005930.KS</p>"
        )


class TestIndexFreshness:
    def test_card_shows_no_history_when_no_cache(self, client):
        from src import analysis_cache as ac
        ac.init_db()
        # AAPL row가 다른 테스트로부터 남아있을 수 있어 명시적 삭제
        import sqlite3
        with sqlite3.connect(ac._DB_PATH) as conn:
            conn.execute("DELETE FROM analysis_cache WHERE cache_key = 'AAPL'")
        resp = client.get("/")
        assert b"AAPL" in resp.data
        # 분석 이력 없음 안내가 카드에 표시
        assert "분석 이력".encode() in resp.data

    def test_card_shows_fresh_badge(self, client):
        from src import analysis_cache as ac
        ac.init_db()
        ac.put("AAPL", "us", "<p>x</p>", "auto_cron")
        resp = client.get("/")
        # fresh 마크 (🟢)
        assert b"\xf0\x9f\x9f\xa2" in resp.data  # 🟢 utf-8

    def test_card_links_to_stock_view(self, client):
        from src import analysis_cache as ac
        ac.init_db()
        ac.put("AAPL", "us", "<p>x</p>", "auto_cron")
        resp = client.get("/")
        assert b'href="/stock/AAPL"' in resp.data

    def test_card_has_reanalyze_form(self, client):
        from src import analysis_cache as ac
        ac.init_db()
        ac.put("AAPL", "us", "<p>x</p>", "auto_cron")
        resp = client.get("/")
        # 재분석 폼 (return_to=jobs) 존재
        assert b'name="return_to"' in resp.data


class TestSafeCacheGetAndReturnToAllowlist:
    def test_safe_cache_get_swallows_sqlite_error(self, client, monkeypatch):
        """analysis_cache.get 가 raise 해도 _safe_cache_get 은 None 반환."""
        import src.web_app as wa
        from src import analysis_cache as ac

        def boom(key):
            raise RuntimeError("sqlite locked")
        monkeypatch.setattr(ac, "get", boom)
        # _safe_cache_get 직접 호출
        result = wa._safe_cache_get("AAPL")
        assert result is None

    def test_index_survives_cache_get_failure(self, client, monkeypatch):
        """대시보드 카드 루프가 cache.get 예외에도 500 안 남."""
        from src import analysis_cache as ac

        def boom(key):
            raise RuntimeError("sqlite locked")
        monkeypatch.setattr(ac, "get", boom)
        resp = client.get("/")
        assert resp.status_code == 200
        # AAPL 카드는 여전히 보여짐 (분석 이력 없음 처리)
        assert b"AAPL" in resp.data

    def test_return_to_unknown_value_falls_back_to_jobs(self, client, monkeypatch):
        """return_to=banana → /jobs/<id> redirect (allowlist)."""
        import src.web_app as wa
        monkeypatch.setattr(wa, "_run_analysis_bg", lambda *a, **k: None)
        resp = _post(client, "/analyze/AAPL", {"return_to": "banana"})
        assert resp.status_code == 303
        assert resp.headers["Location"].startswith("/jobs/")


class TestBasicAuthGate:
    def test_no_auth_allowed_when_disabled(self, client, monkeypatch):
        """ENABLE_BASIC_AUTH 미설정 (기본값) → 인증 없이 모든 요청 허용."""
        import src.web_app as wa
        monkeypatch.setattr(wa, "_basic_auth_on", False)
        resp = client.get("/")
        assert resp.status_code == 200

    def test_returns_401_without_auth_when_enabled(self, client, monkeypatch):
        import src.web_app as wa
        monkeypatch.setattr(wa, "_basic_auth_on", True)
        monkeypatch.setattr(wa, "_basic_auth_users", {"admin": "secret"})
        resp = client.get("/")
        assert resp.status_code == 401
        assert "WWW-Authenticate" in resp.headers
        assert resp.headers["WWW-Authenticate"].startswith("Basic")

    def test_returns_401_on_wrong_password(self, client, monkeypatch):
        import src.web_app as wa
        from base64 import b64encode
        monkeypatch.setattr(wa, "_basic_auth_on", True)
        monkeypatch.setattr(wa, "_basic_auth_users", {"admin": "secret"})
        creds = b64encode(b"admin:wrong").decode()
        resp = client.get("/", headers={"Authorization": f"Basic {creds}"})
        assert resp.status_code == 401

    def test_returns_200_on_correct_credentials(self, client, monkeypatch):
        import src.web_app as wa
        from base64 import b64encode
        monkeypatch.setattr(wa, "_basic_auth_on", True)
        monkeypatch.setattr(wa, "_basic_auth_users", {"admin": "secret"})
        creds = b64encode(b"admin:secret").decode()
        resp = client.get("/", headers={"Authorization": f"Basic {creds}"})
        assert resp.status_code == 200

    def test_multi_users_each_authenticate_independently(self, client, monkeypatch):
        """여러 사용자 — 각자 자기 비밀번호로 통과, 다른 사람 비밀번호는 실패."""
        import src.web_app as wa
        from base64 import b64encode
        monkeypatch.setattr(wa, "_basic_auth_on", True)
        monkeypatch.setattr(wa, "_basic_auth_users", {
            "admin": "pw1", "sykim": "pw2", "guest": "pw3",
        })
        # 각 사용자 자기 비번 → 200
        for user, pw in [("admin", "pw1"), ("sykim", "pw2"), ("guest", "pw3")]:
            creds = b64encode(f"{user}:{pw}".encode()).decode()
            resp = client.get("/", headers={"Authorization": f"Basic {creds}"})
            assert resp.status_code == 200, f"{user} 인증 실패"
        # admin 이 sykim 비번 사용 → 401
        creds = b64encode(b"admin:pw2").decode()
        resp = client.get("/", headers={"Authorization": f"Basic {creds}"})
        assert resp.status_code == 401
        # 모르는 사용자 → 401
        creds = b64encode(b"unknown:any").decode()
        resp = client.get("/", headers={"Authorization": f"Basic {creds}"})
        assert resp.status_code == 401


class TestParseBasicAuthUsers:
    def test_multi_format(self, monkeypatch):
        from src.web_app import _parse_basic_auth_users
        monkeypatch.setenv("BASIC_AUTH_USERS", "admin:pw1;sykim:pw2;guest:pw3")
        monkeypatch.delenv("BASIC_AUTH_USERNAME", raising=False)
        monkeypatch.delenv("BASIC_AUTH_PASSWORD", raising=False)
        assert _parse_basic_auth_users() == {
            "admin": "pw1", "sykim": "pw2", "guest": "pw3",
        }

    def test_password_can_contain_colon(self, monkeypatch):
        from src.web_app import _parse_basic_auth_users
        monkeypatch.setenv("BASIC_AUTH_USERS", "admin:p:w:1;sykim:pw2")
        users = _parse_basic_auth_users()
        assert users["admin"] == "p:w:1"
        assert users["sykim"] == "pw2"

    def test_legacy_single_user_fallback(self, monkeypatch):
        from src.web_app import _parse_basic_auth_users
        monkeypatch.delenv("BASIC_AUTH_USERS", raising=False)
        monkeypatch.setenv("BASIC_AUTH_USERNAME", "admin")
        monkeypatch.setenv("BASIC_AUTH_PASSWORD", "secret")
        assert _parse_basic_auth_users() == {"admin": "secret"}

    def test_empty_returns_empty_dict(self, monkeypatch):
        from src.web_app import _parse_basic_auth_users
        for k in ("BASIC_AUTH_USERS", "BASIC_AUTH_USERNAME", "BASIC_AUTH_PASSWORD"):
            monkeypatch.delenv(k, raising=False)
        assert _parse_basic_auth_users() == {}

    def test_malformed_entries_skipped(self, monkeypatch):
        from src.web_app import _parse_basic_auth_users
        monkeypatch.setenv("BASIC_AUTH_USERS", "good:pw;malformed_no_colon;:nopwuser")
        monkeypatch.delenv("BASIC_AUTH_USERNAME", raising=False)
        monkeypatch.delenv("BASIC_AUTH_PASSWORD", raising=False)
        assert _parse_basic_auth_users() == {"good": "pw"}


class TestLogout:
    def test_logout_returns_401_with_logout_realm(self, client, monkeypatch):
        """/logout 은 401 + realm='logout' + Clear-Site-Data 응답 (Basic Auth 캐시 무효화)."""
        import src.web_app as wa
        monkeypatch.setattr(wa, "_basic_auth_on", True)
        monkeypatch.setattr(wa, "_basic_auth_users", {"admin": "secret"})
        from base64 import b64encode
        creds = b64encode(b"admin:secret").decode()
        resp = client.get("/logout", headers={"Authorization": f"Basic {creds}"})
        assert resp.status_code == 401
        assert resp.headers.get("WWW-Authenticate") == 'Basic realm="logout"'
        assert resp.headers.get("Clear-Site-Data") == '"*"'
        assert "로그아웃".encode() in resp.data

    def test_logout_bypasses_auth_gate(self, client, monkeypatch):
        """/logout 은 인증 안 된 사용자도 접근 가능 (안내 페이지 보여줌)."""
        import src.web_app as wa
        monkeypatch.setattr(wa, "_basic_auth_on", True)
        monkeypatch.setattr(wa, "_basic_auth_users", {"admin": "secret"})
        # 자격 없이 /logout 접근 — gate 가 우회 → 라우트 자체가 401 응답
        resp = client.get("/logout")
        assert resp.status_code == 401
        assert "로그아웃".encode() in resp.data  # 안내 본문 도달함

    def test_logout_link_in_topbar_when_auth_enabled(self, client, monkeypatch):
        import src.web_app as wa
        monkeypatch.setattr(wa, "_basic_auth_on", True)
        monkeypatch.setattr(wa, "_basic_auth_users", {"admin": "secret"})
        from base64 import b64encode
        creds = b64encode(b"admin:secret").decode()
        resp = client.get("/", headers={"Authorization": f"Basic {creds}"})
        assert b'href="/logout"' in resp.data
        assert "로그아웃".encode() in resp.data

    def test_no_logout_link_when_auth_disabled(self, client, monkeypatch):
        import src.web_app as wa
        monkeypatch.setattr(wa, "_basic_auth_on", False)
        resp = client.get("/")
        assert b'href="/logout"' not in resp.data


class TestModelTabs:
    def test_renders_5_radio_inputs_with_rf_checked(self, client):
        """탭바 — 라디오 5개, RF 가 기본 활성."""
        from src.web_app import _render_model_tabs
        html = _render_model_tabs()
        # 5개 라디오
        for key in ("rf", "lgbm", "lstm", "transformer", "ensemble"):
            assert f'id="mtab-{key}"' in html
            assert f'for="mtab-{key}"' in html
            assert f'mtab-panel-{key}' in html
        # RF 가 checked — radio input 자체에 'checked' 속성 + RF 키워드
        rf_input_segment = html.split('id="mtab-rf"')[1].split(">")[0]
        assert "checked" in rf_input_segment

    def test_panels_contain_model_descriptions(self, client):
        """각 모델 패널에 한국어 설명 키워드 포함."""
        from src.web_app import _render_model_tabs
        html = _render_model_tabs()
        assert "Random Forest" in html
        assert "그래디언트 부스팅" in html  # LGBM
        assert "Long Short-Term Memory" in html
        assert "어텐션" in html              # Transformer
        assert "앙상블" in html              # Ensemble

    def test_panel_has_strengths_weaknesses(self, client):
        """각 패널에 '강점' 과 '약점' 마크업."""
        from src.web_app import _render_model_tabs
        html = _render_model_tabs()
        # 5 모델 × 2 → 최소 5번씩 등장
        assert html.count("<strong>강점</strong>") >= 5
        assert html.count("<strong>약점</strong>") >= 5


class TestHitRateSummary:
    def test_empty_rates_shows_pending_alert(self, client):
        from src.web_app import _render_hit_rate_summary
        html = _render_hit_rate_summary({})
        assert "평가된 예측이 아직 없습니다" in html

    def test_renders_5_cards_with_pct(self, client):
        from src.web_app import _render_hit_rate_summary
        rates = {
            "rf":          {"hit_rate": 0.72, "n": 50},
            "lgbm":        {"hit_rate": 0.65, "n": 50},
            "lstm":        {"hit_rate": 0.45, "n": 50},
            "transformer": {"hit_rate": 0.55, "n": 50},
            "ensemble":    {"hit_rate": 0.68, "n": 50},
        }
        html = _render_hit_rate_summary(rates)
        for label in ("RF", "LGBM", "LSTM", "Transformer", "Ensemble"):
            assert label in html
        assert "72.0%" in html
        assert "45.0%" in html
        assert "50회 평가" in html
        # 색상 클래스 — green 60%+, amber 50%+, red <50
        assert "var(--green-600)" in html  # rf=72, ensemble=68
        assert "var(--amber-500)" in html  # transformer=55
        assert "var(--red-600)" in html    # lstm=45

    def test_missing_model_shows_empty_card(self, client):
        from src.web_app import _render_hit_rate_summary
        rates = {"rf": {"hit_rate": 0.7, "n": 10}}
        html = _render_hit_rate_summary(rates)
        # 다른 4 모델은 "평가 없음"
        assert html.count('hit-rate-card empty') == 4
        assert "평가 없음" in html


class TestHistoryTable:
    def _row(self, *, td=1730000000, ensemble_hit=1, actual=105.0,
             models=None):
        if models is None:
            models = {
                m: {"direction": "상승", "confidence": 70.0, "hit": ensemble_hit}
                for m in ("rf", "lgbm", "lstm", "transformer", "ensemble")
            }
        return {
            "target_date": td,
            "ts": td - 86400,
            "base_close": 100.0,
            "actual_close": actual,
            "ensemble_hit": ensemble_hit,
            "models": models,
        }

    def test_renders_thead_with_9_columns(self, client):
        from src.web_app import _render_history_table
        html = _render_history_table([self._row()])
        for col in ("분석일", "기준 종가", "RF", "LGBM", "LSTM",
                    "Transf", "Ensemble", "실제 종가", "판정"):
            assert col in html

    def test_hit_row_shows_green_verdict(self, client):
        from src.web_app import _render_history_table
        html = _render_history_table([self._row(ensemble_hit=1, actual=105.0)])
        assert 'badge-hit' in html
        assert "적중" in html
        assert 'pred-hit' in html  # 모델 셀

    def test_miss_row_shows_red_verdict(self, client):
        from src.web_app import _render_history_table
        miss_models = {
            m: {"direction": "상승", "confidence": 70.0, "hit": 0}
            for m in ("rf", "lgbm", "lstm", "transformer", "ensemble")
        }
        html = _render_history_table([self._row(
            ensemble_hit=0, actual=95.0, models=miss_models,
        )])
        assert 'badge-miss' in html
        assert "빗나감" in html
        assert 'pred-miss' in html

    def test_pending_row_is_grey(self, client):
        from src.web_app import _render_history_table
        pending_models = {
            m: {"direction": "하락", "confidence": 60.0, "hit": None}
            for m in ("rf", "lgbm", "lstm", "transformer", "ensemble")
        }
        html = _render_history_table([self._row(
            ensemble_hit=None, actual=None, models=pending_models,
        )])
        assert 'class="row-pending"' in html
        assert "평가 대기" in html
        assert "—" in html  # actual_close 자리
        assert 'pred-pending' in html

    def test_missing_model_cell_shows_dash(self, client):
        from src.web_app import _render_history_table
        partial = {
            "rf": {"direction": "상승", "confidence": 70.0, "hit": 1},
            # lgbm/lstm/transformer/ensemble 누락
        }
        html = _render_history_table([self._row(
            ensemble_hit=None, actual=105.0, models=partial,
        )])
        # 누락 셀 4개
        assert html.count("<td>—</td>") >= 4

    def test_arrow_direction(self, client):
        from src.web_app import _render_history_table
        up_models = {m: {"direction": "상승", "confidence": 70.0, "hit": 1}
                     for m in ("rf", "lgbm", "lstm", "transformer", "ensemble")}
        down_models = {m: {"direction": "하락", "confidence": 60.0, "hit": 0}
                       for m in ("rf", "lgbm", "lstm", "transformer", "ensemble")}
        html = _render_history_table([
            self._row(td=1730086400, ensemble_hit=1, actual=105.0, models=up_models),
            self._row(td=1730000000, ensemble_hit=0, actual=95.0, models=down_models),
        ])
        assert "🔼" in html  # 상승
        assert "🔽" in html  # 하락

    def test_confidence_displayed_as_integer_percent(self, client):
        """confidence 는 DB 에 0~100 단위로 저장됨 — 표시도 그대로 정수 % (곱셈 X)."""
        from src.web_app import _render_history_table
        models = {
            "rf":          {"direction": "상승", "confidence": 75.0, "hit": 1},
            "lgbm":        {"direction": "상승", "confidence": 68.5, "hit": 1},
            "lstm":        {"direction": "하락", "confidence": 51.4, "hit": 0},
            "transformer": {"direction": "상승", "confidence": 100.0, "hit": 1},
            "ensemble":    {"direction": "상승", "confidence": 73.7, "hit": 1},
        }
        html = _render_history_table([self._row(
            ensemble_hit=1, actual=105.0, models=models,
        )])
        # 75.0 → "75%", 68.5 → "69%", 51.4 → "51%", 100 → "100%", 73.7 → "74%"
        assert "75%" in html
        assert "69%" in html
        assert "51%" in html
        assert "100%" in html
        assert "74%" in html
        # 곱셈된 값 (7500%, 6850% 등) 는 절대 안 나타남
        assert "7500%" not in html
        assert "6850%" not in html


class TestRenderPredictionHistory:
    def test_empty_when_no_data(self, client, monkeypatch):
        """rates + rows 둘 다 비어있으면 빈 문자열."""
        from src import prediction_history
        from src.web_app import _render_prediction_history
        monkeypatch.setattr(prediction_history, "hit_rate_by_model",
                            lambda *a, **k: {})
        monkeypatch.setattr(prediction_history, "list_history",
                            lambda *a, **k: [])
        assert _render_prediction_history("AAPL") == ""

    def test_shows_summary_only_when_no_rows(self, client, monkeypatch):
        """rates 만 있고 rows 비어있으면 섹션 표시 + details 안내."""
        from src import prediction_history
        from src.web_app import _render_prediction_history
        monkeypatch.setattr(prediction_history, "hit_rate_by_model",
                            lambda *a, **k: {"rf": {"hit_rate": 0.7, "n": 10}})
        monkeypatch.setattr(prediction_history, "list_history",
                            lambda *a, **k: [])
        html = _render_prediction_history("AAPL")
        assert "예측 정확도" in html
        assert "70.0%" in html
        assert "아직 평가된 예측 이력이 없습니다" in html

    def test_full_rendering_with_rates_and_rows(self, client, monkeypatch):
        """rates + rows 둘 다 있으면 헤더 + summary + tabs + details 모두."""
        from src import prediction_history
        from src.web_app import _render_prediction_history
        monkeypatch.setattr(prediction_history, "hit_rate_by_model",
                            lambda *a, **k: {"ensemble": {"hit_rate": 0.65, "n": 20}})
        monkeypatch.setattr(prediction_history, "list_history",
                            lambda *a, **k: [{
                                "target_date": 1730000000, "ts": 1729900000,
                                "base_close": 100.0, "actual_close": 105.0,
                                "ensemble_hit": 1,
                                "models": {
                                    m: {"direction": "상승", "confidence": 70.0, "hit": 1}
                                    for m in ("rf", "lgbm", "lstm", "transformer", "ensemble")
                                },
                            }])
        html = _render_prediction_history("AAPL")
        assert "<h2>예측 정확도</h2>" in html
        # 요약 카드
        assert "65.0%" in html
        # 탭바
        assert 'id="mtab-rf"' in html
        # 표 안 — details 펼친 안내
        assert "최근 90일 예측 히스토리" in html
        assert "1회" in html or "(1회)" in html
        # 시간순 row
        assert "✅ 적중" in html

    def test_section_order(self, client, monkeypatch):
        """헤더 → summary → tabs → details 순서."""
        from src import prediction_history
        from src.web_app import _render_prediction_history
        monkeypatch.setattr(prediction_history, "hit_rate_by_model",
                            lambda *a, **k: {"ensemble": {"hit_rate": 0.65, "n": 20}})
        monkeypatch.setattr(prediction_history, "list_history",
                            lambda *a, **k: [{
                                "target_date": 1730000000, "ts": 1729900000,
                                "base_close": 100.0, "actual_close": 105.0,
                                "ensemble_hit": 1,
                                "models": {"ensemble": {"direction": "상승", "confidence": 70.0, "hit": 1}},
                            }])
        html = _render_prediction_history("AAPL")
        i_header = html.index("예측 정확도")
        i_summary = html.index("hit-rate-grid")
        i_tabs = html.index("model-tabs")
        i_details = html.index("history-details")
        assert i_header < i_summary < i_tabs < i_details


class TestPredictionHistorySection:
    def test_section_absent_when_no_history(self, client, monkeypatch):
        """예측 row 0건 → '예측 정확도' 헤더 미표시."""
        from src import analysis_cache as ac
        from src import prediction_history
        ac.init_db()
        ac.put("AAPL", "us", "<p>cached</p>", "auto_cron")
        monkeypatch.setattr(prediction_history, "hit_rate_by_model",
                            lambda *a, **k: {})
        monkeypatch.setattr(prediction_history, "list_history",
                            lambda *a, **k: [])
        resp = client.get("/stock/AAPL")
        assert resp.status_code == 200
        assert "예측 정확도".encode() not in resp.data

    def test_section_present_when_history_exists(self, client, monkeypatch):
        from src import analysis_cache as ac
        from src import prediction_history
        ac.init_db()
        ac.put("AAPL", "us", "<p>cached</p>", "auto_cron")
        monkeypatch.setattr(prediction_history, "hit_rate_by_model",
                            lambda *a, **k: {"ensemble": {"hit_rate": 0.7, "n": 10}})
        monkeypatch.setattr(prediction_history, "list_history",
                            lambda *a, **k: [{
                                "target_date": 1730000000, "ts": 1729900000,
                                "base_close": 100.0, "actual_close": 105.0,
                                "ensemble_hit": 1,
                                "models": {
                                    m: {"direction": "상승", "confidence": 70.0, "hit": 1}
                                    for m in ("rf", "lgbm", "lstm", "transformer", "ensemble")
                                },
                            }])
        resp = client.get("/stock/AAPL")
        assert resp.status_code == 200
        assert "예측 정확도".encode() in resp.data
        assert b'id="mtab-rf"' in resp.data           # 탭바
        assert b'class="hit-rate-grid"' in resp.data  # 요약
        assert b'class="history-table"' in resp.data  # 표

    def test_pending_row_renders_grey(self, client, monkeypatch):
        from src import analysis_cache as ac
        from src import prediction_history
        ac.init_db()
        ac.put("AAPL", "us", "<p>cached</p>", "auto_cron")
        monkeypatch.setattr(prediction_history, "hit_rate_by_model",
                            lambda *a, **k: {})
        monkeypatch.setattr(prediction_history, "list_history",
                            lambda *a, **k: [{
                                "target_date": 1730086400, "ts": 1730000000,
                                "base_close": 100.0, "actual_close": None,
                                "ensemble_hit": None,
                                "models": {
                                    m: {"direction": "하락", "confidence": 60.0, "hit": None}
                                    for m in ("rf", "lgbm", "lstm", "transformer", "ensemble")
                                },
                            }])
        resp = client.get("/stock/AAPL")
        assert b'class="row-pending"' in resp.data
        assert "평가 대기".encode() in resp.data

    def test_details_summary_text(self, client, monkeypatch):
        from src import analysis_cache as ac
        from src import prediction_history
        ac.init_db()
        ac.put("AAPL", "us", "<p>cached</p>", "auto_cron")
        monkeypatch.setattr(prediction_history, "hit_rate_by_model",
                            lambda *a, **k: {"rf": {"hit_rate": 0.5, "n": 5}})
        monkeypatch.setattr(prediction_history, "list_history",
                            lambda *a, **k: [{
                                "target_date": 1730000000, "ts": 1729900000,
                                "base_close": 100.0, "actual_close": 105.0,
                                "ensemble_hit": 1,
                                "models": {"ensemble": {"direction": "상승", "confidence": 70.0, "hit": 1}},
                            }])
        resp = client.get("/stock/AAPL")
        assert b'<details' in resp.data
        assert "최근 90일 예측 히스토리".encode() in resp.data

    def test_history_error_does_not_break_page(self, client, monkeypatch):
        """list_history 가 raise 해도 페이지 자체는 200 + 캐시 결과 표시."""
        from src import analysis_cache as ac
        from src import prediction_history
        ac.init_db()
        ac.put("AAPL", "us", "<p>cached body unique</p>", "auto_cron")
        def boom(*a, **k):
            raise RuntimeError("db locked")
        monkeypatch.setattr(prediction_history, "list_history", boom)
        resp = client.get("/stock/AAPL")
        assert resp.status_code == 200
        # 캐시 본문은 정상 표시
        assert b"cached body unique" in resp.data
        # 섹션은 누락
        assert "예측 정확도".encode() not in resp.data


class TestAnalyzeBgSavesSignal:
    def test_run_analysis_bg_passes_signal_to_cache(self, client, monkeypatch):
        import src.web_app as wa
        from src import analysis_cache as ac

        captured = {}

        def fake_analyze_stock(symbol, name, market=None):
            return {
                "name": name, "symbol": symbol,
                "df": None, "prediction": None, "news": [], "sentiment": None,
                "signal": {"signal": "매수", "score": 3, "reasons": []},
            }

        def fake_generate_report(analyses):
            return "<p>fake</p>"

        def fake_put(cache_key, market, result_html, source, *,
                     signal_value=None, signal_score=None,
                     bnf_signal_value=None, bnf_signal_score=None):
            captured["signal_value"] = signal_value
            captured["signal_score"] = signal_score

        monkeypatch.setattr("main.analyze_stock", fake_analyze_stock)
        monkeypatch.setattr("src.report_generator.generate_report", fake_generate_report)
        monkeypatch.setattr(ac, "put", fake_put)

        wa._jobs.clear()
        wa._jobs["jobsig1"] = {
            "status": "running", "symbol": "AAPL", "name": "Apple",
            "result_html": None, "error": None, "started_at": "00:00:00",
        }
        wa._run_analysis_bg("jobsig1", "AAPL", "Apple")

        assert captured["signal_value"] == "매수"
        assert captured["signal_score"] == 3


class TestFullAnalysisSavesSignal:
    def test_run_full_analysis_bg_passes_signal_per_symbol(self, client, monkeypatch):
        """종목별 put 에 signal 전달, ALL put 은 signal 없이."""
        import src.web_app as wa
        from src import analysis_cache as ac

        fake_analyses = [
            {"symbol": "AAPL", "name": "Apple",
             "signal": {"signal": "매수", "score": 3}},
            {"symbol": "005930.KS", "name": "삼성전자",
             "signal": {"signal": "매도", "score": -2}},
        ]
        fake_config = {"stocks": {
            "us":    [{"symbol": "AAPL", "name": "Apple"}],
            "korea": [{"symbol": "005930.KS", "name": "삼성전자"}],
        }}
        monkeypatch.setattr("main.collect_analyses", lambda cfg: fake_analyses)
        monkeypatch.setattr("main.load_config", lambda: fake_config)
        monkeypatch.setattr("src.report_generator.generate_report",
                            lambda items: f"<p>{len(items)}</p>")

        captured = []
        monkeypatch.setattr(ac, "put",
                            lambda *a, **k: captured.append((a, k)))

        wa._jobs.clear()
        wa._jobs["fsig1"] = {
            "status": "running", "symbol": "ALL", "name": "전체 종목",
            "result_html": None, "error": None, "started_at": "00:00:00",
        }
        wa._run_full_analysis_bg("fsig1")

        # 종목별 put — signal kwargs 포함
        symbol_calls = {c[0][0]: c[1] for c in captured if c[0][0] != "ALL"}
        assert symbol_calls["AAPL"]["signal_value"] == "매수"
        assert symbol_calls["AAPL"]["signal_score"] == 3
        assert symbol_calls["005930.KS"]["signal_value"] == "매도"
        assert symbol_calls["005930.KS"]["signal_score"] == -2
        # ALL put — signal 없음
        all_calls = [c[1] for c in captured if c[0][0] == "ALL"]
        assert len(all_calls) == 1
        assert all_calls[0].get("signal_value") is None
        assert all_calls[0].get("signal_score") is None


class TestRenderSignalBadge:
    def test_none_value_returns_empty_string(self, client):
        from src.web_app import _render_signal_badge
        assert _render_signal_badge(None, None) == ""
        assert _render_signal_badge(None, 5) == ""
        assert _render_signal_badge("", 0) == ""

    def test_buy_with_positive_score(self, client):
        from src.web_app import _render_signal_badge
        html = _render_signal_badge("매수", 3)
        assert "signal-badge" in html
        assert "signal-buy" in html
        assert "매수 +3" in html

    def test_sell_with_negative_score(self, client):
        from src.web_app import _render_signal_badge
        html = _render_signal_badge("매도", -2)
        assert "signal-sell" in html
        assert "매도 -2" in html

    def test_hold_with_positive_score(self, client):
        from src.web_app import _render_signal_badge
        html = _render_signal_badge("관망", 1)
        assert "signal-hold" in html
        assert "관망 +1" in html

    def test_score_zero_no_sign(self, client):
        from src.web_app import _render_signal_badge
        html = _render_signal_badge("관망", 0)
        assert "관망 0" in html
        assert "+0" not in html


class TestIndexCardSignal:
    def test_card_shows_signal_badge_when_cache_has_signal(self, client):
        from src import analysis_cache as ac
        ac.init_db()
        ac.put("AAPL", "us", "<p/>", "auto_cron",
               signal_value="매수", signal_score=3)
        resp = client.get("/")
        assert b"signal-badge" in resp.data
        assert "매수 +3".encode() in resp.data

    def test_card_no_signal_when_signal_value_null(self, client):
        from src import analysis_cache as ac
        ac.init_db()
        ac.put("AAPL", "us", "<p/>", "auto_cron")
        resp = client.get("/")
        assert b"signal-badge" not in resp.data

    def test_card_no_signal_when_no_cache_row(self, client):
        from src import analysis_cache as ac
        ac.init_db()
        import sqlite3
        with sqlite3.connect(ac._DB_PATH) as conn:
            conn.execute("DELETE FROM analysis_cache WHERE cache_key = 'AAPL'")
        resp = client.get("/")
        assert b"signal-badge" not in resp.data


class TestWorkerBnfSignal:
    def test_run_analysis_bg_passes_bnf_signal_to_cache(self, client, monkeypatch):
        import src.web_app as wa
        from src import analysis_cache as ac

        captured = {}

        def fake_analyze_stock(symbol, name, market=None):
            return {
                "name": name, "symbol": symbol,
                "df": None, "prediction": None, "news": [], "sentiment": None,
                "signal": {"signal": "매수", "score": 3},
                "bnf_signal": {"signal": "매도", "score": -2},
            }

        monkeypatch.setattr("main.analyze_stock", fake_analyze_stock)
        monkeypatch.setattr("src.report_generator.generate_report", lambda a: "<p/>")

        def fake_put(*a, **k):
            captured.update(k)

        monkeypatch.setattr(ac, "put", fake_put)

        wa._jobs.clear()
        wa._jobs["jobbnf1"] = {
            "status": "running", "symbol": "AAPL", "name": "Apple",
            "result_html": None, "error": None, "started_at": "00:00:00",
        }
        wa._run_analysis_bg("jobbnf1", "AAPL", "Apple")

        assert captured["signal_value"] == "매수"
        assert captured["signal_score"] == 3
        assert captured["bnf_signal_value"] == "매도"
        assert captured["bnf_signal_score"] == -2


class TestRenderSignalBadgeBnfPrefix:
    def test_prefix_bnf(self, client):
        from src.web_app import _render_signal_badge
        html = _render_signal_badge("매수", 3, prefix="BNF ")
        assert "BNF 매수 +3" in html
        assert "signal-buy" in html

    def test_default_prefix_unchanged(self, client):
        from src.web_app import _render_signal_badge
        html = _render_signal_badge("매도", -2)
        assert "매도 -2" in html
        assert "BNF" not in html


class TestIndexCardBnfBadge:
    def test_card_shows_bnf_badge(self, client):
        from src import analysis_cache as ac
        ac.init_db()
        ac.put("AAPL", "us", "<p/>", "auto_cron",
               signal_value="매수", signal_score=3,
               bnf_signal_value="매도", bnf_signal_score=-2)
        resp = client.get("/")
        assert "매수 +3".encode() in resp.data
        assert "BNF 매도 -2".encode() in resp.data

    def test_card_no_bnf_badge_when_null(self, client):
        from src import analysis_cache as ac
        ac.init_db()
        ac.put("AAPL", "us", "<p/>", "auto_cron",
               signal_value="매수", signal_score=3)  # bnf 없이
        resp = client.get("/")
        assert "매수 +3".encode() in resp.data
        assert b"BNF " not in resp.data

    def test_card_no_badges_when_no_cache_row(self, client):
        from src import analysis_cache as ac
        ac.init_db()
        import sqlite3
        with sqlite3.connect(ac._DB_PATH) as conn:
            conn.execute("DELETE FROM analysis_cache WHERE cache_key = 'AAPL'")
        resp = client.get("/")
        assert b"BNF " not in resp.data


class TestApiSignal:
    def test_cache_hit_returns_json(self, client):
        from src import analysis_cache as ac
        ac.init_db()
        ac.put("AAPL", "us", "<p/>", "auto_cron",
               signal_value="매수", signal_score=3,
               bnf_signal_value="관망", bnf_signal_score=1)
        resp = client.get("/api/signal/AAPL")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["symbol"] == "AAPL"
        assert data["market"] == "us"
        assert data["tech"] == {"signal": "매수", "score": 3}
        assert data["bnf"] == {"signal": "관망", "score": 1}
        assert isinstance(data["generated_at_unix"], int)
        assert "KST" in data["generated_at_kst"]
        assert isinstance(data["is_fresh"], bool)

    def test_cache_miss_returns_404(self, client):
        from src import analysis_cache as ac
        ac.init_db()
        import sqlite3
        with sqlite3.connect(ac._DB_PATH) as conn:
            conn.execute("DELETE FROM analysis_cache WHERE cache_key='UNKNOWN'")
        resp = client.get("/api/signal/UNKNOWN")
        assert resp.status_code == 404
        data = resp.get_json()
        assert data["error"] == "no_cache"
        assert data["symbol"] == "UNKNOWN"

    def test_invalid_symbol_returns_400(self, client):
        resp = client.get("/api/signal/<script>")
        assert resp.status_code == 400

    def test_partial_signal_returns_null_fields(self, client):
        from src import analysis_cache as ac
        ac.init_db()
        ac.put("AAPL", "us", "<p/>", "auto_cron",
               signal_value="매수", signal_score=2)  # bnf 없이
        resp = client.get("/api/signal/AAPL")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["tech"] == {"signal": "매수", "score": 2}
        assert data["bnf"] == {"signal": None, "score": None}


class TestApiUniverse:
    """POST /api/universe/<symbol> — auto-trader 등 외부 시스템용 등록 endpoint.

    Spec: ~/Projects/auto-trader/docs/superpowers/specs/2026-05-07-universe-push-design.md
    """

    def test_post_new_symbol_returns_201(self, client, config_file):
        """신규 심볼 → 201 + {added: true}, settings.yaml 에 append."""
        resp = client.post(
            "/api/universe/MSFT",
            json={"name": "Microsoft", "market": "us"},
        )
        assert resp.status_code == 201
        data = resp.get_json()
        assert data == {
            "added": True, "symbol": "MSFT", "name": "Microsoft", "market": "us",
        }
        # yaml 갱신 확인
        cfg = yaml.safe_load(config_file.read_text())
        symbols = [s["symbol"] for s in cfg["stocks"]["us"]]
        assert "MSFT" in symbols

    def test_post_existing_symbol_returns_200(self, client, config_file):
        """이미 있는 AAPL → 200 + {added: false}, yaml 변동 없음."""
        before = yaml.safe_load(config_file.read_text())
        resp = client.post(
            "/api/universe/AAPL",
            json={"name": "Apple", "market": "us"},
        )
        assert resp.status_code == 200
        assert resp.get_json()["added"] is False
        after = yaml.safe_load(config_file.read_text())
        assert before == after

    def test_invalid_symbol_returns_400(self, client):
        resp = client.post(
            "/api/universe/<script>",
            json={"name": "x", "market": "us"},
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "invalid_symbol"

    def test_invalid_market_returns_400(self, client):
        resp = client.post(
            "/api/universe/MSFT",
            json={"name": "Microsoft", "market": "invalid"},
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "invalid_market"

    def test_no_csrf_required(self, client, config_file):
        """JSON POST 가 CSRF 토큰 없이 통과 (기존 /stocks/add 의 form 과 분리)."""
        resp = client.post(
            "/api/universe/NVDA",
            json={"name": "NVIDIA", "market": "us"},
        )
        assert resp.status_code == 201  # CSRF 거부면 400 에러 났을 것
