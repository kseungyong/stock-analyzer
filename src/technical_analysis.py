import logging
import time

import pandas as pd
import ta

logger = logging.getLogger(__name__)


_MARKET_INDEX = {
    "korea":  "^KS11",   # KOSPI
    "kosdaq": "^KQ11",   # KOSDAQ
    "us":     "^GSPC",   # S&P 500
}

_market_cache: dict = {}  # {index: (df, cached_at_unix)}
_MARKET_CACHE_TTL = 15 * 60  # 15분


def resolve_index_market(symbol: str) -> tuple[str, str]:
    """심볼 suffix로 (지수 표시명, market_key) 반환.

    market_key는 _MARKET_INDEX의 키 — fetch_market_df()에 그대로 전달된다.

    예:
        '005930.KS' -> ('KOSPI', 'korea')
        '247540.KQ' -> ('KOSDAQ', 'kosdaq')
        'AAPL'      -> ('S&P 500', 'us')
    """
    if symbol.endswith(".KS"):
        return ("KOSPI", "korea")
    if symbol.endswith(".KQ"):
        return ("KOSDAQ", "kosdaq")
    return ("S&P 500", "us")


def fetch_market_df(market: str):
    """시장 인덱스 데이터 fetch + 15분 TTL 메모리 캐시.

    market: "korea" | "kosdaq" | "us". 그 외/None/fetch 실패 시 None.

    Returns: pd.DataFrame | None
    """
    index = _MARKET_INDEX.get(market)
    if not index:
        return None
    cached = _market_cache.get(index)
    if cached and (time.time() - cached[1] < _MARKET_CACHE_TTL):
        return cached[0]
    try:
        # 함수 안 import 로 순환 의존 회피
        from src.data_fetcher import fetch_stock_data as _fetch
        df = _fetch(index)
        df = compute_indicators(df)
        _market_cache[index] = (df, time.time())
        return df
    except Exception as e:
        logger.warning("시장 데이터 fetch 실패 (%s): %s", index, e)
        # stale 데이터 정리
        _market_cache.pop(index, None)
        return None


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

    # Stochastic Oscillator
    stoch = ta.momentum.StochasticOscillator(df["High"], df["Low"], close)
    df["Stoch_K"] = stoch.stoch()
    df["Stoch_D"] = stoch.stoch_signal()

    # ATR (비율로 정규화)
    atr = ta.volatility.AverageTrueRange(df["High"], df["Low"], close)
    df["ATR_pct"] = atr.average_true_range() / close

    # OBV 변화율
    df["OBV_Change"] = ta.volume.on_balance_volume(close, df["Volume"]).pct_change()

    # Williams %R
    df["Williams_R"] = ta.momentum.WilliamsRIndicator(df["High"], df["Low"], close).williams_r()

    # CCI
    df["CCI"] = ta.trend.CCIIndicator(df["High"], df["Low"], close).cci()

    # 수익률
    df["Return_1d"] = close.pct_change(1)
    df["Return_5d"] = close.pct_change(5)
    df["Return_20d"] = close.pct_change(20)

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


def generate_bnf_signal(df: pd.DataFrame, market_df=None) -> dict:
    """BNF 스타일 매수/매도/관망 시그널 — mean reversion + 시장 패닉 매수.

    점수 항목:
    - MA20 이격율: <= -10% +2, <= -5% +1, >= +7% -1, >= +10% -2
    - RSI: <= 30 +1, >= 70 -1
    - 거래량 ≥2배 + 음봉: +1 (BNF 는 추격 매수 안 함, 양봉은 0)
    - 시장 이격율 (market_df 있을 때): 시장<=-3% AND 종목<=-10% → +1,
                                          시장>=+5% AND 종목>=+7% → -1

    임계값: score >= 2 매수, <= -2 매도, 그 외 관망.

    Args:
        df: 종목 OHLCV + 기술지표 (compute_indicators 적용 완료)
        market_df: 시장 인덱스 OHLCV + 기술지표 (None 이면 시장 점수 항목 0)

    Returns:
        {
            "signal": "매수"|"매도"|"관망",
            "score": int,
            "reasons": [...],
            "indicators": [...],
            "disparity": float,  # 종목 MA20 이격율 %
            "market_disparity": float | None,  # 시장 이격율 %
        }
    """
    latest = df.iloc[-1]
    score = 0
    reasons: list[str] = []
    indicators: list[dict] = []

    close = float(latest["Close"])
    ma20 = float(latest["MA20"])
    disparity = (close - ma20) / ma20 * 100 if pd.notna(ma20) and ma20 != 0 else 0.0

    # 1) MA20 이격율
    if disparity <= -10:
        score += 2
        reasons.append(f"MA20 {disparity:.1f}% 강한 과매도")
        d_comment = "강한 과매도 — 평균회귀 반발 매수 후보"
    elif disparity <= -5:
        score += 1
        reasons.append(f"MA20 {disparity:.1f}% 과매도")
        d_comment = "과매도 — 반발 가능성"
    elif disparity >= 10:
        score -= 2
        reasons.append(f"MA20 +{disparity:.1f}% 강한 과열")
        d_comment = "강한 과열 — 평균회귀 매도 후보"
    elif disparity >= 7:
        score -= 1
        reasons.append(f"MA20 +{disparity:.1f}% 과열")
        d_comment = "과열 — 조정 가능성"
    else:
        d_comment = "이격 적정 범위"
    indicators.append({
        "name": "MA20 이격율", "value": f"{disparity:.1f}%", "comment": d_comment,
    })

    # 2) RSI
    rsi_val = float(latest["RSI"]) if pd.notna(latest.get("RSI")) else 50.0
    if rsi_val <= 30:
        score += 1
        reasons.append(f"RSI {rsi_val:.0f} 과매도")
    elif rsi_val >= 70:
        score -= 1
        reasons.append(f"RSI {rsi_val:.0f} 과매수")
    indicators.append({"name": "RSI", "value": round(rsi_val, 1), "comment": "BNF 보조"})

    # 3) 거래량 + 음봉 (양봉은 0)
    vol_ratio = float(latest.get("Volume_Ratio", 1.0)) if pd.notna(latest.get("Volume_Ratio")) else 1.0
    open_val = float(latest["Open"])
    is_red = close < open_val
    if vol_ratio >= 2.0 and is_red:
        score += 1
        reasons.append(f"거래량 {vol_ratio:.1f}배 음봉 — 패닉 매도 후 반발 가능")
        v_comment = f"급증 음봉 — 반발 매수 후보 ({vol_ratio:.1f}배)"
    elif vol_ratio >= 2.0:
        v_comment = f"급증 양봉 — BNF 는 추격 매수 안 함 ({vol_ratio:.1f}배)"
    else:
        v_comment = f"평이 ({vol_ratio:.1f}배)"
    indicators.append({"name": "거래량+캔들", "value": f"{vol_ratio:.1f}배",
                        "comment": v_comment})

    # 4) 시장 이격율 (옵션)
    market_disparity = None
    if market_df is not None:
        m_latest = market_df.iloc[-1]
        m_close = float(m_latest["Close"])
        m_ma20 = float(m_latest["MA20"])
        if pd.notna(m_ma20) and m_ma20 != 0:
            market_disparity = (m_close - m_ma20) / m_ma20 * 100
            if market_disparity <= -3 and disparity <= -10:
                score += 1
                reasons.append(f"시장 {market_disparity:.1f}% + 종목 패닉")
                m_comment = "시장 패닉 + 종목 과매도 — BNF 매수 강화"
            elif market_disparity >= 5 and disparity >= 7:
                score -= 1
                reasons.append(f"시장 +{market_disparity:.1f}% + 종목 과열")
                m_comment = "시장 과열 + 종목 과열 — 조정 강화"
            else:
                m_comment = f"시장 이격 {market_disparity:.1f}%"
            indicators.append({
                "name": "시장 이격율",
                "value": f"{market_disparity:.1f}%",
                "comment": m_comment,
            })

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
        "disparity": round(disparity, 1),
        "market_disparity": round(market_disparity, 1) if market_disparity is not None else None,
    }
