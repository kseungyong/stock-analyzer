#!/usr/bin/env python3
"""주식시장 분석 시스템 - CLI 진입점"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from src.data_fetcher import fetch_stock_data, fetch_multiple, fetch_news
from src.technical_analysis import compute_indicators, generate_signal
from src.ml_predictor import run_prediction
from src.report_generator import generate_report
from src.email_sender import send_report
from src.scheduler import start_scheduler
from src.validators import validate_stock_symbol, sanitize_stock_symbol


CONFIG_PATH = Path(__file__).parent / "config" / "settings.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


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
        print(f"  [ERROR] 유효하지 않은 심볼: {symbol}")
        return None

    try:
        df = fetch_stock_data(symbol)
        df = compute_indicators(df)
        signal = generate_signal(df)
        prediction = run_prediction(df)
        news = fetch_news(symbol)
        # --- NEW: Sentiment Analysis ---
        from src.ml_predictor import analyze_sentiment
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
        print(f"  [ERROR] {name} ({symbol}): {e}")
        return None


def run_full_analysis(config: dict) -> str | None:
    """전체 종목 분석 + 리포트 생성."""
    stocks = get_all_stocks(config)
    print(f"[분석 시작] {len(stocks)}개 종목")

    analyses = []
    for stock in stocks:
        print(f"  분석 중: {stock['name']} ({stock['symbol']})")
        result = analyze_stock(stock["symbol"], stock["name"])
        if result:
            analyses.append(result)

    if not analyses:
        print("[WARNING] 분석 결과 없음")
        return None

    html = generate_report(analyses)
    print(f"[완료] {len(analyses)}개 종목 리포트 생성")
    return html


def daily_job():
    """스케줄러에서 호출되는 일일 작업."""
    config = load_config()
    html = run_full_analysis(config)
    if html:
        send_report(html, config["email"])


def main():
    parser = argparse.ArgumentParser(description="주식시장 분석 시스템")
    parser.add_argument("--run-now", action="store_true", help="즉시 분석 실행")
    parser.add_argument("--symbol", type=str, help="특정 종목 분석 (예: AAPL)")
    parser.add_argument("--start-scheduler", action="store_true", help="스케줄러 시작")
    parser.add_argument("--output", type=str, help="리포트 저장 경로 (HTML)")
    parser.add_argument("--web", action="store_true", help="웹 대시보드 실행 (기본 포트 5000)")
    parser.add_argument("--port", type=int, default=8080, help="웹 서버 포트 (기본: 8080)")
    args = parser.parse_args()

    if args.web:
        from src.web_app import run_web
        run_web(port=args.port)
        return

    config = load_config()

    if args.symbol:
        # 설정에서 이름 찾기
        name = args.symbol
        for stock in get_all_stocks(config):
            if stock["symbol"] == args.symbol:
                name = stock["name"]
                break

        print(f"[분석] {name} ({args.symbol})")
        result = analyze_stock(args.symbol, name)
        if result:
            html = generate_report([result])
            out = args.output or f"{args.symbol.replace('.', '_')}_report.html"
            Path(out).write_text(html, encoding="utf-8")
            print(f"[저장] {out}")
        return

    if args.start_scheduler:
        start_scheduler(daily_job, config["schedule"])
        return

    if args.run_now:
        html = run_full_analysis(config)
        if html:
            out = args.output or "report.html"
            Path(out).write_text(html, encoding="utf-8")
            print(f"[저장] {out}")
            send_report(html, config["email"])
        return

    parser.print_help()


if __name__ == "__main__":
    main()
