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
