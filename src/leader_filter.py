"""leader_filter: 정량 hard filter (Spec §4.1).

universe.yaml 의 kospi200 + kosdaq150 섹션 합산. ETF 섹션 제외.
3 hard filter (가격 a/b/c) + 2번 (이익) 모두 통과해야 leader 후보.
3번 (PER) 은 점수만 산출, filter 아님.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import asdict, dataclass
from typing import Any

import yaml
import yfinance as yf  # type: ignore

logger = logging.getLogger(__name__)


@dataclass
class LeaderCandidate:
    symbol: str
    name: str
    market: str            # 'KOSPI' | 'KOSDAQ'
    sector: str | None
    industry: str | None
    last_close: float
    market_cap: int | None
    market_cap_quintile: int | None
    near_high_pct: float | None
    return_1y_pct: float | None
    index_return_1y_pct: float | None
    rel_return_pp: float | None
    trailing_eps: float | None
    forward_eps: float | None
    eps_growth_yoy: float | None
    trailing_pe: float | None
    pe_quintile: int | None
    cond1_passed: bool
    cond2_passed: bool
    cond3_score: int | None
    passed: bool

    def as_row(self) -> dict[str, Any]:
        return asdict(self)


def load_universe(path: str) -> list[tuple[str, str]]:
    """universe.yaml 파싱 → [(symbol_with_suffix, market), ...].

    auto-trader yaml 의 6자리 코드를 yfinance suffix 가 붙은 형식으로 변환:
      kospi200 → '.KS', kosdaq150 → '.KQ'. etf 섹션은 제외.
    """
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    out: list[tuple[str, str]] = []
    for code in data.get("kospi200") or []:
        out.append((f"{code}.KS", "KOSPI"))
    for code in data.get("kosdaq150") or []:
        out.append((f"{code}.KQ", "KOSDAQ"))
    return out


def compute_index_return(symbol: str) -> float | None:
    """yfinance Ticker.history(period='1y') 의 첫/마지막 종가 비율 - 1."""
    try:
        hist = yf.Ticker(symbol).history(period="1y", auto_adjust=True)
    except Exception as e:
        logger.warning("index history fetch 실패 %s: %s", symbol, e)
        return None
    if hist.empty or len(hist) < 2:
        return None
    first = float(hist["Close"].iloc[0])
    last = float(hist["Close"].iloc[-1])
    if first <= 0:
        return None
    return (last / first) - 1.0


_NEAR_HIGH_THRESHOLD = 0.85       # 신고가 -15% 이내
_REL_RETURN_THRESHOLD = 0.20      # 시장 대비 +20%p
_TOP_QUINTILE = 1                 # 시총 상위 20%


def _evaluate_single(
    *,
    symbol: str,
    market: str,
    ticker: Any,
    index_return_1y: float,
    market_cap_quintile: int | None,
    pe_quintile: int | None,
) -> LeaderCandidate | None:
    """단일 종목의 cond1/cond2/cond3 계산.

    Returns None 만약 데이터 부족으로 평가 불가 (price history empty).
    실패한 조건은 cond*_passed=False 로 반환, row 자체는 보존.
    """
    info = getattr(ticker, "info", {}) or {}
    try:
        hist = ticker.history(period="1y", auto_adjust=True)
    except Exception as e:
        logger.warning("history fetch 실패 %s: %s", symbol, e)
        return None
    if hist.empty or len(hist) < 2:
        logger.warning("history 데이터 부족 %s", symbol)
        return None

    closes = hist["Close"].astype(float)
    highs = hist["High"].astype(float)
    last_close = float(closes.iloc[-1])
    first_close = float(closes.iloc[0])
    high_52w = float(highs.max())

    near_high = (last_close / high_52w) if high_52w > 0 else None
    return_1y = (last_close / first_close - 1.0) if first_close > 0 else None
    rel_return = (
        (return_1y - index_return_1y) if (return_1y is not None) else None
    )

    market_cap = info.get("marketCap")
    trailing_eps = info.get("trailingEps")
    forward_eps = info.get("forwardEps")
    eps_growth = info.get("earningsGrowth")
    trailing_pe = info.get("trailingPE")

    cond1a = near_high is not None and near_high >= _NEAR_HIGH_THRESHOLD
    cond1b = rel_return is not None and rel_return >= _REL_RETURN_THRESHOLD
    cond1c = market_cap_quintile == _TOP_QUINTILE
    cond1 = bool(cond1a and cond1b and cond1c)

    eps_t = float(trailing_eps) if trailing_eps is not None else None
    eps_f = float(forward_eps) if forward_eps is not None else None
    cond2 = (
        (eps_t is not None and eps_t > 0)
        or (eps_t is not None and eps_f is not None and eps_f > eps_t)
    )

    cond3_score = pe_quintile

    return LeaderCandidate(
        symbol=symbol,
        name=str(info.get("longName") or info.get("shortName") or symbol),
        market=market,
        sector=info.get("sector"),
        industry=info.get("industry"),
        last_close=last_close,
        market_cap=int(market_cap) if market_cap is not None else None,
        market_cap_quintile=market_cap_quintile,
        near_high_pct=near_high,
        return_1y_pct=return_1y,
        index_return_1y_pct=index_return_1y,
        rel_return_pp=rel_return,
        trailing_eps=eps_t,
        forward_eps=eps_f,
        eps_growth_yoy=float(eps_growth) if eps_growth is not None else None,
        trailing_pe=float(trailing_pe) if trailing_pe is not None else None,
        pe_quintile=pe_quintile,
        cond1_passed=cond1,
        cond2_passed=cond2,
        cond3_score=cond3_score,
        passed=bool(cond1 and cond2),
    )


def _quintile(values: list[float | None], descending: bool = True) -> dict[int, int]:
    """idx → 1..5 분위 (1 = 상위 20%, 5 = 하위 20%).

    None 은 5 (최하) 로 처리.
    """
    indexed = [(i, v) for i, v in enumerate(values)]
    indexed.sort(key=lambda x: (x[1] is None, -(x[1] or 0.0) if descending else (x[1] or 0.0)))
    n = len(indexed)
    result: dict[int, int] = {}
    for rank, (i, _) in enumerate(indexed):
        q = min(5, (rank * 5) // max(n, 1) + 1)
        result[i] = q
    return result


def run_filter(universe: list[tuple[str, str]]) -> list[LeaderCandidate]:
    """전체 흐름: 시장 지수 fetch → 종목별 info+price fetch → 분위 → cond 평가.

    Returns 모든 종목 (passed True/False 모두). 호출자가 passed 로 필터링.
    """
    kospi_r = compute_index_return("^KS11")
    kosdaq_r = compute_index_return("^KQ11")
    if kospi_r is None or kosdaq_r is None:
        raise RuntimeError(
            f"시장 지수 fetch 실패 (KOSPI={kospi_r}, KOSDAQ={kosdaq_r}) — run 중단"
        )

    tickers: list[Any] = []
    market_caps: list[float | None] = []
    pes: list[float | None] = []
    skipped: list[str] = []
    for sym, market in universe:
        try:
            t = yf.Ticker(sym)
            _ = t.info
            tickers.append(t)
            market_caps.append(t.info.get("marketCap"))
            pes.append(t.info.get("trailingPE"))
        except Exception as e:
            logger.warning("ticker fetch 실패 %s: %s", sym, e)
            tickers.append(None)
            market_caps.append(None)
            pes.append(None)
            skipped.append(sym)

    mc_q = _quintile([float(v) if v is not None else None for v in market_caps], descending=True)
    pe_q = _quintile(
        [float(v) if v is not None else None for v in pes], descending=False
    )

    out: list[LeaderCandidate] = []
    history_skipped = 0
    for i, (sym, market) in enumerate(universe):
        if tickers[i] is None:
            continue
        idx_r = kospi_r if market == "KOSPI" else kosdaq_r
        c = _evaluate_single(
            symbol=sym, market=market, ticker=tickers[i],
            index_return_1y=idx_r,
            market_cap_quintile=mc_q.get(i),
            pe_quintile=pe_q.get(i),
        )
        if c is None:
            history_skipped += 1
            continue
        out.append(c)

    total_skipped = len(skipped) + history_skipped
    skip_pct = total_skipped / max(len(universe), 1)
    logger.info(
        "leader_filter: universe=%d, evaluated=%d, passed=%d, "
        "skipped=%d (info=%d + history=%d, %.0f%%)",
        len(universe), len(out), sum(1 for c in out if c.passed),
        total_skipped, len(skipped), history_skipped, skip_pct * 100,
    )
    if skip_pct > 0.10:
        raise RuntimeError(f"skip 률 {skip_pct:.0%} > 10% 임계 초과")
    return out
