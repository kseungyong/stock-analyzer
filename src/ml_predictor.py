import pandas as pd
import numpy as np
import time
import threading
from prophet import Prophet
from sklearn.ensemble import RandomForestClassifier
from lightgbm import LGBMClassifier
from sklearn.preprocessing import MinMaxScaler
import warnings
import logging
import os

os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"

# Prophet 로그 억제
logging.getLogger("prophet").setLevel(logging.WARNING)
logging.getLogger("cmdstanpy").setLevel(logging.WARNING)
warnings.filterwarnings("ignore", category=FutureWarning)

DISCLAIMER = "⚠️ ML 예측은 참고용이며, 투자 판단의 근거로 사용해서는 안 됩니다."

_finbert_pipeline = None
_finbert_lock = threading.Lock()

# 예측 결과 TTL 캐시: {cache_key: (timestamp, result)}
_prediction_cache: dict[str, tuple[float, dict]] = {}
_PREDICTION_CACHE_TTL = 3600  # 1시간


def _get_finbert():
    """FinBERT 파이프라인을 로드하고 캐싱한다. 최초 호출 시에만 모델을 로드한다 (thread-safe)."""
    global _finbert_pipeline
    if _finbert_pipeline is None:
        with _finbert_lock:
            if _finbert_pipeline is None:  # double-checked locking
                from transformers import pipeline
                _finbert_pipeline = pipeline("sentiment-analysis", model="ProsusAI/finbert")
    return _finbert_pipeline


def predict_with_prophet(df: pd.DataFrame, days: int = 7) -> pd.DataFrame:
    """Prophet으로 향후 가격 추세를 예측한다.

    Args:
        df: OHLCV 데이터프레임
        days: 예측 일수

    Returns:
        예측 결과 데이터프레임 (ds, yhat, yhat_lower, yhat_upper)
    """
    prophet_df = df[["Close"]].reset_index()
    prophet_df.columns = ["ds", "y"]
    prophet_df["ds"] = pd.to_datetime(prophet_df["ds"])

    model = Prophet(daily_seasonality=False, yearly_seasonality=True)
    model.fit(prophet_df)

    future = model.make_future_dataframe(periods=days)
    forecast = model.predict(future)

    return forecast[["ds", "yhat", "yhat_lower", "yhat_upper"]].tail(days)


_CLF_FEATURES = ["MA5", "MA20", "RSI", "MACD", "MACD_Hist", "BB_Upper", "BB_Lower"]


def _prepare_clf_data(df: pd.DataFrame) -> tuple | None:
    """분류 모델 공통 데이터 준비 — 피처 행렬, 타겟, train/test 분할을 반환한다.

    Returns:
        (X_train, X_test, y_train, y_test, X_all) 또는 데이터 부족 시 None
    """
    data = df.dropna(subset=_CLF_FEATURES).copy()
    if len(data) < 30:
        return None
    data["Target"] = (data["Close"].shift(-1) > data["Close"]).astype(int)
    data = data.dropna()

    X = data[_CLF_FEATURES].values
    y = data["Target"].values
    split = int(len(X) * 0.8)
    return X[:split], X[split:], y[:split], y[split:], X


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

    clf = LGBMClassifier(n_estimators=100, random_state=42, verbose=-1)
    clf.fit(X_train, y_train)
    accuracy = clf.score(X_test, y_test) if len(X_test) > 0 else 0.0

    proba = clf.predict_proba(X[-1].reshape(1, -1))[0]
    pred = clf.predict(X[-1].reshape(1, -1))[0]
    return {
        "direction": "상승" if pred == 1 else "하락",
        "confidence": round(float(max(proba)) * 100, 1),
        "accuracy": round(accuracy * 100, 1),
    }


def predict_direction_lstm(df: pd.DataFrame, lookback: int = 20) -> dict:
    """LSTM으로 다음 날 상승/하락을 분류한다.

    최근 lookback일간의 기술 지표 시퀀스를 입력으로 사용한다.
    """
    from tensorflow.keras.models import Sequential
    from tensorflow.keras.layers import LSTM, Dense, Dropout
    from tensorflow.keras.callbacks import EarlyStopping

    features = _CLF_FEATURES
    data = df.dropna(subset=features).copy()

    if len(data) < lookback + 30:
        return {"direction": "데이터 부족", "confidence": 0.0}

    data["Target"] = (data["Close"].shift(-1) > data["Close"]).astype(int)
    data = data.dropna()

    scaler = MinMaxScaler()
    scaled = scaler.fit_transform(data[features].values)

    X, y = [], []
    for i in range(lookback, len(scaled)):
        X.append(scaled[i - lookback:i])
        y.append(data["Target"].iloc[i])
    X = np.array(X)
    y = np.array(y)

    split = int(len(X) * 0.8)
    X_train, X_test = X[:split], X[split:]
    y_train, y_test = y[:split], y[split:]

    if len(X_test) == 0:
        return {"direction": "데이터 부족", "confidence": 0.0}

    model = Sequential([
        LSTM(50, return_sequences=False, input_shape=(lookback, len(features))),
        Dropout(0.2),
        Dense(1, activation="sigmoid"),
    ])
    model.compile(optimizer="adam", loss="binary_crossentropy", metrics=["accuracy"])
    early_stop = EarlyStopping(monitor="val_loss", patience=5, restore_best_weights=True)
    model.fit(X_train, y_train, epochs=50, batch_size=32,
              validation_data=(X_test, y_test), callbacks=[early_stop], verbose=0)

    loss, accuracy = model.evaluate(X_test, y_test, verbose=0)

    latest = X[-1].reshape(1, lookback, len(features))
    prob = float(model.predict(latest, verbose=0)[0][0])
    pred = 1 if prob >= 0.5 else 0
    confidence = prob if pred == 1 else 1 - prob

    return {
        "direction": "상승" if pred == 1 else "하락",
        "confidence": round(confidence * 100, 1),
        "accuracy": round(accuracy * 100, 1),
    }


def predict_direction_transformer(df: pd.DataFrame, lookback: int = 20) -> dict:
    """Transformer 계열 딥러닝 모델로 다음 날 상승/하락을 분류한다.
    
    LSTM보다 장기 의존성(Long-term dependency)을 더 잘 파악할 수 있는 최신 구조.
    """
    try:
        import torch
        import torch.nn as nn
        from torch.utils.data import TensorDataset, DataLoader
    except ImportError:
        return {"direction": "PyTorch 미설치", "confidence": 0.0, "accuracy": 0.0}

    features = _CLF_FEATURES
    data = df.dropna(subset=features).copy()

    if len(data) < lookback + 30:
        return {"direction": "데이터 부족", "confidence": 0.0}

    data["Target"] = (data["Close"].shift(-1) > data["Close"]).astype(int)
    data = data.dropna()

    scaler = MinMaxScaler()
    scaled = scaler.fit_transform(data[features].values)

    X, y = [], []
    for i in range(lookback, len(scaled)):
        X.append(scaled[i - lookback:i])
        y.append(data["Target"].iloc[i])
    X = np.array(X)
    y = np.array(y)

    split = int(len(X) * 0.8)
    X_train, X_test = X[:split], X[split:]
    y_train, y_test = y[:split], y[split:]

    if len(X_test) == 0:
        return {"direction": "데이터 부족", "confidence": 0.0}

    # PyTorch Tensors
    X_train_t = torch.tensor(X_train, dtype=torch.float32)
    y_train_t = torch.tensor(y_train, dtype=torch.float32).unsqueeze(1)
    X_test_t = torch.tensor(X_test, dtype=torch.float32)
    y_test_t = torch.tensor(y_test, dtype=torch.float32).unsqueeze(1)

    train_data = TensorDataset(X_train_t, y_train_t)
    train_loader = DataLoader(train_data, batch_size=32, shuffle=True)

    # 간단한 Time-Series Transformer 모형
    class TSTransformer(nn.Module):
        def __init__(self, feature_dim, d_model=32, nhead=4, num_layers=2, dropout=0.2):
            super().__init__()
            self.input_linear = nn.Linear(feature_dim, d_model)
            self.pos_encoder = nn.Parameter(torch.zeros(1, lookback, d_model))
            encoder_layers = nn.TransformerEncoderLayer(
                d_model=d_model, nhead=nhead, dropout=dropout, batch_first=True
            )
            self.transformer_encoder = nn.TransformerEncoder(encoder_layers, num_layers)
            self.fc = nn.Linear(d_model, 1)
            self.sigmoid = nn.Sigmoid()

        def forward(self, src):
            # src: [batch_size, seq_len, features] -> [batch_size, seq_len, d_model]
            x = self.input_linear(src)
            # Add positional encoding
            x = x + self.pos_encoder
            # Transformer
            x = self.transformer_encoder(x)
            # Use only the last time step for prediction
            x = x[:, -1, :]
            out = self.sigmoid(self.fc(x))
            return out

    device = torch.device("cpu")
    model = TSTransformer(feature_dim=len(features)).to(device)
    criterion = nn.BCELoss()
    optimizer = torch.optim.Adam(model.parameters(), lr=0.001)

    # 훈련 (10 epochs for speed, as it's run synchronously in web request)
    model.train()
    for _ in range(10):
        for bx, by in train_loader:
            optimizer.zero_grad()
            out = model(bx.to(device))
            loss = criterion(out, by.to(device))
            loss.backward()
            optimizer.step()

    # 평가 / 예측
    model.eval()
    with torch.no_grad():
        test_preds = model(X_test_t.to(device))
        test_preds_bin = (test_preds >= 0.5).float()
        correct = (test_preds_bin == y_test_t.to(device)).sum().item()
        accuracy = correct / len(y_test_t)

        latest_x = torch.tensor(X[-1], dtype=torch.float32).unsqueeze(0).to(device)
        prob = model(latest_x).item()

    pred = 1 if prob >= 0.5 else 0
    confidence = prob if pred == 1 else 1 - prob

    return {
        "direction": "상승" if pred == 1 else "하락",
        "confidence": round(confidence * 100, 1),
        "accuracy": round(accuracy * 100, 1),
    }


def run_prediction(df: pd.DataFrame, cache_key: str = "") -> dict:
    """Prophet + RF + LightGBM + LSTM + Transformer 예측을 실행하고 결과를 반환한다.

    Args:
        df: OHLCV + 기술지표 데이터프레임
        cache_key: 캐시 키 (보통 종목 심볼). 지정 시 TTL 캐시를 사용한다.
    """
    if cache_key:
        cached = _prediction_cache.get(cache_key)
        if cached and time.time() - cached[0] < _PREDICTION_CACHE_TTL:
            return cached[1]

    prophet_result = None
    try:
        prophet_forecast = predict_with_prophet(df)
        last_close = df["Close"].iloc[-1]
        predicted = prophet_forecast["yhat"].iloc[-1]
        change_pct = (predicted - last_close) / last_close * 100
        prophet_result = {
            "predicted_price": round(predicted, 2),
            "change_pct": round(change_pct, 2),
            "range": [
                round(prophet_forecast["yhat_lower"].iloc[-1], 2),
                round(prophet_forecast["yhat_upper"].iloc[-1], 2),
            ],
        }
    except Exception as e:
        prophet_result = {"error": str(e)}

    try:
        rf_result = predict_direction(df)
    except Exception as e:
        rf_result = {"error": str(e)}

    try:
        lgbm_result = predict_direction_lgbm(df)
    except Exception as e:
        lgbm_result = {"error": str(e)}

    lstm_result = None
    try:
        lstm_result = predict_direction_lstm(df)
    except Exception as e:
        lstm_result = {"error": str(e)}

    transformer_result = None
    try:
        transformer_result = predict_direction_transformer(df)
    except Exception as e:
        transformer_result = {"error": str(e)}

    result = {
        "prophet": prophet_result,
        "random_forest": rf_result,
        "lightgbm": lgbm_result,
        "lstm": lstm_result,
        "transformer": transformer_result,
        "disclaimer": DISCLAIMER,
    }

    if cache_key:
        _prediction_cache[cache_key] = (time.time(), result)

    return result


def analyze_sentiment(news_items: list[dict]) -> dict:
    """FinBERT를 사용하여 뉴스 기사의 감성을 분석한다.
    
    Args:
        news_items: [{"title": ..., "summary": ...}, ...]
        
    Returns:
        {"label": "Bullish/Bearish/Neutral", "score": float, "details": [...]}
    """
    if not news_items:
        return {"label": "뉴스 없음", "score": 0.0, "details": []}

    # 텍스트 수집 — FinBERT 로드 전에 먼저 확인
    texts = []
    for item in news_items:
        # FinBERT is trained on English — use original English text when available
        title = item.get("title_en") or item.get("title", "")
        summary = item.get("summary_en") or item.get("summary", "")
        text = ". ".join(filter(None, [title, summary]))
        if text:
            texts.append((item, text[:512]))  # truncate to 512 chars

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
            # ProsusAI/finbert labels: positive, negative, neutral
            res = sentiment_pipeline(text)[0]
            label = res["label"]
            conf = res["score"]
            
            if label == "positive":
                score = conf
                kor_label = "긍정"
            elif label == "negative":
                score = -conf
                kor_label = "부정"
            else:
                score = 0
                kor_label = "중립"
                
            total_score += score
            valid_count += 1
            
            results.append({
                "title": item.get("title", ""),
                "label": kor_label,
                "confidence": round(conf * 100, 1)
            })
            
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

    return {
        "label": overall_label,
        "score": round(avg_score, 3),
        "details": results
    }
