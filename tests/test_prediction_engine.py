"""PredictionEngine 검증 스크립트."""
import time
from pathlib import Path

from src.data_fetcher import fetch_stock_data
from src.technical_analysis import compute_indicators
from src.prediction_engine import PredictionEngine


def main():
    engine = PredictionEngine()

    print("=== PredictionEngine 검증 ===\n")

    # 1. 데이터 준비
    print("[1] 데이터 수집 중... (AAPL)")
    df = fetch_stock_data("AAPL")
    df = compute_indicators(df)
    print(f"    행수: {len(df)}, 마지막 날짜: {df.index[-1].date()}\n")

    # 2. 최초 실행 (캐시 MISS)
    print("[2] 최초 예측 (캐시 MISS)...")
    t0 = time.time()
    result = engine.run(df, cache_key="AAPL_VERIFY")
    elapsed = time.time() - t0
    print(f"    소요시간: {elapsed:.1f}s\n")

    ok, fail = [], []
    for model, val in result.items():
        if model == "disclaimer":
            continue
        if val and "error" not in val:
            ok.append(model)
            print(f"    ✅ {model}: {val}")
        else:
            fail.append(model)
            print(f"    ❌ {model}: {val}")

    # 3. 재실행 (캐시 HIT)
    print(f"\n[3] 재실행 (캐시 HIT)...")
    t0 = time.time()
    result2 = engine.run(df, cache_key="AAPL_VERIFY")
    elapsed2 = time.time() - t0
    print(f"    소요시간: {elapsed2:.3f}s")
    print(f"    결과 일치: {result['random_forest'] == result2['random_forest']}")

    # 4. 캐시 파일 확인
    cache_dir = Path(".cache/predictions")
    files = list(cache_dir.glob("AAPL_VERIFY*.pkl")) if cache_dir.exists() else []
    print(f"\n[4] 캐시 파일: {[f.name for f in files]}")

    # 5. 결과 요약
    print(f"\n=== 결과 ===")
    print(f"    성공: {len(ok)}개 {ok}")
    print(f"    실패: {len(fail)}개 {fail}")
    print(f"    최초 실행: {elapsed:.1f}s / 캐시 히트: {elapsed2:.3f}s")


if __name__ == "__main__":
    main()
