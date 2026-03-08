import pandas as pd
import ta


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """기술적 분석 지표를 계산한다.

    이동평균(5/20/60일), RSI, MACD, 볼린저밴드를 추가한다.
    """
    close = df["Close"]

    # 이동평균선
    df["MA5"] = close.rolling(5).mean()
    df["MA20"] = close.rolling(20).mean()
    df["MA60"] = close.rolling(60).mean()

    # RSI
    df["RSI"] = ta.momentum.rsi(close, window=14)

    # MACD
    macd = ta.trend.MACD(close)
    df["MACD"] = macd.macd()
    df["MACD_Signal"] = macd.macd_signal()
    df["MACD_Hist"] = macd.macd_diff()

    # 볼린저 밴드
    bb = ta.volatility.BollingerBands(close, window=20)
    df["BB_Upper"] = bb.bollinger_hband()
    df["BB_Middle"] = bb.bollinger_mavg()
    df["BB_Lower"] = bb.bollinger_lband()

    # 거래량 이동평균 (20일)
    df["Volume_MA20"] = df["Volume"].rolling(20).mean()
    # 거래량 비율: 오늘 거래량 / 20일 평균
    df["Volume_Ratio"] = df["Volume"] / df["Volume_MA20"]

    return df


def generate_signal(df: pd.DataFrame) -> dict:
    """최신 데이터 기반 매수/매도/관망 시그널을 생성한다."""
    latest = df.dropna().iloc[-1]
    prev = df.dropna().iloc[-2]
    score = 0
    reasons = []
    indicators = []

    # RSI 시그널
    rsi_val = latest["RSI"]
    if rsi_val < 30:
        score += 2
        reasons.append("RSI 과매도")
        rsi_comment = "과매도 구간 — 반등 가능성"
    elif rsi_val < 40:
        rsi_comment = "약세 구간"
    elif rsi_val <= 60:
        rsi_comment = "중립 구간"
    elif rsi_val <= 70:
        rsi_comment = "강세 구간"
    else:
        score -= 2
        reasons.append("RSI 과매수")
        rsi_comment = "과매수 구간 — 조정 가능성"
    indicators.append({"name": "RSI", "value": round(rsi_val, 1), "comment": rsi_comment})

    # MACD 시그널
    hist = latest["MACD_Hist"]
    prev_hist = prev["MACD_Hist"]
    if hist > 0 and prev_hist <= 0:
        score += 2
        reasons.append("MACD 골든크로스")
        macd_comment = "골든크로스 — 상승 전환 신호"
    elif hist < 0 and prev_hist >= 0:
        score -= 2
        reasons.append("MACD 데드크로스")
        macd_comment = "데드크로스 — 하락 전환 신호"
    elif hist > 0 and hist > prev_hist:
        macd_comment = "상승 모멘텀 강화"
    elif hist > 0 and hist <= prev_hist:
        macd_comment = "상승 모멘텀 약화"
    elif hist < 0 and abs(hist) < abs(prev_hist):
        macd_comment = "하락 모멘텀 약화"
    elif hist < 0 and abs(hist) >= abs(prev_hist):
        macd_comment = "하락 모멘텀 강화"
    else:
        macd_comment = "중립"
    indicators.append({"name": "MACD", "value": round(hist, 4), "comment": macd_comment})

    # 이동평균 시그널
    close = latest["Close"]
    ma5 = latest["MA5"]
    ma20 = latest["MA20"]
    ma60 = latest["MA60"]
    if close > ma5 > ma20 > ma60:
        score += 1
        reasons.append("정배열")
        ma_comment = "강한 정배열 — 상승 추세"
        ma_value = "강한 정배열"
    elif close > ma20 > ma60:
        score += 1
        reasons.append("정배열")
        ma_comment = "정배열 — 상승 추세"
        ma_value = "정배열"
    elif close < ma20 < ma60:
        score -= 1
        reasons.append("역배열")
        ma_comment = "역배열 — 하락 추세"
        ma_value = "역배열"
    else:
        ma_comment = "혼조세"
        ma_value = "혼조"
    indicators.append({"name": "이동평균", "value": ma_value, "comment": ma_comment})

    # 볼린저 밴드
    if close >= latest["BB_Upper"]:
        score -= 1
        reasons.append("볼린저 상단 터치")
        bb_comment = "상단 돌파 — 과매수 또는 강한 상승"
        bb_value = "상단"
    elif close <= latest["BB_Lower"]:
        score += 1
        reasons.append("볼린저 하단 터치")
        bb_comment = "하단 터치 — 과매도 또는 강한 하락"
        bb_value = "하단"
    elif close > latest["BB_Middle"]:
        bb_comment = "밴드 상단 부근 — 상대적 강세"
        bb_value = "중상단"
    else:
        bb_comment = "밴드 하단 부근 — 상대적 약세"
        bb_value = "중하단"
    indicators.append({"name": "볼린저밴드", "value": bb_value, "comment": bb_comment})

    # 거래량 시그널
    vol_ratio = latest.get("Volume_Ratio", float("nan"))
    if pd.notna(vol_ratio):
        vol_ratio = float(vol_ratio)
        if vol_ratio >= 2.0:
            vol_comment = f"급등 (평균 대비 {vol_ratio:.1f}배) — 강한 추세 가능성"
            if score > 0:
                score += 1
                reasons.append("거래량 급증")
        elif vol_ratio >= 1.5:
            vol_comment = f"증가 (평균 대비 {vol_ratio:.1f}배)"
        elif vol_ratio <= 0.5:
            vol_comment = f"감소 (평균 대비 {vol_ratio:.1f}배) — 추세 약화 가능성"
        else:
            vol_comment = f"보통 ({vol_ratio:.1f}배)"
        indicators.append({"name": "거래량", "value": f"{vol_ratio:.1f}배", "comment": vol_comment})

    if score >= 2:
        signal = "매수"
    elif score <= -2:
        signal = "매도"
    else:
        signal = "관망"

    return {
        "signal": signal,
        "score": score,
        "reasons": reasons,
        "indicators": indicators,
        "rsi": round(rsi_val, 1),
        "macd_hist": round(hist, 4),
        "close": round(close, 2),
    }
