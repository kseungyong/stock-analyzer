"""src/foreign_ranking.py 단위 테스트."""
from __future__ import annotations

from datetime import date as date_cls, timedelta
from pathlib import Path

import pytest
import yaml

from src import foreign_ranking as fr


@pytest.fixture
def _tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(fr, "_DB_PATH", db_path)
    yield db_path


@pytest.fixture
def _tmp_settings(tmp_path, monkeypatch):
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(yaml.dump({
        "stocks": {
            "korea": [
                {"symbol": "005930.KS", "name": "삼성전자"},
                {"symbol": "000660.KS", "name": "SK하이닉스"},
            ],
            "us": [{"symbol": "AAPL", "name": "Apple"}],
        },
    }, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(fr, "_SETTINGS_PATH", settings_path)
    yield settings_path


@pytest.fixture
def _tmp_overlay(tmp_path, monkeypatch):
    from src import universe
    overlay_path = tmp_path / "foreign_ranking.yaml"
    monkeypatch.setattr(universe, "OVERLAY_PATH", overlay_path)
    yield overlay_path


class TestRankingRow:
    def test_parse_kis_response(self):
        raw = {
            "mksc_shrn_iscd": "034220", "hts_kor_isnm": "LG디스플레이",
            "frgn_ntby_qty": "3352000", "frgn_ntby_tr_pbmn": "55945",
            "orgn_ntby_qty": "-14000", "orgn_ntby_tr_pbmn": "-234",
            "fund_ntby_qty": "111000", "fund_ntby_tr_pbmn": "1853",
        }
        r = fr.RankingRow.from_kis(raw)
        assert r.symbol == "034220"
        assert r.name == "LG디스플레이"
        assert r.frgn_qty == 3352000 and r.frgn_val == 55945
        assert r.orgn_qty == -14000 and r.orgn_val == -234
        assert r.fund_qty == 111000 and r.fund_val == 1853

    def test_handles_empty_and_invalid_values(self):
        raw = {"mksc_shrn_iscd": "X", "hts_kor_isnm": "Y",
               "frgn_ntby_qty": "", "frgn_ntby_tr_pbmn": None,
               "orgn_ntby_qty": "not_a_number", "orgn_ntby_tr_pbmn": "1,234",
               "fund_ntby_qty": "1.5", "fund_ntby_tr_pbmn": "0"}
        r = fr.RankingRow.from_kis(raw)
        assert r.frgn_qty == 0 and r.frgn_val == 0
        assert r.orgn_qty == 0 and r.orgn_val == 1234
        assert r.fund_qty == 1


class TestDB:
    def test_save_and_query_single_day(self, _tmp_db):
        snap = date_cls(2026, 6, 1)
        fr.save_snapshot(snap, [
            fr.RankingRow("A", "AlphaCo", frgn_qty=100, frgn_val=500,
                          orgn_qty=50, orgn_val=200, fund_qty=10, fund_val=40),
            fr.RankingRow("B", "BetaCo", frgn_qty=200, frgn_val=300,
                          orgn_qty=150, orgn_val=400, fund_qty=80, fund_val=300),
        ])
        top = fr.top_n_by_investor(snap, "foreign", period_days=1, n=5)
        # 외인 거래대금 desc: A(500) > B(300)
        assert [r["symbol"] for r in top] == ["A", "B"]
        assert top[0]["val"] == 500

        top_inst = fr.top_n_by_investor(snap, "institution", period_days=1, n=5)
        assert [r["symbol"] for r in top_inst] == ["B", "A"]  # B 400 > A 200

    def test_5day_aggregation(self, _tmp_db):
        snap = date_cls(2026, 6, 1)
        # 5일 데이터: A 일별 100 × 5 = 500 누적, B 일별 200 × 1 (마지막날만) = 200
        for i in range(5):
            d = snap - timedelta(days=4 - i)
            rows = [fr.RankingRow("A", "AlphaCo", 0, 100, 0, 0, 0, 0)]
            if i == 4:  # 마지막 날에만 B 등장
                rows.append(fr.RankingRow("B", "BetaCo", 0, 1000, 0, 0, 0, 0))
            fr.save_snapshot(d, rows)

        top = fr.top_n_by_investor(snap, "foreign", period_days=5, n=5)
        # 5일 누적: B(1000) > A(500)
        assert top[0]["symbol"] == "B" and top[0]["val"] == 1000
        assert top[1]["symbol"] == "A" and top[1]["val"] == 500

    def test_negative_val_filtered_out(self, _tmp_db):
        snap = date_cls(2026, 6, 1)
        fr.save_snapshot(snap, [
            fr.RankingRow("A", "AlphaCo", 0, 500, 0, 0, 0, 0),
            fr.RankingRow("C", "GammaCo", 0, -100, 0, 0, 0, 0),  # 순매도 — top buy 제외
        ])
        top = fr.top_n_by_investor(snap, "foreign", period_days=1, n=5)
        assert [r["symbol"] for r in top] == ["A"]

    def test_sell_direction_ranks_most_negative_first(self, _tmp_db):
        snap = date_cls(2026, 6, 1)
        fr.save_snapshot(snap, [
            fr.RankingRow("A", "AlphaCo", 0, 500, 0, 0, 0, 0),    # 순매수 — sell 제외
            fr.RankingRow("C", "GammaCo", 0, -100, 0, 0, 0, 0),   # 순매도 -100
            fr.RankingRow("D", "DeltaCo", 0, -300, 0, 0, 0, 0),   # 순매도 -300 (더 많이 팜)
        ])
        sell = fr.top_n_by_investor(
            snap, "foreign", period_days=1, n=5, direction="sell")
        # 가장 많이 판 종목 먼저: D(-300) > C(-100), 순매수 A 는 제외
        assert [r["symbol"] for r in sell] == ["D", "C"]
        assert sell[0]["val"] == -300
        # buy 방향은 여전히 A 만
        buy = fr.top_n_by_investor(
            snap, "foreign", period_days=1, n=5, direction="buy")
        assert [r["symbol"] for r in buy] == ["A"]

    def test_invalid_direction_raises(self, _tmp_db):
        with pytest.raises(ValueError):
            fr.top_n_by_investor(date_cls(2026, 6, 1), "foreign", direction="hold")


class TestFetchToday:
    def test_merges_buy_and_sell_snapshots(self):
        buy = [{
            "mksc_shrn_iscd": "005930", "hts_kor_isnm": "삼성전자",
            "frgn_ntby_qty": "100", "frgn_ntby_tr_pbmn": "500",
            "orgn_ntby_qty": "0", "orgn_ntby_tr_pbmn": "0",
            "fund_ntby_qty": "0", "fund_ntby_tr_pbmn": "0",
        }]
        sell = [{
            "mksc_shrn_iscd": "034220", "hts_kor_isnm": "LG디스플레이",
            "frgn_ntby_qty": "-200", "frgn_ntby_tr_pbmn": "-800",
            "orgn_ntby_qty": "0", "orgn_ntby_tr_pbmn": "0",
            "fund_ntby_qty": "0", "fund_ntby_tr_pbmn": "0",
        }]

        class _FakeClient:
            def __init__(self):
                self.calls = []

            def fetch_foreign_institution_total(self, *, sort_code="0"):
                self.calls.append(sort_code)
                return buy if sort_code == "0" else sell

        client = _FakeClient()
        rows = fr.fetch_today(client=client)
        # 순매수(0) + 순매도(1) 두 번 호출
        assert client.calls == ["0", "1"]
        by_symbol = {r.symbol: r for r in rows}
        assert set(by_symbol) == {"005930", "034220"}
        assert by_symbol["005930"].frgn_val == 500
        assert by_symbol["034220"].frgn_val == -800

    def test_dedupes_symbol_present_in_both(self):
        row = {
            "mksc_shrn_iscd": "005930", "hts_kor_isnm": "삼성전자",
            "frgn_ntby_qty": "100", "frgn_ntby_tr_pbmn": "500",
            "orgn_ntby_qty": "-5", "orgn_ntby_tr_pbmn": "-30",
            "fund_ntby_qty": "0", "fund_ntby_tr_pbmn": "0",
        }

        class _FakeClient:
            def fetch_foreign_institution_total(self, *, sort_code="0"):
                return [dict(row)]  # 양쪽 응답에 동일 종목

        rows = fr.fetch_today(client=_FakeClient())
        assert len(rows) == 1
        assert rows[0].symbol == "005930" and rows[0].frgn_val == 500


class TestUniverseFollow:
    def test_push_writes_overlay_and_skips_user_entries(
        self, _tmp_db, _tmp_settings, _tmp_overlay,
    ):
        from src import universe
        snap = date_cls(2026, 6, 1)
        # 가짜 ranking — 005930 (사용자가 이미 등록), 034220 (신규)
        fr.save_snapshot(snap, [
            fr.RankingRow("005930", "삼성전자", 0, 1000, 0, 0, 0, 0),
            fr.RankingRow("034220", "LG디스플레이", 0, 500, 0, 0, 0, 0),
        ])
        union = fr.compute_union_top(snap, n=10)
        removed, added = fr.push_to_overlay(union)

        # overlay 파일에는 034220 만 (사용자 등록 005930 은 제외)
        overlay = universe.load_overlay()
        overlay_symbols = {s["symbol"]: s for s in overlay["korea"]}
        assert "034220.KS" in overlay_symbols
        assert overlay_symbols["034220.KS"]["source"] == fr._SOURCE_TAG
        assert "005930.KS" not in overlay_symbols
        assert added == 1

        # settings.yaml 은 전혀 수정되지 않음 (사용자 종목 그대로)
        cfg = yaml.safe_load(_tmp_settings.read_text(encoding="utf-8"))
        user_symbols = {s["symbol"] for s in cfg["stocks"]["korea"]}
        assert user_symbols == {"005930.KS", "000660.KS"}

        # apply_overlay 머지 결과 — 사용자 + overlay 합집합
        merged = universe.apply_overlay(cfg)
        merged_symbols = {s["symbol"] for s in merged["stocks"]["korea"]}
        assert merged_symbols == {"005930.KS", "000660.KS", "034220.KS"}

    def test_push_replaces_old_overlay_entries(
        self, _tmp_db, _tmp_settings, _tmp_overlay,
    ):
        from src import universe
        # overlay 에 이전 ranking 종목 미리 박아두기
        universe.write_overlay({"korea": [
            {"symbol": "999999.KS", "name": "OldRanking", "source": fr._SOURCE_TAG},
        ]})

        # 새 ranking — 999999 는 없음, 034220 만
        snap = date_cls(2026, 6, 1)
        fr.save_snapshot(snap, [
            fr.RankingRow("034220", "LG디스플레이", 0, 500, 0, 0, 0, 0),
        ])
        removed, added = fr.push_to_overlay(fr.compute_union_top(snap, n=10))

        overlay_symbols = [s["symbol"] for s in universe.load_overlay()["korea"]]
        # 999999 제거됨 (전체 교체), 034220 추가됨
        assert "999999.KS" not in overlay_symbols
        assert "034220.KS" in overlay_symbols
        assert removed == 1 and added == 1

        # settings.yaml 사용자 등록 보존
        cfg = yaml.safe_load(_tmp_settings.read_text(encoding="utf-8"))
        user_symbols = {s["symbol"] for s in cfg["stocks"]["korea"]}
        assert user_symbols == {"005930.KS", "000660.KS"}

    def test_compute_union_dedupe_across_investors(self, _tmp_db):
        snap = date_cls(2026, 6, 1)
        # 한 종목이 외인/기관/연기금 모든 ranking 에 들어가도 union 에서 1번만
        fr.save_snapshot(snap, [
            fr.RankingRow("A", "AlphaCo",
                          frgn_qty=1, frgn_val=100,
                          orgn_qty=1, orgn_val=200,
                          fund_qty=1, fund_val=50),
        ])
        union = fr.compute_union_top(snap, n=10)
        assert len(union) == 1 and union[0] == ("A", "AlphaCo")
