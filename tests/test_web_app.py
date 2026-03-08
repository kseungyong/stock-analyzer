"""src/web_app.py Flask 라우트 테스트."""
import pytest
import yaml
import tempfile
from pathlib import Path

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


@pytest.fixture
def client(config_file, monkeypatch):
    """Flask 테스트 클라이언트 — CONFIG_PATH를 임시 파일로 교체."""
    import src.web_app as wa
    monkeypatch.setattr(wa, "CONFIG_PATH", config_file)
    wa.app.config["TESTING"] = True
    wa._jobs.clear()
    with wa.app.test_client() as c:
        yield c


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
        resp = client.post("/stocks/add", data={"symbol": "TSLA", "name": "Tesla", "market": "us"})
        assert resp.status_code == 303
        config = yaml.safe_load(config_file.read_text())
        symbols = [s["symbol"] for s in config["stocks"]["us"]]
        assert "TSLA" in symbols

    def test_add_duplicate_redirects_with_error(self, client):
        resp = client.post("/stocks/add", data={"symbol": "AAPL", "name": "Apple", "market": "us"})
        assert resp.status_code == 303
        assert b"error=" in resp.headers["Location"].encode()

    def test_add_invalid_symbol_redirects_with_error(self, client):
        resp = client.post("/stocks/add", data={"symbol": "<script>", "name": "hack", "market": "us"})
        assert resp.status_code == 303
        assert b"error=" in resp.headers["Location"].encode()

    def test_add_empty_symbol_redirects_with_error(self, client):
        resp = client.post("/stocks/add", data={"symbol": "", "name": "NoName", "market": "us"})
        assert resp.status_code == 303

    def test_korea_symbol_appends_ks(self, client, config_file):
        client.post("/stocks/add", data={"symbol": "005930", "name": "삼성전자", "market": "korea"})
        config = yaml.safe_load(config_file.read_text())
        symbols = [s["symbol"] for s in config["stocks"].get("korea", [])]
        assert "005930.KS" in symbols


class TestStocksDelete:
    def test_delete_existing(self, client, config_file):
        resp = client.post("/stocks/delete", data={"symbol": "AAPL"})
        assert resp.status_code == 303
        config = yaml.safe_load(config_file.read_text())
        symbols = [s["symbol"] for s in config["stocks"]["us"]]
        assert "AAPL" not in symbols


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
