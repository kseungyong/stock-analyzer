#!/usr/bin/env python3
"""주식시장 분석 시스템 - CLI 진입점"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml

# .env 파일 자동 로드
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import pandas as pd
from src.data_fetcher import fetch_stock_data, fetch_multiple, fetch_news
from src.technical_analysis import compute_indicators, generate_signal, generate_bnf_signal, fetch_market_df
from src.prediction_engine import PredictionEngine
from src.report_generator import generate_report
from src.email_sender import send_report
from src.scheduler import start_scheduler
from src.validators import validate_stock_symbol, sanitize_stock_symbol
from src import prediction_history, analysis_cache

_engine = PredictionEngine()


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / "config" / "settings.yaml"


def _next_business_day_unix(last_index) -> int:
    """df 마지막 인덱스 → 다음 영업일 KST 자정 → UTC unix epoch."""
    ts = pd.Timestamp(last_index)
    if ts.tz is None:
        ts = ts.tz_localize("Asia/Seoul", nonexistent="shift_forward", ambiguous="raise")
    else:
        ts = ts.tz_convert("Asia/Seoul")
    next_bday = (ts + pd.tseries.offsets.BDay(1)).normalize()
    return int(next_bday.tz_convert("UTC").timestamp())


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


# 모듈 로드 시점에 1회 — DB 파일/스키마 보장
prediction_history.init_db()
analysis_cache.init_db()
from src import portfolio as _portfolio_init
_portfolio_init.init_db()


def get_all_stocks(config: dict) -> list[dict]:
    stocks = []
    for group in config.get("stocks", {}).values():
        stocks.extend(group)
    return stocks


def analyze_stock(symbol: str, name: str, market: str | None = None) -> dict | None:
    """단일 종목 분석을 수행한다.

    Args:
        symbol: 주식 심볼 (예: AAPL, 005930.KS)
        name: 종목명
        market: 'korea' 또는 'us'. None 이면 BNF 시그널은 시장 통합 없이 종목 단독.

    Returns:
        분석 결과 딕셔너리 또는 실패 시 None
    """
    # 입력 검증
    symbol = sanitize_stock_symbol(symbol)
    if not validate_stock_symbol(symbol):
        logger.error("유효하지 않은 심볼: %s", symbol)
        return None

    try:
        df = fetch_stock_data(symbol)
        df = compute_indicators(df)

        # 인라인 백필 (즉시성 보조 — cron이 메인 메커니즘)
        try:
            prediction_history.backfill_inline(symbol, df)
        except Exception as e:
            logger.warning("backfill_inline 실패 (분석은 계속): %s", e)

        signal = generate_signal(df)

        # BNF 시그널 — 실패해도 분석 본체에 영향 없음
        bnf_signal = None
        try:
            market_df = fetch_market_df(market) if market else None
            bnf_signal = generate_bnf_signal(df, market_df=market_df)
        except Exception as e:
            logger.warning("generate_bnf_signal 실패 (분석은 계속): %s", e)

        from src.ml_predictor import analyze_sentiment
        # ML 예측과 뉴스 수집을 동시에 실행
        with ThreadPoolExecutor(max_workers=2) as ex:
            fut_pred = ex.submit(_engine.run, df, symbol)
            fut_news = ex.submit(fetch_news, symbol)
            prediction = fut_pred.result()
            news = fut_news.result()

        # live 예측 저장 (DB 장애 → 분석 결과는 정상 반환)
        try:
            last_close = float(df["Close"].iloc[-1])
            target_date = _next_business_day_unix(df.index[-1])
            prediction_history.insert_live(symbol, prediction, last_close, target_date)
        except Exception as e:
            logger.warning("insert_live 실패 (분석 결과는 정상 반환): %s", e)

        sentiment = analyze_sentiment(news)

        # Pattern indicators (Phase A: 이동평균 4상태) — 실패해도 분석 본체 무관
        patterns = None
        try:
            from src.pattern_indicators import detect_all_patterns
            # df 컬럼명 lowercase 변환 — fetch_stock_data 는 'Close', detect 는 'close'
            df_lower = df.rename(columns={c: c.lower() for c in df.columns})
            patterns = detect_all_patterns(df_lower, market or "korea")
        except Exception as e:
            logger.warning("pattern detection 실패 (분석은 계속): %s", e)

        return {
            "name": name,
            "symbol": symbol,
            "df": df,
            "last_close": float(df["Close"].iloc[-1]),
            "signal": signal,
            "bnf_signal": bnf_signal,
            "prediction": prediction,
            "news": news,
            "sentiment": sentiment,
            "patterns": patterns,
        }
    except Exception as e:
        logger.error("분석 실패 — %s (%s): %s", name, symbol, e)
        return None


def collect_analyses(config: dict) -> list[dict]:
    """전체 종목 분석을 병렬 실행하고 성공한 결과 list 를 반환한다.

    실패한 종목은 결과에서 제외된다 (logger.warning 으로 표시됨).
    """
    # market 별로 종목 모음 → (symbol, name, market) 튜플
    stocks_with_market: list[tuple] = []
    for market, group in config.get("stocks", {}).items():
        for s in group:
            stocks_with_market.append((s["symbol"], s["name"], market))
    logger.info("분석 시작: %d개 종목", len(stocks_with_market))

    analyses: list[dict] = []
    max_workers = min(len(stocks_with_market), 3)
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        future_to_stock = {
            ex.submit(analyze_stock, sym, nm, mk): (sym, nm)
            for (sym, nm, mk) in stocks_with_market
        }
        for future in as_completed(future_to_stock):
            sym, nm = future_to_stock[future]
            logger.info("분석 중: %s (%s)", nm, sym)
            result = future.result()
            if result:
                analyses.append(result)
    return analyses


def run_full_analysis(config: dict) -> str | None:
    """전체 종목 분석 + 단일 다이제스트 HTML 생성."""
    analyses = collect_analyses(config)
    if not analyses:
        logger.warning("분석 결과 없음")
        return None
    html = generate_report(analyses)
    logger.info("리포트 생성 완료: %d개 종목", len(analyses))
    return html


def auto_analyze_market(market: str) -> None:
    """시장의 모든 종목을 차례로 분석하고 analysis_cache 에 UPSERT.

    cron (KST 16:00 한국 / KST 06:00 미국) 에서 호출된다.
    """
    from src import report_generator as _rg

    config = load_config()
    stocks = config.get("stocks", {}).get(market, [])
    logger.info("자동분석 시작 — market=%s n=%d", market, len(stocks))
    success = 0
    for s in stocks:
        try:
            result = analyze_stock(s["symbol"], s["name"], market=market)
            if result is None:
                logger.warning("자동분석 실패(결과 없음): %s", s["symbol"])
                continue
            html = _rg.generate_report([result])
            sig = result.get("signal") or {}
            bnf = result.get("bnf_signal") or {}
            patterns = result.get("patterns") or {}
            pat_summary = patterns.get("summary") or {}
            import json as _json
            analysis_cache.put(
                cache_key=s["symbol"],
                market=market,
                result_html=html,
                source="auto_cron",
                signal_value=sig.get("signal"),
                signal_score=sig.get("score"),
                bnf_signal_value=bnf.get("signal"),
                bnf_signal_score=bnf.get("score"),
                pattern_json=_json.dumps(patterns, ensure_ascii=False) if patterns else None,
                pattern_signal=pat_summary.get("signal"),
                pattern_score=pat_summary.get("score"),
                last_close=result.get("last_close"),
            )
            success += 1
        except Exception as e:
            logger.exception("자동분석 오류 — %s: %s", s["symbol"], e)
    logger.info("자동분석 완료 — market=%s ok=%d/%d", market, success, len(stocks))


def daily_email_job() -> None:
    """캐시에서 종목별 결과를 모아 이메일 발송. 분석 재실행하지 않는다."""
    from src.email_sender import render_email_digest

    config = load_config()
    rows = analysis_cache.list_symbols()
    if not rows:
        logger.warning("이메일 발송 스킵 — analysis_cache 가 비어있음")
        return
    html = render_email_digest(rows)
    send_report(html, config["email"])


def leaders_refresh() -> None:
    """주도주 발굴 cron 진입점 (Spec §5 Cron 흐름).

    1. universe.yaml 파싱
    2. leader_filter.run_filter → 정량 평가
    3. leader_cache.diff_with_existing → 신규/유지/stale/탈락
    4. leader_llm.analyze_one(신규 + stale) 순차 호출
    5. leader_cache.upsert_all (user_* 보존)
    6. mark_dropped + recompute_stale
    """
    import os
    from src import leader_cache, leader_filter, leader_llm

    leader_cache.init_db()
    path = os.environ.get(
        "AUTO_TRADER_UNIVERSE_PATH", "../auto-trader/config/universe.yaml"
    )
    logger.info("leaders-refresh 시작: universe=%s", path)
    universe = leader_filter.load_universe(path)
    candidates = leader_filter.run_filter(universe)
    passed = [c for c in candidates if c.passed]
    rows = [c.as_row() for c in candidates]
    leader_cache.upsert_quantitative(rows)

    passed_syms = [c.symbol for c in passed]
    diff = leader_cache.diff_with_existing(passed_syms)
    to_llm = diff["new"] + diff["stale"]
    by_sym = {c.symbol: c for c in passed}

    llm_calls = 0
    llm_errors = 0
    for sym in to_llm:
        c = by_sym.get(sym)
        if c is None:
            continue
        inputs = {
            "symbol": c.symbol, "name": c.name, "market": c.market,
            "sector": c.sector, "industry": c.industry,
            "market_cap": c.market_cap or 0,
            "return_1y_pct": c.return_1y_pct or 0.0,
            "rel_return_pp": c.rel_return_pp or 0.0,
            "trailing_eps": c.trailing_eps, "forward_eps": c.forward_eps,
            "revenue_growth_pct": c.eps_growth_yoy or 0.0,
            "trailing_pe": c.trailing_pe,
        }
        result = leader_llm.analyze_one(inputs)
        llm_calls += 1
        if result.error:
            llm_errors += 1
            leader_cache.upsert_llm(
                sym, {}, model="gemini-2.5-flash",
                raw=result.raw, error=result.error,
            )
        else:
            leader_cache.upsert_llm(
                sym, result.fields, model="gemini-2.5-flash", raw=result.raw,
            )

    leader_cache.mark_dropped(diff["dropped"])
    leader_cache.recompute_stale()
    logger.info(
        "leaders-refresh 완료: passed=%d llm_calls=%d errors=%d dropped=%d",
        len(passed), llm_calls, llm_errors, len(diff["dropped"]),
    )


def run_scan(args):
    """수급/모멘텀 스캐너 실행."""
    from src.supply_scanner import scan_supply, scan_momentum, format_scan_result

    mode = args.mode
    days = args.days
    top_n = args.top

    if mode in ("supply", "all"):
        logger.info("외인/기관 수급 스캔 시작 (최근 %d일, 상위 %d개)", days, top_n)
        results = scan_supply(days=days, top_n=top_n)
        print(f"\n[외인/기관 동시 순매수 상위 {top_n}개 — 최근 {days}일]")
        print(format_scan_result(results, mode="supply"))
        print()

    if mode in ("momentum", "all"):
        m_days = args.days if args.days != 5 else 20  # momentum 기본값 20일
        logger.info("모멘텀 스캔 시작 (최근 %d일, 상위 %d개)", m_days, top_n)
        results = scan_momentum(days=m_days, top_n=top_n)
        print(f"\n[모멘텀 상위 {top_n}개 — 최근 {m_days}일]")
        print(format_scan_result(results, mode="momentum"))
        print()


def main():
    parser = argparse.ArgumentParser(description="주식시장 분석 시스템")
    subparsers = parser.add_subparsers(dest="command")

    # scan 서브커맨드
    scan_parser = subparsers.add_parser("scan", help="외인/기관 수급 스캐너")
    scan_parser.add_argument("--mode", choices=["supply", "momentum", "all"],
                             default="supply", help="스캔 모드 (기본: supply)")
    scan_parser.add_argument("--days", type=int, default=5, help="조회 기간 (기본: 5일)")
    scan_parser.add_argument("--top", type=int, default=20, help="상위 N개 (기본: 20)")

    # 단발 cron 서브커맨드 (launchd 분리 운영용)
    auto_parser = subparsers.add_parser("auto-analyze", help="시장 자동분석 (launchd cron 용)")
    auto_parser.add_argument("market", choices=["korea", "us"])
    subparsers.add_parser("backfill", help="예측 히스토리 backfill (launchd cron 용)")
    subparsers.add_parser("daily-email", help="다이제스트 이메일 발송 (launchd cron 용)")
    subparsers.add_parser("leaders-refresh", help="주도주 발굴 cron (launchd)")

    # 기존 옵션
    parser.add_argument("--run-now", action="store_true", help="즉시 분석 실행")
    parser.add_argument("--symbol", type=str, help="특정 종목 분석 (예: AAPL)")
    parser.add_argument("--start-scheduler", action="store_true", help="스케줄러 시작")
    parser.add_argument("--output", type=str, help="리포트 저장 경로 (HTML)")
    parser.add_argument("--web", action="store_true", help="웹 대시보드 실행 (기본 포트 5000)")
    parser.add_argument("--port", type=int, default=8080, help="웹 서버 포트 (기본: 8080)")
    parser.add_argument("--prod", action="store_true", help="프로덕션 모드 (Gunicorn, --web 함께 사용)")
    args = parser.parse_args()

    if args.command == "scan":
        run_scan(args)
        return

    if args.command == "auto-analyze":
        auto_analyze_market(args.market)
        return

    if args.command == "backfill":
        prediction_history.backfill_all(fetch_fn=fetch_stock_data)
        return

    if args.command == "daily-email":
        daily_email_job()
        return

    if args.command == "leaders-refresh":
        leaders_refresh()
        return

    if args.web:
        from src.web_app import run_web
        run_web(port=args.port, production=args.prod)
        return

    config = load_config()

    if args.symbol:
        # 설정에서 이름 찾기
        name = args.symbol
        for stock in get_all_stocks(config):
            if stock["symbol"] == args.symbol:
                name = stock["name"]
                break

        logger.info("단일 종목 분석: %s (%s)", name, args.symbol)
        result = analyze_stock(args.symbol, name)
        if result:
            html = generate_report([result])
            out = args.output or f"{args.symbol.replace('.', '_')}_report.html"
            Path(out).write_text(html, encoding="utf-8")
            logger.info("리포트 저장: %s", out)
        return

    if args.start_scheduler:
        from apscheduler.triggers.cron import CronTrigger
        extra_jobs = {
            "auto_analyze_korea": {
                "func": lambda: auto_analyze_market("korea"),
                "trigger": CronTrigger(
                    hour=16, minute=0, timezone="Asia/Seoul"
                ),
                "name": "Korea Auto Analysis",
            },
            "auto_analyze_us": {
                "func": lambda: auto_analyze_market("us"),
                "trigger": CronTrigger(
                    hour=6, minute=0, timezone="Asia/Seoul"
                ),
                "name": "US Auto Analysis (post-close)",
            },
            "backfill_daily": {
                "func": lambda: prediction_history.backfill_all(fetch_fn=fetch_stock_data),
                "trigger": CronTrigger(
                    hour=18, minute=0, timezone="Asia/Seoul"
                ),
                "name": "Daily Prediction Backfill",
            },
        }
        start_scheduler(daily_email_job, config["schedule"], extra_jobs=extra_jobs)
        return

    if args.run_now:
        html = run_full_analysis(config)
        if html:
            out = args.output or "report.html"
            Path(out).write_text(html, encoding="utf-8")
            logger.info("리포트 저장: %s", out)
            send_report(html, config["email"])
        return

    parser.print_help()


if __name__ == "__main__":
    main()
