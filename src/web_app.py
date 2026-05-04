"""Flask 웹 대시보드 — 종목 분석, 추가/삭제, 리포트 조회."""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import yaml
from flask import Flask, request, redirect, url_for, jsonify, Response
from markupsafe import escape

from src.validators import validate_stock_symbol, validate_stock_name, sanitize_stock_symbol, is_valid_search_query
from src.stock_search import search_stocks

app = Flask(__name__)
app.json.ensure_ascii = False  # Korean 종목명을 JSON에 그대로 출력 (응답 크기 절감)

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"

# 백그라운드 작업 저장소: {job_id: {status, symbol, name, result_html, error, started_at}}
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_JOBS_MAX = 50  # 완료된 작업 보관 최대 개수

_config_lock = threading.RLock()  # settings.yaml read-modify-write 보호 (재진입 허용)


def _jobs_set(job_id: str, **kwargs) -> None:
    """Lock을 획득한 뒤 _jobs[job_id]를 업데이트한다."""
    with _jobs_lock:
        _jobs[job_id].update(kwargs)


def _jobs_snapshot() -> dict[str, dict]:
    """현재 _jobs의 얕은 복사본을 반환한다 (읽기 전용 사용)."""
    with _jobs_lock:
        return dict(_jobs)


def _trim_jobs():
    """완료/오류 작업이 _JOBS_MAX 초과 시 오래된 것부터 제거한다."""
    with _jobs_lock:
        done = [jid for jid, j in _jobs.items() if j["status"] != "running"]
        for jid in done[:-_JOBS_MAX]:
            del _jobs[jid]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _save_config(config: dict) -> None:
    with _config_lock:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.dump(config, f, allow_unicode=True, default_flow_style=False)


def _get_all_stocks(config: dict) -> list[dict]:
    stocks = []
    for market, group in config.get("stocks", {}).items():
        for s in group:
            stocks.append({**s, "market": market})
    return stocks


def _run_analysis_bg(job_id: str, symbol: str, name: str):
    """백그라운드 스레드에서 분석 실행."""
    try:
        from main import analyze_stock
        from src.report_generator import generate_report

        result = analyze_stock(symbol, name)
        if result is None:
            _jobs_set(job_id, status="error", error=f'"{symbol}" 분석 중 오류 발생')
        else:
            html = generate_report([result])
            _jobs_set(job_id, status="done", result_html=html)
    except Exception as e:
        _jobs_set(job_id, status="error", error=str(e))
    finally:
        _trim_jobs()


def _run_full_analysis_bg(job_id: str):
    """백그라운드 스레드에서 전체 분석 실행."""
    try:
        from main import run_full_analysis, load_config

        config = load_config()
        html = run_full_analysis(config)
        if html is None:
            _jobs_set(job_id, status="error", error="분석 결과 없음")
        else:
            _jobs_set(job_id, status="done", result_html=html)
    except Exception as e:
        _jobs_set(job_id, status="error", error=str(e))
    finally:
        _trim_jobs()


# ---------------------------------------------------------------------------
# shared layout
# ---------------------------------------------------------------------------

_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       max-width: 960px; margin: 0 auto; padding: 24px; background: #f5f7fa; color: #333; }
h1 { margin-bottom: 20px; }
a { color: #0066cc; text-decoration: none; }
a:hover { text-decoration: underline; }
.card { background: #fff; border-radius: 8px; padding: 16px; margin: 10px 0;
        box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
.badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.8em;
         color: #fff; }
.badge-korea { background: #0066cc; }
.badge-us { background: #28a745; }
button, .btn { padding: 8px 16px; border: none; border-radius: 6px; cursor: pointer;
               font-size: 0.9em; color: #fff; }
.btn-primary { background: #0066cc; }
.btn-danger { background: #dc3545; }
.btn-success { background: #28a745; }
.btn-disabled { background: #999; cursor: not-allowed; }
input, select { padding: 8px; border: 1px solid #ccc; border-radius: 6px; font-size: 0.9em; }
.form-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin: 12px 0; }
.stock-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 12px; }
.signal-buy { color: #28a745; font-weight: bold; }
.signal-sell { color: #dc3545; font-weight: bold; }
.signal-hold { color: #6c757d; font-weight: bold; }
img.chart { width: 100%; max-width: 700px; margin-top: 8px; }
.nav { margin-bottom: 20px; }
.spinner { display: inline-block; width: 16px; height: 16px; border: 2px solid #ccc;
           border-top-color: #0066cc; border-radius: 50%; animation: spin 0.8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.status-running { color: #0066cc; }
.status-done { color: #28a745; }
.status-error { color: #dc3545; }
"""


def _page(title: str, body: str, auto_refresh_js: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang="ko">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — Stock Analyzer</title>
<style>{_CSS}</style></head>
<body>
<div class="nav"><a href="/">← 대시보드</a></div>
<h1>{title}</h1>
{body}
{auto_refresh_js}
</body></html>"""


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    config = _load_config()
    stocks = _get_all_stocks(config)

    # 에러 메시지 배너
    error_msg = request.args.get("error", "")
    error_banner = ""
    if error_msg:
        error_banner = (
            f'<div class="card" style="border-left:4px solid #dc3545; margin-bottom:8px;">'
            f'<span style="color:#dc3545;">⚠ {escape(error_msg)}</span></div>'
        )

    # 진행 중인 작업 표시
    jobs = _jobs_snapshot()
    running = [j for j in jobs.values() if j["status"] == "running"]
    running_banner = ""
    if running:
        items = ", ".join(escape(j["name"]) for j in running)
        running_banner = f"""
        <div class="card" style="border-left:4px solid #0066cc;">
            <span class="spinner"></span>
            <strong>분석 진행 중:</strong> {items}
            <a href="/jobs" style="margin-left:12px;">상태 확인 →</a>
        </div>"""

    cards = []
    for s in stocks:
        badge_cls = "badge-korea" if s["market"] == "korea" else "badge-us"
        market_label = "한국" if s["market"] == "korea" else "미국"

        # 해당 종목이 분석 중인지 확인
        is_running = any(
            j["symbol"] == s["symbol"] and j["status"] == "running"
            for j in jobs.values()
        )
        if is_running:
            analyze_btn = '<span class="btn btn-disabled"><span class="spinner"></span> 분석 중</span>'
        else:
            analyze_btn = f'<a class="btn btn-primary" href="/analyze/{s["symbol"]}">분석</a>'

        cards.append(f"""
        <div class="card">
            <span class="badge {badge_cls}">{market_label}</span>
            <h3 style="margin:8px 0 4px;">{escape(s['name'])}</h3>
            <p style="color:#666; font-size:0.9em;">{escape(s['symbol'])}</p>
            <div style="margin-top:10px; display:flex; gap:6px;">
                {analyze_btn}
                <form method="post" action="/stocks/delete" style="margin:0;"
                      onsubmit="return confirm('삭제하시겠습니까?');">
                    <input type="hidden" name="symbol" value="{s['symbol']}">
                    <button type="submit" class="btn btn-danger">삭제</button>
                </form>
            </div>
        </div>""")

    add_form = """
    <div class="card">
        <h2 style="margin-bottom:10px;">종목 추가</h2>
        <form method="post" action="/stocks/add">
            <div class="form-row">
                <input name="symbol" placeholder="심볼 (예: AAPL)" required>
                <input name="name" placeholder="종목명" required>
                <select name="market">
                    <option value="korea">한국</option>
                    <option value="us">미국</option>
                </select>
                <button type="submit" class="btn btn-success">추가</button>
            </div>
        </form>
    </div>"""

    analyze_all = """
    <form method="post" action="/analyze-all" style="margin:16px 0;">
        <button type="submit" class="btn btn-primary">전체 종목 일괄 분석</button>
    </form>"""

    body = f"""
    {error_banner}
    {running_banner}
    {add_form}
    {analyze_all}
    <div class="stock-grid">{''.join(cards)}</div>
    """ if cards else f"{error_banner}{running_banner}{add_form}<p>등록된 종목이 없습니다.</p>"

    refresh = ""
    if running:
        refresh = "<script>setTimeout(()=>location.reload(), 5000);</script>"

    return _page("대시보드", body, refresh)


@app.route("/analyze/<path:symbol>")
def analyze(symbol: str):
    # 입력 검증
    symbol = sanitize_stock_symbol(symbol)
    if not validate_stock_symbol(symbol):
        return _page("오류", f'<div class="card"><p style="color:#dc3545;">유효하지 않은 심볼: {symbol}</p></div>')

    config = _load_config()
    name = symbol
    for s in _get_all_stocks(config):
        if s["symbol"] == symbol:
            name = s["name"]
            break

    job_id = uuid.uuid4().hex[:8]
    with _jobs_lock:
        _jobs[job_id] = {
            "status": "running",
            "symbol": symbol,
            "name": name,
            "result_html": None,
            "error": None,
            "started_at": datetime.now().strftime("%H:%M:%S"),
        }

    t = threading.Thread(target=_run_analysis_bg, args=(job_id, symbol, name), daemon=True)
    t.start()

    return redirect(f"/jobs/{job_id}", code=303)


@app.route("/analyze-all", methods=["POST"])
def analyze_all():
    job_id = uuid.uuid4().hex[:8]
    with _jobs_lock:
        _jobs[job_id] = {
            "status": "running",
            "symbol": "ALL",
            "name": "전체 종목",
            "result_html": None,
            "error": None,
            "started_at": datetime.now().strftime("%H:%M:%S"),
        }

    t = threading.Thread(target=_run_full_analysis_bg, args=(job_id,), daemon=True)
    t.start()

    return redirect(f"/jobs/{job_id}", code=303)


@app.route("/jobs")
def jobs_list():
    jobs = _jobs_snapshot()
    rows = []
    for jid, j in sorted(jobs.items(), key=lambda x: x[1]["started_at"], reverse=True):
        status_cls = f"status-{j['status']}"
        if j["status"] == "running":
            status_label = '<span class="spinner"></span> 분석 중'
        elif j["status"] == "done":
            status_label = "완료"
        else:
            status_label = "오류"

        link = f'<a href="/jobs/{jid}">보기</a>' if j["status"] != "running" else ""
        rows.append(f"""
        <tr>
            <td>{j['started_at']}</td>
            <td>{escape(j['name'])} ({escape(j['symbol'])})</td>
            <td class="{status_cls}">{status_label}</td>
            <td>{link}</td>
        </tr>""")

    table = f"""
    <table style="width:100%;border-collapse:collapse;">
        <thead><tr style="border-bottom:2px solid #ddd;text-align:left;">
            <th style="padding:8px;">시작</th>
            <th style="padding:8px;">종목</th>
            <th style="padding:8px;">상태</th>
            <th style="padding:8px;"></th>
        </tr></thead>
        <tbody>{''.join(rows) if rows else '<tr><td colspan="4" style="padding:8px;">작업 없음</td></tr>'}</tbody>
    </table>"""

    has_running = any(j["status"] == "running" for j in jobs.values())
    refresh = "<script>setTimeout(()=>location.reload(), 3000);</script>" if has_running else ""

    return _page("작업 목록", f'<div class="card">{table}</div>', refresh)


@app.route("/jobs/<job_id>")
def job_detail(job_id: str):
    job = _jobs_snapshot().get(job_id)
    if not job:
        return _page("오류", "<p>작업을 찾을 수 없습니다.</p>")

    if job["status"] == "running":
        body = f"""
        <div class="card">
            <p><span class="spinner"></span> <strong>{job['name']}</strong> ({job['symbol']}) 분석 중...</p>
            <p style="color:#666;margin-top:8px;">시작: {job['started_at']}</p>
        </div>"""
        refresh = "<script>setTimeout(()=>location.reload(), 3000);</script>"
        return _page(f"{job['name']} 분석 중", body, refresh)

    if job["status"] == "error":
        return _page("분석 실패", f'<div class="card"><p style="color:#dc3545;">{escape(job["error"])}</p></div>')

    download_btn = (
        f'<div style="margin-bottom:12px;">'
        f'<a class="btn btn-primary" href="/jobs/{job_id}/download">HTML 다운로드</a>'
        f'</div>'
    )
    return _page(f"{job['name']} 분석 결과", f'{download_btn}<div class="card">{job["result_html"]}</div>')


@app.route("/jobs/<job_id>/download")
def job_download(job_id: str):
    job = _jobs_snapshot().get(job_id)
    if not job or job["status"] != "done":
        return _page("오류", "<p>다운로드할 리포트가 없습니다.</p>")

    filename = f"report_{job['symbol']}_{datetime.now().strftime('%Y%m%d')}.html"
    encoded = quote(filename, safe="")
    return Response(
        job["result_html"],
        mimetype="text/html",
        headers={"Content-Disposition": f"attachment; filename=\"{filename}\"; filename*=UTF-8''{encoded}"},
    )


@app.route("/api/jobs/<job_id>")
def api_job_status(job_id: str):
    job = _jobs_snapshot().get(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    return jsonify({"status": job["status"], "symbol": job["symbol"], "name": job["name"]})


@app.route("/api/stocks/search")
def api_stocks_search():
    """종목 자동완성 검색 API. 빈/잘못된 쿼리는 빈 배열을 반환."""
    q = request.args.get("q", "").strip()
    if not is_valid_search_query(q) or len(q) < 2:
        return jsonify([])
    try:
        results = search_stocks(q, limit=10)
    except Exception as e:
        logger.warning("종목 검색 실패: q=%s error=%s", q, e)
        return jsonify([])
    return jsonify(results)


@app.route("/stocks/add", methods=["POST"])
def stocks_add():
    symbol = request.form.get("symbol", "").strip()
    name = request.form.get("name", "").strip()
    market = request.form.get("market", "korea")

    if not symbol or not name:
        return redirect(url_for("index", error="심볼과 종목명을 모두 입력하세요."), code=303)

    if not validate_stock_name(name):
        return redirect(url_for("index", error=f"종목명은 1-50자 이내여야 합니다."), code=303)

    # 심볼 정리 및 검증
    symbol = sanitize_stock_symbol(symbol)

    # 한국 시장의 경우 .KS/.KQ 자동 추가
    if market == "korea" and not symbol.endswith((".KS", ".KQ")):
        symbol = symbol + ".KS"

    # 검증
    if not validate_stock_symbol(symbol):
        return redirect(url_for("index", error=f"유효하지 않은 심볼입니다: {symbol}"), code=303)

    with _config_lock:
        config = _load_config()
        if market not in config.get("stocks", {}):
            config.setdefault("stocks", {})[market] = []

        existing = {s["symbol"] for s in config["stocks"][market]}
        if symbol in existing:
            return redirect(url_for("index", error=f"{symbol} 은(는) 이미 등록된 종목입니다."), code=303)

        config["stocks"][market].append({"symbol": symbol, "name": name})
        _save_config(config)

    return redirect(url_for("index"), code=303)


@app.route("/stocks/delete", methods=["POST"])
def stocks_delete():
    symbol = request.form.get("symbol", "").strip()
    if not symbol:
        return redirect(url_for("index"))

    with _config_lock:
        config = _load_config()
        for market, group in config.get("stocks", {}).items():
            config["stocks"][market] = [s for s in group if s["symbol"] != symbol]
        _save_config(config)

    return redirect(url_for("index"), code=303)


def run_web(host: str = "0.0.0.0", port: int = 8080, debug: bool = False):
    """Flask 웹 서버를 실행한다.

    Args:
        host: 바인딩 호스트 (기본: 0.0.0.0)
        port: 바인딩 포트 (기본: 8080)
        debug: 디버그 모드 (기본: False, 보안상 프로덕션에서는 False 권장)
    """
    app.run(host=host, port=port, debug=debug)
