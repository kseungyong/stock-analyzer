"""src/dart_rules.py 단위 테스트."""
import pytest
from src import dart_rules


def _disclosures(**overrides):
    """모든 key 가 빈 list 인 baseline + 일부 채우기."""
    base = {
        "list": [], "capital_increase": [], "capital_decrease": [],
        "treasury_acquire": [], "treasury_dispose": [], "merger": [],
        "major_holders": [], "exec_holders": [], "free_increase": [],
    }
    base.update(overrides)
    return base


class TestClassifyDisclosures:
    def test_treasury_acquire_is_tier1_critical(self):
        disc = _disclosures(treasury_acquire=[{"rcept_no": "X", "aqpln_amount": "20000000000"}])
        result = dart_rules.classify_disclosures(disc)
        assert result["count"] == 1
        assert result["critical_events"][0]["type"] == "treasury_acquire"
        assert result["critical_events"][0]["tier"] == "high"
        assert result["should_call_llm"] is False  # count == 1

    def test_exec_holders_below_threshold_excluded(self):
        # 임원 1주 매수 (1000주 미만) → 제외
        disc = _disclosures(exec_holders=[
            {"rcept_no": "X", "stkqy": "1", "stkrt": "0.001"}
        ])
        result = dart_rules.classify_disclosures(disc)
        assert result["count"] == 0

    def test_exec_holders_above_threshold_included(self):
        # 임원 5000주 매수 → critical
        disc = _disclosures(exec_holders=[
            {"rcept_no": "X", "stkqy": "5000", "stkrt": "0.05"}
        ])
        result = dart_rules.classify_disclosures(disc)
        assert result["count"] == 1
        assert result["critical_events"][0]["type"] == "exec_holders"
        assert result["critical_events"][0]["tier"] == "medium"

    def test_major_holders_below_threshold_excluded(self):
        # 변동 0.1%p (< 0.5%p) → 제외
        disc = _disclosures(major_holders=[
            {"rcept_no": "X", "stkrt": "5.1", "stkrt_irds": "0.1"}
        ])
        result = dart_rules.classify_disclosures(disc)
        assert result["count"] == 0

    def test_major_holders_above_threshold_included(self):
        # 변동 0.6%p (>= 0.5%p) AND 보유 5.5% (>= 5%) → critical
        disc = _disclosures(major_holders=[
            {"rcept_no": "X", "stkrt": "5.5", "stkrt_irds": "0.6"}
        ])
        result = dart_rules.classify_disclosures(disc)
        assert result["count"] == 1
        assert result["critical_events"][0]["type"] == "major_holders"
        assert result["critical_events"][0]["tier"] == "medium"

    def test_exec_holders_above_value_threshold_included(self):
        # 임원 적은 주식수지만 거래금액 2억원 (>= 1억) → critical
        disc = _disclosures(exec_holders=[
            {"rcept_no": "X", "stkqy": "100", "stkrt": "0.001",
             "trd_amount": "200000000"}
        ])
        result = dart_rules.classify_disclosures(disc)
        assert result["count"] == 1

    def test_should_call_llm_true_when_count_ge_2(self):
        disc = _disclosures(
            treasury_acquire=[{"rcept_no": "A", "aqpln_amount": "1"}],
            capital_increase=[{"rcept_no": "B", "nstk_ostk_qy": "1"}],
        )
        result = dart_rules.classify_disclosures(disc)
        assert result["count"] == 2
        assert result["should_call_llm"] is True


class TestRenderTemplate:
    def test_treasury_acquire_returns_buy_view(self):
        event = {
            "type": "treasury_acquire", "tier": "high",
            "raw": {"rcept_no": "20260520000001", "aqpln_amount": "20000000000"},
        }
        result = dart_rules.render_template(event)
        assert result["sentiment"] == "긍정"
        assert "매수" in result["trading_view"]
        assert "자기주식" in result["summary"]
        assert len(result["key_events"]) >= 1
        assert result["model"] == "rule_based"
        assert "generated_at" in result
