"""RF + LGBM walk-forward 백테스트."""
from __future__ import annotations

import logging
import time
import uuid

import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from lightgbm import LGBMClassifier

from src.ml_predictor import _CLF_FEATURES, _prepare_clf_data

logger = logging.getLogger(__name__)

_MIN_TRAIN_ROWS = 30  # _prepare_clf_data와 동일


def _ensemble_vote(rf_dir: str, rf_conf: float, lgbm_dir: str, lgbm_conf: float) -> tuple[str, float]:
    """RF + LGBM voting. 같은 방향이면 평균 confidence, 다르면 더 높은 쪽."""
    if rf_dir == lgbm_dir:
        return rf_dir, (rf_conf + lgbm_conf) / 2
    return (rf_dir, rf_conf) if rf_conf >= lgbm_conf else (lgbm_dir, lgbm_conf)


def _hit(direction: str, base: float, actual: float) -> int:
    if direction == "상승":
        return 1 if actual > base else 0
    if direction == "하락":
        return 1 if actual < base else 0
    return 0


def _index_to_unix(ts: pd.Timestamp) -> int:
    """KST 자정 → UTC unix epoch."""
    if ts.tz is None:
        ts = ts.tz_localize("Asia/Seoul", nonexistent="shift_forward", ambiguous="raise")
    return int(ts.normalize().tz_convert("UTC").timestamp())


def walk_forward(symbol: str, df: pd.DataFrame, days: int = 126) -> dict:
    """RF + LGBM + 둘의 voting ensemble을 과거 N영업일 walk-forward.

    Returns:
        {'backtest_id': uuid8, 'rows': [...], 'summary': {model: {hit_rate, n}}}
        데이터 부족 시: {'error': '데이터 부족', 'backtest_id': None, 'rows': [], 'summary': {}}
    """
    df = df.sort_index()

    if len(df) < _MIN_TRAIN_ROWS + days + 1:
        return {"backtest_id": None, "rows": [], "summary": {}, "error": "데이터 부족"}

    backtest_id = uuid.uuid4().hex[:8]
    rows: list[dict] = []
    now_unix = int(time.time())

    n = len(df)
    start_t = n - days - 1
    end_t = n - 1

    for t in range(start_t, end_t):
        train_df = df.iloc[: t + 1].dropna(subset=_CLF_FEATURES)
        if len(train_df) < _MIN_TRAIN_ROWS:
            continue
        prepared = _prepare_clf_data(train_df)
        if prepared is None:
            continue
        X_train, _, y_train, _, _ = prepared
        # feature name 있는 DataFrame 으로 통일 (ml_predictor 와 동일 — 경고 방지)
        X_train = pd.DataFrame(X_train, columns=_CLF_FEATURES)

        try:
            rf = RandomForestClassifier(n_estimators=100, random_state=42)
            rf.fit(X_train, y_train)
        except Exception as e:
            logger.warning("RF fit 실패 (t=%d): %s", t, e)
            continue
        try:
            lgbm = LGBMClassifier(n_estimators=100, random_state=42, verbosity=-1)
            lgbm.fit(X_train, y_train)
        except Exception as e:
            logger.warning("LGBM fit 실패 (t=%d): %s", t, e)
            continue

        row_t = df.iloc[t]
        if row_t[_CLF_FEATURES].isna().any():
            continue
        x_t = pd.DataFrame([row_t[_CLF_FEATURES].values], columns=_CLF_FEATURES)

        rf_pred = rf.predict(x_t)[0]
        rf_conf = float(rf.predict_proba(x_t)[0].max() * 100)
        rf_dir = "상승" if rf_pred == 1 else "하락"

        lgbm_pred = lgbm.predict(x_t)[0]
        lgbm_conf = float(lgbm.predict_proba(x_t)[0].max() * 100)
        lgbm_dir = "상승" if lgbm_pred == 1 else "하락"

        ens_dir, ens_conf = _ensemble_vote(rf_dir, rf_conf, lgbm_dir, lgbm_conf)

        base_close = float(df.iloc[t]["Close"])
        actual_close = float(df.iloc[t + 1]["Close"])
        ts_unix = _index_to_unix(df.index[t])
        target_unix = _index_to_unix(df.index[t + 1])

        for model, direction, confidence in [
            ("rf", rf_dir, rf_conf),
            ("lgbm", lgbm_dir, lgbm_conf),
            ("ensemble", ens_dir, ens_conf),
        ]:
            rows.append({
                "symbol": symbol,
                "ts": ts_unix,
                "target_date": target_unix,
                "model": model,
                "direction": direction,
                "confidence": confidence,
                "base_close": base_close,
                "actual_close": actual_close,
                "hit": _hit(direction, base_close, actual_close),
                "evaluated_at": now_unix,
            })

    summary: dict = {}
    for model in ("rf", "lgbm", "ensemble"):
        model_rows = [r for r in rows if r["model"] == model]
        if not model_rows:
            continue
        n_total = len(model_rows)
        hits = sum(r["hit"] for r in model_rows)
        summary[model] = {"hit_rate": hits / n_total, "n": n_total}

    return {"backtest_id": backtest_id, "rows": rows, "summary": summary}
