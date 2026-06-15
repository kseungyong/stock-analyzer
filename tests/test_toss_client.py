import pytest
import src.toss_client as tc


def test_load_credentials_from_env(monkeypatch):
    monkeypatch.setenv("TOSS_CLIENT_ID", "cid")
    monkeypatch.setenv("TOSS_CLIENT_SECRET", "csec")
    assert tc._load_credentials() == ("cid", "csec")


def test_load_credentials_missing_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("TOSS_CLIENT_ID", raising=False)
    monkeypatch.delenv("TOSS_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(tc, "_ENV_PATHS", [tmp_path / "nonexistent.env"])
    with pytest.raises(RuntimeError, match="TOSS_CLIENT"):
        tc._load_credentials()


def test_unwrap_result_ok():
    assert tc._unwrap({"result": [1, 2]}) == [1, 2]


def test_unwrap_result_error_raises():
    with pytest.raises(RuntimeError, match="account-not-found"):
        tc._unwrap({"result": {"error": {"code": "account-not-found", "message": "x"}}})


def test_fetch_candles_paginates(monkeypatch):
    calls = []
    pages = [
        {"candles": [{"t": i} for i in range(200)], "nextBefore": "CURSOR1"},
        {"candles": [{"t": i} for i in range(200, 250)], "nextBefore": None},
    ]
    def fake_get(self, path, params=None, extra_headers=None):
        calls.append(params)
        return pages[len(calls) - 1]
    monkeypatch.setattr(tc.TossClient, "_get", fake_get)
    monkeypatch.setattr(tc.TossClient, "__init__", lambda self: None)  # 자격증명 우회

    client = tc.TossClient()
    result = client.fetch_candles("005930", interval="1d", count=240)
    assert len(result) == 240            # 200 + 50 중 240 개로 트림
    assert "before" not in calls[0]  # 1페이지 커서 없음
    assert calls[1]["before"] == "CURSOR1"  # 2페이지 커서 전달


def test_fetch_candles_stops_on_null_cursor(monkeypatch):
    def fake_get(self, path, params=None, extra_headers=None):
        return {"candles": [{"t": 1}], "nextBefore": None}
    monkeypatch.setattr(tc.TossClient, "_get", fake_get)
    monkeypatch.setattr(tc.TossClient, "__init__", lambda self: None)
    client = tc.TossClient()
    result = client.fetch_candles("005930", count=200)
    assert len(result) == 1   # nextBefore=null → 1페이지서 종료


def test_fetch_candles_stops_at_page_guard(monkeypatch):
    calls = {"n": 0}
    def fake_get(self, path, params=None, extra_headers=None):
        calls["n"] += 1
        return {"candles": [{"t": calls["n"]}], "nextBefore": f"CURSOR{calls['n']}"}  # 항상 새 non-null 커서
    monkeypatch.setattr(tc.TossClient, "_get", fake_get)
    monkeypatch.setattr(tc.TossClient, "__init__", lambda self: None)
    client = tc.TossClient()
    result = client.fetch_candles("005930", count=10000)
    assert calls["n"] == 10        # 10페이지 가드에서 멈춤 (무한루프 방지)
