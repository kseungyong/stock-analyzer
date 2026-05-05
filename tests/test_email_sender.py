"""src/email_sender.py 단위 테스트."""
from __future__ import annotations

import smtplib
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

# python-dotenv 미설치 환경 대응
_dotenv_mock = types.ModuleType("dotenv")
_dotenv_mock.load_dotenv = lambda *a, **kw: None
sys.modules.setdefault("dotenv", _dotenv_mock)

from src.email_sender import send_report  # noqa: E402

_BASE_CONFIG = {
    "smtp_server": "smtp.gmail.com",
    "smtp_port": 587,
    "sender": "sender@example.com",
    "password": "secret",
    "recipients": ["recipient@example.com"],
}


class TestSendReport:
    def test_skips_when_no_credentials(self):
        config = {**_BASE_CONFIG, "sender": "", "password": ""}
        with patch("src.email_sender.os.getenv", return_value=""):
            # 예외 없이 종료되어야 함
            send_report("<html/>", config)

    def test_sends_email_successfully(self):
        mock_server = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_server)
        mock_ctx.__exit__ = MagicMock(return_value=False)

        with patch("src.email_sender.smtplib.SMTP", return_value=mock_ctx):
            with patch.dict("os.environ", {}, clear=False):
                send_report("<html>리포트</html>", _BASE_CONFIG)

        mock_server.starttls.assert_called_once()
        mock_server.login.assert_called_once_with(
            _BASE_CONFIG["sender"], _BASE_CONFIG["password"]
        )
        mock_server.sendmail.assert_called_once()

    def test_env_credentials_override_config(self):
        mock_server = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_server)
        mock_ctx.__exit__ = MagicMock(return_value=False)

        env_sender = "env_sender@example.com"
        env_password = "env_password"

        with patch("src.email_sender.smtplib.SMTP", return_value=mock_ctx):
            with patch("src.email_sender.os.getenv", side_effect=lambda k, d="": {
                "EMAIL_SENDER": env_sender,
                "EMAIL_PASSWORD": env_password,
            }.get(k, d)):
                send_report("<html/>", _BASE_CONFIG)

        mock_server.login.assert_called_once_with(env_sender, env_password)

    def test_auth_error_logged(self, caplog):
        import logging
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(side_effect=smtplib.SMTPAuthenticationError(535, b"auth"))
        mock_ctx.__exit__ = MagicMock(return_value=False)
        with patch("src.email_sender.smtplib.SMTP", return_value=mock_ctx):
            with caplog.at_level(logging.ERROR, logger="src.email_sender"):
                send_report("<html/>", _BASE_CONFIG)
        assert any("인증 실패" in r.message for r in caplog.records)

    def test_connect_error_logged(self, caplog):
        import logging
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(side_effect=smtplib.SMTPConnectError(421, b"connect"))
        mock_ctx.__exit__ = MagicMock(return_value=False)
        with patch("src.email_sender.smtplib.SMTP", return_value=mock_ctx):
            with caplog.at_level(logging.ERROR, logger="src.email_sender"):
                send_report("<html/>", _BASE_CONFIG)
        assert any("연결 실패" in r.message for r in caplog.records)

    def test_os_error_logged(self, caplog):
        import logging
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(side_effect=OSError("network"))
        mock_ctx.__exit__ = MagicMock(return_value=False)
        with patch("src.email_sender.smtplib.SMTP", return_value=mock_ctx):
            with caplog.at_level(logging.ERROR, logger="src.email_sender"):
                send_report("<html/>", _BASE_CONFIG)
        assert any("네트워크" in r.message for r in caplog.records)


class TestRenderEmailDigest:
    def test_empty_rows_returns_header_only(self):
        from src.email_sender import render_email_digest
        html = render_email_digest([])
        assert "<h1>" in html
        assert "다이제스트" in html

    def test_single_symbol_row(self, monkeypatch):
        from src.email_sender import render_email_digest
        from src import analysis_cache as ac
        # is_fresh 결정성을 위해 stub
        monkeypatch.setattr(ac, "is_fresh", lambda row, now: True)
        rows = [{
            "cache_key": "AAPL",
            "market": "us",
            "result_html": "<p>aapl body</p>",
            "generated_at": 1715000000,
            "source": "auto_cron",
        }]
        html = render_email_digest(rows)
        assert "AAPL" in html
        assert "aapl body" in html
        assert "🟢" in html

    def test_stale_row_shows_yellow_mark(self, monkeypatch):
        from src.email_sender import render_email_digest
        from src import analysis_cache as ac
        monkeypatch.setattr(ac, "is_fresh", lambda row, now: False)
        rows = [{
            "cache_key": "AAPL", "market": "us", "result_html": "<p/>",
            "generated_at": 1, "source": "auto_cron",
        }]
        html = render_email_digest(rows)
        assert "🟡" in html
