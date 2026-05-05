"""Flask 웹 대시보드 — 종목 분석, 추가/삭제, 리포트 조회."""
from __future__ import annotations

import logging
import os
import secrets
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import yaml
from flask import Flask, abort, request, redirect, session, url_for, jsonify, Response

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
from markupsafe import escape

from src.validators import validate_stock_symbol, validate_stock_name, sanitize_stock_symbol, is_valid_search_query
from src.stock_search import search_stocks
from src import prediction_history
from src import backtest as bt
from src import analysis_cache

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.json.ensure_ascii = False  # Korean 종목명을 JSON에 그대로 출력 (응답 크기 절감)

_secret_key = os.environ.get("SECRET_KEY")
if not _secret_key:
    _secret_key = secrets.token_hex(32)
    logger.warning(
        "SECRET_KEY 환경변수가 설정되지 않았습니다. "
        "임시 키를 사용합니다 (재시작 시 세션이 초기화됩니다). "
        ".env 파일에 SECRET_KEY를 설정하세요."
    )
app.secret_key = _secret_key

# Basic Auth — 인터넷 노출 시 (예: Tailscale Funnel) 인증 게이트.
# ENABLE_BASIC_AUTH=1 일 때 BASIC_AUTH_USERS 또는 단일 USERNAME/PASSWORD 중 하나 필요.
def _parse_basic_auth_users() -> dict[str, str]:
    """환경변수에서 사용자 dict 를 빌드한다.

    BASIC_AUTH_USERS=user1:pw1;user2:pw2;user3:pw3 형식 우선.
    없으면 BASIC_AUTH_USERNAME/BASIC_AUTH_PASSWORD 단일 쌍 사용 (호환).
    비밀번호에 ':' 포함 가능 (split 한 번만).
    """
    users: dict[str, str] = {}
    multi = os.environ.get("BASIC_AUTH_USERS", "").strip()
    if multi:
        for entry in multi.split(";"):
            entry = entry.strip()
            if ":" not in entry:
                continue
            u, p = entry.split(":", 1)
            u = u.strip()
            if u and p:
                users[u] = p
    else:
        u = os.environ.get("BASIC_AUTH_USERNAME", "").strip()
        p = os.environ.get("BASIC_AUTH_PASSWORD", "")
        if u and p:
            users[u] = p
    return users


_basic_auth_on = os.environ.get("ENABLE_BASIC_AUTH", "").strip().lower() in ("1", "true", "yes")
_basic_auth_users = _parse_basic_auth_users()
if _basic_auth_on and not _basic_auth_users:
    raise RuntimeError(
        "ENABLE_BASIC_AUTH=1 인 경우 BASIC_AUTH_USERS 또는 "
        "BASIC_AUTH_USERNAME/BASIC_AUTH_PASSWORD 중 하나는 채워야 합니다."
    )


@app.before_request
def _basic_auth_gate():
    """ENABLE_BASIC_AUTH=1 일 때 모든 요청에 Basic Auth 검증.

    /logout 만 우회 — 라우트 자체가 401 + Clear-Site-Data 로 캐시 무효화한다.
    """
    if not _basic_auth_on:
        return None
    if request.path == "/logout":
        return None
    auth = request.authorization
    if auth is not None:
        expected = _basic_auth_users.get(auth.username)
        if expected is not None and secrets.compare_digest(auth.password or "", expected):
            return None
    return Response(
        "Authentication required",
        401,
        {"WWW-Authenticate": 'Basic realm="stock-analyzer"'},
    )


CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"

# 백그라운드 작업 저장소: {job_id: {status, symbol, name, result_html, error, started_at}}
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_JOBS_MAX = 50  # 완료된 작업 보관 최대 개수

_backtest_lock = threading.Lock()  # 글로벌 백테스트 동시 실행 1개로 제한

_config_lock = threading.RLock()  # settings.yaml read-modify-write 보호 (재진입 허용)


# ---------------------------------------------------------------------------
# CSRF helpers
# ---------------------------------------------------------------------------

def _csrf_token() -> str:
    """세션에 CSRF 토큰이 없으면 생성 후 반환한다."""
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)
    return session["csrf_token"]


def _csrf_input() -> str:
    """POST 폼에 삽입할 숨김 CSRF 입력 필드 HTML을 반환한다."""
    return f'<input type="hidden" name="csrf_token" value="{_csrf_token()}">'


def _csrf_validate() -> None:
    """요청의 CSRF 토큰을 검증한다. 불일치 시 403을 반환한다."""
    token = session.get("csrf_token")
    form_token = request.form.get("csrf_token", "")
    if not token or not secrets.compare_digest(token, form_token):
        abort(403)


def _jobs_set(job_id: str, **kwargs) -> None:
    """Lock을 획득한 뒤 _jobs[job_id]를 업데이트한다."""
    with _jobs_lock:
        _jobs[job_id].update(kwargs)


def _jobs_snapshot() -> dict[str, dict]:
    """현재 _jobs의 얕은 복사본을 반환한다 (읽기 전용 사용)."""
    with _jobs_lock:
        return dict(_jobs)


def _trim_jobs() -> None:
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


def _run_analysis_bg(job_id: str, symbol: str, name: str) -> None:
    """백그라운드 스레드에서 분석 실행. 성공 시 analysis_cache UPSERT."""
    logger.info("분석 시작: job_id=%s symbol=%s name=%s", job_id, symbol, name)
    try:
        from main import analyze_stock
        from src.report_generator import generate_report

        result = analyze_stock(symbol, name)
        if result is None:
            logger.warning("분석 결과 없음: job_id=%s symbol=%s", job_id, symbol)
            _jobs_set(job_id, status="error", error=f'"{symbol}" 분석 중 오류 발생')
        else:
            html = generate_report([result])
            _jobs_set(job_id, status="done", result_html=html)
            try:
                market = _market_of(symbol)
                sig = result.get("signal") or {}
                analysis_cache.put(
                    symbol, market, html, source="manual",
                    signal_value=sig.get("signal"),
                    signal_score=sig.get("score"),
                )
            except Exception as e:
                logger.warning("analysis_cache.put 실패 (job 결과는 정상): %s", e)
            logger.info("분석 완료: job_id=%s symbol=%s", job_id, symbol)
    except Exception as e:
        logger.exception("분석 오류: job_id=%s symbol=%s error=%s", job_id, symbol, e)
        _jobs_set(job_id, status="error", error=str(e))
    finally:
        _trim_jobs()


def _market_of(symbol: str) -> str:
    """settings.yaml 에서 symbol 의 시장 (korea/us) 을 찾는다. 없으면 'us' 기본."""
    config = _load_config()
    for market, stocks in config.get("stocks", {}).items():
        for s in stocks:
            if s["symbol"] == symbol:
                return market
    return "us"


def _safe_cache_get(cache_key: str) -> dict | None:
    """analysis_cache.get 의 sqlite 오류를 흡수하고 None 으로 변환한다.

    spec §10 — 캐시 조회 실패 시 캐시 miss 처럼 동작.
    """
    try:
        return analysis_cache.get(cache_key)
    except Exception as e:
        logger.warning("analysis_cache.get(%s) 실패: %s", cache_key, e)
        return None


def _run_full_analysis_bg(job_id: str) -> None:
    """백그라운드 스레드에서 전체 분석 실행. 종목별 + ALL row 모두 UPSERT."""
    logger.info("전체 분석 시작: job_id=%s", job_id)
    try:
        from main import collect_analyses, load_config
        from src.report_generator import generate_report

        config = load_config()
        analyses = collect_analyses(config)
        if not analyses:
            logger.warning("전체 분석 결과 없음: job_id=%s", job_id)
            _jobs_set(job_id, status="error", error="분석 결과 없음")
            return

        # 종목별 cache UPSERT (개별 카드 신선도 갱신)
        symbol_to_market = {
            s["symbol"]: market
            for market, group in config.get("stocks", {}).items()
            for s in group
        }
        cached = 0
        for r in analyses:
            sym = r["symbol"]
            try:
                ind_html = generate_report([r])
                sig = r.get("signal") or {}
                analysis_cache.put(
                    sym, symbol_to_market.get(sym, "us"), ind_html, source="manual",
                    signal_value=sig.get("signal"),
                    signal_score=sig.get("score"),
                )
                cached += 1
            except Exception as e:
                logger.warning("종목별 cache.put 실패 — %s: %s", sym, e)

        # 다이제스트 HTML + ALL row
        full_html = generate_report(analyses)
        _jobs_set(job_id, status="done", result_html=full_html)
        try:
            analysis_cache.put("ALL", "all", full_html, source="manual")
        except Exception as e:
            logger.warning("analysis_cache.put('ALL') 실패: %s", e)
        logger.info(
            "전체 분석 완료: job_id=%s 종목별 캐시=%d/%d, ALL 갱신",
            job_id, cached, len(analyses),
        )
    except Exception as e:
        logger.exception("전체 분석 오류: job_id=%s error=%s", job_id, e)
        _jobs_set(job_id, status="error", error=str(e))
    finally:
        _trim_jobs()


# ---------------------------------------------------------------------------
# shared layout
# ---------------------------------------------------------------------------

_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500;600&family=Fira+Sans:wght@300;400;500;600;700&display=swap');

*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --blue-900: #1E3A8A;
  --blue-800: #1E40AF;
  --blue-600: #2563EB;
  --blue-500: #3B82F6;
  --blue-100: #DBEAFE;
  --blue-50:  #EFF6FF;
  --amber-500: #F59E0B;
  --amber-400: #FBBF24;
  --amber-100: #FEF3C7;
  --red-600:  #DC2626;
  --red-100:  #FEE2E2;
  --green-600: #16A34A;
  --green-100: #DCFCE7;
  --slate-900: #0F172A;
  --slate-700: #334155;
  --slate-500: #64748B;
  --slate-300: #CBD5E1;
  --slate-200: #E2E8F0;
  --slate-100: #F1F5F9;
  --slate-50:  #F8FAFC;
  --white: #FFFFFF;
  --shadow-sm: 0 1px 3px rgba(0,0,0,0.08), 0 1px 2px rgba(0,0,0,0.04);
  --shadow-md: 0 4px 6px rgba(0,0,0,0.07), 0 2px 4px rgba(0,0,0,0.05);
  --radius: 10px;
  --transition: 150ms ease;
}

html { font-size: 15px; }

body {
  font-family: 'Fira Sans', -apple-system, BlinkMacSystemFont, sans-serif;
  background: var(--slate-50);
  color: var(--slate-900);
  min-height: 100vh;
  line-height: 1.6;
}

/* ── Topbar ── */
.topbar {
  background: var(--blue-800);
  padding: 0 24px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 56px;
  position: sticky;
  top: 0;
  z-index: 30;
  box-shadow: 0 2px 8px rgba(30,64,175,0.3);
}
.topbar-brand {
  display: flex;
  align-items: center;
  gap: 10px;
  color: var(--white);
  font-weight: 700;
  font-size: 1.05rem;
  letter-spacing: -0.01em;
  text-decoration: none;
}
.topbar-brand svg { opacity: 0.9; }
.topbar-nav { display: flex; gap: 4px; }
.topbar-link {
  color: rgba(255,255,255,0.75);
  text-decoration: none;
  font-size: 0.875rem;
  font-weight: 500;
  padding: 6px 12px;
  border-radius: 6px;
  transition: background var(--transition), color var(--transition);
}
.topbar-link:hover { background: rgba(255,255,255,0.12); color: var(--white); }

/* ── Main wrapper ── */
.main {
  max-width: 1100px;
  margin: 0 auto;
  padding: 28px 20px 48px;
}

/* ── Page header ── */
.page-header { margin-bottom: 24px; }
.page-header h1 {
  font-size: 1.5rem;
  font-weight: 700;
  color: var(--blue-900);
  letter-spacing: -0.02em;
}
.page-header p { color: var(--slate-500); font-size: 0.875rem; margin-top: 2px; }

/* ── Alerts ── */
.alert {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 12px 16px;
  border-radius: var(--radius);
  margin-bottom: 16px;
  font-size: 0.875rem;
  font-weight: 500;
}
.alert-error { background: var(--red-100); color: var(--red-600); border: 1px solid #FECACA; }
.alert-info  { background: var(--blue-50); color: var(--blue-800); border: 1px solid var(--blue-100); }
.alert svg { flex-shrink: 0; margin-top: 1px; }

/* ── Card ── */
.card {
  background: var(--white);
  border: 1px solid var(--slate-200);
  border-radius: var(--radius);
  box-shadow: var(--shadow-sm);
  padding: 20px;
  margin-bottom: 16px;
}
.card-title {
  font-size: 0.9rem;
  font-weight: 600;
  color: var(--slate-700);
  margin-bottom: 14px;
  display: flex;
  align-items: center;
  gap: 7px;
}
.card-title svg { color: var(--blue-600); }

/* ── Add form ── */
.add-form {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  align-items: flex-end;
}
.field { display: flex; flex-direction: column; gap: 4px; }
.field label { font-size: 0.75rem; font-weight: 600; color: var(--slate-500); text-transform: uppercase; letter-spacing: 0.05em; }
.field input, .field select {
  height: 38px;
  padding: 0 12px;
  border: 1.5px solid var(--slate-300);
  border-radius: 7px;
  font-size: 0.875rem;
  font-family: inherit;
  color: var(--slate-900);
  background: var(--white);
  transition: border-color var(--transition), box-shadow var(--transition);
  outline: none;
  min-width: 0;
}
.field input:focus, .field select:focus {
  border-color: var(--blue-500);
  box-shadow: 0 0 0 3px rgba(59,130,246,0.15);
}
.field input::placeholder { color: var(--slate-300); }

/* ── Buttons ── */
.btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 0 16px;
  height: 38px;
  border: none;
  border-radius: 7px;
  font-size: 0.875rem;
  font-weight: 600;
  font-family: inherit;
  cursor: pointer;
  text-decoration: none;
  transition: opacity var(--transition), transform var(--transition), box-shadow var(--transition);
  white-space: nowrap;
}
.btn:hover { opacity: 0.88; transform: translateY(-1px); box-shadow: var(--shadow-md); }
.btn:active { transform: translateY(0); opacity: 1; }
.btn-primary  { background: var(--blue-800); color: var(--white); }
.btn-success  { background: var(--green-600); color: var(--white); }
.btn-danger   { background: transparent; color: var(--red-600); border: 1.5px solid var(--red-600); }
.btn-danger:hover { background: var(--red-100); box-shadow: none; }
.btn-amber    { background: var(--amber-500); color: var(--white); }
.btn-disabled {
  background: var(--slate-200);
  color: var(--slate-500);
  cursor: not-allowed;
  pointer-events: none;
}
.btn-sm { height: 32px; padding: 0 12px; font-size: 0.8rem; }

/* ── Stock grid ── */
.stock-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(270px, 1fr));
  gap: 14px;
}
.stock-card {
  background: var(--white);
  border: 1px solid var(--slate-200);
  border-radius: var(--radius);
  box-shadow: var(--shadow-sm);
  padding: 18px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  transition: box-shadow var(--transition), transform var(--transition);
  cursor: default;
}
.stock-card:hover { box-shadow: var(--shadow-md); transform: translateY(-2px); }
.stock-card-header { display: flex; align-items: flex-start; justify-content: space-between; }
.stock-card-info h3 {
  font-size: 1rem;
  font-weight: 700;
  color: var(--slate-900);
  line-height: 1.3;
}
.stock-card-info .symbol {
  font-family: 'Fira Code', monospace;
  font-size: 0.78rem;
  color: var(--slate-500);
  margin-top: 2px;
}
.badge {
  display: inline-flex;
  align-items: center;
  padding: 3px 9px;
  border-radius: 20px;
  font-size: 0.72rem;
  font-weight: 700;
  letter-spacing: 0.03em;
}
.badge-korea { background: var(--blue-100); color: var(--blue-800); }
.badge-us    { background: var(--green-100); color: var(--green-600); }
.stock-card-actions { display: flex; gap: 8px; margin-top: 4px; }

/* ── Toolbar row ── */
.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 16px;
}
.toolbar-left { display: flex; align-items: center; gap: 8px; }

/* ── Jobs table ── */
.data-table { width: 100%; border-collapse: collapse; font-size: 0.875rem; }
.data-table thead tr {
  border-bottom: 2px solid var(--slate-200);
  background: var(--slate-50);
}
.data-table th {
  padding: 10px 14px;
  text-align: left;
  font-size: 0.75rem;
  font-weight: 700;
  color: var(--slate-500);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
.data-table td {
  padding: 12px 14px;
  border-bottom: 1px solid var(--slate-100);
  color: var(--slate-700);
}
.data-table tbody tr:last-child td { border-bottom: none; }
.data-table tbody tr:hover td { background: var(--slate-50); }
.mono { font-family: 'Fira Code', monospace; font-size: 0.82rem; }

/* ── Status badges ── */
.status-pill {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 9px;
  border-radius: 20px;
  font-size: 0.78rem;
  font-weight: 600;
}
.status-running { background: var(--blue-50);  color: var(--blue-800); }
.status-done    { background: var(--green-100); color: var(--green-600); }
.status-error   { background: var(--red-100);   color: var(--red-600); }

/* ── Spinner ── */
.spinner {
  display: inline-block;
  width: 14px; height: 14px;
  border: 2px solid rgba(59,130,246,0.2);
  border-top-color: var(--blue-500);
  border-radius: 50%;
  animation: spin 0.7s linear infinite;
  vertical-align: middle;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* ── Running banner ── */
.running-banner {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 16px;
  background: var(--blue-50);
  border: 1px solid var(--blue-100);
  border-radius: var(--radius);
  margin-bottom: 16px;
  font-size: 0.875rem;
  color: var(--blue-800);
}
.running-banner a { color: var(--blue-600); font-weight: 600; text-decoration: none; }
.running-banner a:hover { text-decoration: underline; }

/* ── Empty state ── */
.empty-state {
  text-align: center;
  padding: 48px 24px;
  color: var(--slate-500);
}
.empty-state svg { margin: 0 auto 12px; opacity: 0.35; }
.empty-state p { font-size: 0.95rem; }

/* ── Job detail ── */
.result-frame { margin-top: 16px; }

/* z-index scale: topbar 30, autocomplete-list 20 */
/* ── Autocomplete ── */
.autocomplete-wrap { position: relative; }
.autocomplete-list {
  position: absolute; top: 100%; left: 0; right: 0;
  margin-top: 4px;
  background: var(--white); border: 1px solid var(--slate-200);
  border-radius: var(--radius); box-shadow: var(--shadow-md);
  max-height: 280px; overflow-y: auto; z-index: 20;
  display: none;
}
.autocomplete-list.open { display: block; }
.autocomplete-item {
  padding: 8px 12px; cursor: pointer; display: flex;
  align-items: center; justify-content: space-between; gap: 8px;
  font-size: 0.875rem; border-bottom: 1px solid var(--slate-100);
}
.autocomplete-item:last-child { border-bottom: none; }
.autocomplete-item:hover, .autocomplete-item.active { background: var(--blue-50); }
.autocomplete-item .ac-name { font-weight: 600; color: var(--slate-900); }
.autocomplete-item .ac-symbol { font-family: 'Fira Code', monospace; font-size: 0.78rem; color: var(--slate-500); margin-left: 6px; }
.autocomplete-empty { padding: 12px; color: var(--slate-500); font-size: 0.875rem; text-align: center; }

/* ── Responsive ── */
@media (max-width: 640px) {
  .topbar { padding: 0 16px; }
  .main { padding: 16px 12px 32px; }
  .add-form { flex-direction: column; }
  .field input, .field select { width: 100%; }
  .toolbar { flex-direction: column; align-items: flex-start; }
}

/* ── prediction-history section ───────────────────────────────────────── */
.hit-rate-grid {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 10px;
  margin-bottom: 16px;
}
.hit-rate-card {
  background: var(--white); border: 1px solid var(--slate-200);
  border-radius: var(--radius); padding: 14px; text-align: center;
}
.hit-rate-card.empty { opacity: 0.55; }
.hit-rate-card .name  { font-size: 0.78rem; color: var(--slate-500); font-weight: 600; }
.hit-rate-card .value { font-size: 1.6rem; font-weight: 700; margin: 4px 0; }
.hit-rate-card .n     { font-size: 0.72rem; color: var(--slate-500); }

.history-details { margin-top: 12px; }
.history-details summary {
  font-weight: 600; color: var(--blue-800); padding: 6px 0;
  cursor: pointer; list-style: revert;
}
.history-details[open] summary { margin-bottom: 12px; }

.history-table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
.history-table th {
  background: var(--slate-50); padding: 8px 10px; font-size: 0.72rem;
  text-transform: uppercase; color: var(--slate-500); text-align: center;
  white-space: nowrap;
}
.history-table td { padding: 8px 10px; border-bottom: 1px solid var(--slate-100); text-align: center; }
.history-table td.num { font-family: 'Fira Code', monospace; text-align: right; }
.history-table tbody tr:hover td { background: var(--slate-50); }
.history-table tr.row-pending td { color: var(--slate-500); background: var(--slate-50); }

.pred-cell { font-family: 'Fira Code', monospace; font-size: 0.78rem; padding: 4px 8px; }
.pred-cell.pred-hit  { background: var(--green-100); color: var(--green-600); }
.pred-cell.pred-miss { background: var(--red-100);   color: var(--red-600); }
.pred-cell.pred-pending { color: var(--slate-500); }

.badge-hit, .badge-miss, .badge-pending {
  display: inline-block; padding: 3px 9px; border-radius: 20px;
  font-size: 0.78rem; font-weight: 600;
}
.badge-hit { background: var(--green-100); color: var(--green-600); }
.badge-miss { background: var(--red-100); color: var(--red-600); }
.badge-pending { background: var(--slate-100); color: var(--slate-500); }

/* ── CSS-only 모델 설명 탭바 ─────────────────────────────────────────── */
.model-tabs { margin: 16px 0 12px; }
.mtab-radio { position: absolute; opacity: 0; pointer-events: none; }
.mtab-list {
  display: flex; gap: 2px; border-bottom: 2px solid var(--slate-200);
  flex-wrap: wrap;
}
.mtab-label {
  padding: 8px 16px; cursor: pointer; font-weight: 600;
  font-size: 0.85rem; color: var(--slate-500);
  border-bottom: 2px solid transparent; margin-bottom: -2px;
  transition: color var(--transition), border-color var(--transition);
  user-select: none;
}
.mtab-label:hover { color: var(--blue-600); }

.mtab-panel { display: none; padding: 16px 4px; line-height: 1.7; color: var(--slate-700); }
.mtab-panel h3 { font-size: 1rem; color: var(--blue-900); margin-bottom: 8px; }
.mtab-panel strong { color: var(--slate-900); }

#mtab-rf:checked          ~ .mtab-list .mtab-label-rf,
#mtab-lgbm:checked        ~ .mtab-list .mtab-label-lgbm,
#mtab-lstm:checked        ~ .mtab-list .mtab-label-lstm,
#mtab-transformer:checked ~ .mtab-list .mtab-label-transformer,
#mtab-ensemble:checked    ~ .mtab-list .mtab-label-ensemble {
  color: var(--blue-800); border-bottom-color: var(--blue-600);
}
#mtab-rf:checked          ~ .mtab-panels .mtab-panel-rf,
#mtab-lgbm:checked        ~ .mtab-panels .mtab-panel-lgbm,
#mtab-lstm:checked        ~ .mtab-panels .mtab-panel-lstm,
#mtab-transformer:checked ~ .mtab-panels .mtab-panel-transformer,
#mtab-ensemble:checked    ~ .mtab-panels .mtab-panel-ensemble { display: block; }

.mtab-radio:focus-visible ~ .mtab-list .mtab-label {
  outline: 2px solid var(--blue-500); outline-offset: 2px;
}

/* ── 카드 시그널 뱃지 ─────────────────────────────────────────────────── */
.stock-card-badges {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 6px;
}

.signal-buy,
.signal-sell,
.signal-hold {
  display: inline-flex;
  align-items: center;
  padding: 4px 10px;
  border-radius: 6px;
  font-size: 0.78rem;
  font-weight: 700;
  font-family: 'Fira Code', monospace;
  letter-spacing: 0.02em;
  white-space: nowrap;
}
.signal-buy  { background: var(--green-100); color: var(--green-600); }
.signal-sell { background: var(--red-100);   color: var(--red-600); }
.signal-hold { background: var(--slate-100); color: var(--slate-500); }
"""

_AUTOCOMPLETE_JS = """
<script>
(() => {
  const input = document.getElementById('stock-search-input');
  const list = document.getElementById('autocomplete-list');
  if (!input || !list) return;
  const nameInput = document.querySelector('input[name="name"]');
  const marketSel = document.querySelector('select[name="market"]');

  let timer = null;
  let activeIdx = -1;
  let items = [];
  let currentFetchId = 0;

  function close() {
    list.classList.remove('open');
    list.innerHTML = '';
    activeIdx = -1;
    items = [];
    input.setAttribute('aria-expanded', 'false');
    input.removeAttribute('aria-activedescendant');
  }

  function pick(idx) {
    const r = items[idx];
    if (!r) return;
    input.value = r.symbol;
    if (nameInput) nameInput.value = r.name;
    if (marketSel) marketSel.value = r.market;
    close();
  }

  function highlight(idx) {
    [...list.querySelectorAll('.autocomplete-item')].forEach((el, i) => {
      el.classList.toggle('active', i === idx);
      el.setAttribute('aria-selected', i === idx ? 'true' : 'false');
    });
    if (idx >= 0) {
      input.setAttribute('aria-activedescendant', 'autocomplete-item-' + idx);
    } else {
      input.removeAttribute('aria-activedescendant');
    }
  }

  function render(results) {
    list.innerHTML = '';
    if (results.length === 0) {
      const div = document.createElement('div');
      div.className = 'autocomplete-empty';
      div.textContent = '검색 결과 없음';
      list.appendChild(div);
    } else {
      results.forEach((r, i) => {
        const it = document.createElement('div');
        it.className = 'autocomplete-item';
        it.id = 'autocomplete-item-' + i;
        it.setAttribute('role', 'option');
        it.setAttribute('aria-selected', 'false');
        const left = document.createElement('div');
        const name = document.createElement('span');
        name.className = 'ac-name';
        name.textContent = r.name;
        const sym = document.createElement('span');
        sym.className = 'ac-symbol';
        sym.textContent = r.symbol;
        left.appendChild(name);
        left.appendChild(sym);
        const badge = document.createElement('span');
        badge.className = 'badge ' + (r.market === 'korea' ? 'badge-korea' : 'badge-us');
        badge.textContent = r.market === 'korea' ? '한국' : '미국';
        it.appendChild(left);
        it.appendChild(badge);
        it.addEventListener('mousedown', (e) => { e.preventDefault(); pick(i); });
        list.appendChild(it);
      });
    }
    list.classList.add('open');
    input.setAttribute('aria-expanded', 'true');
    items = results;
    activeIdx = -1;
  }

  input.addEventListener('input', () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 2) { close(); return; }
    timer = setTimeout(async () => {
      const fetchId = ++currentFetchId;
      try {
        const res = await fetch('/api/stocks/search?q=' + encodeURIComponent(q));
        if (fetchId !== currentFetchId) return;  // 더 새로운 검색이 시작됨 — 무시
        if (!res.ok) { close(); return; }
        const data = await res.json();
        if (fetchId !== currentFetchId) return;
        render(Array.isArray(data) ? data : []);
      } catch (err) {
        if (fetchId === currentFetchId) close();
      }
    }, 300);
  });

  input.addEventListener('keydown', (e) => {
    if (!list.classList.contains('open') || items.length === 0) return;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      activeIdx = (activeIdx + 1) % items.length;
      highlight(activeIdx);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      activeIdx = (activeIdx - 1 + items.length) % items.length;
      highlight(activeIdx);
    } else if (e.key === 'Enter' && activeIdx >= 0) {
      e.preventDefault();
      pick(activeIdx);
    } else if (e.key === 'Escape') {
      close();
    }
  });

  document.addEventListener('click', (e) => {
    if (!input.contains(e.target) && !list.contains(e.target)) close();
  });
})();
</script>
"""

# SVG 아이콘 상수
_ICON_CHART = '<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>'
_ICON_PLUS  = '<svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>'
_ICON_TRASH = '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4h6v2"/></svg>'
_ICON_PLAY  = '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"/></svg>'
_ICON_WARN  = '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>'
_ICON_INFO  = '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>'
_ICON_DL    = '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>'
_ICON_LIST  = '<svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>'


def _page(title: str, body: str, auto_refresh_js: str = "") -> str:
    logout_link = (
        '<a class="topbar-link" href="/logout" style="opacity:0.75;">로그아웃</a>'
        if _basic_auth_on else ""
    )
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{title} — Stock Analyzer</title>
  <style>{_CSS}</style>
</head>
<body>
<nav class="topbar">
  <a class="topbar-brand" href="/">
    {_ICON_CHART}
    Stock Analyzer
  </a>
  <div class="topbar-nav">
    <a class="topbar-link" href="/">대시보드</a>
    <a class="topbar-link" href="/jobs">작업 내역</a>
    {logout_link}
  </div>
</nav>
<main class="main">
{body}
</main>
{auto_refresh_js}
</body></html>"""


# ---------------------------------------------------------------------------
# stock view helpers
# ---------------------------------------------------------------------------

def _format_kst(unix_ts: int) -> str:
    """unix epoch → 'YYYY-MM-DD HH:MM KST' 표시."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.fromtimestamp(unix_ts, tz=ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M KST")


def _render_meta_bar(row: dict, fresh: bool, name: str) -> str:
    """결과 페이지 상단 메타바 카드."""
    when = _format_kst(row["generated_at"])
    if fresh:
        bar = f'<div class="alert alert-info">🟢 분석 시각: {when} · {row["source"]}</div>'
    else:
        bar = (
            f'<div class="alert alert-error" style="background:#FEF3C7;color:#92400E;border-color:#FDE68A;">'
            f'🟡 분석 시각: {when} · {row["source"]}<br>'
            f'⚠️ 마지막 분석 후 시장이 다시 마감되었습니다. 재분석을 권장합니다.'
            f'</div>'
        )
    reanalyze_form = f'''
    <form method="post" action="/analyze/{escape(row["cache_key"])}" style="margin:8px 0 16px 0;">
      {_csrf_input()}
      <input type="hidden" name="return_to" value="stock">
      <button type="submit" class="btn btn-amber">🔄 재분석</button>
    </form>'''
    return f'<div class="page-header"><h1>{escape(name)} ({escape(row["cache_key"])})</h1></div>{bar}{reanalyze_form}'


def _render_no_cache(symbol: str, name: str) -> str:
    """캐시 miss 안내 페이지."""
    return f'''
    <div class="page-header"><h1>{escape(name)} ({escape(symbol)})</h1></div>
    <div class="alert alert-info">⚪ 분석 이력이 없습니다. 아래 버튼으로 첫 분석을 시작하세요.</div>
    <form method="post" action="/analyze/{escape(symbol)}" style="margin:16px 0;">
      {_csrf_input()}
      <input type="hidden" name="return_to" value="stock">
      <button type="submit" class="btn btn-primary">▶ 분석 시작</button>
    </form>'''


_SIGNAL_CLASS = {
    "매수": "signal-buy",
    "매도": "signal-sell",
    "관망": "signal-hold",
}


def _render_signal_badge(value: str | None, score: int | None) -> str:
    """시그널 뱃지 HTML — value 가 None/빈문자열이면 빈 문자열 반환.

    score 양수는 ' +N', 음수는 자동 ' -N', 0 은 sign 없이 ' 0'.
    예: ("매수", 3) → '<span class="signal-badge signal-buy">매수 +3</span>'
    """
    if not value:
        return ""
    cls = _SIGNAL_CLASS.get(value, "signal-hold")
    if score is None:
        score_part = ""
    elif score > 0:
        score_part = f" +{score}"
    elif score < 0:
        score_part = f" {score}"  # 음수 자동 '-'
    else:
        score_part = " 0"
    return f'<span class="signal-badge {cls}">{value}{score_part}</span>'


# ── 모델 설명 (정적) ──────────────────────────────────────────────────────
_MODEL_INFO = {
    "rf": {
        "name": "RF (Random Forest)",
        "desc": (
            "여러 결정 트리를 무작위 샘플링으로 학습 시키고 다수결로 결정한다. "
            "비선형 패턴 포착에 강하고 과적합 저항성이 높음. "
            "<strong>강점</strong>: 안정적이고 해석 가능. "
            "<strong>약점</strong>: 시간 의존성을 직접 모델링하지 않음."
        ),
    },
    "lgbm": {
        "name": "LGBM (LightGBM)",
        "desc": (
            "그래디언트 부스팅 트리. 약한 학습기를 순차적으로 쌓아 잔차를 줄인다. "
            "leaf-wise 성장으로 학습 빠르고 메모리 효율적. "
            "<strong>강점</strong>: 정확도 높고 학습 빠름. "
            "<strong>약점</strong>: 작은 데이터에 과적합 가능."
        ),
    },
    "lstm": {
        "name": "LSTM (Long Short-Term Memory)",
        "desc": (
            "순환 신경망 변형. 게이트 구조로 시계열의 장기 의존성을 학습. "
            "긴 추세 포착에 강함. "
            "<strong>강점</strong>: 시계열 패턴 모델링. "
            "<strong>약점</strong>: 학습 느리고 데이터를 많이 요구함."
        ),
    },
    "transformer": {
        "name": "Transformer",
        "desc": (
            "어텐션 메커니즘 기반. 시계열 임의 위치 간 관계를 동시에 가중. "
            "최근 NLP·시계열에서 SOTA. "
            "<strong>강점</strong>: 긴/복잡한 패턴. "
            "<strong>약점</strong>: 작은 데이터에서 과적합 위험, 연산량 큼."
        ),
    },
    "ensemble": {
        "name": "Ensemble (앙상블)",
        "desc": (
            "위 4개 모델 (RF, LGBM, LSTM, Transformer) 의 예측을 가중 평균/투표로 결합. "
            "단일 모델의 약점을 상쇄해 안정성을 높인다. "
            "<strong>강점</strong>: 평균적으로 가장 신뢰할 만한 신호. "
            "<strong>약점</strong>: 개별 모델보다 해석이 어려움."
        ),
    },
}


def _render_model_tabs() -> str:
    """CSS-only 모델 설명 탭바 (radio + label + :checked 셀렉터)."""
    radios = []
    labels = []
    panels = []
    for i, key in enumerate(("rf", "lgbm", "lstm", "transformer", "ensemble")):
        info = _MODEL_INFO[key]
        checked = " checked" if i == 0 else ""
        short = info["name"].split(" (")[0]
        radios.append(
            f'<input type="radio" name="model-tab" id="mtab-{key}" class="mtab-radio"{checked}>'
        )
        labels.append(
            f'<label for="mtab-{key}" class="mtab-label mtab-label-{key}">{short}</label>'
        )
        panels.append(
            f'<section class="mtab-panel mtab-panel-{key}">'
            f'<h3>{info["name"]}</h3><p>{info["desc"]}</p>'
            f'</section>'
        )
    return (
        f'<div class="model-tabs">'
        f'{"".join(radios)}'
        f'<div class="mtab-list">{"".join(labels)}</div>'
        f'<div class="mtab-panels">{"".join(panels)}</div>'
        f'</div>'
    )


def _hit_rate_card(name: str, pct: float | None, n: int) -> str:
    if pct is None:
        return (
            f'<div class="hit-rate-card empty">'
            f'<div class="name">{name}</div>'
            f'<div class="value">—</div>'
            f'<div class="n">평가 없음</div></div>'
        )
    color = "var(--green-600)" if pct >= 60 else ("var(--amber-500)" if pct >= 50 else "var(--red-600)")
    return (
        f'<div class="hit-rate-card">'
        f'<div class="name">{name}</div>'
        f'<div class="value" style="color:{color};">{pct:.1f}%</div>'
        f'<div class="n">{n}회 평가</div></div>'
    )


def _render_hit_rate_summary(rates: dict) -> str:
    """모델 5개 hit rate 요약 카드 그리드. 비어있으면 안내 alert."""
    if not rates:
        return '<div class="alert alert-info">평가된 예측이 아직 없습니다.</div>'
    label = {"rf": "RF", "lgbm": "LGBM", "lstm": "LSTM",
             "transformer": "Transformer", "ensemble": "Ensemble"}
    cards = []
    for m in ("rf", "lgbm", "lstm", "transformer", "ensemble"):
        info = rates.get(m)
        if info is None:
            cards.append(_hit_rate_card(label[m], None, 0))
        else:
            cards.append(_hit_rate_card(label[m], info["hit_rate"] * 100, info["n"]))
    return f'<div class="hit-rate-grid">{"".join(cards)}</div>'


def _pred_cell(m: dict | None) -> str:
    """시간순 표의 모델 셀 — 방향 + 신뢰도 % + hit/miss/pending 색상.

    DB 의 confidence 컬럼은 이미 0~100 단위 (percentage) 로 저장됨.
    예측 모델이 반환하는 값을 그대로 저장하며, 별도 정규화 X.
    """
    if m is None:
        return '<td>—</td>'
    arrow = "🔼" if m["direction"] == "상승" else "🔽"
    # round-half-up (Python 의 round 는 banker's rounding 이라 68.5 → 68)
    pct = int(m["confidence"] + 0.5)
    if m.get("hit") is None:
        cls = "pending"
    elif m["hit"] == 1:
        cls = "hit"
    else:
        cls = "miss"
    return f'<td class="pred-cell pred-{cls}">{arrow}{pct}%</td>'


def _render_history_table(rows: list[dict]) -> str:
    """시간순 예측 히스토리 표 — list_history 결과 기반."""
    head = (
        "<thead><tr>"
        "<th>분석일</th><th>기준 종가</th>"
        "<th>RF</th><th>LGBM</th><th>LSTM</th><th>Transf</th><th>Ensemble</th>"
        "<th>실제 종가</th><th>판정</th>"
        "</tr></thead>"
    )
    body_rows = []
    for r in rows:
        date_str = _format_kst(r["target_date"]).split()[0]  # 'YYYY-MM-DD'
        base = f"{r['base_close']:,.0f}"
        cells = "".join(
            _pred_cell(r["models"].get(m))
            for m in ("rf", "lgbm", "lstm", "transformer", "ensemble")
        )
        if r["actual_close"] is None:
            actual = "—"
            verdict = '<span class="badge-pending">평가 대기</span>'
            row_attr = ' class="row-pending"'
        else:
            actual = f"{r['actual_close']:,.0f}"
            verdict = (
                '<span class="badge-hit">✅ 적중</span>'
                if r["ensemble_hit"] == 1
                else '<span class="badge-miss">❌ 빗나감</span>'
            )
            row_attr = ""
        body_rows.append(
            f"<tr{row_attr}><td>{date_str}</td><td class='num'>{base}</td>"
            f"{cells}<td class='num'>{actual}</td><td>{verdict}</td></tr>"
        )
    return (
        f'<table class="history-table">{head}'
        f'<tbody>{"".join(body_rows)}</tbody>'
        f'</table>'
    )


def _render_prediction_history(symbol: str) -> str:
    """예측 정확도 섹션 — 헤더 + 요약 카드 + 모델 탭바 + 시간순 표 (<details>)."""
    rates = prediction_history.hit_rate_by_model(symbol, source="live")
    rows = prediction_history.list_history(symbol, days=90)
    if not rates and not rows:
        return ""

    summary = _render_hit_rate_summary(rates)
    tabs = _render_model_tabs()

    if rows:
        details_inner = _render_history_table(rows)
        details_summary_text = f"최근 90일 예측 히스토리 ({len(rows)}회) — 클릭하여 펼치기"
    else:
        details_inner = "<p>아직 평가된 예측 이력이 없습니다.</p>"
        details_summary_text = "최근 90일 예측 히스토리"

    details = (
        f'<details class="history-details">'
        f'<summary>{details_summary_text}</summary>'
        f'<div style="overflow-x:auto;">{details_inner}</div>'
        f'</details>'
    )
    header = '<div class="page-header" style="margin-top:32px;"><h2>예측 정확도</h2></div>'
    return header + summary + tabs + details


def _render_stock_with_overlay(symbol: str, name: str, row: dict | None, job_id: str) -> str:
    """`?job=<id>` 진행 중인 분석에 대해 캐시(흐리게) + 오버레이 + 폴링 JS 렌더."""
    job = _jobs_snapshot()[job_id]
    started = job["started_at"]
    when_html = f'<span style="color:var(--slate-500); font-size:0.9em;">시작 {started}</span>'

    if row is not None:
        when = _format_kst(row["generated_at"])
        meta = (
            f'<div class="alert alert-info">🟢 분석 시각: {when} · {row["source"]} · '
            f'<strong>🔄 재분석 중...</strong> {when_html}</div>'
        )
        existing = f'<div class="card result-frame" style="opacity:0.5;pointer-events:none;">{row["result_html"]}</div>'
    else:
        meta = (
            f'<div class="alert alert-info">🔄 첫 분석 진행 중 — {when_html}</div>'
        )
        existing = ""

    overlay = '''
    <div style="text-align:center;padding:32px;background:var(--blue-50);border:1px solid var(--blue-100);border-radius:10px;margin:16px 0;">
      <div class="spinner"></div>
      <p style="margin-top:12px;font-weight:600;color:var(--blue-800);">⏳ 새 분석 진행 중 (예상 30~60초)</p>
    </div>
    '''

    polling_js = f'''
    <script>
    (() => {{
      const jobId = "{job_id}";
      const tick = async () => {{
        try {{
          const res = await fetch(`/api/jobs/${{jobId}}`);
          if (res.status === 404) {{ window.location.replace(window.location.pathname); return; }}
          const data = await res.json();
          if (data.status === "done" || data.status === "error") {{
            window.location.replace(window.location.pathname);
            return;
          }}
        }} catch (_) {{ }}
        setTimeout(tick, 2000);
      }};
      setTimeout(tick, 2000);
    }})();
    </script>'''

    title = f'<div class="page-header"><h1>{escape(name)} ({escape(symbol)})</h1></div>'
    body = f"{title}{meta}{overlay}{existing}"
    return _page(f"{name} 재분석 중", body, polling_js)


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

@app.route("/logout")
def logout():
    """Basic Auth 로그아웃 — 401 + Clear-Site-Data 로 브라우저 자격 캐시 무효화 시도.

    Basic Auth 는 stateless 라 진짜 server-side logout 은 불가능.
    realm 을 평소 'stock-analyzer' 와 다른 'logout' 으로 응답해 일부 브라우저가
    자격 캐시를 invalidate 하도록 유도. Chrome/Firefox 는 Clear-Site-Data 도 인식.
    Safari 등은 탭 닫기까지 자격이 유지될 수 있음 (브라우저 한계).
    """
    body = """<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<title>로그아웃 — Stock Analyzer</title>
<style>
body{font-family:-apple-system,'Fira Sans',sans-serif;background:#F8FAFC;color:#0F172A;
     min-height:100vh;display:flex;align-items:center;justify-content:center;}
.card{background:#fff;padding:48px 56px;border-radius:12px;
      box-shadow:0 4px 6px rgba(0,0,0,0.07);text-align:center;max-width:420px;}
h1{color:#1E3A8A;font-size:1.4rem;margin-bottom:12px;}
p{color:#64748B;margin:8px 0;line-height:1.6;}
a{display:inline-block;margin-top:20px;color:#2563EB;text-decoration:none;
  border:1.5px solid #2563EB;padding:8px 20px;border-radius:7px;font-weight:600;}
a:hover{background:#EFF6FF;}
</style></head><body>
<div class="card">
  <h1>로그아웃되었습니다</h1>
  <p>다른 사용자로 로그인하려면 아래 버튼을 누르세요.</p>
  <p style="font-size:0.82rem;color:#94A3B8;">
    일부 브라우저는 탭을 닫아야 자격이 완전히 사라집니다.
  </p>
  <a href="/">다시 로그인</a>
</div>
</body></html>"""
    return Response(
        body,
        401,
        {
            "WWW-Authenticate": 'Basic realm="logout"',
            "Clear-Site-Data": '"*"',
            "Content-Type": "text/html; charset=utf-8",
        },
    )


@app.route("/")
def index():
    config = _load_config()
    stocks = _get_all_stocks(config)
    jobs = _jobs_snapshot()

    # 에러 메시지 배너
    error_msg = request.args.get("error", "")
    error_banner = (
        f'<div class="alert alert-error">{_ICON_WARN}<span>{escape(error_msg)}</span></div>'
        if error_msg else ""
    )

    # 진행 중인 작업 배너
    running = [j for j in jobs.values() if j["status"] == "running"]
    running_banner = ""
    if running:
        items = ", ".join(f"<strong>{escape(j['name'])}</strong>" for j in running)
        running_banner = f"""
        <div class="running-banner">
          <span class="spinner"></span>
          분석 진행 중: {items}
          <a href="/jobs" style="margin-left:auto;">작업 내역 보기 →</a>
        </div>"""

    # 종목 카드
    cards = []
    now_ts = int(time.time())
    for s in stocks:
        badge_cls = "badge-korea" if s["market"] == "korea" else "badge-us"
        market_label = "한국" if s["market"] == "korea" else "미국"
        is_running = any(
            j["symbol"] == s["symbol"] and j["status"] == "running"
            for j in jobs.values()
        )

        # 신선도 줄
        cache_row = _safe_cache_get(s["symbol"])
        if cache_row is None:
            freshness_line = '<div style="font-size:0.78rem;color:var(--slate-500);">⚪ 분석 이력 없음</div>'
            primary_btn = f'''
            <form method="post" action="/analyze/{s["symbol"]}" style="display:inline; margin:0;">
              {_csrf_input()}
              <input type="hidden" name="return_to" value="jobs">
              <button type="submit" class="btn btn-primary btn-sm">{_ICON_PLAY} 분석 시작</button>
            </form>'''
        else:
            fresh = analysis_cache.is_fresh(cache_row, now_ts)
            when = _format_kst(cache_row["generated_at"])
            mark = "🟢" if fresh else "🟡"
            color = "var(--green-600)" if fresh else "#92400E"
            freshness_line = f'<div style="font-size:0.78rem;color:{color};">{mark} {when}</div>'
            primary_btn = f'<a class="btn btn-primary btn-sm" href="/stock/{s["symbol"]}">{_ICON_PLAY} 결과 보기</a>'

        if is_running:
            primary_btn = f'<span class="btn btn-primary btn-sm btn-disabled">{_ICON_PLAY} 분석 중</span>'

        # 캐시 있을 때만 별도 재분석 아이콘 (카드 클릭 → /jobs 흐름)
        reanalyze_btn = ""
        if cache_row is not None and not is_running:
            reanalyze_btn = f'''
            <form method="post" action="/analyze/{s["symbol"]}" style="display:inline; margin:0;">
              {_csrf_input()}
              <input type="hidden" name="return_to" value="jobs">
              <button type="submit" class="btn btn-amber btn-sm" title="재분석">🔄</button>
            </form>'''

        signal_badge_html = _render_signal_badge(
            cache_row.get("signal_value") if cache_row else None,
            cache_row.get("signal_score") if cache_row else None,
        )
        cards.append(f"""
        <div class="stock-card">
          <div class="stock-card-header">
            <div class="stock-card-info">
              <h3>{escape(s['name'])}</h3>
              <div class="symbol">{escape(s['symbol'])}</div>
            </div>
            <div class="stock-card-badges">
              {signal_badge_html}
              <span class="badge {badge_cls}">{market_label}</span>
            </div>
          </div>
          {freshness_line}
          <div class="stock-card-actions">
            {primary_btn}
            {reanalyze_btn}
            <form method="post" action="/stocks/delete" style="margin:0;"
                  onsubmit="return confirm('{escape(s['name'])} 종목을 삭제하시겠습니까?');">
              {_csrf_input()}
              <input type="hidden" name="symbol" value="{s['symbol']}">
              <button type="submit" class="btn btn-danger btn-sm">{_ICON_TRASH} 삭제</button>
            </form>
          </div>
        </div>""")

    # 종목 추가 폼
    add_form = f"""
    <div class="card">
      <div class="card-title">{_ICON_PLUS} 종목 추가</div>
      <form method="post" action="/stocks/add">
        {_csrf_input()}
        <div class="add-form">
          <div class="field autocomplete-wrap">
            <label for="stock-search-input">검색</label>
            <input name="symbol" id="stock-search-input"
                   role="combobox" aria-autocomplete="list"
                   aria-expanded="false" aria-controls="autocomplete-list"
                   placeholder="종목명 또는 심볼 검색" autocomplete="off"
                   required style="width:240px;">
            <div id="autocomplete-list" class="autocomplete-list"
                 role="listbox" aria-label="종목 검색 결과"></div>
          </div>
          <div class="field">
            <label>종목명</label>
            <input name="name" placeholder="예: Apple" required style="width:160px;">
          </div>
          <div class="field">
            <label>시장</label>
            <select name="market" style="width:110px;">
              <option value="us">미국</option>
              <option value="korea">한국</option>
            </select>
          </div>
          <div class="field">
            <label>&nbsp;</label>
            <button type="submit" class="btn btn-success">{_ICON_PLUS} 추가</button>
          </div>
        </div>
      </form>
    </div>"""

    # 전체 분석 버튼
    analyze_all_form = f"""
    <form method="post" action="/analyze-all">
      {_csrf_input()}
      <button type="submit" class="btn btn-amber">{_ICON_PLAY} 전체 종목 일괄 분석</button>
    </form>"""

    # 종목 없을 때 빈 상태
    stock_section = f'<div class="stock-grid">{"".join(cards)}</div>' if cards else """
    <div class="empty-state">
      <svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 9h6M9 13h6M9 17h4"/></svg>
      <p>등록된 종목이 없습니다. 종목을 추가해보세요.</p>
    </div>"""

    body = f"""
    <div class="page-header">
      <h1>대시보드</h1>
      <p>종목 관리 및 기술적 분석 · ML 예측</p>
    </div>
    {error_banner}
    {running_banner}
    {add_form}
    <div class="toolbar">
      <div class="toolbar-left">
        <span style="font-size:0.875rem;color:var(--slate-500);">
          총 <strong style="color:var(--slate-900);">{len(stocks)}</strong>개 종목
        </span>
      </div>
      {analyze_all_form}
    </div>
    {stock_section}"""

    refresh_script = "<script>setTimeout(()=>location.reload(),5000);</script>" if running else ""
    return _page("대시보드", body, refresh_script + _AUTOCOMPLETE_JS)


@app.route("/analyze/<path:symbol>", methods=["POST"])
def analyze(symbol: str):
    _csrf_validate()

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

    return_to = request.form.get("return_to", "jobs")
    if return_to not in ("jobs", "stock"):
        return_to = "jobs"
    if return_to == "stock":
        return redirect(f"/stock/{symbol}?job={job_id}", code=303)
    return redirect(f"/jobs/{job_id}", code=303)


@app.route("/stock/<path:symbol>")
def stock_view(symbol: str):
    symbol = sanitize_stock_symbol(symbol)
    if not validate_stock_symbol(symbol):
        abort(400)

    name = symbol
    for s in _get_all_stocks(_load_config()):
        if s["symbol"] == symbol:
            name = s["name"]
            break

    row = _safe_cache_get(symbol)
    job_id = request.args.get("job", "").strip()

    # 진행 중 → 오버레이 + 폴링 (Task 11)
    job = _jobs_snapshot().get(job_id) if job_id else None
    if job and job["status"] == "running":
        return _render_stock_with_overlay(symbol, name, row, job_id)

    # job_id 가 있는데 종료 상태 → PRG redirect 로 쿼리 제거
    if job_id and (not job or job["status"] != "running"):
        return redirect(f"/stock/{symbol}", code=303)

    if row is None:
        return _page(f"{name} 분석", _render_no_cache(symbol, name))

    fresh = analysis_cache.is_fresh(row, int(time.time()))
    body_parts = [
        _render_meta_bar(row, fresh, name),
        f'<div class="card result-frame">{row["result_html"]}</div>',
    ]
    try:
        body_parts.append(_render_prediction_history(symbol))
    except Exception as e:
        logger.warning("prediction_history 렌더 실패 — %s: %s", symbol, e)
    return _page(f"{name} 분석 결과", "".join(body_parts))


@app.route("/stock/all")
def stock_all_view():
    row = _safe_cache_get("ALL")
    if row is None:
        body = f'''
        <div class="page-header"><h1>전체 종목 분석</h1></div>
        <div class="alert alert-info">⚪ 전체 분석 이력이 없습니다.</div>
        <form method="post" action="/analyze-all" style="margin:16px 0;">
          {_csrf_input()}
          <button type="submit" class="btn btn-amber">▶ 전체 분석 시작</button>
        </form>'''
        return _page("전체 분석", body)

    fresh = analysis_cache.is_fresh(row, int(time.time()))
    when = _format_kst(row["generated_at"])
    if fresh:
        bar = f'<div class="alert alert-info">🟢 분석 시각: {when} · {row["source"]}</div>'
    else:
        bar = (
            f'<div class="alert alert-error" style="background:#FEF3C7;color:#92400E;border-color:#FDE68A;">'
            f'🟡 분석 시각: {when} · {row["source"]}<br>⚠️ 일부 종목이 만료되었습니다. 재분석 권장.'
            f'</div>'
        )
    reanalyze = f'''
    <form method="post" action="/analyze-all" style="margin:8px 0 16px 0;">
      {_csrf_input()}
      <button type="submit" class="btn btn-amber">🔄 전체 재분석</button>
    </form>'''
    body = f'<div class="page-header"><h1>전체 종목 분석</h1></div>{bar}{reanalyze}<div class="card result-frame">{row["result_html"]}</div>'
    return _page("전체 분석", body)


@app.route("/analyze-all", methods=["POST"])
def analyze_all():
    _csrf_validate()
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
        if j["status"] == "running":
            status_html = f'<span class="status-pill status-running"><span class="spinner"></span> 분석 중</span>'
        elif j["status"] == "done":
            status_html = '<span class="status-pill status-done">완료</span>'
        else:
            status_html = '<span class="status-pill status-error">오류</span>'

        link = f'<a class="btn btn-primary btn-sm" href="/jobs/{jid}">결과 보기</a>' if j["status"] == "done" else ""
        rows.append(f"""
        <tr>
          <td class="mono">{j['started_at']}</td>
          <td><strong>{escape(j['name'])}</strong> <span class="mono" style="color:var(--slate-500);">({escape(j['symbol'])})</span></td>
          <td>{status_html}</td>
          <td style="text-align:right;">{link}</td>
        </tr>""")

    empty = '<tr><td colspan="4"><div class="empty-state" style="padding:32px;"><p>작업 없음</p></div></td></tr>'
    table = f"""
    <table class="data-table">
      <thead>
        <tr>
          <th>시작 시각</th>
          <th>종목</th>
          <th>상태</th>
          <th></th>
        </tr>
      </thead>
      <tbody>{''.join(rows) if rows else empty}</tbody>
    </table>"""

    has_running = any(j["status"] == "running" for j in jobs.values())
    refresh = "<script>setTimeout(()=>location.reload(),3000);</script>" if has_running else ""

    body = f"""
    <div class="page-header">
      <h1>작업 내역</h1>
      <p>백그라운드 분석 작업 현황</p>
    </div>
    <div class="card" style="padding:0;overflow:hidden;">{table}</div>"""
    return _page("작업 내역", body, refresh)


@app.route("/jobs/<job_id>")
def job_detail(job_id: str):
    job = _jobs_snapshot().get(job_id)
    if not job:
        body = f"""
        <div class="page-header"><h1>오류</h1></div>
        <div class="alert alert-error">{_ICON_WARN}<span>작업을 찾을 수 없습니다.</span></div>"""
        return _page("오류", body)

    if job["status"] == "running":
        body = f"""
        <div class="page-header">
          <h1>{escape(job['name'])} 분석 중</h1>
          <p>잠시 후 자동으로 새로고침됩니다.</p>
        </div>
        <div class="alert alert-info">
          {_ICON_INFO}
          <span><span class="spinner"></span>&nbsp; <strong>{escape(job['name'])}</strong> ({escape(job['symbol'])}) 분석 진행 중 — 시작: {job['started_at']}</span>
        </div>"""
        refresh = "<script>setTimeout(()=>location.reload(),3000);</script>"
        return _page(f"{job['name']} 분석 중", body, refresh)

    if job["status"] == "error":
        body = f"""
        <div class="page-header"><h1>분석 실패</h1></div>
        <div class="alert alert-error">{_ICON_WARN}<span>{escape(job['error'])}</span></div>"""
        return _page("분석 실패", body)

    backtest_form_html = ""
    if not job["name"].endswith("백테스트"):
        backtest_form_html = f'''
    <form method="post" action="/backtest/{escape(job['symbol'])}" style="margin:24px 0;">
      {_csrf_input()}
      <button type="submit" class="btn btn-amber">
        🔬 백테스트 실행 (RF+LGBM, 6개월 walk-forward)
      </button>
    </form>
    '''

    body = f"""
    <div class="page-header">
      <h1>{escape(job['name'])} 분석 결과</h1>
      <p>시작: {job['started_at']}</p>
    </div>
    <div style="margin-bottom:16px;">
      <a class="btn btn-primary" href="/jobs/{job_id}/download">{_ICON_DL} HTML 다운로드</a>
    </div>
    <div class="card result-frame">{job["result_html"]}</div>
    {backtest_form_html}"""
    return _page(f"{job['name']} 분석 결과", body)


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


@app.route("/backtest/<path:symbol>", methods=["POST"])
def start_backtest(symbol: str):
    """백테스트 실행 트리거. 동시 1개로 제한."""
    _csrf_validate()
    symbol = sanitize_stock_symbol(symbol)
    if not validate_stock_symbol(symbol):
        abort(400)

    if not _backtest_lock.acquire(blocking=False):
        return redirect(
            url_for("index", error="다른 백테스트가 실행 중입니다. 잠시 후 다시 시도하세요."),
            code=303,
        )

    job_id = uuid.uuid4().hex[:8]
    backtest_id = uuid.uuid4().hex[:8]
    with _jobs_lock:
        _jobs[job_id] = {
            "status": "running",
            "symbol": symbol,
            "name": f"{symbol} 백테스트",
            "result_html": None,
            "error": None,
            "started_at": datetime.now().strftime("%H:%M:%S"),
        }

    threading.Thread(
        target=_run_backtest_bg,
        args=(job_id, symbol, backtest_id),
        daemon=True,
    ).start()
    return redirect(f"/jobs/{job_id}", code=303)


def _run_backtest_bg(job_id: str, symbol: str, backtest_id: str) -> None:
    """백그라운드 백테스트 실행. 성공/실패 모두 _backtest_lock 해제."""
    logger.info("백테스트 시작: job_id=%s symbol=%s backtest_id=%s",
                job_id, symbol, backtest_id)
    try:
        from src.data_fetcher import fetch_stock_data
        from src.technical_analysis import compute_indicators

        df = fetch_stock_data(symbol)
        df = compute_indicators(df)
        result = bt.walk_forward(symbol, df, days=126)

        if result.get("error"):
            _jobs_set(job_id, status="error", error=result["error"])
            return

        prediction_history.insert_backtest(result["rows"], backtest_id)
        html_out = _render_backtest_report(symbol, result)
        _jobs_set(job_id, status="done", result_html=html_out)
        logger.info("백테스트 완료: job_id=%s", job_id)
    except Exception as e:
        logger.exception("백테스트 실패: %s", e)
        _jobs_set(job_id, status="error", error=str(e))
    finally:
        _backtest_lock.release()
        _trim_jobs()


def _render_backtest_report(symbol: str, result: dict) -> str:
    """백테스트 결과 HTML 렌더."""
    summary = result["summary"]
    rows_html = []
    model_label = {"rf": "RandomForest", "lgbm": "LightGBM", "ensemble": "Ensemble (RF+LGBM)"}
    for model in ("rf", "lgbm", "ensemble"):
        info = summary.get(model)
        if not info:
            continue
        pct = info["hit_rate"] * 100
        rows_html.append(
            f"<tr><td>{model_label[model]}</td>"
            f"<td>{pct:.1f}%</td>"
            f"<td>{info['n']}</td></tr>"
        )
    return f"""
    <h2>{escape(symbol)} 백테스트 결과 (6개월 walk-forward)</h2>
    <table style="margin:16px 0;">
      <thead><tr><th>모델</th><th>Hit Rate</th><th>평가 횟수</th></tr></thead>
      <tbody>{''.join(rows_html)}</tbody>
    </table>
    <p style="color:#666; font-size:0.9em;">
      backtest_id: <code>{escape(result['backtest_id'])}</code><br>
      ⚠️ 백테스트 결과는 과거 데이터 기반이며, 미래 수익을 보장하지 않습니다.
    </p>
    """


@app.route("/stocks/add", methods=["POST"])
def stocks_add():
    _csrf_validate()
    logger.info("종목 추가 요청: symbol=%s name=%s market=%s",
                request.form.get("symbol"), request.form.get("name"), request.form.get("market"))
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
    _csrf_validate()
    logger.info("종목 삭제 요청: symbol=%s", request.form.get("symbol"))
    symbol = request.form.get("symbol", "").strip()
    if not symbol:
        return redirect(url_for("index"))

    with _config_lock:
        config = _load_config()
        for market, group in config.get("stocks", {}).items():
            config["stocks"][market] = [s for s in group if s["symbol"] != symbol]
        _save_config(config)

    return redirect(url_for("index"), code=303)


def run_web(host: str = "0.0.0.0", port: int = 8080, debug: bool = False,
            production: bool = False) -> None:
    """Flask 웹 서버를 실행한다.

    Args:
        host: 바인딩 호스트 (기본: 0.0.0.0)
        port: 바인딩 포트 (기본: 8080)
        debug: 디버그 모드 (기본: False, 보안상 프로덕션에서는 False 권장)
        production: Gunicorn으로 실행 (기본: False)
    """
    if production:
        import shutil
        import subprocess
        import sys
        from pathlib import Path

        project_root = Path(__file__).resolve().parent.parent
        conf = project_root / "gunicorn.conf.py"
        os.environ.setdefault("PORT", str(port))

        # gunicorn 실행 파일 탐색: 현재 Python 환경 → 프로젝트 .venv → PATH
        gunicorn_bin = (
            Path(sys.executable).parent / "gunicorn"
        )
        if not gunicorn_bin.exists():
            gunicorn_bin = project_root / ".venv" / "bin" / "gunicorn"
        if not gunicorn_bin.exists():
            found = shutil.which("gunicorn")
            gunicorn_bin = Path(found) if found else None

        if gunicorn_bin is None or not gunicorn_bin.exists():
            raise RuntimeError(
                "gunicorn을 찾을 수 없습니다. "
                "pip install gunicorn 또는 venv를 활성화하세요."
            )

        cmd = [str(gunicorn_bin), "-c", str(conf), "src.web_app:app"]
        logger.info("프로덕션 모드 시작 (Gunicorn): %s", " ".join(cmd))
        subprocess.run(cmd, check=True, cwd=str(project_root))
    else:
        logger.warning("개발 서버로 실행 중입니다. 프로덕션에서는 --prod 플래그를 사용하세요.")
        app.run(host=host, port=port, debug=debug)
