import pandas as pd
import numpy as np
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


def predict_direction(df: pd.DataFrame) -> dict:
    """Random Forest로 다음 날 상승/하락을 분류한다.

    기술적 지표를 피처로 사용한다.
    """
    features = ["MA5", "MA20", "RSI", "MACD", "MACD_Hist", "BB_Upper", "BB_Lower"]
    data = df.dropna(subset=features).copy()

    if len(data) < 30:
        return {"direction": "데이터 부족", "confidence": 0.0}

    data["Target"] = (data["Close"].shift(-1) > data["Close"]).astype(int)
    data = data.dropna()

    X = data[features].values
    y = data["Target"].values

    split = int(len(X) * 0.8)
    X_train, X_test = X[:split], X[split:]
    y_train, y_test = y[:split], y[split:]

    clf = RandomForestClassifier(n_estimators=100, random_state=42)
    clf.fit(X_train, y_train)

    accuracy = clf.score(X_test, y_test) if len(X_test) > 0 else 0.0

    latest = X[-1].reshape(1, -1)
    proba = clf.predict_proba(latest)[0]
    pred = clf.predict(latest)[0]

    return {
        "direction": "상승" if pred == 1 else "하락",
        "confidence": round(float(max(proba)) * 100, 1),
        "accuracy": round(accuracy * 100, 1),
    }


def predict_direction_lgbm(df: pd.DataFrame) -> dict:
    """LightGBM으로 다음 날 상승/하락을 분류한다."""
    features = ["MA5", "MA20", "RSI", "MACD", "MACD_Hist", "BB_Upper", "BB_Lower"]
    data = df.dropna(subset=features).copy()

    if len(data) < 30:
        return {"direction": "데이터 부족", "confidence": 0.0}

    data["Target"] = (data["Close"].shift(-1) > data["Close"]).astype(int)
    data = data.dropna()

    X = data[features].values
    y = data["Target"].values

    split = int(len(X) * 0.8)
    X_train, X_test = X[:split], X[split:]
    y_train, y_test = y[:split], y[split:]

    clf = LGBMClassifier(n_estimators=100, random_state=42, verbose=-1)
    clf.fit(X_train, y_train)

    accuracy = clf.score(X_test, y_test) if len(X_test) > 0 else 0.0

    latest = X[-1].reshape(1, -1)
    proba = clf.predict_proba(latest)[0]
    pred = clf.predict(latest)[0]

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

    features = ["MA5", "MA20", "RSI", "MACD", "MACD_Hist", "BB_Upper", "BB_Lower"]
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


def run_prediction(df: pd.DataFrame) -> dict:
    """Prophet + RF 예측을 실행하고 결과를 반환한다."""
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

    return {
        "prophet": prophet_result,
        "random_forest": rf_result,
        "lightgbm": lgbm_result,
        "lstm": lstm_result,
        "disclaimer": DISCLAIMER,
    }
