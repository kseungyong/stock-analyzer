from __future__ import annotations

import base64
import io
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
plt.rcParams["font.family"] = "AppleGothic"
plt.rcParams["axes.unicode_minus"] = False
import matplotlib.dates as mdates
import pandas as pd


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


def generate_report(analyses: list[dict]) -> str:
    """HTML 리포트를 생성한다.

    Args:
        analyses: [{name, symbol, df, signal, prediction}, ...]

    Returns:
        HTML 문자열
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = []

    for item in analyses:
        name = item["name"]
        sig = item["signal"]
        pred = item["prediction"]
        chart_b64 = _create_chart(item["df"], name)

        prophet = pred.get("prophet", {})
        rf = pred.get("random_forest", {})

        # 기술 지표별 의견 테이블
        ind_rows = ""
        for ind in sig.get("indicators", []):
            ind_rows += (
                f"<tr><td style='padding:4px 8px; font-weight:bold;'>{ind['name']}</td>"
                f"<td style='padding:4px 8px;'>{ind['value']}</td>"
                f"<td style='padding:4px 8px;'>{ind['comment']}</td></tr>"
            )
        indicators_html = ""
        if ind_rows:
            indicators_html = (
                "<table style='width:100%; border-collapse:collapse; font-size:0.9em; margin:8px 0;'>"
                "<tr style='background:#f0f0f0;'><th style='padding:4px 8px; text-align:left;'>지표</th>"
                "<th style='padding:4px 8px; text-align:left;'>값</th>"
                "<th style='padding:4px 8px; text-align:left;'>의견</th></tr>"
                f"{ind_rows}</table>"
            )

        # ML 예측 결과 테이블
        ml_rows = ""
        if "error" not in prophet:
            ml_rows += (
                f"<tr><td style='padding:4px 8px; font-weight:bold;'>Prophet</td>"
                f"<td style='padding:4px 8px;'>{prophet['predicted_price']} ({prophet['change_pct']:+.1f}%)</td>"
                f"<td style='padding:4px 8px;'>7일 후 예측</td></tr>"
            )
        ml_rows += (
            f"<tr><td style='padding:4px 8px; font-weight:bold;'>Random Forest</td>"
            f"<td style='padding:4px 8px;'>{rf.get('direction', 'N/A')}</td>"
            f"<td style='padding:4px 8px;'>신뢰도 {rf.get('confidence', 0)}%</td></tr>"
        )
        lgbm = pred.get("lightgbm", {})
        ml_rows += (
            f"<tr><td style='padding:4px 8px; font-weight:bold;'>LightGBM</td>"
            f"<td style='padding:4px 8px;'>{lgbm.get('direction', 'N/A')}</td>"
            f"<td style='padding:4px 8px;'>신뢰도 {lgbm.get('confidence', 0)}%</td></tr>"
        )
        lstm = pred.get("lstm", {})
        if lstm and "error" not in lstm:
            ml_rows += (
                f"<tr><td style='padding:4px 8px; font-weight:bold;'>LSTM</td>"
                f"<td style='padding:4px 8px;'>{lstm.get('direction', 'N/A')}</td>"
                f"<td style='padding:4px 8px;'>신뢰도 {lstm.get('confidence', 0)}%</td></tr>"
            )
        else:
            ml_rows += (
                "<tr><td style='padding:4px 8px; font-weight:bold;'>LSTM</td>"
                "<td style='padding:4px 8px;' colspan='2'>예측 불가</td></tr>"
            )

        # --- NEW: Transformer ---
        transformer = pred.get("transformer", {})
        if transformer and "error" not in transformer:
            ml_rows += (
                f"<tr><td style='padding:4px 8px; font-weight:bold; color:#0066cc;'>Transformer (Advanced)</td>"
                f"<td style='padding:4px 8px;'>{transformer.get('direction', 'N/A')}</td>"
                f"<td style='padding:4px 8px;'>신뢰도 {transformer.get('confidence', 0)}%</td></tr>"
            )
        else:
            ml_rows += (
                "<tr><td style='padding:4px 8px; font-weight:bold; color:#0066cc;'>Transformer (Advanced)</td>"
                "<td style='padding:4px 8px;' colspan='2'>예측 불가</td></tr>"
            )
            
        ml_html = (
            "<table style='width:100%; border-collapse:collapse; font-size:0.9em; margin:8px 0;'>"
            "<tr style='background:#f0f0f0;'><th style='padding:4px 8px; text-align:left;'>모델</th>"
            "<th style='padding:4px 8px; text-align:left;'>예측</th>"
            "<th style='padding:4px 8px; text-align:left;'>비고</th></tr>"
            f"{ml_rows}</table>"
        )

        # 관련 뉴스
        news_html = ""
        news_items = item.get("news", [])
        if news_items:
            news_li = ""
            for n in news_items:
                title = n.get("title", "")
                link = n.get("link", "")
                publisher = n.get("publisher", "")
                pub_tag = f' <span style="color:#999;">— {publisher}</span>' if publisher else ""
                summary = n.get("summary", "")
                summary_html = f'<br><span style="color:#555;font-size:0.85em;">{summary}</span>' if summary else ""
                if link:
                    news_li += f'<li style="margin:6px 0;"><a href="{link}" target="_blank" style="color:#0066cc;">{title}</a>{pub_tag}{summary_html}</li>'
                else:
                    news_li += f'<li style="margin:6px 0;">{title}{pub_tag}{summary_html}</li>'
            news_html = f'<h4 style="margin:12px 0 4px;">관련 뉴스</h4><ul style="font-size:0.9em; padding-left:20px;">{news_li}</ul>'

        # --- NEW: 감성 분석 표시 ---
        sentiment = item.get("sentiment", {})
        sentiment_html = ""
        if sentiment:
            if "error" in sentiment:
                sentiment_html = f'<p style="font-size:0.9em; color:#dc3545;">[감성 분석 오류] {sentiment["error"]}</p>'
            else:
                sent_label = sentiment.get("label", "N/A")
                sent_score = sentiment.get("score", 0.0)
                sent_color = "#2ca02c" if "긍정" in sent_label else ("#dc3545" if "부정" in sent_label else "#666")
                sentiment_html = (
                    f'<div style="margin-top:10px; padding:10px; background:#f5f7fa; border-radius:6px; border-left:4px solid {sent_color};">'
                    f'<h4 style="margin:0 0 4px; color:#333;">뉴스 감성 분석 (FinBERT)</h4>'
                    f'<p style="margin:0; font-size:0.95em;">종합 의견: <strong style="color:{sent_color};">{sent_label}</strong> '
                    f'<span style="color:#666; font-size:0.9em;">(점수: {sent_score:+.3f})</span></p>'
                    f'</div>'
                )

        rows.append(f"""
        <div style="border:1px solid #ddd; border-radius:8px; padding:16px; margin:12px 0;">
            <h3>{name} ({item['symbol']})</h3>
            <p>현재가: <b>{sig['close']:,.2f}</b> | RSI: {sig['rsi']}
               | <span style="color:{_signal_color(sig['signal'])}; font-weight:bold;">
                 {sig['signal']}</span> (점수: {sig['score']})</p>
            <p style="font-size:0.9em;">{', '.join(sig['reasons']) if sig['reasons'] else '특이사항 없음'}</p>
            {sentiment_html}
            <h4 style="margin:12px 0 4px;">기술 지표 분석</h4>
            {indicators_html}
            <h4 style="margin:12px 0 4px;">ML 예측</h4>
            {ml_html}
            <img src="data:image/png;base64,{chart_b64}" style="width:100%; max-width:700px;"/>
            {news_html}
        </div>""")

    disclaimer = pred.get("disclaimer", "") if analyses else ""

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Stock Report {now}</title></head>
<body style="font-family:Arial,sans-serif; max-width:800px; margin:auto; padding:20px;">
<h1>주식 시장 분석 리포트</h1>
<p style="color:#666;">생성: {now}</p>
{''.join(rows)}
<p style="color:#999; font-size:0.85em; margin-top:20px;">{disclaimer}</p>
</body></html>"""
    return html
