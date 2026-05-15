"""실제 차트 탭 데이터 빌더 — matplotlib 으로 패턴 마킹 차트 생성."""
from __future__ import annotations

import base64
import io
import logging
import time
from collections import OrderedDict
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# 메모리 LRU cache — key: (symbol, pattern, date), value: (result_dict, expires_at)
_chart_cache: OrderedDict[tuple[str, str, str | None], tuple[dict, float]] = OrderedDict()
_CACHE_MAX = 128
_CACHE_TTL = 3600  # 1 hour


def _fetch_ohlc(symbol: str) -> pd.DataFrame:
    """yfinance 60일 OHLC fetch. 실패 시 빈 DF."""
    try:
        df = yf.Ticker(symbol).history(period="60d")
        return df if df is not None else pd.DataFrame()
    except Exception as e:
        logger.warning("yfinance fetch %s 실패: %s", symbol, e)
        return pd.DataFrame()


def find_detection(
    pattern_json: dict, pattern_name: str, date: str | None
) -> dict | None:
    """pattern_json 안에서 (pattern_name, date) 검출 row 찾기.

    date 미지정 시: 가장 최근 (to_date 또는 date 기준).
    """
    candidates: list[dict] = []
    for cp in pattern_json.get("chart_patterns") or []:
        if cp.get("name") == pattern_name:
            candidates.append({**cp, "_kind": "chart", "_sort_date": cp.get("to_date", "")})
    for c in pattern_json.get("candles") or []:
        if c.get("name") == pattern_name:
            candidates.append({**c, "_kind": "candle", "_sort_date": c.get("date", "")})
    if not candidates:
        return None
    if date is None:
        candidates.sort(key=lambda x: x["_sort_date"], reverse=True)
        return candidates[0]
    for c in candidates:
        if c["_sort_date"] == date:
            return c
    return None


def _infer_signal(detection: dict) -> str | None:
    """chart pattern 에서 signal 추론 (저장된 signal 없을 때)."""
    sig = detection.get("signal")
    if sig:
        return sig
    if detection.get("breakout"):
        return "매수"
    if detection.get("breakdown"):
        return "매도"
    return None


def _is_chart_kind(detection: dict) -> bool:
    """_kind 필드 없이도 chart pattern 인지 판별."""
    if detection.get("_kind") == "chart":
        return True
    if detection.get("_kind") == "candle":
        return False
    # heuristic: chart patterns have structural coordinate keys
    chart_keys = {"low1", "low2", "high1", "high2", "left_shoulder", "head", "right_shoulder",
                  "neckline", "from_date", "to_date", "breakout", "breakdown"}
    return bool(chart_keys & set(detection.keys()))


def _build_caption(detection: dict) -> str:
    """검출 dict → 사람이 읽을 caption."""
    name = detection.get("name", "")
    if _is_chart_kind(detection):
        if name == "더블바텀(W)":
            l1 = detection.get("low1", {})
            l2 = detection.get("low2", {})
            neck = detection.get("neckline")
            br = "돌파" if detection.get("breakout") else "미돌파"
            return (
                f"저점1 {l1.get('date','')} {l1.get('price','?'):,} → "
                f"저점2 {l2.get('date','')} {l2.get('price','?'):,} "
                f"(넥라인 {neck:,} {br})"
            ) if l1 and l2 else detection.get("details", name)
        if name == "더블탑(M)":
            h1 = detection.get("high1", {})
            h2 = detection.get("high2", {})
            neck = detection.get("neckline")
            br = "이탈" if detection.get("breakdown") else "유지"
            return (
                f"고점1 {h1.get('date','')} {h1.get('price','?'):,} → "
                f"고점2 {h2.get('date','')} {h2.get('price','?'):,} "
                f"(넥라인 {neck:,} {br})"
            ) if h1 and h2 else detection.get("details", name)
        if name in ("헤드앤숄더", "역헤드앤숄더"):
            ls = detection.get("left_shoulder", {})
            h = detection.get("head", {})
            rs = detection.get("right_shoulder", {})
            return (
                f"좌어깨 {ls.get('date','')} {ls.get('price','?'):,} / "
                f"헤드 {h.get('date','')} {h.get('price','?'):,} / "
                f"우어깨 {rs.get('date','')} {rs.get('price','?'):,}"
            ) if ls and h and rs else detection.get("details", name)
        return detection.get("details", name)
    # candle
    date_str = detection.get("date", "")
    signal = detection.get("signal", "")
    return f"{date_str} — {name} ({signal})"


def _render_chart(ohlc: pd.DataFrame, detection: dict) -> str:
    """OHLC + detection → matplotlib chart → base64 PNG."""
    fig, ax = plt.subplots(figsize=(8, 4), dpi=100)
    dates = ohlc.index
    ax.plot(dates, ohlc["Close"], color="#1E40AF", linewidth=1.5, label="Close")

    kind = detection.get("_kind")
    if kind == "chart":
        # chart pattern markers
        for coord_key, color_label in [
            ("low1", ("#16A34A", "저점1")),
            ("low2", ("#16A34A", "저점2")),
            ("high1", ("#DC2626", "고점1")),
            ("high2", ("#DC2626", "고점2")),
            ("left_shoulder", ("#7C3AED", "좌어깨")),
            ("head", ("#7C3AED", "헤드")),
            ("right_shoulder", ("#7C3AED", "우어깨")),
        ]:
            pt = detection.get(coord_key)
            if pt and pt.get("date") and pt.get("price") is not None:
                try:
                    d = pd.to_datetime(pt["date"]).tz_localize(dates.tz) if dates.tz else pd.to_datetime(pt["date"])
                    ax.plot(d, pt["price"], "o", color=color_label[0], markersize=8)
                    ax.annotate(color_label[1], (d, pt["price"]),
                                textcoords="offset points", xytext=(5, 5), fontsize=8)
                except Exception as e:
                    logger.debug("marker %s 실패: %s", coord_key, e)
        # neckline
        neck = detection.get("neckline")
        if neck is not None:
            ax.axhline(neck, color="#999", linestyle="--", linewidth=1)
        # detection range box
        from_d = detection.get("from_date")
        to_d = detection.get("to_date")
        if from_d and to_d:
            try:
                d1 = pd.to_datetime(from_d).tz_localize(dates.tz) if dates.tz else pd.to_datetime(from_d)
                d2 = pd.to_datetime(to_d).tz_localize(dates.tz) if dates.tz else pd.to_datetime(to_d)
                ax.axvspan(d1, d2, alpha=0.15, color="#FBBF24")
            except Exception as e:
                logger.debug("range box 실패: %s", e)
    elif kind == "candle":
        # candle pattern: vertical line at detection date
        det_date = detection.get("date")
        if det_date:
            try:
                d = pd.to_datetime(det_date).tz_localize(dates.tz) if dates.tz else pd.to_datetime(det_date)
                ax.axvline(d, color="#DC2626" if detection.get("signal") == "매도" else "#16A34A",
                           linestyle="--", linewidth=1.5)
                # 라벨
                price_at = ohlc.loc[ohlc.index >= d, "Close"]
                if not price_at.empty:
                    ax.annotate(f"↓ {detection.get('name','')}", (d, price_at.iloc[0]),
                                textcoords="offset points", xytext=(5, 15), fontsize=9,
                                fontweight="bold",
                                color="#DC2626" if detection.get("signal") == "매도" else "#16A34A")
            except Exception as e:
                logger.debug("candle marker 실패: %s", e)

    ax.set_xlabel("Date")
    ax.set_ylabel("Price")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
    ax.grid(True, alpha=0.3)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100)
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode()


def build_actual_chart(
    symbol: str, pattern: str, date: str | None, pattern_json: dict
) -> dict[str, Any]:
    """모달의 "실제 차트" 탭 데이터 빌드.

    Returns:
        {"chart_b64": str|None, "caption": str, "signal_at_detection": str|None,
         "symbol": str, "pattern": str, "date": str|None}
    """
    cache_key = (symbol, pattern, date)
    now = time.time()

    # cache check
    cached = _chart_cache.get(cache_key)
    if cached is not None:
        result, expires_at = cached
        if expires_at > now:
            _chart_cache.move_to_end(cache_key)
            return result
        del _chart_cache[cache_key]

    detection = find_detection(pattern_json, pattern, date)
    if detection is None:
        return {
            "chart_b64": None, "caption": "검출 데이터 없음",
            "signal_at_detection": None, "symbol": symbol, "pattern": pattern, "date": date,
        }

    sig = _infer_signal(detection)
    ohlc = _fetch_ohlc(symbol)
    if ohlc.empty:
        result = {
            "chart_b64": None, "caption": "차트 데이터 없음 — yfinance fetch 실패",
            "signal_at_detection": sig,
            "symbol": symbol, "pattern": pattern, "date": date,
        }
    else:
        try:
            png_b64 = _render_chart(ohlc, detection)
            result = {
                "chart_b64": png_b64,
                "caption": _build_caption(detection),
                "signal_at_detection": sig,
                "symbol": symbol, "pattern": pattern, "date": date,
            }
        except Exception as e:
            logger.exception("차트 렌더 실패 %s %s: %s", symbol, pattern, e)
            result = {
                "chart_b64": None, "caption": "차트 데이터 없음 — 렌더 실패",
                "signal_at_detection": sig,
                "symbol": symbol, "pattern": pattern, "date": date,
            }

    # cache store + LRU eviction
    _chart_cache[cache_key] = (result, now + _CACHE_TTL)
    if len(_chart_cache) > _CACHE_MAX:
        _chart_cache.popitem(last=False)
    return result
