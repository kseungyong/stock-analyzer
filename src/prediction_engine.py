"""병렬 ML 예측 엔진.

ProcessPoolExecutor(spawn)로 각 ML 모델을 별도 프로세스에서 병렬 실행하고,
결과를 디스크에 캐시하여 서버 재시작 후에도 빠른 응답을 보장한다.
"""
from __future__ import annotations

import atexit
import logging
import multiprocessing as mp
import os
import pickle
import re
import warnings
from concurrent.futures import ProcessPoolExecutor, BrokenExecutor, as_completed
from pathlib import Path

import pandas as pd

from src.ml_predictor import (
    DISCLAIMER,
    predict_direction,
    predict_direction_lgbm,
    predict_direction_lstm,
    predict_direction_transformer,
    predict_ensemble,
    predict_prophet,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 자식 프로세스 초기화 — spawn 방식에서는 환경 변수가 상속되지 않으므로 재설정
# ---------------------------------------------------------------------------

def _worker_init() -> None:
    """ProcessPoolExecutor 자식 프로세스 초기화."""
    import sys

    # spawn 방식에서는 부모의 sys.path가 상속되지 않으므로 프로젝트 루트 추가
    _project_root = str(Path(__file__).resolve().parent.parent)
    if _project_root not in sys.path:
        sys.path.insert(0, _project_root)

    # libomp 다중 로드 충돌 방지 — scikit-learn / LightGBM / PyTorch 번들 충돌
    os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"
    os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"
    warnings.filterwarnings("ignore")
    logging.getLogger("prophet").setLevel(logging.WARNING)
    logging.getLogger("cmdstanpy").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Pool 사전 생성 — 모듈 수준 싱글톤 (요청마다 프로세스 생성 오버헤드 방지)
# Flask 멀티스레드 환경에서도 Pool은 하나만 유지된다.
# ---------------------------------------------------------------------------

_SPAWN_CTX = mp.get_context("spawn")  # set_start_method 대신 컨텍스트 명시
_pool: ProcessPoolExecutor | None = None


def _get_pool() -> ProcessPoolExecutor:
    global _pool
    if _pool is None:
        _pool = ProcessPoolExecutor(
            max_workers=5,
            mp_context=_SPAWN_CTX,
            initializer=_worker_init,
        )
        atexit.register(lambda: _pool.shutdown(wait=False))
        logger.info("PredictionEngine: ProcessPoolExecutor 초기화 완료 (max_workers=5)")
    return _pool


def _reset_pool() -> None:
    """Broken pool을 폐기하고 다음 호출 시 재생성되도록 초기화한다."""
    global _pool
    if _pool is not None:
        try:
            _pool.shutdown(wait=False)
        except Exception:
            pass
        _pool = None
    logger.info("PredictionEngine: pool 리셋 완료")


# ---------------------------------------------------------------------------
# PredictionEngine
# ---------------------------------------------------------------------------

class PredictionEngine:
    """병렬 ML 예측 엔진.

    Usage:
        engine = PredictionEngine()
        result = engine.run(df, cache_key="AAPL")
    """

    CACHE_DIR = Path(".cache/predictions")
    TTL = 3600  # 캐시 유효 시간 (초)

    # 모델명 → top-level 함수 매핑
    _MODEL_FUNCS = {
        "prophet": predict_prophet,
        "random_forest": predict_direction,
        "lightgbm": predict_direction_lgbm,
        "lstm": predict_direction_lstm,
        "transformer": predict_direction_transformer,
    }

    def run(self, df: pd.DataFrame, cache_key: str = "") -> dict:
        """5개 ML 모델을 병렬 실행하고 결과를 반환한다.

        Args:
            df: OHLCV + 기술지표 데이터프레임
            cache_key: 캐시 키 (보통 종목 심볼). 비어있으면 캐시를 사용하지 않는다.

        Returns:
            {prophet, random_forest, lightgbm, lstm, transformer, disclaimer} 딕셔너리
        """
        keyed = self._build_keyed(df, cache_key) if cache_key else ""

        if keyed:
            cached = self._load_cache(keyed)
            if cached is not None:
                logger.info("캐시 히트: %s", keyed)
                return cached

        results = self._run_parallel(df)

        if keyed:
            self._save_cache(keyed, results)

        return results

    # ------------------------------------------------------------------
    # 내부 메서드
    # ------------------------------------------------------------------

    def _build_keyed(self, df: pd.DataFrame, cache_key: str) -> str:
        """캐시 키에 날짜를 포함하여 최신 데이터 구분을 보장한다."""
        last_date = df.index[-1].date() if hasattr(df.index[-1], "date") else str(df.index[-1])[:10]
        return f"{cache_key}_{last_date}"

    def _run_parallel(self, df: pd.DataFrame) -> dict:
        """ProcessPoolExecutor로 모델별 병렬 실행. Pool crash 시 재생성 후 1회 재시도.

        macOS gunicorn 환경처럼 fork+ML 라이브러리가 segfault 일으키는 경우
        PREDICTION_ENGINE_NO_PROCESS_POOL=1 환경변수로 ProcessPool 자체를 우회한다.
        """
        if os.environ.get("PREDICTION_ENGINE_NO_PROCESS_POOL", "").strip().lower() in ("1", "true", "yes"):
            return self._run_fallback(df)
        for attempt in range(2):
            try:
                pool = _get_pool()
                future_to_name = {
                    pool.submit(fn, df): name
                    for name, fn in self._MODEL_FUNCS.items()
                }
                results: dict = {}
                for future in as_completed(future_to_name):
                    name = future_to_name[future]
                    try:
                        results[name] = future.result()
                        logger.debug("모델 완료: %s → %s", name, results[name])
                    except BrokenExecutor:
                        raise  # pool 자체가 broken → 재시도 루프로 전달
                    except Exception as e:
                        logger.warning("모델 실패: %s — %s", name, e)
                        results[name] = {"error": str(e)}

                results["ensemble"] = predict_ensemble(results)
                return {
                    "prophet": results.get("prophet"),
                    "random_forest": results.get("random_forest"),
                    "lightgbm": results.get("lightgbm"),
                    "lstm": results.get("lstm"),
                    "transformer": results.get("transformer"),
                    "ensemble": results.get("ensemble"),
                    "disclaimer": DISCLAIMER,
                }
            except BrokenExecutor:
                logger.warning("ProcessPool이 broken 상태 — pool 재생성 후 재시도 (attempt %d)", attempt + 1)
                _reset_pool()
                if attempt == 1:
                    # 2회 모두 실패 → ThreadPoolExecutor 폴백
                    logger.error("ProcessPool 재시도 실패 — ThreadPoolExecutor 폴백")
                    return self._run_fallback(df)

        return self._run_fallback(df)  # unreachable but satisfies type checker

    def _run_fallback(self, df: pd.DataFrame) -> dict:
        """순차 실행 폴백 (ProcessPool 완전 실패 또는 비활성화 시).

        macOS native ML 라이브러리(Prophet/cmdstanpy 등)가 thread 동시 실행에서도
        간헐적 segfault 일으키므로, 안전 우선으로 순차 실행을 기본으로 한다.
        """
        results: dict = {}
        for name, fn in self._MODEL_FUNCS.items():
            try:
                results[name] = fn(df)
            except Exception as e:
                logger.warning("폴백 모델 실패: %s — %s", name, e)
                results[name] = {"error": str(e)}
        results["ensemble"] = predict_ensemble(results)
        return {
            "prophet": results.get("prophet"),
            "random_forest": results.get("random_forest"),
            "lightgbm": results.get("lightgbm"),
            "lstm": results.get("lstm"),
            "transformer": results.get("transformer"),
            "ensemble": results.get("ensemble"),
            "disclaimer": DISCLAIMER,
        }

    def _safe_cache_path(self, key: str) -> Path:
        """캐시 키를 안전한 파일 경로로 변환한다. Path traversal 방어."""
        safe_key = re.sub(r"[^\w.\-]", "_", key)
        cache_dir = self.CACHE_DIR.resolve()
        path = (cache_dir / f"{safe_key}.pkl").resolve()
        if not str(path).startswith(str(cache_dir)):
            raise ValueError(f"유효하지 않은 캐시 키: {key!r}")
        return path

    def _load_cache(self, key: str) -> dict | None:
        """TTL 내 디스크 캐시를 로드한다."""
        import time
        try:
            path = self._safe_cache_path(key)
            if not path.exists():
                return None
            if time.time() - path.stat().st_mtime > self.TTL:
                path.unlink(missing_ok=True)
                return None
            return pickle.loads(path.read_bytes())
        except Exception as e:
            logger.warning("캐시 로드 실패: %s — %s", key, e)
            return None

    def _save_cache(self, key: str, data: dict) -> None:
        """결과를 디스크에 원자적으로 저장한다 (tmp → fsync → rename).

        fsync 로 데이터 디스크 flush 후 rename — power loss 시 0-byte
        partial file 방지. tmp.replace 자체는 rename 만 atomic, 실 데이터는
        write buffer 에만 있을 수 있음.
        """
        import os
        try:
            path = self._safe_cache_path(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            with open(tmp, "wb") as f:
                f.write(pickle.dumps(data))
                f.flush()
                os.fsync(f.fileno())
            tmp.replace(path)  # 원자적 rename
            logger.info("캐시 저장: %s", path.name)
        except Exception as e:
            logger.warning("캐시 저장 실패: %s — %s", key, e)
