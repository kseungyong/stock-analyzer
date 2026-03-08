"""src/validators.py 단위 테스트."""
import pytest
from src.validators import validate_stock_symbol, sanitize_stock_symbol, validate_stock_name


class TestValidateStockSymbol:
    def test_us_symbol(self):
        assert validate_stock_symbol("AAPL") is True

    def test_korean_symbol(self):
        assert validate_stock_symbol("005930.KS") is True

    def test_hyphen_symbol(self):
        assert validate_stock_symbol("BRK-A") is True

    def test_empty_string(self):
        assert validate_stock_symbol("") is False

    def test_none(self):
        assert validate_stock_symbol(None) is False

    def test_too_long(self):
        assert validate_stock_symbol("A" * 21) is False

    def test_special_chars(self):
        assert validate_stock_symbol("AAPL; DROP TABLE") is False

    def test_xss(self):
        assert validate_stock_symbol("<script>") is False

    def test_path_traversal(self):
        assert validate_stock_symbol("../../etc") is False


class TestValidateStockName:
    def test_valid_name(self):
        assert validate_stock_name("Apple") is True

    def test_korean_name(self):
        assert validate_stock_name("삼성전자") is True

    def test_empty_string(self):
        assert validate_stock_name("") is False

    def test_none(self):
        assert validate_stock_name(None) is False

    def test_whitespace_only(self):
        assert validate_stock_name("   ") is False

    def test_max_length(self):
        assert validate_stock_name("A" * 50) is True

    def test_over_max_length(self):
        assert validate_stock_name("A" * 51) is False


class TestSanitizeStockSymbol:
    def test_lowercase_to_upper(self):
        assert sanitize_stock_symbol("aapl") == "AAPL"

    def test_strip_whitespace(self):
        assert sanitize_stock_symbol("  AAPL  ") == "AAPL"

    def test_mixed(self):
        assert sanitize_stock_symbol(" msft ") == "MSFT"
