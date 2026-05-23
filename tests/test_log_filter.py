"""src/log_filter.py 단위 테스트."""
import logging
import pytest
from src.log_filter import SecretFilter, install_secret_filter


class TestSecretFilter:
    def test_redacts_api_key_in_message(self):
        f = SecretFilter(["super-secret-key-12345"])
        record = logging.LogRecord(
            name="test", level=logging.WARNING, pathname="", lineno=0,
            msg="fetch failed: https://api.example.com/?key=super-secret-key-12345&q=1",
            args=(), exc_info=None,
        )
        f.filter(record)
        assert "super-secret-key-12345" not in record.getMessage()
        assert "***" in record.getMessage()

    def test_passthrough_when_no_secret(self):
        f = SecretFilter(["api-key-abc"])
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="normal log without any secret",
            args=(), exc_info=None,
        )
        f.filter(record)
        assert record.getMessage() == "normal log without any secret"


class TestInstallSecretFilter:
    def test_masks_secret_from_child_logger(self):
        """install_secret_filter 후 src.* child logger 메시지도 마스킹."""
        import io
        root = logging.getLogger()
        original_handlers = list(root.handlers)
        original_filters = list(root.filters)
        original_level = root.level
        buffer = io.StringIO()
        handler = logging.StreamHandler(buffer)
        handler.setLevel(logging.WARNING)
        root.handlers = [handler]
        root.setLevel(logging.WARNING)
        try:
            install_secret_filter(["my-secret-token-xyz"])
            child = logging.getLogger("src.test.child")
            child.setLevel(logging.WARNING)
            child.warning("calling api: token=my-secret-token-xyz extra")
            handler.flush()
            output = buffer.getvalue()
            assert "my-secret-token-xyz" not in output
            assert "***" in output
        finally:
            root.handlers = original_handlers
            root.filters = original_filters
            root.setLevel(original_level)
            # cleanup child logger
            logging.getLogger("src.test.child").setLevel(logging.NOTSET)
