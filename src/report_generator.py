from __future__ import annotations

import base64
import html
import io
from datetime import datetime
from pathlib import Path

from src import prediction_history

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as _fm
import matplotlib.dates as mdates
import pandas as pd

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

# CSS를 파일에서 로드한다. 파일이 없으면 빈 문자열을 사용한다.
def _load_css() -> str:
    css_path = _TEMPLATES_DIR / "report.css"
    try:
        return css_path.read_text(encoding="utf-8")
    except OSError:
        return ""

_REPORT_CSS = _load_css()


# 크로스 플랫폼 한글 폰트 자동 감지
def _detect_korean_font() -> str:
    available = {f.name for f in _fm.fontManager.ttflist}
    for font in ("AppleGothic", "NanumGothic", "Malgun Gothic", "NanumBarunGothic", "UnDotum"):
        if font in available:
            return font
    return "DejaVu Sans"  # fallback — 한글 깨질 수 있으나 오류 없음

plt.rcParams["font.family"] = _detect_korean_font()
plt.rcParams["axes.unicode_minus"] = False


def _create_chart(df: pd.DataFrame, name: str) -> str:
    """가격 + 기술지표 차트를 생성하고 base64 인코딩된 이미지를 반환한다."""
    fig, axes = plt.subplots(3, 1, figsize=(10, 8), height_ratios=[3, 1, 1])
    fig.suptitle(f"{name} Technical Analysis", fontsize=14, fontweight="bold")

    plot_df = df.tail(60).copy()
    dates = plot_df.index

    # 가격 + 이동평균 + 볼린저밴드
    ax = axes[0]
    ax.plot(dates, plot_df["Close"], label="Close", linewidth=1.5)
    for ma, color in [("MA5", "#ff7f0e"), ("MA20", "#2ca02c"), ("MA60", "#d62728")]:
        if ma in plot_df.columns:
            ax.plot(dates, plot_df[ma], label=ma, linewidth=0.8, color=color)
    if "BB_Upper" in plot_df.columns:
        ax.fill_between(dates, plot_df["BB_Lower"], plot_df["BB_Upper"],
                        alpha=0.1, color="gray", label="Bollinger")
    ax.legend(fontsize=7, loc="upper left")
    ax.set_ylabel("Price")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))

    # RSI
    ax = axes[1]
    if "RSI" in plot_df.columns:
        ax.plot(dates, plot_df["RSI"], color="purple", linewidth=1)
        ax.axhline(70, color="red", linewidth=0.5, linestyle="--")
        ax.axhline(30, color="green", linewidth=0.5, linestyle="--")
        ax.set_ylabel("RSI")
        ax.set_ylim(0, 100)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))

    # MACD
    ax = axes[2]
    if "MACD" in plot_df.columns:
        ax.plot(dates, plot_df["MACD"], label="MACD", linewidth=1)
        ax.plot(dates, plot_df["MACD_Signal"], label="Signal", linewidth=1)
        colors = ["green" if v >= 0 else "red" for v in plot_df["MACD_Hist"]]
        ax.bar(dates, plot_df["MACD_Hist"], color=colors, alpha=0.5, width=0.8)
        ax.legend(fontsize=7, loc="upper left")
        ax.set_ylabel("MACD")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))

    plt.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100)
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode()


def _signal_color(signal: str) -> str:
    return {"매수": "#28a745", "매도": "#dc3545"}.get(signal, "#6c757d")


def _signal_class(signal: str) -> str:
    return {"매수": "signal-buy", "매도": "signal-sell"}.get(signal, "signal-hold")


def _render_indicators_table(indicators: list[dict]) -> str:
    """기술 지표 테이블 HTML을 반환한다."""
    if not indicators:
        return ""
    rows = "".join(
        f"<tr><td class='label'>{ind['name']}</td>"
        f"<td>{ind['value']}</td>"
        f"<td>{ind['comment']}</td></tr>"
        for ind in indicators
    )
    return (
        "<table class='analysis-table'>"
        "<tr><th>지표</th><th>값</th><th>의견</th></tr>"
        f"{rows}</table>"
    )


def _render_ml_table(pred: dict) -> str:
    """ML 예측 결과 테이블 HTML을 반환한다."""
    prophet = pred.get("prophet", {})
    rf = pred.get("random_forest", {})
    lgbm = pred.get("lightgbm", {})
    lstm = pred.get("lstm", {})
    transformer = pred.get("transformer", {})

    rows = ""
    if prophet and "error" not in prophet:
        rows += (
            f"<tr><td class='label'>Prophet</td>"
            f"<td>{prophet['predicted_price']} ({prophet['change_pct']:+.1f}%)</td>"
            f"<td>7일 후 예측</td></tr>"
        )
    rows += (
        f"<tr><td class='label'>Random Forest</td>"
        f"<td>{rf.get('direction', 'N/A')}</td>"
        f"<td>신뢰도 {rf.get('confidence', 0)}%</td></tr>"
    )
    rows += (
        f"<tr><td class='label'>LightGBM</td>"
        f"<td>{lgbm.get('direction', 'N/A')}</td>"
        f"<td>신뢰도 {lgbm.get('confidence', 0)}%</td></tr>"
    )
    if lstm and "error" not in lstm:
        rows += (
            f"<tr><td class='label'>LSTM</td>"
            f"<td>{lstm.get('direction', 'N/A')}</td>"
            f"<td>신뢰도 {lstm.get('confidence', 0)}%</td></tr>"
        )
    else:
        rows += "<tr><td class='label'>LSTM</td><td colspan='2'>예측 불가</td></tr>"

    if transformer and "error" not in transformer:
        rows += (
            f"<tr><td class='accent'>Transformer (Advanced)</td>"
            f"<td>{transformer.get('direction', 'N/A')}</td>"
            f"<td>신뢰도 {transformer.get('confidence', 0)}%</td></tr>"
        )
    else:
        rows += "<tr><td class='accent'>Transformer (Advanced)</td><td colspan='2'>예측 불가</td></tr>"

    return (
        "<table class='analysis-table'>"
        "<tr><th>모델</th><th>예측</th><th>비고</th></tr>"
        f"{rows}</table>"
    )


def _render_hit_rate_section(symbol: str) -> str:
    """모델별 누적 hit rate를 표 형태로 렌더링. 데이터 없으면 빈 문자열."""
    rates = prediction_history.hit_rate_by_model(symbol, source="live")
    if not rates:
        return ""

    rows = []
    model_label = {
        "rf": "RandomForest", "lgbm": "LightGBM",
        "lstm": "LSTM", "transformer": "Transformer", "ensemble": "Ensemble",
    }
    for model_key in ("rf", "lgbm", "lstm", "transformer", "ensemble"):
        info = rates.get(model_key)
        if not info:
            continue
        rate_pct = info["hit_rate"] * 100
        n = info["n"]
        if n < 10:
            display = f'<span style="color:#999;">데이터 부족 (n={n})</span>'
        else:
            display = f"{rate_pct:.1f}% (n={n})"
        rows.append(f"<tr><td>{model_label[model_key]}</td><td>{display}</td></tr>")

    if not rows:
        return ""

    return (
        '<h4 class="section-title">📊 누적 적중률 (live tracking)</h4>'
        '<table class="analysis-table">'
        '<tr><th>모델</th><th>Hit Rate</th></tr>'
        f"{''.join(rows)}</table>"
    )


def _render_news(news_items: list[dict]) -> str:
    """뉴스 목록 HTML을 반환한다."""
    if not news_items:
        return ""
    items_html = ""
    for n in news_items:
        title = n.get("title", "")
        link = n.get("link", "")
        publisher = n.get("publisher", "")
        safe_title = html.escape(title)
        safe_summary = html.escape(n.get("summary", ""))
        safe_publisher = html.escape(publisher)
        pub_tag = f' <span class="news-publisher">— {safe_publisher}</span>' if safe_publisher else ""
        summary_tag = f'<span class="news-summary">{safe_summary}</span>' if safe_summary else ""
        safe_link = link if link.startswith(("http://", "https://")) else ""
        if safe_link:
            items_html += (
                f'<li class="news-item">'
                f'<a class="news-link" href="{safe_link}" target="_blank">{safe_title}</a>'
                f'{pub_tag}{summary_tag}</li>'
            )
        else:
            items_html += f'<li class="news-item">{safe_title}{pub_tag}{summary_tag}</li>'
    return f'<h4 class="section-title">관련 뉴스</h4><ul class="news-list">{items_html}</ul>'


def _render_sentiment(sentiment: dict) -> str:
    """감성 분석 결과 HTML을 반환한다."""
    if not sentiment:
        return ""
    if "error" in sentiment:
        return f'<p class="sentiment-error">[감성 분석 오류] {sentiment["error"]}</p>'
    sent_label = sentiment.get("label", "N/A")
    sent_score = sentiment.get("score", 0.0)
    sent_color = "#2ca02c" if "긍정" in sent_label else ("#dc3545" if "부정" in sent_label else "#666")
    return (
        f'<div class="sentiment-box" style="border-left-color:{sent_color}; color:{sent_color};">'
        f'<h4>뉴스 감성 분석 (FinBERT)</h4>'
        f'<p>종합 의견: <strong>{sent_label}</strong> '
        f'<span class="sentiment-score">(점수: {sent_score:+.3f})</span></p>'
        f'</div>'
    )


_STAGE_LABEL = {
    "market_open": "장중",
    "after_close": "마감 후",
    "before_open": "장 시작 전",
    "weekend": "주말",
}


def _norm_zero(x: float) -> float:
    """-0.0을 +0.0으로 정규화. float 연산이 -0.0을 만들 수 있어 클래스(flat)와 표시(-0.00%)가 어긋나는 것을 방지."""
    return 0.0 if x == 0 else x


def _rel_perf_class(value: float) -> str:
    if value > 0:
        return "up"
    if value < 0:
        return "down"
    return "flat"


def _render_rel_perf(rel_perf: dict | None) -> str:
    """종목 vs 시장 인덱스 등락률 한 줄 렌더. None이면 빈 문자열."""
    if not rel_perf:
        return ""
    stock_pct = _norm_zero(rel_perf["stock_pct"])
    index_pct = _norm_zero(rel_perf["index_pct"])
    alpha_pp = _norm_zero(rel_perf["alpha_pp"])
    index_name = html.escape(rel_perf["index_name"])
    as_of = html.escape(rel_perf.get("as_of", ""))
    stage = _STAGE_LABEL.get(rel_perf.get("stage", ""), "")
    asof_suffix = f"{as_of}, {stage}" if stage else as_of

    return (
        '<p class="rel-perf">'
        f'금일: <span class="{_rel_perf_class(stock_pct)}">{stock_pct:+.2f}%</span>'
        f' │ {index_name}: <span class="{_rel_perf_class(index_pct)}">{index_pct:+.2f}%</span>'
        f' │ 알파: <span class="{_rel_perf_class(alpha_pp)}">{alpha_pp:+.2f}%pp</span>'
        f' <span class="rel-perf-asof">({asof_suffix})</span>'
        '</p>'
    )


def _render_stock_card(item: dict) -> str:
    """종목 분석 카드 HTML을 반환한다."""
    name = html.escape(item["name"])
    symbol_esc = html.escape(item["symbol"])
    sig = item["signal"]
    pred = item["prediction"]
    chart_b64 = _create_chart(item["df"], item["name"])

    signal_cls = _signal_class(sig["signal"])
    indicators_html = _render_indicators_table(sig.get("indicators", []))
    ml_html = _render_ml_table(pred)
    hit_rate_html = _render_hit_rate_section(item["symbol"])
    news_html = _render_news(item.get("news", []))
    sentiment_html = _render_sentiment(item.get("sentiment", {}))
    rel_perf_html = _render_rel_perf(item.get("rel_perf"))

    return f"""
    <div class="stock-card">
        <h3>{name} ({symbol_esc})</h3>
        <p class="stock-summary">현재가: <b>{sig['close']:,.2f}</b> | RSI: {sig['rsi']}
           | <span class="{signal_cls}">{sig['signal']}</span> (점수: {sig['score']})</p>
        <p class="stock-reasons">{', '.join(sig['reasons']) if sig['reasons'] else '특이사항 없음'}</p>
        {rel_perf_html}
        {sentiment_html}
        <h4 class="section-title">기술 지표 분석</h4>
        {indicators_html}
        <h4 class="section-title">ML 예측</h4>
        {ml_html}
        {hit_rate_html}
        <img class="stock-chart" src="data:image/png;base64,{chart_b64}" alt="{name} chart"/>
        {news_html}
    </div>"""


def generate_report(analyses: list[dict]) -> str:
    """HTML 리포트를 생성한다.

    Args:
        analyses: [{name, symbol, df, signal, prediction}, ...]

    Returns:
        HTML 문자열
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    cards = "".join(_render_stock_card(item) for item in analyses)
    disclaimer = analyses[-1]["prediction"].get("disclaimer", "") if analyses else ""

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Stock Report {now}</title>
<style>{_REPORT_CSS}</style>
</head>
<body>
<h1>주식 시장 분석 리포트</h1>
<p class="generated-at">생성: {now}</p>
{cards}
<p class="disclaimer">{disclaimer}</p>
</body>
</html>"""
