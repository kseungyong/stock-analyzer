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
from flask import Flask, abort, flash, request, redirect, session, url_for, jsonify, Response, render_template

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
from markupsafe import escape, Markup

from src.validators import validate_stock_symbol, validate_stock_name, sanitize_stock_symbol, is_valid_search_query
from src.stock_search import search_stocks
from src import prediction_history
from src import backtest as bt
from src import analysis_cache
from src import portfolio as portfolio_db
from src import pattern_metadata as _pattern_meta
from src import pattern_popup as _pattern_popup

portfolio_db.init_db()

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
def _session_auth_gate():
    """ENABLE_BASIC_AUTH=1 일 때 모든 요청에 세션 검증.

    환경변수 이름은 호환성으로 ENABLE_BASIC_AUTH 유지하지만 인증 메커니즘은
    Flask session 기반. /login (GET, POST) 와 /logout 은 우회.
    /api/* 는 machine-to-machine 호출 (예: auto-trader analyzer push) 을
    위해 HTTP Basic Auth header 도 함께 허용.
    """
    if not _basic_auth_on:
        return None
    if request.path in ("/login", "/logout"):
        return None
    if session.get("username") in _basic_auth_users:
        return None
    # /api/* — machine-to-machine — Basic Auth header 폴백
    if request.path.startswith("/api/"):
        auth = request.authorization
        if auth and auth.username and auth.password is not None:
            expected = _basic_auth_users.get(auth.username)
            if expected is not None and secrets.compare_digest(auth.password, expected):
                return None
        return Response("Authentication required", 401,
                        {"WWW-Authenticate": 'Basic realm="api"'})
    # 그 외 비-GET 은 401 (CSRF 토큰 없는 웹 폼은 어차피 거부됨)
    if request.method != "GET":
        return Response("Authentication required", 401)
    return redirect(url_for("login_view", next=request.path))


def _current_user() -> str:
    """현재 사용자 식별 — 인증 ON 이면 session.username, OFF 면 'default'."""
    if not _basic_auth_on:
        return "default"
    return session.get("username") or "default"


CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"

# 백그라운드 작업 저장소: {job_id: {status, symbol, name, result_html, error, started_at}}
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_JOBS_MAX = 50  # 완료된 작업 보관 최대 개수

_backtest_lock = threading.Lock()  # 글로벌 백테스트 동시 실행 1개로 제한

_config_lock = threading.RLock()  # settings.yaml read-modify-write 보호 (재진입 허용)

_leaders_refresh_lock = threading.Lock()  # 주도주 LLM 재분석 동시 실행 방지 (spec §C.1)


# ---------------------------------------------------------------------------
# CSRF helpers
# ---------------------------------------------------------------------------

def _csrf_token() -> str:
    """세션에 CSRF 토큰이 없으면 생성 후 반환한다."""
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)
    return session["csrf_token"]


def _csrf_input() -> Markup:
    """POST 폼에 삽입할 숨김 CSRF 입력 필드 HTML을 반환한다.

    Markup 으로 래핑 — Jinja2 autoescape 가 HTML 을 text 로 변환하는 버그 방지.
    """
    return Markup(f'<input type="hidden" name="csrf_token" value="{_csrf_token()}">')


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
        config = yaml.safe_load(f)
    # foreign_ranking 자동 종목(overlay) 머지 — settings.yaml 은 사용자 종목만 보관
    from src.universe import apply_overlay
    return apply_overlay(config)


def _save_config(config: dict) -> None:
    # overlay(foreign_ranking) 종목은 settings.yaml 에 쓰지 않음 (별도 파일로 분리)
    from src.universe import strip_overlay
    with _config_lock:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.dump(strip_overlay(config), f, allow_unicode=True,
                      default_flow_style=False)


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

        market = _market_of(symbol)
        result = analyze_stock(symbol, name, market=market)
        if result is None:
            logger.warning("분석 결과 없음: job_id=%s symbol=%s", job_id, symbol)
            _jobs_set(job_id, status="error", error=f'"{symbol}" 분석 중 오류 발생')
        else:
            html = generate_report([result])
            _jobs_set(job_id, status="done", result_html=html)
            try:
                sig = result.get("signal") or {}
                bnf = result.get("bnf_signal") or {}
                patterns = result.get("patterns") or {}
                pat_summary = patterns.get("summary") or {}
                import json as _json
                analysis_cache.put(
                    symbol, market, html, source="manual",
                    signal_value=sig.get("signal"),
                    signal_score=sig.get("score"),
                    bnf_signal_value=bnf.get("signal"),
                    bnf_signal_score=bnf.get("score"),
                    pattern_json=_json.dumps(patterns, ensure_ascii=False) if patterns else None,
                    pattern_signal=pat_summary.get("signal"),
                    pattern_score=pat_summary.get("score"),
                    last_close=result.get("last_close"),
                    rel_perf_json=_json.dumps(result["rel_perf"], ensure_ascii=False)
                                  if result.get("rel_perf") else None,
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
    """settings.yaml 에서 symbol 의 시장 (korea/us) 을 찾는다. 없으면 heuristic 적용.

    .KS / .KQ / 6자리 숫자 → korea, 그 외 → us.
    이전에는 universe 외 종목도 silent us 처리 → 한국 종목 yfinance 잘못 fetch 위험.
    """
    config = _load_config()
    for market, stocks in config.get("stocks", {}).items():
        for s in stocks:
            if s["symbol"] == symbol:
                return market
    # Heuristic fallback (수동 분석 trigger 등 universe 외 종목)
    if symbol.endswith((".KS", ".KQ")):
        return "korea"
    if symbol.isdigit() and len(symbol) == 6:
        return "korea"
    logger.warning(
        "_market_of: %s not in universe, defaulting to 'us' (heuristic)", symbol,
    )
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
                bnf = r.get("bnf_signal") or {}
                patterns = r.get("patterns") or {}
                pat_summary = patterns.get("summary") or {}
                import json as _json
                analysis_cache.put(
                    sym, symbol_to_market.get(sym, "us"), ind_html, source="manual",
                    signal_value=sig.get("signal"),
                    signal_score=sig.get("score"),
                    bnf_signal_value=bnf.get("signal"),
                    bnf_signal_score=bnf.get("score"),
                    pattern_json=_json.dumps(patterns, ensure_ascii=False) if patterns else None,
                    pattern_signal=pat_summary.get("signal"),
                    pattern_score=pat_summary.get("score"),
                    last_close=r.get("last_close"),
                    rel_perf_json=_json.dumps(r["rel_perf"], ensure_ascii=False)
                                  if r.get("rel_perf") else None,
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


def _render_flashes() -> str:
    """세션의 flash 메시지를 alert 카드로 렌더링한다 (없으면 빈 문자열).

    카테고리 매핑: success → 녹색, warning → 호박색, error → alert-error.
    request 컨텍스트가 없을 때(에러 페이지 등)는 조용히 건너뛴다.
    """
    try:
        from flask import get_flashed_messages
        msgs = get_flashed_messages(with_categories=True)
    except Exception:
        return ""
    if not msgs:
        return ""
    styles = {
        "success": 'background:#DCFCE7;color:#166534;border:1px solid #BBF7D0;',
        "warning": 'background:#FEF3C7;color:#92400E;border:1px solid #FDE68A;',
        "error": "",  # alert-error CSS 클래스 사용
    }
    out = []
    for category, message in msgs:
        cls = "alert alert-error" if category == "error" else "alert"
        style = styles.get(category, styles["success"]) if category != "error" else ""
        style_attr = f' style="{style}"' if style else ""
        out.append(f'<div class="{cls}"{style_attr}>{escape(message)}</div>')
    return "".join(out)


def _page(title: str, body: str, auto_refresh_js: str = "") -> str:
    logout_link = (
        '<a class="topbar-link" href="/logout" style="opacity:0.75;">로그아웃</a>'
        if _basic_auth_on else ""
    )
    user_badge = ""
    try:
        user = _current_user()
        n = portfolio_db.count_holdings(user)
        user_badge = (
            f'<span class="topbar-link" '
            f'style="opacity:0.85;font-size:0.85rem;cursor:default;" '
            f'title="현재 사용자">👤 {escape(user)} · {n}종목</span>'
        )
    except Exception:
        pass  # request 컨텍스트 없을 때 (예: 에러 페이지) 건너뜀
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{title} — Stock Analyzer</title>
  <style>{_CSS}</style>
  <link rel="stylesheet" href="/static/pattern-modal.css">
  <script src="/static/pattern-modal.js" defer></script>
</head>
<body>
<nav class="topbar">
  <a class="topbar-brand" href="/">
    {_ICON_CHART}
    Stock Analyzer
  </a>
  <div class="topbar-nav">
    <a class="topbar-link" href="/">대시보드</a>
    <a class="topbar-link" href="/leaders">주도주</a>
    <a class="topbar-link" href="/foreign-ranking">외인 ranking</a>
    <a class="topbar-link" href="/portfolio">포트폴리오</a>
    <a class="topbar-link" href="/jobs">작업 내역</a>
    {user_badge}
    {logout_link}
  </div>
</nav>
<main class="main">
{_render_flashes()}
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


def _render_signal_badge(
    value: str | None,
    score: int | None,
    prefix: str = "",
) -> str:
    """시그널 뱃지 HTML — value 가 None/빈문자열이면 빈 문자열 반환.

    score 양수는 ' +N', 음수는 자동 ' -N', 0 은 sign 없이 ' 0'.
    prefix 가 있으면 라벨 앞에 붙음 (예: 'BNF 매수 +3').
    """
    if not value:
        return ""
    cls = _SIGNAL_CLASS.get(value, "signal-hold")
    if score is None:
        score_part = ""
    elif score > 0:
        score_part = f" +{score}"
    elif score < 0:
        score_part = f" {score}"
    else:
        score_part = " 0"
    label = f"{prefix}{value}" if prefix else value
    return f'<span class="signal-badge {cls}">{label}{score_part}</span>'


def _pattern_link(pattern_name: str, symbol: str | None = None, date: str | None = None) -> str:
    """패턴 이름 → 모달 트리거 anchor.

    pattern_name: 한글 패턴명 (XSS escape 처리)
    symbol: 선택 — analysis 컨텍스트 있을 때만
    date: 선택 — 다중 검출 식별
    """
    attrs = f'data-pattern="{escape(pattern_name)}"'
    if symbol:
        attrs += f' data-symbol="{escape(symbol)}"'
    if date:
        attrs += f' data-date="{escape(date)}"'
    return f'<a href="#" {attrs}>{escape(pattern_name)}</a>'


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

def _login_page(error: str = "", next_url: str = "/") -> str:
    err_html = (
        f'<div style="background:#FEE2E2;color:#991B1B;padding:10px 14px;'
        f'border-radius:6px;margin-bottom:14px;font-size:0.9rem;">{escape(error)}</div>'
        if error else ""
    )
    safe_next = escape(next_url)
    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>로그인 — Stock Analyzer</title>
<style>
body{{font-family:-apple-system,'Fira Sans',sans-serif;background:#F8FAFC;color:#0F172A;
     min-height:100vh;display:flex;align-items:center;justify-content:center;margin:0;}}
.card{{background:#fff;padding:36px 40px;border-radius:12px;
      box-shadow:0 4px 12px rgba(0,0,0,0.08);max-width:360px;width:100%;
      box-sizing:border-box;}}
h1{{color:#1E3A8A;font-size:1.4rem;margin:0 0 18px 0;text-align:center;}}
label{{display:block;font-size:0.85rem;color:#475569;margin:10px 0 4px 0;}}
input{{width:100%;padding:10px 12px;border:1.5px solid #CBD5E1;
      border-radius:6px;font-size:1rem;box-sizing:border-box;}}
input:focus{{outline:none;border-color:#2563EB;}}
button{{width:100%;margin-top:18px;padding:11px;background:#2563EB;color:#fff;
       border:none;border-radius:6px;font-size:1rem;font-weight:600;cursor:pointer;}}
button:hover{{background:#1D4ED8;}}
</style></head><body>
<div class="card">
  <h1>📊 Stock Analyzer</h1>
  {err_html}
  <form method="post" action="/login">
    <input type="hidden" name="next" value="{safe_next}">
    <label for="username">사용자명</label>
    <input id="username" name="username" autocomplete="username" autofocus required>
    <label for="password">비밀번호</label>
    <input id="password" name="password" type="password" autocomplete="current-password" required>
    <button type="submit">로그인</button>
  </form>
</div>
</body></html>"""


@app.route("/login", methods=["GET", "POST"])
def login_view():
    if not _basic_auth_on:
        return redirect(url_for("index"))
    next_url = request.values.get("next", "/")
    # next URL 검증 — 외부 도메인 redirect 방지 (open redirect 보안)
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = "/"
    if request.method == "GET":
        # 이미 로그인 상태면 즉시 next 로
        if session.get("username") in _basic_auth_users:
            return redirect(next_url)
        return _login_page(next_url=next_url)
    # POST
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    expected = _basic_auth_users.get(username)
    if expected is None or not secrets.compare_digest(password, expected):
        logger.info("login failed user=%s", username)
        return _login_page(error="사용자명 또는 비밀번호가 올바르지 않습니다.",
                           next_url=next_url), 401
    session["username"] = username
    session.permanent = True
    logger.info("login success user=%s", username)
    return redirect(next_url)


@app.route("/logout")
def logout():
    """세션 삭제 → 로그인 페이지."""
    user = session.get("username")
    session.clear()
    if user:
        logger.info("logout user=%s", user)
    if not _basic_auth_on:
        return redirect(url_for("index"))
    return redirect(url_for("login_view"))


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

    # 캐시 미리 일괄 fetch (정렬 + 카드 빌드 양쪽에서 사용)
    cache_by_symbol = {s["symbol"]: _safe_cache_get(s["symbol"]) for s in stocks}

    # DART 공시 요약 — 한번에 fetch
    try:
        from src import dart_cache as _dart_cache
        dart_summaries = _dart_cache.list_summaries()
    except Exception:
        dart_summaries = {}

    # Composite 정렬 — Tech + BNF + Pattern×0.5 (Pattern range ±10 정규화).
    # tier 0: 분석 완료 + 어떤 score 라도 있음 (composite 내림차순)
    # tier 1: 분석 완료 + 모든 score NULL
    # tier 2: 캐시 없음 (분석 안 됨)
    # 같은 composite 면 symbol 사전순.
    def _composite_score(cache: dict | None) -> float:
        if cache is None:
            return 0.0
        tech = cache.get("signal_score") or 0
        bnf = cache.get("bnf_signal_score") or 0
        pat = cache.get("pattern_score") or 0
        return float(tech) + float(bnf) + float(pat) * 0.5

    def _composite_sort_key(stock: dict) -> tuple[int, float, str]:
        cache = cache_by_symbol.get(stock["symbol"])
        if cache is None:
            return (2, 0.0, stock["symbol"])
        has_any = (cache.get("signal_score") is not None
                   or cache.get("bnf_signal_score") is not None
                   or cache.get("pattern_score") is not None)
        if not has_any:
            return (1, 0.0, stock["symbol"])
        return (0, -_composite_score(cache), stock["symbol"])

    stocks = sorted(stocks, key=_composite_sort_key)

    # 종목 카드 (시장별 그룹)
    cards_by_market: dict[str, list[str]] = {"korea": [], "us": []}
    now_ts = int(time.time())
    for s in stocks:
        badge_cls = "badge-korea" if s["market"] == "korea" else "badge-us"
        market_label = "한국" if s["market"] == "korea" else "미국"
        is_running = any(
            j["symbol"] == s["symbol"] and j["status"] == "running"
            for j in jobs.values()
        )

        # 신선도 줄
        cache_row = cache_by_symbol.get(s["symbol"])
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
        bnf_badge_html = _render_signal_badge(
            cache_row.get("bnf_signal_value") if cache_row else None,
            cache_row.get("bnf_signal_score") if cache_row else None,
            prefix="BNF ",
        )
        # Pattern 배지 (Phase A: 이동평균 4상태) — top_patterns 가 있으면 표시
        pattern_badge_html = ""
        if cache_row and cache_row.get("pattern_signal"):
            try:
                import json as _json
                pj = _json.loads(cache_row.get("pattern_json") or "{}")
                tops = (pj.get("summary") or {}).get("top_patterns") or []
                psig = cache_row["pattern_signal"]
                color = {"매수": "#16A34A", "매도": "#DC2626", "사지마": "#D97706", "팔지마": "#D97706"}.get(psig, "#64748B")
                tops_links = [_pattern_link(p, symbol=s["symbol"]) for p in tops[:2]]
                tops_text = " · ".join(tops_links) if tops_links else ""
                if tops_text:
                    pattern_badge_html = (
                        f'<span class="badge" style="background:{color};color:#fff;">📈 {psig}: {tops_text}</span>'
                    )
                else:
                    pattern_badge_html = (
                        f'<span class="badge" style="background:{color};color:#fff;">📈 {psig}</span>'
                    )
            except (ValueError, KeyError):
                pass

        # 알파 배지 — 종목 vs 시장 지수 등락률 (rel_perf_json)
        alpha_badge_html = ""
        if cache_row and cache_row.get("rel_perf_json"):
            try:
                import json as _json
                rp = _json.loads(cache_row["rel_perf_json"])
                alpha_pp = rp.get("alpha_pp")
                if alpha_pp is not None:
                    if alpha_pp > 0:
                        a_color = "#16A34A"
                        sign = "+"
                    elif alpha_pp < 0:
                        a_color = "#DC2626"
                        sign = ""
                    else:
                        a_color = "#64748B"
                        sign = ""
                    idx = rp.get("index_name", "")
                    stock_pct = rp.get("stock_pct", 0.0)
                    index_pct = rp.get("index_pct", 0.0)
                    title = (
                        f"vs {idx}: 종목 {stock_pct:+.2f}% / 지수 {index_pct:+.2f}%"
                    )
                    alpha_badge_html = (
                        f'<span class="badge" style="background:{a_color};color:#fff;font-weight:600;" '
                        f'title="{escape(title)}">α {sign}{alpha_pp:.2f}pp</span>'
                    )
            except (ValueError, KeyError, TypeError):
                pass

        # Composite 배지 — Tech + BNF + Pattern×0.5
        composite_badge_html = ""
        if cache_row is not None:
            comp = _composite_score(cache_row)
            if comp >= 5:
                comp_color = "#16A34A"   # 진초록 — 강한 매수
            elif comp >= 1:
                comp_color = "#65A30D"   # 연초록 — 약매수
            elif comp <= -5:
                comp_color = "#DC2626"   # 진빨강 — 강한 매도
            elif comp <= -1:
                comp_color = "#EA580C"   # 연빨강 — 약매도
            else:
                comp_color = "#64748B"   # 회색 — 관망
            sign = "+" if comp >= 0 else ""
            composite_badge_html = (
                f'<span class="badge" style="background:{comp_color};color:#fff;font-weight:600;" '
                f'title="Tech+BNF+Pattern×0.5">📊 {sign}{comp:.1f}</span>'
            )

        dart_summary_row = dart_summaries.get(s["symbol"])
        dart_badge_html = ""
        if dart_summary_row:
            sent = dart_summary_row.get("sentiment")
            if sent == "긍정":
                dart_badge_html = '<span class="badge" style="background:#16A34A;color:#fff;">🟢 공시+</span>'
            elif sent == "부정":
                dart_badge_html = '<span class="badge" style="background:#DC2626;color:#fff;">🔴 공시-</span>'
            elif sent == "중립":
                dart_badge_html = '<span class="badge" style="background:#D97706;color:#fff;">🟡 공시=</span>'

        cards_by_market.setdefault(s["market"], []).append(f"""
        <div class="stock-card" data-name="{escape(s['name']).lower()}" data-symbol="{escape(s['symbol']).lower()}">
          <div class="stock-card-header">
            <div class="stock-card-info">
              <h3>{escape(s['name'])}</h3>
              <div class="symbol">{escape(s['symbol'])}</div>
            </div>
            <div class="stock-card-badges">
              {composite_badge_html}
              {signal_badge_html}
              {bnf_badge_html}
              {alpha_badge_html}
              {dart_badge_html}
              {pattern_badge_html}
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

    # 시장별 섹션 (한국 → 미국 순서)
    sections = []
    for market, label in (("korea", "🇰🇷 한국"), ("us", "🇺🇸 미국")):
        market_cards = cards_by_market.get(market) or []
        if not market_cards:
            continue
        sections.append(
            f'<h2 class="market-section-header" data-market="{market}" '
            f'style="margin:24px 0 8px 0;font-size:1.05rem;color:var(--slate-700);">'
            f'{label} <span class="market-count" style="color:var(--slate-500);'
            f'font-weight:normal;">({len(market_cards)}종목)</span></h2>'
            f'<div class="stock-grid" data-market="{market}">{"".join(market_cards)}</div>'
        )
    stock_section = "".join(sections) if sections else """
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
          <span id="card-count">{len(stocks)}</span>개 종목
        </span>
        <input type="text" id="card-search" list="card-suggestions"
               placeholder="🔍 종목명/심볼 검색..." autocomplete="off"
               style="margin-left:1rem; padding:0.4rem 0.75rem; border:1.5px solid var(--slate-300); border-radius:7px; min-width:240px; font-size:0.875rem;">
        <button type="button" id="card-search-clear"
                style="margin-left:0.25rem; padding:0.4rem 0.6rem; border:none; background:transparent; color:var(--slate-500); cursor:pointer; font-size:1rem;"
                title="검색 초기화">×</button>
        <datalist id="card-suggestions">
          {''.join(f'<option value="{escape(s["name"])}"></option>' for s in stocks)}
        </datalist>
      </div>
      {analyze_all_form}
    </div>
    {stock_section}"""

    card_filter_js = """
<script>
(function() {
  const input = document.getElementById('card-search');
  const clearBtn = document.getElementById('card-search-clear');
  const counter = document.getElementById('card-count');
  const grids = Array.from(document.querySelectorAll('.stock-grid'));
  if (!input || grids.length === 0) return;
  const cards = grids.flatMap(g => Array.from(g.querySelectorAll('.stock-card')));
  const total = cards.length;
  const headers = Array.from(document.querySelectorAll('.market-section-header'));

  function applyFilter() {
    const q = input.value.trim().toLowerCase();
    let visible = 0;
    cards.forEach(c => {
      const name = (c.dataset.name || '').toLowerCase();
      const sym = (c.dataset.symbol || '').toLowerCase();
      const match = !q || name.includes(q) || sym.includes(q);
      c.style.display = match ? '' : 'none';
      if (match) visible++;
    });
    // 시장별 헤더 표시/숨김 + 카운트 갱신
    headers.forEach(h => {
      const market = h.dataset.market;
      const grid = document.querySelector('.stock-grid[data-market="' + market + '"]');
      if (!grid) return;
      const visibleHere = grid.querySelectorAll('.stock-card:not([style*="display: none"])').length;
      h.style.display = visibleHere > 0 ? '' : 'none';
      grid.style.display = visibleHere > 0 ? '' : 'none';
      const cnt = h.querySelector('.market-count');
      if (cnt) {
        const totalHere = grid.querySelectorAll('.stock-card').length;
        cnt.textContent = q ? '(' + visibleHere + '/' + totalHere + '종목)' : '(' + totalHere + '종목)';
      }
    });
    if (counter) counter.textContent = q ? (visible + '/' + total) : total;
  }
  input.addEventListener('input', applyFilter);
  if (clearBtn) clearBtn.addEventListener('click', () => {
    input.value = ''; applyFilter(); input.focus();
  });
})();
</script>
"""
    refresh_script = "<script>setTimeout(()=>location.reload(),5000);</script>" if running else ""
    return _page("대시보드", body, refresh_script + _AUTOCOMPLETE_JS + card_filter_js)


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
        _render_portfolio_banner(symbol, row),
        f'<div class="card result-frame">{row["result_html"]}</div>',
    ]
    # 패턴 분석 섹션 (Phase A-E)
    try:
        body_parts.append(_render_pattern_section(row))
    except Exception as e:
        logger.warning("pattern section 렌더 실패 — %s: %s", symbol, e)
    try:
        body_parts.append(_render_prediction_history(symbol))
    except Exception as e:
        logger.warning("prediction_history 렌더 실패 — %s: %s", symbol, e)
    return _page(f"{name} 분석 결과", "".join(body_parts))


def _render_portfolio_banner(symbol: str, row: dict) -> str:
    """분석 페이지 상단 — 현재 사용자가 이 종목 보유 중이면 평균가/손익 배너."""
    h = portfolio_db.get_holding_with_pnl(_current_user(), symbol)
    if h is None:
        return ""
    market = row.get("market") or _market_of(symbol)
    avg = h["avg_price"]
    qty = h["qty"]
    last = h.get("last_close") or row.get("last_close")
    notes = h.get("notes") or ""
    pnl_pct = h.get("pnl_pct")
    pnl_abs = h.get("pnl_abs")
    if pnl_pct is None and last is not None:
        pnl_pct = (last - avg) / avg * 100
        pnl_abs = (last - avg) * qty

    if pnl_pct is None:
        bg = "#F1F5F9"
        border = "var(--slate-300)"
        color = "var(--slate-600)"
        pnl_text = "분석 결과로 손익 계산 가능"
    elif pnl_pct >= 0:
        bg = "#DCFCE7"
        border = "#16A34A"
        color = "#15803D"
        pnl_text = f"+{pnl_pct:.2f}% (+{_format_price(abs(pnl_abs), market)})"
    else:
        bg = "#FEE2E2"
        border = "#DC2626"
        color = "#991B1B"
        pnl_text = f"{pnl_pct:.2f}% (-{_format_price(abs(pnl_abs), market)})"

    avg_str = _format_price(avg, market)
    last_str = _format_price(last, market) if last is not None else "—"
    notes_html = (
        f' · 📝 <em>{escape(notes)}</em>'
        if notes else ""
    )
    return (
        f'<div style="background:{bg};border-left:4px solid {border};'
        f'padding:12px 16px;margin:8px 0 16px 0;border-radius:6px;'
        f'display:flex;flex-wrap:wrap;gap:18px;align-items:center;">'
        f'<div style="font-size:1.05rem;font-weight:600;color:{color};">'
        f'💼 보유 중 · {pnl_text}</div>'
        f'<div style="color:var(--slate-700);font-size:0.9rem;">'
        f'평균 <strong>{avg_str}</strong> → 현재 <strong>{last_str}</strong> '
        f'· {qty}주{notes_html}</div>'
        f'<a href="/portfolio" style="margin-left:auto;font-size:0.85rem;'
        f'color:var(--slate-600);text-decoration:none;">포트폴리오 →</a>'
        f'</div>'
    )


def _format_price(price: float, market: str) -> str:
    """미국주식 → $1,234.56, 한국주식 → 12,345원."""
    if market == "us":
        return f"${price:,.2f}"
    return f"{price:,.0f}원"


def _render_pattern_section(row: dict) -> str:
    """5 카테고리 패턴 분석 섹션 (이동평균/캔들/차트/지지저항/경고).

    market 별 통화: us → $1,234.56 / korea → 12,345원
    """
    import json as _json
    pj_str = row.get("pattern_json")
    if not pj_str:
        return ""
    try:
        pj = _json.loads(pj_str)
    except (ValueError, TypeError):
        return ""

    market = row.get("market", "korea")
    fp = lambda p: _format_price(float(p), market)  # noqa: E731

    summary = pj.get("summary") or {}
    ma = pj.get("ma_state") or {}
    candles = pj.get("candles") or []
    chart = pj.get("chart_patterns") or []
    sr = pj.get("sr_levels") or []
    warning = pj.get("warning")

    sig_color = {"매수": "#16A34A", "약매수": "#65A30D", "매도": "#DC2626",
                 "약매도": "#EA580C", "사지마": "#D97706", "팔지마": "#D97706"}.get(
                     summary.get("signal", "관망"), "#64748B")

    score = summary.get("score", 0)
    sign = "+" if score >= 0 else ""
    _symbol = row.get("cache_key") or ""
    top_list = summary.get("top_patterns") or []
    if top_list:
        tops = " · ".join(_pattern_link(p, symbol=_symbol) for p in top_list)
    else:
        tops = "(패턴 없음)"
    parts = [f"""
    <div class="card" style="margin-top:1rem;">
      <h2 style="margin:0 0 1rem 0;">📊 패턴 분석</h2>
      <div style="background:{sig_color};color:#fff;padding:0.75rem 1rem;border-radius:8px;margin-bottom:1rem;">
        <strong style="font-size:1.1rem;">종합 시그널: {escape(summary.get("signal", "관망"))}</strong>
        &nbsp; <span style="opacity:0.85;">score {sign}{score}</span>
        <br><small>주요 패턴: {tops}</small>
      </div>
    """]

    # 1. 이동평균
    if ma.get("signal"):
        ma_color = sig_color if ma["signal"] in ("매수", "매도") else "#64748B"
        ma_info = ma.get("ma") or {}
        ma_extra = ""
        if ma_info:
            ma_extra = (
                f' <small style="color:#64748B;">'
                f'SMA5: {fp(ma_info.get("sma5", 0))} / '
                f'SMA50: {fp(ma_info.get("sma50", 0))} / '
                f'SMA200: {fp(ma_info.get("sma200", 0))}</small>'
            )
        parts.append(f"""
      <h3 style="margin-top:1rem;">📈 이동평균 (4상태)</h3>
      <p><strong style="color:{ma_color};">{escape(ma["signal"])}</strong>
         — {escape(ma.get("label", ""))}
         <small>(신뢰도 {ma.get("confidence", 0)*100:.0f}%)</small></p>
      <p>{ma_extra}</p>
    """)

    # 2. 캔들 패턴
    if candles:
        rows = []
        for c in candles[:8]:
            csig = c.get("signal", "관망")
            ccolor = {"매수": "#16A34A", "매도": "#DC2626"}.get(csig, "#64748B")
            cname_link = _pattern_link(c.get("name", ""), symbol=_symbol, date=c.get("date"))
            rows.append(
                f'<li><span style="color:{ccolor};font-weight:600;">{cname_link}</span> '
                f'<small style="color:#64748B;">— {escape(csig)} ({escape(c.get("date",""))})</small></li>'
            )
        parts.append(f"""
      <h3 style="margin-top:1rem;">🕯 캔들 패턴 (최근 5일)</h3>
      <ul style="margin:0.25rem 0 0.5rem 1.25rem;">{"".join(rows)}</ul>
    """)

    # 3. 차트 패턴 (구간 정보 + 통화 명시)
    if chart:
        rows = []
        for cp in chart:
            csig = cp.get("signal", "관망")
            ccolor = {"매수": "#16A34A", "매도": "#DC2626"}.get(csig, "#64748B")
            from_d = cp.get("from_date", "")
            to_d = cp.get("to_date", "")
            dur = cp.get("duration_days", 0)
            range_str = f" <small>[{from_d} ~ {to_d}, {dur}일]</small>" if from_d else ""
            # 패턴별 raw price 로 details 빌드 (통화 명시)
            details = _build_chart_pattern_details(cp, fp)
            cpname_link = _pattern_link(
                cp.get("name", ""),
                symbol=_symbol,
                date=cp.get("to_date") or cp.get("from_date"),
            )
            rows.append(
                f'<li><span style="color:{ccolor};font-weight:600;">{cpname_link}</span>'
                f' — {escape(csig)} (신뢰도 {cp.get("confidence",0)*100:.0f}%){range_str}'
                f'<br><small style="color:#475569;">{details}</small></li>'
            )
        parts.append(f"""
      <h3 style="margin-top:1rem;">📊 차트 패턴 (구간 표시)</h3>
      <ul style="margin:0.25rem 0 0.5rem 1.25rem;">{"".join(rows)}</ul>
    """)

    # 4. 지지/저항 (통화 명시)
    if sr:
        rows = []
        for level in sr:
            lcolor = "#DC2626" if level.get("type") == "저항" else "#16A34A"
            rows.append(
                f'<li><span style="color:{lcolor};font-weight:600;">{fp(level.get("price", 0))}</span> '
                f'— {escape(level.get("type", ""))} '
                f'<small>({level.get("touches", 0)}회 반응, '
                f'현재 대비 {level.get("distance_pct", 0):+.1f}%)</small></li>'
            )
        parts.append(f"""
      <h3 style="margin-top:1rem;">🎯 지지/저항 수평선</h3>
      <ul style="margin:0.25rem 0 0.5rem 1.25rem;">{"".join(rows)}</ul>
    """)

    # 5. 확률 경고
    if warning:
        wcolor = "#16A34A" if warning.get("signal") == "매수" else "#DC2626"
        parts.append(f"""
      <h3 style="margin-top:1rem;">⚠️  확률 경고</h3>
      <p style="background:{wcolor};color:#fff;padding:0.75rem;border-radius:6px;display:inline-block;">
        <strong>{escape(warning.get("label", ""))}</strong>
        <small> ({warning.get("confidence_pct", 0)}%)</small>
      </p>
    """)

    parts.append("</div>")
    return "".join(parts)


def _build_chart_pattern_details(cp: dict, fp) -> str:
    """차트 패턴의 raw 가격 fields → 통화 명시된 한국어 details.

    fp: 가격 포맷 함수 (lambda p: _format_price(p, market)).
    raw fields 가 있으면 새 details 빌드, 없으면 cp["details"] 그대로 (구버전 cache).
    """
    name = cp.get("name", "")
    if name == "더블바텀(W)":
        l1 = cp.get("low1", {})
        l2 = cp.get("low2", {})
        if l1 and l2:
            cur = cp.get("current")
            neck = cp.get("neckline")
            br = " — 넥라인 돌파!" if cp.get("breakout") else " — 넥라인 미돌파"
            return (
                f"저점1 {escape(l1.get('date',''))} {fp(l1.get('price', 0))} → "
                f"저점2 {escape(l2.get('date',''))} {fp(l2.get('price', 0))} "
                f"(넥라인 {fp(neck) if neck else '?'}, 현재 {fp(cur) if cur else '?'}{br})"
            )
    elif name == "더블탑(M)":
        h1 = cp.get("high1", {})
        h2 = cp.get("high2", {})
        if h1 and h2:
            cur = cp.get("current")
            neck = cp.get("neckline")
            bd = " — 넥라인 이탈!" if cp.get("breakdown") else " — 넥라인 유지"
            return (
                f"고점1 {escape(h1.get('date',''))} {fp(h1.get('price', 0))} → "
                f"고점2 {escape(h2.get('date',''))} {fp(h2.get('price', 0))} "
                f"(넥라인 {fp(neck) if neck else '?'}, 현재 {fp(cur) if cur else '?'}{bd})"
            )
    elif name in ("역헤드앤숄더", "헤드앤숄더"):
        l = cp.get("left_shoulder", {})
        h = cp.get("head", {})
        r = cp.get("right_shoulder", {})
        if l and h and r:
            return (
                f"좌어깨 {escape(l.get('date',''))} {fp(l.get('price', 0))} / "
                f"헤드 {escape(h.get('date',''))} {fp(h.get('price', 0))} / "
                f"우어깨 {escape(r.get('date',''))} {fp(r.get('price', 0))}"
            )
    # 삼각형 등 — slope 만 (통화 무관)
    return escape(cp.get("details", ""))


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


def _signal_json(row: dict) -> dict:
    """analysis_cache row 를 외부 API 응답용 JSON dict 로 변환.

    Spec: docs/superpowers/specs/2026-05-06-auto-trader-integration-design.md
    """
    return {
        "symbol": row["cache_key"],
        "name": row["cache_key"],  # cache 에 종목명 없음 — symbol 그대로 (follow-up 가능)
        "market": row["market"],
        "generated_at_kst": _format_kst(row["generated_at"]),
        "generated_at_unix": row["generated_at"],
        "is_fresh": analysis_cache.is_fresh(row, int(time.time())),
        "tech": {
            "signal": row.get("signal_value"),
            "score": row.get("signal_score"),
        },
        "bnf": {
            "signal": row.get("bnf_signal_value"),
            "score": row.get("bnf_signal_score"),
        },
    }


@app.route("/api/signal/<path:symbol>")
def api_signal(symbol: str):
    """외부 시스템용 (예: auto-trader) — 캐시된 Tech + BNF 시그널 JSON.

    Returns:
        200 + signal JSON dict (cache hit)
        404 + {"error": "no_cache", "symbol": ...} (cache miss)
        400 + {"error": "invalid_symbol", "symbol": ...} (sanitize/validate 실패)
    """
    sym = sanitize_stock_symbol(symbol)
    if not validate_stock_symbol(sym):
        return jsonify({"error": "invalid_symbol", "symbol": symbol}), 400

    row = _safe_cache_get(sym)
    if row is None:
        return jsonify({
            "error": "no_cache",
            "symbol": sym,
            "message": "분석 이력 없음. POST /analyze/<symbol> 로 트리거 후 polling.",
        }), 404

    return jsonify(_signal_json(row))


@app.route("/api/universe/<path:symbol>", methods=["POST"])
def api_universe_post(symbol: str):
    """auto-trader 등 외부 시스템용 — universe (settings.yaml) 에 종목 추가.

    JSON body: {"name": str, "market": "korea"|"us"}
    멱등: 이미 있으면 200 + {"added": false}, 신규면 201 + {"added": true}.
    CSRF 면제 (stateless API), 기존 _basic_auth_gate 그대로 적용.

    Spec: ~/Projects/auto-trader/docs/superpowers/specs/2026-05-07-universe-push-design.md
    """
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    market = (body.get("market") or "").strip()

    sym = sanitize_stock_symbol(symbol)
    if not validate_stock_symbol(sym):
        return jsonify({"error": "invalid_symbol", "symbol": symbol}), 400
    if market not in ("korea", "us"):
        return jsonify({"error": "invalid_market", "market": market}), 400
    if not name:
        name = sym  # fallback
    if not validate_stock_name(name):
        return jsonify({"error": "invalid_name", "name": name}), 400

    with _config_lock:
        config = _load_config()
        config.setdefault("stocks", {}).setdefault(market, [])
        existing = {s["symbol"] for s in config["stocks"][market]}
        if sym in existing:
            return jsonify({
                "added": False, "symbol": sym, "market": market,
            }), 200
        config["stocks"][market].append({"symbol": sym, "name": name})
        _save_config(config)

    return jsonify({
        "added": True, "symbol": sym, "name": name, "market": market,
    }), 201


@app.route("/api/universe/<path:symbol>", methods=["DELETE"])
def api_universe_delete(symbol: str):
    """auto-trader 등 외부 시스템용 — universe 에서 종목 제거 (멱등).

    Idempotent: 이미 없으면 200 + {"removed": false}, 제거 시 200 + {"removed": true}.
    CSRF 면제 (stateless API), 기존 _basic_auth_gate 적용.
    market 자동 탐색 — `?market=korea|us` query 옵션, 없으면 양쪽 모두 검사.

    Spec: ~/Projects/auto-trader/docs/superpowers/specs/2026-05-07-universe-push-design.md (#4)
    """
    sym = sanitize_stock_symbol(symbol)
    if not validate_stock_symbol(sym):
        return jsonify({"error": "invalid_symbol", "symbol": symbol}), 400

    market_hint = (request.args.get("market") or "").strip()
    if market_hint and market_hint not in ("korea", "us"):
        return jsonify({"error": "invalid_market", "market": market_hint}), 400

    with _config_lock:
        config = _load_config()
        config.setdefault("stocks", {})
        markets = [market_hint] if market_hint else ["korea", "us"]
        # 누락된 market 키 명시 초기화 — 이후 push 시 silent fail 방지
        for m in markets:
            config["stocks"].setdefault(m, [])
        removed_market: str | None = None
        for m in markets:
            entries = config["stocks"][m]
            new_entries = [s for s in entries if s["symbol"] != sym]
            if len(new_entries) != len(entries):
                config["stocks"][m] = new_entries
                removed_market = m
                break
        if removed_market is None:
            return jsonify({
                "removed": False, "symbol": sym,
                "message": "universe 에 없음",
            }), 200
        _save_config(config)

    return jsonify({
        "removed": True, "symbol": sym, "market": removed_market,
    }), 200


# ─── 주도주 발굴 (Leader Stock Finder, Spec 2026-05-15) ─────────────────────


def _current_username() -> str:
    """Session 인증 username 또는 Basic Auth username, 둘 다 없으면 'anonymous'.

    Spec §4.3: update_user_fields 의 user 인자 결정 헬퍼.
    ENABLE_BASIC_AUTH=0 환경에서도 session 우선 확인 후 Basic Auth 폴백.
    """
    if session.get("username"):
        return str(session["username"])
    auth = request.authorization
    if auth and auth.username:
        return str(auth.username)
    return "anonymous"


@app.route("/leaders")
def leaders_list():
    """GET /leaders — 통과 종목 표 (passed=1 AND status='active')."""
    from src import leader_cache as lc
    rows = lc.list_active()
    app.logger.debug("leaders_list: %d 종목", len(rows))
    return render_template("leaders.html", rows=rows)


@app.route("/foreign-ranking")
def foreign_ranking_view():
    """GET /foreign-ranking — 외인/기관/연기금 순매수 ranking (일별 + 5일/10일 누적).

    데이터 소스: foreign_ranking_history 테이블 (KIS API → 매일 16:00 launchd).
    최신 snap_date 기준 일별/5일/10일 ranking 을 3 투자자 × 3 기간으로 표시.
    """
    from datetime import date as _date
    from src import foreign_ranking as fr
    import sqlite3 as _sqlite3

    # 가장 최근 snap_date 조회 — 데이터 없으면 안내 페이지
    try:
        with _sqlite3.connect(fr._DB_PATH) as conn:
            cur = conn.execute(
                "SELECT MAX(snap_date) FROM foreign_ranking_history"
            )
            latest = (cur.fetchone() or (None,))[0]
    except Exception as e:
        app.logger.warning("foreign-ranking DB 조회 실패: %s", e)
        latest = None

    if not latest:
        return render_template(
            "foreign_ranking.html",
            latest_date=None, rankings={}, market_label={},
        )

    snap = _date.fromisoformat(latest)
    rankings: dict[str, dict] = {}
    for investor_key, (_prefix, label) in fr.INVESTORS.items():
        rankings[investor_key] = {"label": label}
        for direction in ("buy", "sell"):
            rankings[investor_key][direction] = {
                "daily": fr.top_n_by_investor(
                    snap, investor_key, period_days=1, n=10, direction=direction),
                "weekly": fr.top_n_by_investor(
                    snap, investor_key, period_days=5, n=10, direction=direction),
                "biweekly": fr.top_n_by_investor(
                    snap, investor_key, period_days=10, n=10, direction=direction),
            }
    return render_template(
        "foreign_ranking.html",
        latest_date=latest,
        rankings=rankings,
    )


@app.route("/leaders/<path:symbol>")
def leaders_detail(symbol: str):
    """GET /leaders/<symbol> — 5축 스코어카드 + LLM 분석 + 메모 폼."""
    from src import leader_cache as lc
    row = lc.get(symbol)
    if row is None:
        app.logger.info("leaders_detail: symbol=%s not found → 404", symbol)
        return render_template("leader_detail.html", row=None, symbol=symbol), 404
    return render_template(
        "leader_detail.html", row=row, symbol=symbol,
        display=lc.display_field,
        csrf_input=_csrf_input,
    )


@app.route("/leaders/<path:symbol>/edit", methods=["POST"])
def leaders_edit(symbol: str):
    """POST /leaders/<symbol>/edit — 4 user_* 필드 부분 업데이트 (spec §4.4).

    form fields: user_tam_narrative, user_narrative_expansion, user_bottleneck, user_moat.
    빈 문자열 필드는 건너뜀 (제출하지 않은 것으로 간주).
    Auth required; CSRF validated.
    """
    _csrf_validate()
    from src import leader_cache as lc
    row = lc.get(symbol)
    if row is None:
        app.logger.warning("leaders_edit: symbol=%s not found → 404", symbol)
        abort(404)
    user = _current_username()
    allowed_fields = ("tam_narrative", "narrative_expansion", "bottleneck", "moat")
    fields: dict[str, str] = {}
    for field in allowed_fields:
        val = request.form.get(f"user_{field}", "")
        if val:  # 빈 문자열 skip — 부분 업데이트
            fields[field] = val
    if fields:
        lc.update_user_fields(symbol, fields, user)
    app.logger.info("leaders_edit: symbol=%s user=%s fields=%s saved", symbol, user, list(fields.keys()))
    return redirect(url_for("leaders_detail", symbol=symbol), code=303)


@app.route("/leaders/<path:symbol>/refresh", methods=["POST"])
def leaders_refresh(symbol: str):
    """POST /leaders/<symbol>/refresh — 단일 LLM 재분석 트리거 (spec §4.4).

    daily limit 초과 시 flash + redirect. LLM 결과는 llm_* 컬럼만 갱신 (user_* 보존).
    동시 실행 방지: _leaders_refresh_lock 사용.
    """
    _csrf_validate()
    from src import leader_cache as lc
    from src import leader_llm
    row = lc.get(symbol)
    if row is None:
        app.logger.warning("leaders_refresh: symbol=%s not found → 404", symbol)
        abort(404)
    if not _leaders_refresh_lock.acquire(blocking=False):
        flash("다른 LLM 재분석이 진행 중입니다. 잠시 후 다시 시도하세요.", "warning")
        return redirect(url_for("leaders_detail", symbol=symbol), code=303)
    try:
        inputs = {
            "symbol": row["symbol"],
            "name": row["name"],
            "market": row["market"],
            "sector": row["sector"],
            "industry": row["industry"],
            "market_cap": row["market_cap"] or 0,
            "return_1y_pct": row["return_1y_pct"] or 0.0,
            "rel_return_pp": row["rel_return_pp"] or 0.0,
            "trailing_eps": row["trailing_eps"],
            "forward_eps": row["forward_eps"],
            "revenue_growth_pct": row["revenue_growth_yoy"] or 0.0,
            "trailing_pe": row["trailing_pe"],
        }
        result = leader_llm.analyze_one(inputs)
        if result.error == "over_limit":
            app.logger.warning(
                "leaders_refresh: daily limit exceeded for symbol=%s", symbol
            )
            flash("일일 LLM 호출 한도를 초과했습니다. 내일 다시 시도하세요.", "error")
            return redirect(url_for("leaders_detail", symbol=symbol), code=303)
        if result.error:
            lc.upsert_llm(
                symbol, {}, model="gemini-2.5-flash",
                raw=result.raw, error=result.error,
            )
        else:
            lc.upsert_llm(
                symbol, result.fields, model="gemini-2.5-flash", raw=result.raw,
            )
        app.logger.info(
            "leaders_refresh: symbol=%s error=%s", symbol, result.error
        )
    finally:
        _leaders_refresh_lock.release()
    return redirect(url_for("leaders_detail", symbol=symbol), code=303)


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


# ---------------------------------------------------------------------------
# Portfolio (보유 종목 1:1 매칭)
# ---------------------------------------------------------------------------

def _composite_score_row(row: dict) -> float:
    """portfolio row 의 Tech+BNF+Pattern×0.5. NULL 안전."""
    tech = row.get("signal_score") or 0
    bnf = row.get("bnf_signal_score") or 0
    pat = row.get("pattern_score") or 0
    return float(tech) + float(bnf) + float(pat) * 0.5


def _format_signed(value: float | None, market: str | None,
                   *, currency: bool = False) -> str:
    if value is None:
        return "—"
    sign = "+" if value >= 0 else ""
    if currency:
        return f"{sign}{_format_price(value, market or 'us')}"
    return f"{sign}{value:.2f}%"


def _portfolio_card(h: dict, now_ts: int, name_map: dict[str, str],
                    dart_summaries: dict | None = None) -> str:
    sym = h["symbol"]
    # 종목명 fallback: settings.yaml > 토스 sync name(portfolio.name) > symbol
    name = name_map.get(sym) or h.get("name") or sym
    market = h.get("market") or _market_of(sym)
    avg = h["avg_price"]
    qty = h["qty"]
    last = h.get("last_close")
    pnl_pct = h.get("pnl_pct")
    pnl_abs = h.get("pnl_abs")
    notes = h.get("notes") or ""
    badge_cls = "badge-korea" if market == "korea" else "badge-us"
    market_label = "한국" if market == "korea" else "미국"

    if pnl_pct is None:
        pnl_color = "var(--slate-500)"
        pnl_text = "분석 필요"
    elif pnl_pct >= 0:
        pnl_color = "#16A34A"
        pnl_text = f"+{pnl_pct:.2f}% ({_format_signed(pnl_abs, market, currency=True)})"
    else:
        pnl_color = "#DC2626"
        pnl_text = f"{pnl_pct:.2f}% ({_format_signed(pnl_abs, market, currency=True)})"

    avg_str = _format_price(avg, market)
    last_str = _format_price(last, market) if last is not None else "—"
    eval_amount = (last * qty) if last is not None else None
    eval_str = _format_price(eval_amount, market) if eval_amount is not None else "—"

    # 신선도 + composite
    fresh_line = ""
    if h.get("generated_at"):
        try:
            fake_row = {
                "market": market,
                "generated_at": h["generated_at"],
                "cache_key": sym,
            }
            fresh = analysis_cache.is_fresh(fake_row, now_ts)
            mark = "🟢" if fresh else "🟡"
            color = "var(--green-600)" if fresh else "#92400E"
            fresh_line = (
                f'<div style="font-size:0.75rem;color:{color};">'
                f'{mark} {_format_kst(h["generated_at"])}</div>'
            )
        except Exception:
            pass
    else:
        fresh_line = (
            '<div style="font-size:0.75rem;color:var(--slate-500);">'
            '⚪ 분석 이력 없음</div>'
        )

    composite = _composite_score_row(h)
    if composite >= 5:
        comp_color = "#16A34A"
    elif composite >= 1:
        comp_color = "#65A30D"
    elif composite <= -5:
        comp_color = "#DC2626"
    elif composite <= -1:
        comp_color = "#EA580C"
    else:
        comp_color = "#64748B"
    comp_sign = "+" if composite >= 0 else ""

    signal_badge_html = _render_signal_badge(
        h.get("signal_value"), h.get("signal_score"),
    )
    bnf_badge_html = _render_signal_badge(
        h.get("bnf_signal_value"), h.get("bnf_signal_score"), prefix="BNF ",
    )
    pattern_badge_html = ""
    if h.get("pattern_signal"):
        try:
            import json as _json
            pj = _json.loads(h.get("pattern_json") or "{}")
            tops = (pj.get("summary") or {}).get("top_patterns") or []
            psig = h["pattern_signal"]
            pcolor = {"매수": "#16A34A", "매도": "#DC2626",
                      "사지마": "#D97706", "팔지마": "#D97706"}.get(psig, "#64748B")
            tops_links = [_pattern_link(p, symbol=sym) for p in tops[:2]]
            tops_text = " · ".join(tops_links) if tops_links else ""
            label = f"📈 {psig}: {tops_text}" if tops_text else f"📈 {psig}"
            pattern_badge_html = (
                f'<span class="badge" style="background:{pcolor};color:#fff;">{label}</span>'
            )
        except (ValueError, KeyError):
            pass

    alpha_badge_html = ""
    if h.get("rel_perf_json"):
        try:
            import json as _json
            rp = _json.loads(h["rel_perf_json"])
            alpha_pp = rp.get("alpha_pp")
            if alpha_pp is not None:
                if alpha_pp > 0:
                    a_color, a_sign = "#16A34A", "+"
                elif alpha_pp < 0:
                    a_color, a_sign = "#DC2626", ""
                else:
                    a_color, a_sign = "#64748B", ""
                idx = rp.get("index_name", "")
                stock_pct = rp.get("stock_pct", 0.0)
                index_pct = rp.get("index_pct", 0.0)
                title = f"vs {idx}: 종목 {stock_pct:+.2f}% / 지수 {index_pct:+.2f}%"
                alpha_badge_html = (
                    f'<span class="badge" style="background:{a_color};color:#fff;font-weight:600;" '
                    f'title="{escape(title)}">α {a_sign}{alpha_pp:.2f}pp</span>'
                )
        except (ValueError, KeyError, TypeError):
            pass

    dart_badge_html = ""
    if dart_summaries:
        dart_summary_row = dart_summaries.get(sym)
        if dart_summary_row:
            sent = dart_summary_row.get("sentiment")
            if sent == "긍정":
                dart_badge_html = '<span class="badge" style="background:#16A34A;color:#fff;">🟢 공시+</span>'
            elif sent == "부정":
                dart_badge_html = '<span class="badge" style="background:#DC2626;color:#fff;">🔴 공시-</span>'
            elif sent == "중립":
                dart_badge_html = '<span class="badge" style="background:#D97706;color:#fff;">🟡 공시=</span>'

    # data-* 로 symbol/notes 전달 — JS 가 escape 안전하게 prompt 호출
    btn_data = f'data-edit-notes data-symbol="{escape(sym)}" data-notes="{escape(notes)}"'
    if notes:
        notes_html = (
            f'<div style="font-size:0.85rem;color:#92400E;background:#FEF3C7;'
            f'border-left:3px solid #F59E0B;padding:6px 10px;margin-top:8px;'
            f'border-radius:4px;display:flex;justify-content:space-between;'
            f'align-items:center;gap:8px;">'
            f'<span>📝 {escape(notes)}</span>'
            f'<button type="button" {btn_data} '
            f'style="background:transparent;border:none;cursor:pointer;'
            f'color:#92400E;font-size:0.85rem;padding:2px 6px;" '
            f'title="메모 수정">✏️</button>'
            f'</div>'
        )
    else:
        notes_html = (
            f'<div style="margin-top:6px;">'
            f'<button type="button" {btn_data} '
            f'style="background:transparent;border:1px dashed var(--slate-300);'
            f'color:var(--slate-500);font-size:0.78rem;padding:4px 10px;'
            f'border-radius:4px;cursor:pointer;">📝 메모 추가</button>'
            f'</div>'
        )

    return f"""
    <div class="stock-card" data-symbol="{escape(sym).lower()}" data-name="{escape(name).lower()}">
      <div class="stock-card-header">
        <div class="stock-card-info">
          <h3>{escape(name)}</h3>
          <div class="symbol">{escape(sym)}</div>
        </div>
        <div class="stock-card-badges">
          <span class="badge" style="background:{comp_color};color:#fff;font-weight:600;"
                title="Tech+BNF+Pattern×0.5">📊 {comp_sign}{composite:.1f}</span>
          {signal_badge_html}
          {bnf_badge_html}
          {alpha_badge_html}
          {dart_badge_html}
          {pattern_badge_html}
          <span class="badge {badge_cls}">{market_label}</span>
        </div>
      </div>
      <div style="font-size:0.95rem;color:{pnl_color};font-weight:600;margin-top:6px;">
        {pnl_text}
      </div>
      <div style="font-size:0.85rem;color:var(--slate-700);margin-top:4px;">
        평균 {avg_str}<button type="button" data-edit-avg-price
          data-symbol="{escape(sym)}" data-current="{avg}" data-market="{market}"
          style="background:transparent;border:none;cursor:pointer;color:var(--slate-500);
          font-size:0.75rem;padding:0 4px;" title="평균가 수정">✏️</button>
        → 현재 {last_str} · {qty}주<button type="button" data-edit-qty
          data-symbol="{escape(sym)}" data-current="{qty}"
          style="background:transparent;border:none;cursor:pointer;color:var(--slate-500);
          font-size:0.75rem;padding:0 4px;" title="수량 수정">✏️</button>
        (평가 {eval_str})
      </div>
      {fresh_line}
      {notes_html}
      <div class="stock-card-actions" style="margin-top:10px;flex-wrap:wrap;">
        <button type="button" data-tx-buy data-symbol="{escape(sym)}"
                data-market="{market}" data-last="{last if last is not None else ''}"
                class="btn btn-sm" style="background:#DCFCE7;color:#15803D;">📈 추가매수</button>
        <button type="button" data-tx-sell data-symbol="{escape(sym)}"
                data-market="{market}" data-last="{last if last is not None else ''}"
                data-max-qty="{qty}"
                class="btn btn-sm" style="background:#FEE2E2;color:#991B1B;">📉 매도</button>
        <a class="btn btn-sm" style="background:var(--slate-100);color:var(--slate-700);"
           href="/portfolio/history/{escape(sym)}">📜 이력</a>
        <a class="btn btn-primary btn-sm" href="/stock/{escape(sym)}">상세 분석 →</a>
        <form method="post" action="/portfolio/delete" style="display:inline;margin:0;"
              onsubmit="return confirm('{escape(sym)} 보유 종목을 삭제하시겠습니까?');">
          {_csrf_input()}
          <input type="hidden" name="symbol" value="{escape(sym)}">
          <button type="submit" class="btn btn-danger btn-sm">{_ICON_TRASH} 삭제</button>
        </form>
      </div>
    </div>"""


def _portfolio_market_stats(rows: list[dict]) -> dict:
    """시장별 포트폴리오 집계 — 평가액, 손익, 자본가중 평균수익률.

    평균수익률 = 손익합계 / 투자원금합계 × 100. 종목별 % 수익률의 단순평균이
    아니라 투자 비중을 반영한다 (단순평균은 소액·고수익 종목이 전체를 왜곡).
    이렇게 하면 같은 카드의 손익·평가 배너와 정확히 일치한다.

    last_close(=분석 결과) 없는 종목은 평가·손익·평균 세 집계 모두에서 동일하게
    제외한다.
    """
    priced = [h for h in rows if h.get("last_close") is not None]
    eval_amt = sum(h["last_close"] * h["qty"] for h in priced)
    pnl_amt = sum(h["pnl_abs"] for h in priced if h.get("pnl_abs") is not None)
    cost_basis = sum(h["avg_price"] * h["qty"] for h in priced)
    avg = (pnl_amt / cost_basis * 100) if cost_basis else None
    return {"eval_amt": eval_amt, "pnl_amt": pnl_amt, "avg": avg}


@app.route("/portfolio")
def portfolio_view():
    sort_key = request.args.get("sort", "pnl_pct")
    error_msg = request.args.get("error", "")
    error_banner = (
        f'<div class="alert alert-error">{_ICON_WARN}<span>{escape(error_msg)}</span></div>'
        if error_msg else ""
    )

    user = _current_user()
    holdings = portfolio_db.list_holdings_with_pnl(user)

    def sort_fn(h: dict) -> tuple:
        tier_no_pnl = 0 if h.get("pnl_pct") is not None else 1
        if sort_key == "pnl_abs":
            return (tier_no_pnl, -(h.get("pnl_abs") or 0), h["symbol"])
        if sort_key == "composite":
            return (tier_no_pnl, -_composite_score_row(h), h["symbol"])
        if sort_key == "symbol":
            return (0, 0, h["symbol"])
        return (tier_no_pnl, -(h.get("pnl_pct") or 0), h["symbol"])

    holdings.sort(key=sort_fn)

    # name_map — 카드에 종목명 표시용
    cfg = _load_config()
    name_map = {s["symbol"]: s["name"] for s in _get_all_stocks(cfg)}

    # 시장별 분리 통계 (KRW 와 USD 는 통화가 달라 합산 불가)
    def _market(h: dict) -> str:
        return h.get("market") or _market_of(h["symbol"])

    by_market = {"korea": [], "us": []}
    for h in holdings:
        by_market.setdefault(_market(h), []).append(h)

    def _market_block(market: str, label: str) -> str:
        rows = by_market.get(market) or []
        if not rows:
            return ""
        stats = _portfolio_market_stats(rows)
        eval_amt = stats["eval_amt"]
        pnl_amt = stats["pnl_amt"]
        avg = stats["avg"]
        eval_str = _format_price(eval_amt, market) if eval_amt else "—"
        pnl_str = _format_signed(pnl_amt, market, currency=True) if pnl_amt else "—"
        avg_str = _format_signed(avg, None) if avg is not None else "—"
        pnl_color = "#16A34A" if pnl_amt >= 0 else "#DC2626"
        avg_color = (
            "#16A34A" if (avg or 0) >= 0 else "#DC2626"
        ) if avg is not None else "var(--slate-500)"
        return (
            f'<div style="border-left:3px solid var(--slate-200);padding-left:14px;">'
            f'<div style="font-size:0.78rem;color:var(--slate-500);">{label} {len(rows)}종목</div>'
            f'<div>평가 <strong>{eval_str}</strong></div>'
            f'<div>손익 <strong style="color:{pnl_color};">{pnl_str}</strong></div>'
            f'<div>평균 수익률 <strong style="color:{avg_color};">{avg_str}</strong></div>'
            f'</div>'
        )

    stats_card = f"""
    <div class="card" style="display:flex;gap:24px;flex-wrap:wrap;align-items:center;">
      <div><strong>{len(holdings)}</strong>종목 보유</div>
      {_market_block("korea", "🇰🇷 한국")}
      {_market_block("us", "🇺🇸 미국")}
    </div>"""

    _last_toss_sync = portfolio_db.get_last_sync(user, "toss")
    _last_sync_label = (
        f'<span style="font-size:0.72rem;color:var(--slate-500);margin-left:8px;">'
        f'마지막 동기화: {_format_kst(_last_toss_sync)}</span>'
        if _last_toss_sync is not None else
        '<span style="font-size:0.72rem;color:var(--slate-500);margin-left:8px;">'
        '동기화 이력 없음</span>'
    )
    sync_form = (
        f'<form method="post" action="/portfolio/sync" style="display:inline;">'
        f'{_csrf_input()}'
        f'<button type="submit" class="badge" '
        f'onclick="return confirm(\'토스 계좌 보유종목으로 포트폴리오를 덮어씁니다. 진행할까요?\')">'
        f'📥 토스 동기화</button>{_last_sync_label}</form>'
    )

    add_form = f"""
    <div class="card">
      <div class="card-title" style="display:flex;justify-content:space-between;align-items:center;">
        <span>{_ICON_PLUS} 보유 종목 추가 / 갱신</span>
        {sync_form}
      </div>
      <form method="post" action="/portfolio/add">
        {_csrf_input()}
        <div class="add-form">
          <div class="field autocomplete-wrap">
            <label for="stock-search-input">심볼</label>
            <input name="symbol" id="stock-search-input"
                   role="combobox" aria-autocomplete="list"
                   aria-expanded="false" aria-controls="autocomplete-list"
                   placeholder="종목 검색 (예: 005930.KS, AAPL)" autocomplete="off"
                   required style="width:240px;">
            <div id="autocomplete-list" class="autocomplete-list"
                 role="listbox" aria-label="종목 검색 결과"></div>
          </div>
          <div class="field">
            <label>평균가</label>
            <input name="avg_price" type="number" step="any" min="0.000001"
                   placeholder="12000" required style="width:140px;">
          </div>
          <div class="field">
            <label>수량</label>
            <input name="qty" type="number" min="0" placeholder="10"
                   required style="width:100px;">
          </div>
          <div class="field">
            <label>메모 (선택)</label>
            <input name="notes" placeholder="예: 장기 보유" style="width:200px;">
          </div>
          <div class="field">
            <label>&nbsp;</label>
            <button type="submit" class="btn btn-success">{_ICON_PLUS} 추가/갱신</button>
          </div>
        </div>
      </form>
    </div>"""

    sort_links = []
    for key, label in (("pnl_pct", "수익률 %"), ("pnl_abs", "손익 절대"),
                      ("composite", "추천 강도"), ("symbol", "종목명")):
        active = key == sort_key
        style = ("background:var(--slate-700);color:#fff;" if active
                 else "background:var(--slate-100);color:var(--slate-700);")
        sort_links.append(
            f'<a href="/portfolio?sort={key}" class="badge" '
            f'style="{style}padding:6px 12px;">{label}</a>'
        )
    sort_bar = '<div style="display:flex;gap:6px;margin:12px 0;">' + "".join(sort_links) + '</div>'

    # DART 공시 요약 — 한번에 fetch
    try:
        from src import dart_cache as _dart_cache
        dart_summaries = _dart_cache.list_summaries()
    except Exception:
        dart_summaries = {}

    now_ts = int(time.time())
    if holdings:
        sections = []
        for market, label in (("korea", "🇰🇷 한국"), ("us", "🇺🇸 미국")):
            rows = by_market.get(market) or []
            if not rows:
                continue
            grid = "".join(_portfolio_card(h, now_ts, name_map, dart_summaries) for h in rows)
            sections.append(
                f'<h2 style="margin:24px 0 8px 0;font-size:1.05rem;color:var(--slate-700);">'
                f'{label} <span style="color:var(--slate-500);font-weight:normal;">'
                f'({len(rows)}종목)</span></h2>'
                f'<div class="stock-grid">{grid}</div>'
            )
        cards_html = "".join(sections)
    else:
        cards_html = """
        <div class="empty-state">
          <p>아직 등록된 보유 종목이 없습니다. 위 폼으로 추가해보세요.</p>
        </div>"""

    update_form = f"""
    <form id="portfolio-update-form" method="post" action="/portfolio/update" style="display:none;">
      {_csrf_input()}
      <input type="hidden" name="symbol" id="update-symbol">
      <input type="hidden" name="avg_price" disabled>
      <input type="hidden" name="qty" disabled>
      <input type="hidden" name="notes" disabled>
    </form>
    <div id="edit-modal" role="dialog" aria-labelledby="edit-modal-title"
         style="position:fixed;inset:0;background:rgba(15,23,42,0.55);
         display:none;align-items:center;justify-content:center;z-index:1000;
         padding:16px;">
      <div style="background:#fff;border-radius:8px;max-width:420px;width:100%;
                  padding:20px;box-shadow:0 12px 32px rgba(0,0,0,0.2);">
        <h3 id="edit-modal-title" style="margin:0 0 12px 0;font-size:1.1rem;">메모 수정</h3>
        <div id="edit-modal-symbol" style="font-size:0.8rem;color:var(--slate-500);
             margin-bottom:10px;"></div>
        <input id="edit-modal-input" type="text" autocomplete="off"
               style="width:100%;padding:10px;border:1.5px solid var(--slate-300);
               border-radius:6px;font-size:1rem;box-sizing:border-box;">
        <div id="edit-modal-error" role="alert"
             style="color:#DC2626;font-size:0.85rem;margin-top:6px;
             min-height:1.2em;"></div>
        <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:14px;">
          <button type="button" id="edit-modal-cancel" class="btn btn-sm"
                  style="background:var(--slate-100);color:var(--slate-700);">취소</button>
          <button type="button" id="edit-modal-save" class="btn btn-sm btn-primary">저장</button>
        </div>
      </div>
    </div>

    <!-- 매수/매도 거래 모달 (단가 + 수량 + 메모) -->
    <form id="tx-form" method="post" action="" style="display:none;">
      {_csrf_input()}
      <input type="hidden" name="symbol" id="tx-symbol">
      <input type="hidden" name="price" id="tx-price">
      <input type="hidden" name="qty" id="tx-qty">
      <input type="hidden" name="notes" id="tx-notes">
    </form>
    <div id="tx-modal" role="dialog" aria-labelledby="tx-modal-title"
         style="position:fixed;inset:0;background:rgba(15,23,42,0.55);
         display:none;align-items:center;justify-content:center;z-index:1000;
         padding:16px;">
      <div style="background:#fff;border-radius:8px;max-width:440px;width:100%;
                  padding:20px;box-shadow:0 12px 32px rgba(0,0,0,0.2);">
        <h3 id="tx-modal-title" style="margin:0 0 4px 0;font-size:1.1rem;">📈 추가 매수</h3>
        <div id="tx-modal-symbol" style="font-size:0.85rem;color:var(--slate-500);margin-bottom:14px;"></div>
        <label style="font-size:0.85rem;color:var(--slate-700);">단가</label>
        <input id="tx-input-price" type="number" step="any" min="0.000001"
               style="width:100%;padding:10px;border:1.5px solid var(--slate-300);
               border-radius:6px;font-size:1rem;margin:4px 0 12px 0;box-sizing:border-box;">
        <label style="font-size:0.85rem;color:var(--slate-700);">
          수량 <span id="tx-qty-hint" style="color:var(--slate-500);font-size:0.78rem;"></span>
        </label>
        <input id="tx-input-qty" type="number" step="1" min="1"
               style="width:100%;padding:10px;border:1.5px solid var(--slate-300);
               border-radius:6px;font-size:1rem;margin:4px 0 12px 0;box-sizing:border-box;">
        <label style="font-size:0.85rem;color:var(--slate-700);">메모 (선택)</label>
        <input id="tx-input-notes" type="text" maxlength="200"
               style="width:100%;padding:10px;border:1.5px solid var(--slate-300);
               border-radius:6px;font-size:1rem;margin:4px 0 0 0;box-sizing:border-box;">
        <div id="tx-modal-error" role="alert"
             style="color:#DC2626;font-size:0.85rem;margin-top:6px;min-height:1.2em;"></div>
        <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:14px;">
          <button type="button" id="tx-modal-cancel" class="btn btn-sm"
                  style="background:var(--slate-100);color:var(--slate-700);">취소</button>
          <button type="button" id="tx-modal-submit" class="btn btn-sm btn-primary">실행</button>
        </div>
      </div>
    </div>"""

    update_js = """
<script>
(function() {
  const FORM = document.getElementById('portfolio-update-form');
  const FIELDS = ['avg_price', 'qty', 'notes'];
  const modal = document.getElementById('edit-modal');
  const titleEl = document.getElementById('edit-modal-title');
  const symbolEl = document.getElementById('edit-modal-symbol');
  const inputEl = document.getElementById('edit-modal-input');
  const errorEl = document.getElementById('edit-modal-error');
  const saveBtn = document.getElementById('edit-modal-save');
  const cancelBtn = document.getElementById('edit-modal-cancel');

  let pending = null;  // {symbol, field, validate}

  function openModal({title, symbol, field, current, validate, inputType, allowEmpty}) {
    pending = {symbol, field, validate, allowEmpty: !!allowEmpty};
    titleEl.textContent = title;
    symbolEl.textContent = symbol;
    inputEl.type = inputType || 'text';
    if (inputType === 'number') {
      inputEl.step = field === 'qty' ? '1' : 'any';
      inputEl.min = field === 'qty' ? '0' : '0.000001';
    } else {
      inputEl.removeAttribute('step');
      inputEl.removeAttribute('min');
    }
    inputEl.value = current || '';
    errorEl.textContent = '';
    modal.style.display = 'flex';
    setTimeout(() => { inputEl.focus(); inputEl.select(); }, 0);
  }
  function closeModal() {
    modal.style.display = 'none';
    pending = null;
  }
  function submitUpdate(symbol, field, value) {
    document.getElementById('update-symbol').value = symbol;
    FIELDS.forEach(f => {
      const el = FORM.querySelector(`input[name="${f}"]`);
      if (f === field) { el.disabled = false; el.value = value; }
      else { el.disabled = true; }
    });
    FORM.submit();
  }
  function tryCommit() {
    if (!pending) return;
    const raw = inputEl.value;
    if (!pending.allowEmpty && !raw.trim()) {
      errorEl.textContent = '값을 입력하세요.';
      return;
    }
    if (pending.validate) {
      const err = pending.validate(raw);
      if (err) { errorEl.textContent = err; return; }
    }
    submitUpdate(pending.symbol, pending.field, raw);
  }

  saveBtn.addEventListener('click', tryCommit);
  cancelBtn.addEventListener('click', closeModal);
  modal.addEventListener('click', (e) => { if (e.target === modal) closeModal(); });
  document.addEventListener('keydown', (e) => {
    if (modal.style.display === 'none' || !modal.style.display) return;
    if (e.key === 'Escape') closeModal();
    else if (e.key === 'Enter') { e.preventDefault(); tryCommit(); }
  });

  document.addEventListener('click', function(e) {
    let btn = e.target.closest('[data-edit-notes]');
    if (btn) {
      openModal({
        title: '메모 수정', symbol: btn.dataset.symbol, field: 'notes',
        current: btn.dataset.notes || '', allowEmpty: true,
      });
      return;
    }
    btn = e.target.closest('[data-edit-avg-price]');
    if (btn) {
      openModal({
        title: '평균가 수정', symbol: btn.dataset.symbol, field: 'avg_price',
        current: btn.dataset.current || '', inputType: 'number',
        validate: (v) => {
          const n = parseFloat(v);
          return (!isFinite(n) || n <= 0) ? '평균가는 0보다 큰 숫자여야 합니다.' : '';
        },
      });
      return;
    }
    btn = e.target.closest('[data-edit-qty]');
    if (btn) {
      openModal({
        title: '수량 수정', symbol: btn.dataset.symbol, field: 'qty',
        current: btn.dataset.current || '', inputType: 'number',
        validate: (v) => {
          const n = parseInt(v, 10);
          return (!isFinite(n) || n < 0 || String(n) !== String(v).trim()) ?
            '수량은 0 이상의 정수여야 합니다.' : '';
        },
      });
      return;
    }
  });

  // ---- Transaction modal (BUY / SELL) ----
  const txModal = document.getElementById('tx-modal');
  const txForm = document.getElementById('tx-form');
  const txTitle = document.getElementById('tx-modal-title');
  const txSymbol = document.getElementById('tx-modal-symbol');
  const txInputPrice = document.getElementById('tx-input-price');
  const txInputQty = document.getElementById('tx-input-qty');
  const txInputNotes = document.getElementById('tx-input-notes');
  const txQtyHint = document.getElementById('tx-qty-hint');
  const txError = document.getElementById('tx-modal-error');
  const txCancel = document.getElementById('tx-modal-cancel');
  const txSubmit = document.getElementById('tx-modal-submit');

  let txPending = null;

  function openTxModal(action, btn) {
    const sym = btn.dataset.symbol;
    const last = parseFloat(btn.dataset.last || '');
    const maxQty = parseInt(btn.dataset.maxQty || '0', 10);
    txPending = {action, symbol: sym, maxQty: maxQty};
    if (action === 'buy') {
      txTitle.textContent = '📈 추가 매수';
      txQtyHint.textContent = '';
      txForm.action = '/portfolio/buy';
    } else {
      txTitle.textContent = '📉 매도';
      txQtyHint.textContent = `(보유 ${maxQty}주 이하)`;
      txForm.action = '/portfolio/sell';
    }
    txSymbol.textContent = sym;
    txInputPrice.value = isFinite(last) ? last : '';
    txInputQty.value = '';
    txInputNotes.value = '';
    txError.textContent = '';
    txModal.style.display = 'flex';
    setTimeout(() => txInputPrice.focus(), 0);
  }
  function closeTxModal() {
    txModal.style.display = 'none';
    txPending = null;
  }
  function submitTx() {
    if (!txPending) return;
    const price = parseFloat(txInputPrice.value);
    const qty = parseInt(txInputQty.value, 10);
    if (!isFinite(price) || price <= 0) {
      txError.textContent = '단가는 0보다 큰 숫자여야 합니다.';
      return;
    }
    if (!isFinite(qty) || qty <= 0) {
      txError.textContent = '수량은 1 이상의 정수여야 합니다.';
      return;
    }
    if (txPending.action === 'sell' && qty > txPending.maxQty) {
      txError.textContent = `매도 수량이 보유 (${txPending.maxQty}주) 초과.`;
      return;
    }
    document.getElementById('tx-symbol').value = txPending.symbol;
    document.getElementById('tx-price').value = String(price);
    document.getElementById('tx-qty').value = String(qty);
    document.getElementById('tx-notes').value = txInputNotes.value;
    txForm.submit();
  }

  txCancel.addEventListener('click', closeTxModal);
  txSubmit.addEventListener('click', submitTx);
  txModal.addEventListener('click', (e) => { if (e.target === txModal) closeTxModal(); });
  document.addEventListener('keydown', (e) => {
    if (txModal.style.display === 'none' || !txModal.style.display) return;
    if (e.key === 'Escape') closeTxModal();
    else if (e.key === 'Enter') { e.preventDefault(); submitTx(); }
  });
  document.addEventListener('click', (e) => {
    const buy = e.target.closest('[data-tx-buy]');
    if (buy) { openTxModal('buy', buy); return; }
    const sell = e.target.closest('[data-tx-sell]');
    if (sell) { openTxModal('sell', sell); return; }
  });
})();
</script>"""

    body = f"""
    <div class="page-header">
      <h1>포트폴리오</h1>
      <p>보유 종목 + 평균가 → 손익 + 매매 시그널 1:1 매칭</p>
    </div>
    {error_banner}
    {stats_card}
    {add_form}
    {sort_bar}
    {cards_html}
    {update_form}"""
    return _page("포트폴리오", body, _AUTOCOMPLETE_JS + update_js)


@app.route("/portfolio/sync", methods=["POST"])
def portfolio_sync():
    """토스 보유주식 → 현재 로그인 사용자 포트폴리오 미러링."""
    _csrf_validate()
    from src import toss_sync
    user = _current_user()
    try:
        res = toss_sync.run_sync(user)
        flash(
            f"토스 동기화 완료 — 추가 {res['added']} · 갱신 {res['updated']} · "
            f"제거 {res['removed']} · 건너뜀 {res['skipped']} · "
            f"실패 {res.get('failed', 0)}",
            "success",
        )
    except toss_sync.SyncAborted as e:
        flash(f"동기화 중단: {e}", "warning")
    except Exception as e:  # noqa: BLE001 — 사용자 대면 flash 로 모든 오류 표면화
        app.logger.exception("portfolio_sync 실패: %s", e)
        flash(f"동기화 실패: {e}", "error")
    return redirect(url_for("portfolio_view"), code=303)


@app.route("/portfolio/add", methods=["POST"])
def portfolio_add():
    _csrf_validate()
    symbol = request.form.get("symbol", "").strip()
    avg_price_s = request.form.get("avg_price", "").strip()
    qty_s = request.form.get("qty", "").strip()
    notes = (request.form.get("notes") or "").strip() or None

    if not symbol:
        return redirect(url_for("portfolio_view", error="심볼을 입력하세요."), code=303)
    symbol = sanitize_stock_symbol(symbol)
    if not validate_stock_symbol(symbol):
        return redirect(url_for("portfolio_view", error=f"유효하지 않은 심볼: {symbol}"), code=303)
    try:
        avg_price = float(avg_price_s)
        qty = int(qty_s)
    except ValueError:
        return redirect(url_for("portfolio_view", error="평균가/수량이 숫자여야 합니다."), code=303)
    user = _current_user()
    try:
        # 신규 추가 → record_buy 로 기록 (transactions 에도 BUY 남김)
        portfolio_db.record_buy(user, symbol, avg_price, qty, notes=notes)
    except ValueError as e:
        return redirect(url_for("portfolio_view", error=str(e)), code=303)
    logger.info("portfolio.add user=%s %s avg=%s qty=%s", user, symbol, avg_price, qty)
    return redirect(url_for("portfolio_view"), code=303)


@app.route("/portfolio/buy", methods=["POST"])
def portfolio_buy():
    """추가 매수 — 가중평균 자동 재계산."""
    _csrf_validate()
    symbol = request.form.get("symbol", "").strip()
    price_s = request.form.get("price", "").strip()
    qty_s = request.form.get("qty", "").strip()
    notes = (request.form.get("notes") or "").strip() or None
    if not symbol:
        return redirect(url_for("portfolio_view"), code=303)
    try:
        price = float(price_s)
        qty = int(qty_s)
    except ValueError:
        return redirect(url_for("portfolio_view", error="단가/수량 숫자 오류"), code=303)
    user = _current_user()
    try:
        portfolio_db.record_buy(user, symbol, price, qty, notes=notes)
    except ValueError as e:
        return redirect(url_for("portfolio_view", error=str(e)), code=303)
    return redirect(url_for("portfolio_view"), code=303)


@app.route("/portfolio/sell", methods=["POST"])
def portfolio_sell():
    """매도 — 평균가 cost basis 유지, 수량 차감. 0주 시 자동 삭제."""
    _csrf_validate()
    symbol = request.form.get("symbol", "").strip()
    price_s = request.form.get("price", "").strip()
    qty_s = request.form.get("qty", "").strip()
    notes = (request.form.get("notes") or "").strip() or None
    if not symbol:
        return redirect(url_for("portfolio_view"), code=303)
    try:
        price = float(price_s)
        qty = int(qty_s)
    except ValueError:
        return redirect(url_for("portfolio_view", error="단가/수량 숫자 오류"), code=303)
    user = _current_user()
    try:
        portfolio_db.record_sell(user, symbol, price, qty, notes=notes)
    except ValueError as e:
        return redirect(url_for("portfolio_view", error=str(e)), code=303)
    return redirect(url_for("portfolio_view"), code=303)


@app.route("/portfolio/history/<path:symbol>")
def portfolio_history(symbol: str):
    """거래 이력 페이지 — 종목별 BUY/SELL/ADJUST 시계열."""
    symbol = sanitize_stock_symbol(symbol)
    user = _current_user()
    txs = portfolio_db.list_transactions(user, symbol, limit=200)
    current = portfolio_db.get_holding_with_pnl(user, symbol)
    market = (current.get("market") if current else None) or _market_of(symbol)

    rows_html = []
    for t in txs:
        side_color = {"BUY": "#16A34A", "SELL": "#DC2626",
                      "ADJUST": "#D97706"}.get(t["side"], "var(--slate-500)")
        side_ko = {"BUY": "매수", "SELL": "매도",
                   "ADJUST": "조정"}.get(t["side"], t["side"])
        rows_html.append(
            f'<tr>'
            f'<td>{_format_kst(t["ts"])}</td>'
            f'<td style="color:{side_color};font-weight:600;">{side_ko}</td>'
            f'<td style="text-align:right;">{_format_price(t["price"], market)}</td>'
            f'<td style="text-align:right;">{t["qty"]}</td>'
            f'<td style="color:var(--slate-500);">{escape(t["notes"] or "")}</td>'
            f'</tr>'
        )
    table_html = (
        '<table style="width:100%;border-collapse:collapse;">'
        '<thead><tr style="border-bottom:2px solid var(--slate-200);">'
        '<th style="text-align:left;padding:8px;">시각</th>'
        '<th style="text-align:left;padding:8px;">유형</th>'
        '<th style="text-align:right;padding:8px;">단가</th>'
        '<th style="text-align:right;padding:8px;">수량</th>'
        '<th style="text-align:left;padding:8px;">메모</th>'
        '</tr></thead>'
        f'<tbody>{"".join(rows_html)}</tbody></table>'
        if txs else
        '<p style="color:var(--slate-500);">거래 이력 없음.</p>'
    )

    cur_state = ""
    if current:
        cur_state = (
            f'<div class="card" style="margin-bottom:16px;">'
            f'현재 보유: 평균 <strong>{_format_price(current["avg_price"], market)}</strong> '
            f'× {current["qty"]}주'
            f'</div>'
        )

    body = f"""
    <div class="page-header">
      <h1>{escape(symbol)} 거래 이력</h1>
      <p><a href="/portfolio">← 포트폴리오</a></p>
    </div>
    {cur_state}
    <div class="card">{table_html}</div>"""
    return _page(f"{symbol} 거래 이력", body)


@app.route("/portfolio/update", methods=["POST"])
def portfolio_update():
    _csrf_validate()
    symbol = request.form.get("symbol", "").strip()
    if not symbol:
        return redirect(url_for("portfolio_view"), code=303)
    kwargs: dict = {}
    if request.form.get("avg_price"):
        try:
            kwargs["avg_price"] = float(request.form["avg_price"])
        except ValueError:
            return redirect(url_for("portfolio_view", error="평균가 숫자 오류"), code=303)
    if request.form.get("qty"):
        try:
            kwargs["qty"] = int(request.form["qty"])
        except ValueError:
            return redirect(url_for("portfolio_view", error="수량 숫자 오류"), code=303)
    if "notes" in request.form:
        kwargs["notes"] = request.form["notes"]
    user = _current_user()
    try:
        portfolio_db.update_holding(user, symbol, **kwargs)
    except ValueError as e:
        return redirect(url_for("portfolio_view", error=str(e)), code=303)
    return redirect(url_for("portfolio_view"), code=303)


@app.route("/portfolio/delete", methods=["POST"])
def portfolio_delete():
    _csrf_validate()
    symbol = request.form.get("symbol", "").strip()
    if symbol:
        user = _current_user()
        portfolio_db.remove_holding(user, symbol)
        logger.info("portfolio.delete user=%s %s", user, symbol)
    return redirect(url_for("portfolio_view"), code=303)


def _fetch_pattern_json_for_symbol(symbol: str) -> dict | None:
    """analysis cache 에서 symbol 의 pattern_json 가져오기.

    Returns:
        파싱된 dict or None (cache row 없음 또는 pattern_json 컬럼 비어있음 또는 파싱 실패).
    """
    import json as _json
    import sqlite3
    from src.analysis_cache import _DB_PATH  # 기존 cache DB 경로

    try:
        with sqlite3.connect(_DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute("SELECT pattern_json FROM analysis_cache WHERE cache_key = ?", (symbol,))
            row = cur.fetchone()
        if row is None or row[0] is None:
            return None
        return _json.loads(row[0])
    except Exception as e:
        logger.warning("_fetch_pattern_json_for_symbol %s 실패: %s", symbol, e)
        return None


@app.route("/api/pattern-popup/textbook", methods=["GET"])
def api_pattern_popup_textbook():
    """교과서 탭 — 패턴별 정적 SVG + 설명."""
    pattern = request.args.get("pattern")
    if not pattern:
        return jsonify({"error": "pattern parameter required"}), 400
    entry = _pattern_meta.lookup(pattern)
    if entry is None:
        return jsonify({"error": f"unknown pattern: {pattern}"}), 404
    return jsonify({
        "pattern": pattern,
        "svg": entry["svg"],
        "description_html": entry["description_html"],
        "signal_typical": entry["signal_typical"],
    })


@app.route("/api/pattern-popup/actual", methods=["GET"])
def api_pattern_popup_actual():
    """실제 차트 탭 — 종목의 해당 패턴 검출 위치 마킹 차트."""
    symbol = request.args.get("symbol")
    pattern = request.args.get("pattern")
    date = request.args.get("date")  # optional
    if not symbol or not pattern:
        return jsonify({"error": "symbol and pattern required"}), 400

    pattern_json = _fetch_pattern_json_for_symbol(symbol)
    if pattern_json is None:
        return jsonify({"error": f"no analysis cache for symbol: {symbol}"}), 404

    result = _pattern_popup.build_actual_chart(symbol, pattern, date, pattern_json)
    return jsonify(result)


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
