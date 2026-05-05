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

        def fake_analyze_stock(symbol, name):
            return {"name": name, "symbol": symbol, "df": None,
                    "signal": None, "prediction": None, "news": [], "sentiment": None}

        def fake_generate_report(analyses):
            return "<p>fake report</p>"

        def fake_put(cache_key, market, result_html, source):
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
        monkeypatch.setattr("main.analyze_stock", lambda s, n: None)

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
