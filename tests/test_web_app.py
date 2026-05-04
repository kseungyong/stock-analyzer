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
