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
from src.technical_analysis import compute_indicators, generate_signal
from src.prediction_engine import PredictionEngine
from src.report_generator import generate_report
from src.email_sender import send_report
from src.scheduler import start_scheduler
from src.validators import validate_stock_symbol, sanitize_stock_symbol
from src import prediction_history

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


def get_all_stocks(config: dict) -> list[dict]:
    stocks = []
    for group in config.get("stocks", {}).values():
        stocks.extend(group)
    return stocks


def analyze_stock(symbol: str, name: str) -> dict | None:
    """단일 종목 분석을 수행한다.

    Args:
        symbol: 주식 심볼 (예: AAPL, 005930.KS)
        name: 종목명

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

        return {
            "name": name,
            "symbol": symbol,
            "df": df,
            "signal": signal,
            "prediction": prediction,
            "news": news,
            "sentiment": sentiment,
        }
    except Exception as e:
        logger.error("분석 실패 — %s (%s): %s", name, symbol, e)
        return None


def run_full_analysis(config: dict) -> str | None:
    """전체 종목 분석 + 리포트 생성."""
    stocks = get_all_stocks(config)
    logger.info("분석 시작: %d개 종목", len(stocks))

    analyses = []
    # 종목별 분석을 병렬로 실행 (ML은 CPU 집약적이므로 max_workers 제한)
    max_workers = min(len(stocks), 3)
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        future_to_stock = {
            ex.submit(analyze_stock, s["symbol"], s["name"]): s
            for s in stocks
        }
        for future in as_completed(future_to_stock):
            s = future_to_stock[future]
            logger.info("분석 중: %s (%s)", s["name"], s["symbol"])
            result = future.result()
            if result:
                analyses.append(result)

    if not analyses:
        logger.warning("분석 결과 없음")
        return None

    html = generate_report(analyses)
    logger.info("리포트 생성 완료: %d개 종목", len(analyses))
    return html


def daily_job():
    """스케줄러에서 호출되는 일일 작업."""
    config = load_config()
    html = run_full_analysis(config)
    if html:
        send_report(html, config["email"])


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
            "backfill_daily": {
                "func": lambda: prediction_history.backfill_all(fetch_fn=fetch_stock_data),
                "trigger": CronTrigger(
                    hour=18, minute=0, timezone="Asia/Seoul"
                ),
                "name": "Daily Prediction Backfill",
            }
        }
        start_scheduler(daily_job, config["schedule"], extra_jobs=extra_jobs)
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
