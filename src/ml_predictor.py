from __future__ import annotations

import os
import time
import warnings
import logging
import threading

# libomp 다중 로드 충돌 방지 (scikit-learn / LightGBM / PyTorch 각자 번들 충돌)
# → torch를 가장 먼저 import해서 torch 번들 libomp를 primary로 고정
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"
try:
    import torch as _torch_preload  # noqa: F401 — libomp 선점 목적
except ImportError:
    pass

import pandas as pd
import numpy as np
from concurrent.futures import ThreadPoolExecutor, as_completed
from sklearn.ensemble import RandomForestClassifier
from sklearn.preprocessing import MinMaxScaler
import lightgbm as lgb
from lightgbm import LGBMClassifier
from prophet import Prophet

os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"
logging.getLogger("prophet").setLevel(logging.WARNING)
logging.getLogger("cmdstanpy").setLevel(logging.WARNING)
warnings.filterwarnings("ignore", category=FutureWarning)

DISCLAIMER = "⚠️ ML 예측은 참고용이며, 투자 판단의 근거로 사용해서는 안 됩니다."

_finbert_pipeline = None
_finbert_lock = threading.Lock()

_prediction_cache: dict = {}
_prediction_cache_lock = threading.Lock()
_PREDICTION_CACHE_TTL: int = 3600  # 1시간

_CLF_FEATURES = [
    "MA5", "MA20", "RSI", "MACD", "MACD_Hist", "BB_Upper", "BB_Lower",
    "Volume_Ratio", "Stoch_K", "Stoch_D", "ATR_pct", "OBV_Change",
    "Williams_R", "CCI", "Return_1d", "Return_5d", "Return_20d",
]


# ---------------------------------------------------------------------------
# TSTransformer — top-level (ProcessPoolExecutor pickle 요건)
# ---------------------------------------------------------------------------

def _build_transformer_model(feature_dim: int, lookback: int, d_model: int = 32,
                              nhead: int = 4, num_layers: int = 2, dropout: float = 0.2):
    """PyTorch Transformer 모델을 생성한다. top-level 함수로 분리하여 pickle 가능하게 한다."""
    import torch
    import torch.nn as nn

    class TSTransformer(nn.Module):
        def __init__(self):
            super().__init__()
            self.input_linear = nn.Linear(feature_dim, d_model)
            self.pos_encoder = nn.Parameter(torch.zeros(1, lookback, d_model))
            encoder_layer = nn.TransformerEncoderLayer(
                d_model=d_model, nhead=nhead, dropout=dropout, batch_first=True
            )
            self.transformer_encoder = nn.TransformerEncoder(encoder_layer, num_layers)
            self.fc = nn.Linear(d_model, 1)
            self.sigmoid = nn.Sigmoid()

        def forward(self, src):
            x = self.input_linear(src) + self.pos_encoder
            x = self.transformer_encoder(x)
            return self.sigmoid(self.fc(x[:, -1, :]))

    return TSTransformer()


# ---------------------------------------------------------------------------
# 공통 데이터 준비
# ---------------------------------------------------------------------------

def _prepare_clf_data(df: pd.DataFrame) -> tuple | None:
    data = df.dropna(subset=_CLF_FEATURES).copy()
    if len(data) < 30:
        return None
    data["Target"] = (data["Close"].shift(-1) > data["Close"]).astype(int)
    data = data.dropna()
    X = data[_CLF_FEATURES].values
    y = data["Target"].values
    split = int(len(X) * 0.8)
    return X[:split], X[split:], y[:split], y[split:], X


def _prepare_sequence_data(df: pd.DataFrame, features: list[str], lookback: int) -> tuple | None:
    data = df.dropna(subset=features).copy()
    if len(data) < lookback + 30:
        return None
    data["Target"] = (data["Close"].shift(-1) > data["Close"]).astype(int)
    data = data.dropna()
    scaler = MinMaxScaler()
    scaled = scaler.fit_transform(data[features].values)
    X, y = [], []
    for i in range(lookback, len(scaled)):
        X.append(scaled[i - lookback:i])
        y.append(data["Target"].iloc[i])
    X, y = np.array(X), np.array(y)
    split = int(len(X) * 0.8)
    return X[:split], X[split:], y[:split], y[split:], X


# ---------------------------------------------------------------------------
# 모델별 top-level 예측 함수 (ProcessPoolExecutor 직렬화 가능)
# ---------------------------------------------------------------------------

def predict_prophet(df: pd.DataFrame, days: int = 7) -> dict:
    """Prophet으로 향후 가격 추세를 예측한다."""
    prophet_df = df[["Close"]].reset_index()
    prophet_df.columns = ["ds", "y"]
    prophet_df["ds"] = pd.to_datetime(prophet_df["ds"])

    model = Prophet(daily_seasonality=False, yearly_seasonality=True)
    model.fit(prophet_df)
    future = model.make_future_dataframe(periods=days)
    forecast = model.predict(future)
    tail = forecast[["ds", "yhat", "yhat_lower", "yhat_upper"]].tail(days)

    last_close = df["Close"].iloc[-1]
    predicted = tail["yhat"].iloc[-1]
    change_pct = (predicted - last_close) / last_close * 100
    return {
        "predicted_price": round(predicted, 2),
        "change_pct": round(change_pct, 2),
        "range": [round(tail["yhat_lower"].iloc[-1], 2), round(tail["yhat_upper"].iloc[-1], 2)],
    }


def predict_direction(df: pd.DataFrame) -> dict:
    """Random Forest로 다음 날 상승/하락을 분류한다."""
    prepared = _prepare_clf_data(df)
    if prepared is None:
        return {"direction": "데이터 부족", "confidence": 0.0}
    X_train, X_test, y_train, y_test, X = prepared
    clf = RandomForestClassifier(n_estimators=100, random_state=42)
    clf.fit(X_train, y_train)
    accuracy = clf.score(X_test, y_test) if len(X_test) > 0 else 0.0
    proba = clf.predict_proba(X[-1].reshape(1, -1))[0]
    pred = clf.predict(X[-1].reshape(1, -1))[0]
    return {
        "direction": "상승" if pred == 1 else "하락",
        "confidence": round(float(max(proba)) * 100, 1),
        "accuracy": round(accuracy * 100, 1),
    }


def predict_direction_lgbm(df: pd.DataFrame) -> dict:
    """LightGBM으로 다음 날 상승/하락을 분류한다."""
    prepared = _prepare_clf_data(df)
    if prepared is None:
        return {"direction": "데이터 부족", "confidence": 0.0}
    X_train, X_test, y_train, y_test, X = prepared
    clf = LGBMClassifier(
        n_estimators=300, num_leaves=31, learning_rate=0.05,
        min_child_samples=20, random_state=42, verbose=-1,
    )
    callbacks = []
    eval_set = None
    if len(X_test) > 0:
        eval_set = [(X_test, y_test)]
        callbacks = [lgb.early_stopping(30, verbose=False), lgb.log_evaluation(period=-1)]
    clf.fit(X_train, y_train, eval_set=eval_set, callbacks=callbacks)
    accuracy = clf.score(X_test, y_test) if len(X_test) > 0 else 0.0
    proba = clf.predict_proba(X[-1].reshape(1, -1))[0]
    pred = clf.predict(X[-1].reshape(1, -1))[0]
    return {
        "direction": "상승" if pred == 1 else "하락",
        "confidence": round(float(max(proba)) * 100, 1),
        "accuracy": round(accuracy * 100, 1),
    }


def predict_direction_lstm(df: pd.DataFrame, lookback: int = 20) -> dict:
    """LSTM으로 다음 날 상승/하락을 분류한다 (PyTorch 구현)."""
    try:
        import torch
        import torch.nn as nn
        from torch.utils.data import TensorDataset, DataLoader
    except ImportError:
        return {"direction": "PyTorch 미설치", "confidence": 0.0, "accuracy": 0.0}

    prepared = _prepare_sequence_data(df, _CLF_FEATURES, lookback)
    if prepared is None:
        return {"direction": "데이터 부족", "confidence": 0.0}
    X_train, X_test, y_train, y_test, X = prepared
    if len(X_test) == 0:
        return {"direction": "데이터 부족", "confidence": 0.0}

    class LSTMModel(nn.Module):
        def __init__(self, input_size, hidden_size=50, dropout=0.2):
            super().__init__()
            self.lstm = nn.LSTM(input_size, hidden_size, batch_first=True)
            self.dropout = nn.Dropout(dropout)
            self.fc = nn.Linear(hidden_size, 1)
            self.sigmoid = nn.Sigmoid()

        def forward(self, x):
            out, _ = self.lstm(x)
            out = self.dropout(out[:, -1, :])
            return self.sigmoid(self.fc(out))

    X_train_t = torch.tensor(X_train, dtype=torch.float32)
    y_train_t = torch.tensor(y_train, dtype=torch.float32).unsqueeze(1)
    X_test_t = torch.tensor(X_test, dtype=torch.float32)
    y_test_t = torch.tensor(y_test, dtype=torch.float32).unsqueeze(1)

    train_loader = DataLoader(TensorDataset(X_train_t, y_train_t), batch_size=32, shuffle=True)

    device = torch.device("cpu")
    model = LSTMModel(input_size=len(_CLF_FEATURES)).to(device)
    criterion = nn.BCELoss()
    optimizer = torch.optim.Adam(model.parameters(), lr=0.001)

    best_val_loss = float("inf")
    patience, no_improve = 3, 0
    best_state = None

    model.train()
    for _ in range(20):
        for bx, by in train_loader:
            optimizer.zero_grad()
            loss = criterion(model(bx.to(device)), by.to(device))
            loss.backward()
            optimizer.step()
        with torch.no_grad():
            val_loss = criterion(model(X_test_t.to(device)), y_test_t.to(device)).item()
        if val_loss < best_val_loss:
            best_val_loss = val_loss
            best_state = {k: v.clone() for k, v in model.state_dict().items()}
            no_improve = 0
        else:
            no_improve += 1
            if no_improve >= patience:
                break

    if best_state:
        model.load_state_dict(best_state)

    model.eval()
    with torch.no_grad():
        preds = model(X_test_t.to(device))
        preds_bin = (preds >= 0.5).float()
        accuracy = (preds_bin == y_test_t.to(device)).float().mean().item()
        prob = model(torch.tensor(X[-1], dtype=torch.float32).unsqueeze(0).to(device)).item()

    pred = 1 if prob >= 0.5 else 0
    confidence = prob if pred == 1 else 1 - prob
    return {
        "direction": "상승" if pred == 1 else "하락",
        "confidence": round(confidence * 100, 1),
        "accuracy": round(accuracy * 100, 1),
    }


def predict_direction_transformer(df: pd.DataFrame, lookback: int = 20) -> dict:
    """Transformer 모델로 다음 날 상승/하락을 분류한다."""
    try:
        import torch
        import torch.nn as nn
        from torch.utils.data import TensorDataset, DataLoader
    except ImportError:
        return {"direction": "PyTorch 미설치", "confidence": 0.0, "accuracy": 0.0}

    prepared = _prepare_sequence_data(df, _CLF_FEATURES, lookback)
    if prepared is None:
        return {"direction": "데이터 부족", "confidence": 0.0}
    X_train, X_test, y_train, y_test, X = prepared
    if len(X_test) == 0:
        return {"direction": "데이터 부족", "confidence": 0.0}

    X_train_t = torch.tensor(X_train, dtype=torch.float32)
    y_train_t = torch.tensor(y_train, dtype=torch.float32).unsqueeze(1)
    X_test_t = torch.tensor(X_test, dtype=torch.float32)
    y_test_t = torch.tensor(y_test, dtype=torch.float32).unsqueeze(1)

    train_loader = DataLoader(TensorDataset(X_train_t, y_train_t), batch_size=32, shuffle=True)

    device = torch.device("cpu")
    model = _build_transformer_model(feature_dim=len(_CLF_FEATURES), lookback=lookback).to(device)
    criterion = nn.BCELoss()
    optimizer = torch.optim.Adam(model.parameters(), lr=0.001)

    model.train()
    for _ in range(10):
        for bx, by in train_loader:
            optimizer.zero_grad()
            loss = criterion(model(bx.to(device)), by.to(device))
            loss.backward()
            optimizer.step()

    model.eval()
    with torch.no_grad():
        preds = model(X_test_t.to(device))
        preds_bin = (preds >= 0.5).float()
        accuracy = (preds_bin == y_test_t.to(device)).float().mean().item()
        prob = model(torch.tensor(X[-1], dtype=torch.float32).unsqueeze(0).to(device)).item()

    pred = 1 if prob >= 0.5 else 0
    confidence = prob if pred == 1 else 1 - prob
    return {
        "direction": "상승" if pred == 1 else "하락",
        "confidence": round(confidence * 100, 1),
        "accuracy": round(accuracy * 100, 1),
    }


# ---------------------------------------------------------------------------
# FinBERT 감성 분석 (별도 — ProcessPoolExecutor 대상 아님)
# ---------------------------------------------------------------------------

def _get_finbert():
    global _finbert_pipeline
    if _finbert_pipeline is None:
        with _finbert_lock:
            if _finbert_pipeline is None:
                from transformers import pipeline
                _finbert_pipeline = pipeline("sentiment-analysis", model="ProsusAI/finbert", framework="pt", device="cpu")
    return _finbert_pipeline


def analyze_sentiment(news_items: list[dict]) -> dict:
    """FinBERT를 사용하여 뉴스 기사의 감성을 분석한다."""
    if not news_items:
        return {"label": "뉴스 없음", "score": 0.0, "details": []}

    texts = []
    for item in news_items:
        title = item.get("title_en") or item.get("title", "")
        summary = item.get("summary_en") or item.get("summary", "")
        text = ". ".join(filter(None, [title, summary]))
        if text:
            texts.append((item, text[:512]))

    if not texts:
        return {"label": "분석할 텍스트 없음", "score": 0.0, "details": []}

    try:
        sentiment_pipeline = _get_finbert()
    except ImportError:
        return {"error": "transformers library not installed"}
    except Exception as e:
        return {"error": f"Failed to load FinBERT: {e}"}

    results = []
    total_score = 0
    valid_count = 0

    try:
        for item, text in texts:
            res = sentiment_pipeline(text)[0]
            label = res["label"]
            conf = res["score"]
            if label == "positive":
                score, kor_label = conf, "긍정"
            elif label == "negative":
                score, kor_label = -conf, "부정"
            else:
                score, kor_label = 0, "중립"
            total_score += score
            valid_count += 1
            results.append({"title": item.get("title", ""), "label": kor_label,
                             "confidence": round(conf * 100, 1)})
    except Exception as e:
        return {"error": f"Analysis failed: {e}"}

    if valid_count == 0:
        return {"label": "분석 불가", "score": 0.0, "details": []}

    avg_score = total_score / valid_count
    if avg_score > 0.2:
        overall_label = "긍정적 (Bullish)"
    elif avg_score < -0.2:
        overall_label = "부정적 (Bearish)"
    else:
        overall_label = "중립적 (Neutral)"

    return {"label": overall_label, "score": round(avg_score, 3), "details": results}


# ---------------------------------------------------------------------------
# 앙상블 예측
# ---------------------------------------------------------------------------

def predict_ensemble(results: dict) -> dict:
    """4개 분류 모델(random_forest, lightgbm, lstm, transformer)의 가중 투표 앙상블."""
    model_keys = ["random_forest", "lightgbm", "lstm", "transformer"]
    up_weight = 0.0
    down_weight = 0.0
    total_weight = 0.0
    model_count = 0

    for key in model_keys:
        res = results.get(key)
        if res is None or "error" in res:
            continue
        direction = res.get("direction", "")
        confidence = res.get("confidence", 0.0)
        accuracy = res.get("accuracy", 0.0)
        if direction not in ("상승", "하락"):
            continue
        weight = (confidence / 100) * (accuracy / 100)
        total_weight += weight
        if direction == "상승":
            up_weight += weight
        else:
            down_weight += weight
        model_count += 1

    if model_count == 0 or total_weight == 0:
        return {"direction": "데이터 부족", "confidence": 0.0, "vote_ratio": 0.0, "model_count": 0}

    if up_weight >= down_weight:
        direction = "상승"
        vote_ratio = up_weight / total_weight
    else:
        direction = "하락"
        vote_ratio = down_weight / total_weight

    confidence = round(vote_ratio * 100, 1)
    return {
        "direction": direction,
        "confidence": confidence,
        "vote_ratio": round(vote_ratio, 3),
        "model_count": model_count,
    }


# ---------------------------------------------------------------------------
# 하위 호환 래퍼 — prediction_engine 미사용 환경 대응
# ---------------------------------------------------------------------------

def run_prediction(df: pd.DataFrame, cache_key: str = "") -> dict:
    """ThreadPoolExecutor 기반 병렬 예측 (하위 호환용).

    PredictionEngine을 사용할 수 없는 환경에서 폴백으로 사용한다.
    """
    if cache_key:
        with _prediction_cache_lock:
            if cache_key in _prediction_cache:
                ts, cached = _prediction_cache[cache_key]
                if time.time() - ts < _PREDICTION_CACHE_TTL:
                    return cached

    tasks = {
        "prophet": lambda: predict_prophet(df),
        "random_forest": lambda: predict_direction(df),
        "lightgbm": lambda: predict_direction_lgbm(df),
        "lstm": lambda: predict_direction_lstm(df),
        "transformer": lambda: predict_direction_transformer(df),
    }
    results: dict = {}
    with ThreadPoolExecutor(max_workers=5) as executor:
        future_to_name = {executor.submit(fn): name for name, fn in tasks.items()}
        for future in as_completed(future_to_name):
            name = future_to_name[future]
            try:
                results[name] = future.result()
            except Exception as e:
                results[name] = {"error": str(e)}

    output = {
        "prophet": results.get("prophet"),
        "random_forest": results.get("random_forest"),
        "lightgbm": results.get("lightgbm"),
        "lstm": results.get("lstm"),
        "transformer": results.get("transformer"),
        "ensemble": predict_ensemble(results),
        "disclaimer": DISCLAIMER,
    }

    if cache_key:
        with _prediction_cache_lock:
            _prediction_cache[cache_key] = (time.time(), output)

    return output
