"""src/stock_search.py 단위 테스트."""
from src.stock_search import search_stocks


class TestShortQuery:
    def test_empty_returns_empty_list(self):
        assert search_stocks("") == []

    def test_one_char_returns_empty_list(self):
        assert search_stocks("a") == []

    def test_whitespace_only_returns_empty_list(self):
        assert search_stocks("   ") == []
