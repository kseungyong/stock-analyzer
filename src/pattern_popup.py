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
import matplotlib.dates as mdates
import pandas as pd
import yfinance as yf
from matplotlib.figure import Figure

from src.report_generator import _detect_korean_font as _detect_korean_font_rg

# 모듈 초기화 시 한글 폰트 설정 — import 순서에 무관하게 항상 실행
matplotlib.rcParams["font.family"] = _detect_korean_font_rg()
matplotlib.rcParams["axes.unicode_minus"] = False

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


def _to_compatible_ts(date_str: str, ref_index: pd.DatetimeIndex) -> pd.Timestamp:
    """Parse date_str to Timestamp matching ref_index's tz (or naive if ref naive)."""
    ts = pd.to_datetime(date_str)
    if ref_index.tz is not None:
        # ts is naive — localize to ref tz
        if ts.tz is None:
            ts = ts.tz_localize(ref_index.tz)
        else:
            ts = ts.tz_convert(ref_index.tz)
    # else: ref is naive, ts can stay naive
    return ts


def _render_chart(ohlc: pd.DataFrame, detection: dict) -> str:
    """OHLC + detection → matplotlib chart → base64 PNG."""
    # Use Figure() directly to avoid pyplot global state (thread-safe for gunicorn)
    fig = Figure(figsize=(8, 4), dpi=100)
    ax = fig.subplots()
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
                    d = _to_compatible_ts(pt["date"], dates)
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
                d1 = _to_compatible_ts(from_d, dates)
                d2 = _to_compatible_ts(to_d, dates)
                ax.axvspan(d1, d2, alpha=0.15, color="#FBBF24")
            except Exception as e:
                logger.debug("range box 실패: %s", e)
        # Fix 5: 현재 가격 우상단 표시 (spec §4.3)
        current_price = detection.get("current")
        if current_price is not None:
            ax.text(0.98, 0.97, f"현재 {current_price:,}",
                    transform=ax.transAxes, fontsize=9, fontweight="bold",
                    ha="right", va="top",
                    bbox=dict(boxstyle="round,pad=0.3", facecolor="#eff6ff", edgecolor="#1E40AF"))
    elif kind == "candle":
        # candle pattern: vertical line at detection date
        det_date = detection.get("date")
        if det_date:
            try:
                d = _to_compatible_ts(det_date, dates)
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
    # No plt.close() needed — Figure() is not registered with pyplot state machine
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
