"""src/cleanup.py 단위 테스트."""
import pytest
from src import cleanup


class TestIsEtf:
    def test_kodex_kr(self):
        assert cleanup.is_etf("069500.KS", "KODEX 200") is True

    def test_tiger_kr(self):
        assert cleanup.is_etf("102110.KS", "TIGER 200") is True

    def test_us_spy(self):
        assert cleanup.is_etf("SPY", "SPDR S&P 500") is True

    def test_us_qqq(self):
        assert cleanup.is_etf("QQQ", "Invesco QQQ") is True

    def test_us_koru(self):
        assert cleanup.is_etf("KORU", "Direxion Daily South Korea Bull") is True

    def test_regular_kr_stock(self):
        assert cleanup.is_etf("005930.KS", "삼성전자") is False

    def test_regular_us_stock(self):
        assert cleanup.is_etf("AAPL", "Apple") is False

    def test_arirang_kr(self):
        assert cleanup.is_etf("XXX.KS", "ARIRANG 신흥국MSCI") is True


class TestShouldRemove:
    """5개 조건 AND 판정."""
    def _rows_seven_days(self, value: float):
        """7개 row (값 모두 동일) 만들기."""
        import time
        now = int(time.time())
        return [(now - i * 86400, value) for i in range(7)]

    def test_all_seven_days_below_threshold(self):
        assert cleanup.should_remove(
            symbol="005930.KS", name="삼성전자",
            history_rows=self._rows_seven_days(-6.0),
            is_held=False, is_pinned_or_noted=False,
        ) is True

    def test_one_recovery_day_protects(self):
        rows = self._rows_seven_days(-6.0)
        rows[0] = (rows[0][0], 2.0)  # 최근 1일 +2 회복
        assert cleanup.should_remove(
            symbol="005930.KS", name="삼성전자",
            history_rows=rows,
            is_held=False, is_pinned_or_noted=False,
        ) is False

    def test_insufficient_history_protects(self):
        rows = self._rows_seven_days(-6.0)[:3]  # 3 rows만
        assert cleanup.should_remove(
            symbol="005930.KS", name="삼성전자",
            history_rows=rows,
            is_held=False, is_pinned_or_noted=False,
        ) is False

    def test_etf_protected(self):
        assert cleanup.should_remove(
            symbol="069500.KS", name="KODEX 200",
            history_rows=self._rows_seven_days(-7.0),
            is_held=False, is_pinned_or_noted=False,
        ) is False

    def test_portfolio_protected(self):
        assert cleanup.should_remove(
            symbol="005930.KS", name="삼성전자",
            history_rows=self._rows_seven_days(-6.0),
            is_held=True, is_pinned_or_noted=False,
        ) is False

    def test_pinned_protected(self):
        assert cleanup.should_remove(
            symbol="005930.KS", name="삼성전자",
            history_rows=self._rows_seven_days(-6.0),
            is_held=False, is_pinned_or_noted=True,
        ) is False

    def test_threshold_boundary_minus_5_protects(self):
        """composite == -5 는 threshold 미달 — 보호."""
        assert cleanup.should_remove(
            symbol="005930.KS", name="삼성전자",
            history_rows=self._rows_seven_days(-5.0),
            is_held=False, is_pinned_or_noted=False,
        ) is False


import time
from src import composite_history as ch


@pytest.fixture
def _isolated_history_db(tmp_path, monkeypatch):
    """find_candidates 가 composite_history.recent 를 호출하므로 격리."""
    db = tmp_path / "test_predictions.db"
    monkeypatch.setattr(ch, "_DB_PATH", db)
    ch.init_db()
    yield ch


class TestFindCandidates:
    def _seed_history(self, ch_mod, symbol: str, value: float, days: int = 7):
        now = int(time.time())
        for i in range(days):
            ch_mod.insert(symbol, value, recorded_at=now - i * 86400)

    def test_finds_simple_candidate(self, _isolated_history_db):
        ch_mod = _isolated_history_db
        self._seed_history(ch_mod, "BAD.KS", -7.0, days=7)
        config = {
            "stocks": {"korea": [{"name": "잡주", "symbol": "BAD.KS"}]}
        }
        result = cleanup.find_candidates(config, held_symbols=set())
        assert len(result) == 1
        assert result[0]["symbol"] == "BAD.KS"
        assert result[0]["name"] == "잡주"
        assert result[0]["market"] == "korea"
        assert result[0]["composite_avg"] == pytest.approx(-7.0)
        assert result[0]["days"] == 7

    def test_excludes_etf(self, _isolated_history_db):
        ch_mod = _isolated_history_db
        self._seed_history(ch_mod, "069500.KS", -7.0, days=7)
        config = {
            "stocks": {"korea": [{"name": "KODEX 200", "symbol": "069500.KS"}]}
        }
        assert cleanup.find_candidates(config, held_symbols=set()) == []

    def test_excludes_held(self, _isolated_history_db):
        ch_mod = _isolated_history_db
        self._seed_history(ch_mod, "BAD.KS", -7.0, days=7)
        config = {
            "stocks": {"korea": [{"name": "잡주", "symbol": "BAD.KS"}]}
        }
        assert cleanup.find_candidates(config, held_symbols={"BAD.KS"}) == []

    def test_excludes_pinned(self, _isolated_history_db):
        ch_mod = _isolated_history_db
        self._seed_history(ch_mod, "BAD.KS", -7.0, days=7)
        config = {
            "stocks": {"korea": [
                {"name": "잡주", "symbol": "BAD.KS", "pinned": True}
            ]}
        }
        assert cleanup.find_candidates(config, held_symbols=set()) == []

    def test_excludes_noted(self, _isolated_history_db):
        ch_mod = _isolated_history_db
        self._seed_history(ch_mod, "BAD.KS", -7.0, days=7)
        config = {
            "stocks": {"korea": [
                {"name": "잡주", "symbol": "BAD.KS", "note": "장기 보유 의도"}
            ]}
        }
        assert cleanup.find_candidates(config, held_symbols=set()) == []

    def test_multi_market(self, _isolated_history_db):
        ch_mod = _isolated_history_db
        self._seed_history(ch_mod, "BAD.KS", -7.0, days=7)
        self._seed_history(ch_mod, "TRASH", -8.0, days=7)
        self._seed_history(ch_mod, "GOOD.KS", 5.0, days=7)
        config = {
            "stocks": {
                "korea": [
                    {"name": "잡주", "symbol": "BAD.KS"},
                    {"name": "좋은주", "symbol": "GOOD.KS"},
                ],
                "us": [
                    {"name": "쓰레기", "symbol": "TRASH"},
                ],
            }
        }
        result = cleanup.find_candidates(config, held_symbols=set())
        syms = sorted(c["symbol"] for c in result)
        assert syms == ["BAD.KS", "TRASH"]
