# 토스 보유주식 → 포트폴리오 자동 동기화 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 토스증권 Open API holdings 조회 → stock-analyzer `portfolio` 테이블 전체 미러링 (수동 버튼 + launchd 자동).

**Architecture:** 외부 I/O(`toss_client.py`)와 비즈니스 로직(`toss_sync.py`)을 분리. sync 로직은 토스 API 없이 임시 DB + stub 으로 단위 테스트. 미국 소수점 수량 지원을 위해 `portfolio.qty` 를 float 로 다룬다(SQLite type affinity 활용 — 테이블 재생성 불필요).

**Tech Stack:** Python 3, httpx(또는 requests fallback), SQLite, Flask, FinanceDataReader(KRX listing), launchd.

**Spec:** `docs/superpowers/specs/2026-06-15-toss-portfolio-sync-design.md`

---

## File Structure

| 파일 | 역할 | 신규/수정 |
|------|------|-----------|
| `src/portfolio.py` | `int(qty)` → `float(qty)`, 스키마 선언 REAL | 수정 |
| `src/toss_client.py` | OAuth2 토큰 + accounts + holdings (외부 I/O) | 신규 |
| `src/toss_sync.py` | symbol 변환 + 미러링 + 오케스트레이션 | 신규 |
| `main.py` | `toss-sync` 서브커맨드 | 수정 |
| `src/web_app.py` | `POST /portfolio/sync` + 버튼 | 수정 |
| `scripts/toss-sync.plist.template` | launchd 잡 | 신규 |
| `tests/test_portfolio.py` | 소수점 qty 회귀 테스트 | 수정 |
| `tests/test_toss_sync.py` | 변환 + 미러링 단위 테스트 | 신규 |

---

## Task 1: portfolio.qty 소수점 지원

**Files:**
- Modify: `src/portfolio.py` (스키마 선언 + `int(qty)` → `float(qty)` 전수)
- Test: `tests/test_portfolio.py`

소수점 수량(미국 fractional shares)을 보존한다. SQLite INTEGER affinity 컬럼은 1.5 처럼
정수 변환 불가 값을 자동으로 REAL 로 저장하므로 **테이블 재생성은 불필요**. 코드의
`int(qty)` 절삭만 제거하면 된다. 스키마 선언도 REAL 로 바꿔 신규 DB 의도를 명확히 한다.

- [ ] **Step 1: 소수점 보존 실패 테스트 작성**

`tests/test_portfolio.py` 에 추가:

```python
def test_add_holding_preserves_fractional_qty(tmp_path, monkeypatch):
    import src.portfolio as pf
    monkeypatch.setattr(pf, "_DB_PATH", tmp_path / "p.db")
    pf.init_db()
    pf.add_holding("admin", "AAPL", avg_price=190.5, qty=1.5)
    rows = pf.list_holdings("admin")
    assert len(rows) == 1
    assert rows[0]["qty"] == 1.5   # int 절삭되면 1.0 으로 실패
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_portfolio.py::test_add_holding_preserves_fractional_qty -v`
Expected: FAIL — `assert 1.0 == 1.5` (현재 `int(qty)` 가 절삭)

- [ ] **Step 3: 스키마 선언 REAL 로 변경**

`src/portfolio.py` `_SCHEMA` 의 portfolio 테이블 (transactions 는 그대로):

```python
CREATE TABLE IF NOT EXISTS portfolio (
    username   TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    avg_price  REAL NOT NULL,
    qty        REAL NOT NULL DEFAULT 0,
    added_at   INTEGER NOT NULL,
    notes      TEXT,
    PRIMARY KEY (username, symbol)
);
```

- [ ] **Step 4: `_validate` 와 `add_holding` 의 int 절삭 제거**

`src/portfolio.py` `_validate` (line ~135):

```python
def _validate(avg_price: float, qty: float) -> None:
    if avg_price is None or float(avg_price) <= 0:
        raise ValueError(f"avg_price must be positive, got {avg_price!r}")
    if qty is None or float(qty) < 0:
        raise ValueError(f"qty must be non-negative, got {qty!r}")
```

`add_holding` 의 INSERT 파라미터 (line ~164): `int(qty)` → `float(qty)`:

```python
                    (username, symbol, float(avg_price), float(qty), now, notes),
```

`add_holding` 시그니처 타입 힌트도 `qty: float` 로:

```python
def add_holding(
    username: str, symbol: str, avg_price: float, qty: float,
    notes: str | None = None,
) -> bool:
```

- [ ] **Step 5: `update_holding` / `list_holdings` / `get_holding_with_pnl` 의 int 제거**

`update_holding` (line ~207-211):

```python
    if qty is not None:
        if float(qty) < 0:
            raise ValueError(f"qty must be non-negative, got {qty!r}")
        sets.append("qty = ?")
        vals.append(float(qty))
```

`list_holdings` (line ~242) — `int(r[2])` → `float(r[2])`:

```python
        {"symbol": r[0], "avg_price": float(r[1]), "qty": float(r[2]),
```

`get_holding_with_pnl` / `list_holdings_with_pnl` 내부의 `int(r[2])` / `int(qty)`
형태도 모두 `float(...)` 로 변경 (line ~279 등 — `grep -n "int(r\[2\])\|int(qty)\|int(existing\[1\])" src/portfolio.py` 로 전수 확인).

`record_buy` / `record_sell` 의 `old_qty = int(existing[1])` → `float(existing[1])`
(line ~347, ~404). 단 거래 수량 인자 `qty = int(qty)` (line ~337, ~393) 는
수동 거래(정수)용이므로 유지 — transactions.qty 는 INTEGER 스키마 그대로.

- [ ] **Step 6: 테스트 통과 확인 + 기존 회귀**

Run: `.venv/bin/python -m pytest tests/test_portfolio.py -v`
Expected: 신규 테스트 PASS + 기존 테스트 전부 PASS

- [ ] **Step 7: Commit**

```bash
git add src/portfolio.py tests/test_portfolio.py
git commit -m "feat(portfolio): qty 소수점 지원 (미국 fractional shares)"
```

---

## Task 2: toss_client.py — 자격증명 + 토큰

**Files:**
- Create: `src/toss_client.py`
- Test: `tests/test_toss_client.py`

`kis_client.py` 와 동일 패턴. 자격증명 로드 + 토큰 캐시(파일).

- [ ] **Step 1: 자격증명 로드 실패 테스트**

`tests/test_toss_client.py`:

```python
import pytest
import src.toss_client as tc


def test_load_credentials_from_env(monkeypatch):
    monkeypatch.setenv("TOSS_CLIENT_ID", "cid")
    monkeypatch.setenv("TOSS_CLIENT_SECRET", "csec")
    assert tc._load_credentials() == ("cid", "csec")


def test_load_credentials_missing_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("TOSS_CLIENT_ID", raising=False)
    monkeypatch.delenv("TOSS_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(tc, "_ENV_PATHS", [tmp_path / "nonexistent.env"])
    with pytest.raises(RuntimeError, match="TOSS_CLIENT"):
        tc._load_credentials()
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_toss_client.py -v`
Expected: FAIL — `ModuleNotFoundError` 또는 `AttributeError`

- [ ] **Step 3: toss_client.py 자격증명 + 토큰 캐시 작성**

```python
"""토스증권 Open API 클라이언트 — accounts + holdings 조회 (읽기 전용).

인증: OAuth2 client_credentials. 토큰 캐시 파일(TTL 24h).
응답은 {"result": ...} envelope. 에러 시 {"result": {"error": {...}}}.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_BASE_URL = "https://openapi.tossinvest.com"
_TOKEN_CACHE = Path.home() / ".cache" / "stock-analyzer" / "toss_token.json"
_REQUEST_INTERVAL = 0.1
_ENV_PATHS = [Path(__file__).resolve().parent.parent / ".env"]

try:
    import httpx
    def _http_get(url, **kw): return httpx.get(url, timeout=15, **kw)
    def _http_post(url, **kw): return httpx.post(url, timeout=15, **kw)
except ImportError:
    import requests
    def _http_get(url, **kw): return requests.get(url, timeout=15, **kw)
    def _http_post(url, **kw): return requests.post(url, timeout=15, **kw)


def _load_dotenv(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        env[k.strip()] = v
    return env


def _load_credentials() -> tuple[str, str]:
    cid = os.environ.get("TOSS_CLIENT_ID", "")
    csec = os.environ.get("TOSS_CLIENT_SECRET", "")
    if cid and csec:
        return cid, csec
    for env_path in _ENV_PATHS:
        env = _load_dotenv(env_path)
        if env.get("TOSS_CLIENT_ID") and env.get("TOSS_CLIENT_SECRET"):
            return env["TOSS_CLIENT_ID"], env["TOSS_CLIENT_SECRET"]
    raise RuntimeError("TOSS_CLIENT_ID/TOSS_CLIENT_SECRET 미설정 — env 또는 .env 필요")


def _load_cached_token() -> str | None:
    try:
        if not _TOKEN_CACHE.exists():
            return None
        payload = json.loads(_TOKEN_CACHE.read_text())
        if payload.get("expires_at", 0) > time.time() + 60:
            return payload.get("access_token")
    except (json.JSONDecodeError, OSError):
        return None
    return None


def _save_token(token: str, expires_in: int) -> None:
    _TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_CACHE.write_text(json.dumps(
        {"access_token": token, "expires_at": int(time.time()) + int(expires_in)}
    ))


def _issue_token(cid: str, csec: str) -> str:
    resp = _http_post(
        f"{_BASE_URL}/oauth2/token",
        data={"grant_type": "client_credentials", "client_id": cid, "client_secret": csec},
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    resp.raise_for_status()
    body = resp.json()
    _save_token(body["access_token"], body.get("expires_in", 86400))
    logger.info("토스 토큰 신규 발급 — expires_in=%ss", body.get("expires_in"))
    return body["access_token"]
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_toss_client.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add src/toss_client.py tests/test_toss_client.py
git commit -m "feat(toss): OAuth2 자격증명 로드 + 토큰 캐시"
```

---

## Task 3: toss_client.py — accounts + holdings 조회

**Files:**
- Modify: `src/toss_client.py` (`TossClient` 클래스 추가)
- Test: `tests/test_toss_client.py` (envelope 언랩 단위 테스트)

- [ ] **Step 1: envelope 언랩 테스트**

`tests/test_toss_client.py` 에 추가:

```python
def test_unwrap_result_ok():
    assert tc._unwrap({"result": [1, 2]}) == [1, 2]

def test_unwrap_result_error_raises():
    with pytest.raises(RuntimeError, match="account-not-found"):
        tc._unwrap({"result": {"error": {"code": "account-not-found", "message": "x"}}})
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_toss_client.py::test_unwrap_result_ok -v`
Expected: FAIL — `AttributeError: module ... has no attribute '_unwrap'`

- [ ] **Step 3: `_unwrap` + `TossClient` 구현**

`src/toss_client.py` 에 추가:

```python
def _unwrap(body: dict):
    """{"result": ...} envelope 언랩. error 객체면 RuntimeError."""
    if not isinstance(body, dict) or "result" not in body:
        raise RuntimeError(f"예상치 못한 응답 구조: {str(body)[:200]}")
    result = body["result"]
    if isinstance(result, dict) and "error" in result:
        err = result["error"] or {}
        raise RuntimeError(f"토스 API 에러: {err.get('code')} {err.get('message')}")
    return result


class TossClient:
    def __init__(self) -> None:
        self._cid, self._csec = _load_credentials()
        self._token: str | None = None
        self._last_request_at = 0.0

    def __enter__(self): return self
    def __exit__(self, *exc): return None

    def _ensure_token(self) -> str:
        if self._token:
            return self._token
        cached = _load_cached_token()
        self._token = cached or _issue_token(self._cid, self._csec)
        return self._token

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request_at
        if elapsed < _REQUEST_INTERVAL:
            time.sleep(_REQUEST_INTERVAL - elapsed)
        self._last_request_at = time.time()

    def _get(self, path: str, extra_headers: dict | None = None):
        self._throttle()
        token = self._ensure_token()
        headers = {"authorization": f"Bearer {token}"}
        if extra_headers:
            headers.update(extra_headers)
        resp = _http_get(f"{_BASE_URL}{path}", headers=headers)
        if resp.status_code == 401:
            logger.warning("토스 401 — 토큰 재발급")
            self._token = None
            try:
                _TOKEN_CACHE.unlink()
            except OSError:
                pass
            headers["authorization"] = f"Bearer {self._ensure_token()}"
            resp = _http_get(f"{_BASE_URL}{path}", headers=headers)
        resp.raise_for_status()
        return _unwrap(resp.json())

    def fetch_accounts(self) -> list[dict]:
        """[{accountNo, accountSeq, accountType}, ...] (빈 리스트 가능)."""
        result = self._get("/api/v1/accounts")
        return result if isinstance(result, list) else []

    def fetch_holdings(self, account_seq: int | str) -> list[dict]:
        """보유 종목 items[]. X-Tossinvest-Account 헤더에 accountSeq(정수) 사용."""
        result = self._get(
            "/api/v1/holdings",
            extra_headers={"X-Tossinvest-Account": str(account_seq)},
        )
        if isinstance(result, dict):
            return result.get("items", []) or []
        return []
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_toss_client.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/toss_client.py tests/test_toss_client.py
git commit -m "feat(toss): accounts/holdings 조회 + result envelope 언랩"
```

---

## Task 4: toss_sync.py — symbol 변환

**Files:**
- Create: `src/toss_sync.py`
- Test: `tests/test_toss_sync.py`

토스 symbol + marketCountry → stock-analyzer symbol. 한국은 KRX listing 으로 .KS/.KQ 판정.

- [ ] **Step 1: 변환 테스트**

`tests/test_toss_sync.py`:

```python
import pytest
import src.toss_sync as ts


@pytest.fixture
def _krx_stub(monkeypatch):
    # _load_krx_cache 가 주는 형식: symbol 에 suffix 가 이미 붙어있음
    monkeypatch.setattr(ts, "_krx_listing", lambda: {
        "005930": ".KS",   # KOSPI
        "035720": ".KQ",   # KOSDAQ (가정)
    })


def test_us_symbol_passthrough(_krx_stub):
    assert ts._to_sa_symbol({"symbol": "AAPL", "marketCountry": "US"}) == "AAPL"


def test_kr_kospi_suffix(_krx_stub):
    assert ts._to_sa_symbol({"symbol": "005930", "marketCountry": "KR"}) == "005930.KS"


def test_kr_kosdaq_suffix(_krx_stub):
    assert ts._to_sa_symbol({"symbol": "035720", "marketCountry": "KR"}) == "035720.KQ"


def test_kr_unknown_code_defaults_ks(_krx_stub):
    # listing 에 없는 코드 → .KS 기본
    assert ts._to_sa_symbol({"symbol": "999999", "marketCountry": "KR"}) == "999999.KS"


def test_unconvertible_returns_none(_krx_stub):
    assert ts._to_sa_symbol({"symbol": "", "marketCountry": "KR"}) is None
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_toss_sync.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 변환 구현**

`src/toss_sync.py`:

```python
"""토스 holdings → portfolio 미러링.

toss_client(외부 I/O) 와 분리된 비즈니스 로직. 토스 API 없이 단위 테스트 가능.
"""
from __future__ import annotations

import logging
import os

from src import portfolio as portfolio_db

logger = logging.getLogger(__name__)


def _krx_listing() -> dict[str, str]:
    """{6자리코드: '.KS'|'.KQ'} 매핑. stock_search 캐시 재사용."""
    from src.stock_search import _load_krx_cache
    out: dict[str, str] = {}
    for item in _load_krx_cache():
        sym = item.get("symbol", "")  # 예: '005930.KS'
        if sym.endswith((".KS", ".KQ")) and len(sym) > 3:
            code, suffix = sym[:-3], sym[-3:]
            out[code] = suffix
    return out


def _to_sa_symbol(holding: dict) -> str | None:
    """토스 holding → stock-analyzer symbol. 변환 불가 시 None."""
    sym = str(holding.get("symbol", "")).strip()
    if not sym:
        return None
    country = str(holding.get("marketCountry", "")).strip().upper()
    if country == "US":
        return sym
    if country == "KR":
        suffix = _krx_listing().get(sym, ".KS")
        return f"{sym}{suffix}"
    logger.warning("알 수 없는 marketCountry=%s symbol=%s — skip", country, sym)
    return None
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_toss_sync.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add src/toss_sync.py tests/test_toss_sync.py
git commit -m "feat(toss): symbol 변환 (한국 .KS/.KQ 판정, 미국 passthrough)"
```

---

## Task 5: toss_sync.py — 미러링 + 안전장치

**Files:**
- Modify: `src/toss_sync.py` (`mirror_to_portfolio`)
- Test: `tests/test_toss_sync.py`

- [ ] **Step 1: 미러링 + 가드 테스트**

`tests/test_toss_sync.py` 에 추가:

```python
@pytest.fixture
def _pf(tmp_path, monkeypatch):
    monkeypatch.setattr(portfolio_db := __import__("src.portfolio", fromlist=["x"]),
                        "_DB_PATH", tmp_path / "p.db")
    import src.portfolio as pf
    pf.init_db()
    return pf


def _h(symbol, country, qty, avg):
    return {"symbol": symbol, "marketCountry": country, "quantity": str(qty),
            "averagePurchasePrice": str(avg)}


def test_mirror_adds_and_updates(_pf, _krx_stub):
    _pf.add_holding("admin", "000660.KS", 100000.0, 5)  # 기존, 토스에 없음 → 제거
    res = ts.mirror_to_portfolio("admin", [
        _h("005930", "KR", 10, 70000),
        _h("AAPL", "US", 1.5, 190.5),
    ])
    syms = {r["symbol"]: r for r in _pf.list_holdings("admin")}
    assert "005930.KS" in syms and syms["005930.KS"]["qty"] == 10
    assert "AAPL" in syms and syms["AAPL"]["qty"] == 1.5
    assert "000660.KS" not in syms          # 토스에 없으니 제거
    assert res["added"] == 2 and res["removed"] == 1


def test_mirror_skips_zero_and_negative(_pf, _krx_stub):
    res = ts.mirror_to_portfolio("admin", [
        _h("005930", "KR", 0, 70000),     # qty 0 → skip
        _h("000660", "KR", 5, -1),        # avg<=0 → skip
    ])
    assert _pf.list_holdings("admin") == []
    assert res["skipped"] == 2


def test_mirror_50pct_delete_guard_aborts(_pf, _krx_stub, monkeypatch):
    for s in ("005930.KS", "000660.KS", "035720.KQ", "AAPL"):
        _pf.add_holding("admin", s, 1000.0, 1)
    monkeypatch.delenv("TOSS_SYNC_FORCE", raising=False)
    # 토스가 1종목만 → 3/4=75% 삭제 → 가드 abort
    with pytest.raises(ts.SyncAborted, match="50%"):
        ts.mirror_to_portfolio("admin", [_h("005930", "KR", 1, 1000)])
    # 포트폴리오 무변경
    assert len(_pf.list_holdings("admin")) == 4


def test_mirror_force_bypasses_guard(_pf, _krx_stub, monkeypatch):
    for s in ("005930.KS", "000660.KS", "035720.KQ", "AAPL"):
        _pf.add_holding("admin", s, 1000.0, 1)
    monkeypatch.setenv("TOSS_SYNC_FORCE", "1")
    res = ts.mirror_to_portfolio("admin", [_h("005930", "KR", 1, 1000)])
    assert res["removed"] == 3
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_toss_sync.py -k mirror -v`
Expected: FAIL — `AttributeError: ... 'mirror_to_portfolio'`

- [ ] **Step 3: 미러링 구현**

`src/toss_sync.py` 에 추가:

```python
class SyncAborted(RuntimeError):
    """안전장치 발동으로 sync 중단 (포트폴리오 무변경)."""


def _to_float(v) -> float | None:
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


def mirror_to_portfolio(username: str, holdings: list[dict]) -> dict:
    """토스 holdings 로 portfolio 전체 미러링.

    Returns: {added, updated, removed, skipped, target_count}
    Raises: SyncAborted (50% 삭제 가드, TOSS_SYNC_FORCE=1 로 우회)
    """
    target: dict[str, tuple[float, float]] = {}
    skipped = 0
    for h in holdings:
        sym = _to_sa_symbol(h)
        qty = _to_float(h.get("quantity"))
        avg = _to_float(h.get("averagePurchasePrice"))
        if sym is None or qty is None or avg is None or qty <= 0 or avg <= 0:
            skipped += 1
            logger.info("skip holding: symbol=%s qty=%s avg=%s",
                        h.get("symbol"), h.get("quantity"), h.get("averagePurchasePrice"))
            continue
        target[sym] = (avg, qty)

    current = {r["symbol"] for r in portfolio_db.list_holdings(username)}
    to_remove = current - target.keys()

    # 안전장치: 삭제가 현재 보유의 50% 초과면 abort (FORCE 로 우회)
    if current and len(to_remove) > len(current) * 0.5:
        if os.environ.get("TOSS_SYNC_FORCE") != "1":
            raise SyncAborted(
                f"삭제 대상 {len(to_remove)}/{len(current)} 이 50%% 초과 — "
                f"대량삭제 의심. TOSS_SYNC_FORCE=1 로 강제 가능."
            )
        logger.warning("TOSS_SYNC_FORCE — 50%% 가드 우회, %d 종목 제거", len(to_remove))

    added = updated = removed = 0
    for sym, (avg, qty) in target.items():
        is_new = portfolio_db.add_holding(username, sym, avg, qty)
        if is_new:
            added += 1
        else:
            updated += 1
    for sym in to_remove:
        if portfolio_db.remove_holding(username, sym):
            removed += 1

    result = {"added": added, "updated": updated, "removed": removed,
              "skipped": skipped, "target_count": len(target)}
    logger.info("미러링 완료 — %s", result)
    return result
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_toss_sync.py -v`
Expected: PASS (9 passed)

- [ ] **Step 5: Commit**

```bash
git add src/toss_sync.py tests/test_toss_sync.py
git commit -m "feat(toss): portfolio 미러링 + 50% 삭제 가드"
```

---

## Task 6: toss_sync.py — run_sync 오케스트레이션

**Files:**
- Modify: `src/toss_sync.py` (`run_sync`)
- Test: `tests/test_toss_sync.py` (client stub 으로 dry_run)

- [ ] **Step 1: run_sync 테스트 (client stub)**

`tests/test_toss_sync.py` 에 추가:

```python
class _FakeClient:
    def __init__(self, accounts, holdings):
        self._accounts, self._holdings = accounts, holdings
    def __enter__(self): return self
    def __exit__(self, *a): return None
    def fetch_accounts(self): return self._accounts
    def fetch_holdings(self, seq): return self._holdings


def test_run_sync_dry_run_no_db_change(_pf, _krx_stub, monkeypatch):
    fake = _FakeClient([{"accountNo": "1", "accountSeq": 7, "accountType": "BROKERAGE"}],
                       [_h("005930", "KR", 10, 70000)])
    monkeypatch.setattr(ts, "TossClient", lambda: fake)
    res = ts.run_sync("admin", dry_run=True)
    assert res["target_count"] == 1
    assert _pf.list_holdings("admin") == []   # dry_run → DB 무변경


def test_run_sync_aborts_on_empty_accounts(_pf, _krx_stub, monkeypatch):
    fake = _FakeClient([], [])
    monkeypatch.setattr(ts, "TossClient", lambda: fake)
    with pytest.raises(ts.SyncAborted, match="계좌"):
        ts.run_sync("admin")


def test_run_sync_applies(_pf, _krx_stub, monkeypatch):
    fake = _FakeClient([{"accountNo": "1", "accountSeq": 7, "accountType": "BROKERAGE"}],
                       [_h("005930", "KR", 10, 70000)])
    monkeypatch.setattr(ts, "TossClient", lambda: fake)
    res = ts.run_sync("admin")
    assert res["added"] == 1
    assert {r["symbol"] for r in _pf.list_holdings("admin")} == {"005930.KS"}
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_toss_sync.py -k run_sync -v`
Expected: FAIL — `AttributeError: ... 'run_sync'` / `'TossClient'`

- [ ] **Step 3: run_sync 구현**

`src/toss_sync.py` 상단 import 에 추가:

```python
from src.toss_client import TossClient
```

`src/toss_sync.py` 에 추가 (dry_run 은 mirror 대신 diff 계산만):

```python
def run_sync(username: str, *, dry_run: bool = False,
             account_seq: int | str | None = None) -> dict:
    """fetch accounts → holdings → 미러링. dry_run 이면 DB 무변경 diff 만.

    Raises: SyncAborted (빈 계좌 / 50% 가드), RuntimeError (API 에러)
    """
    with TossClient() as client:
        accounts = client.fetch_accounts()
        if not accounts:
            raise SyncAborted("계좌 조회 결과 없음 — sync 중단 (포트폴리오 무변경)")
        seq = account_seq or os.environ.get("TOSS_SYNC_ACCOUNT_SEQ") \
            or accounts[0].get("accountSeq")
        holdings = client.fetch_holdings(seq)

    if dry_run:
        target = {}
        skipped = 0
        for h in holdings:
            sym = _to_sa_symbol(h)
            qty = _to_float(h.get("quantity"))
            avg = _to_float(h.get("averagePurchasePrice"))
            if sym is None or qty is None or avg is None or qty <= 0 or avg <= 0:
                skipped += 1
                continue
            target[sym] = (avg, qty)
        current = {r["symbol"] for r in portfolio_db.list_holdings(username)}
        return {"dry_run": True, "target_count": len(target), "skipped": skipped,
                "would_add": sorted(target.keys() - current),
                "would_remove": sorted(current - target.keys())}

    return mirror_to_portfolio(username, holdings)
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_toss_sync.py -v`
Expected: PASS (12 passed)

- [ ] **Step 5: Commit**

```bash
git add src/toss_sync.py tests/test_toss_sync.py
git commit -m "feat(toss): run_sync 오케스트레이션 + dry-run + 빈계좌 가드"
```

---

## Task 7: main.py — toss-sync 서브커맨드

**Files:**
- Modify: `main.py` (서브파서 등록 + 핸들러)

기존 `foreign-ranking` 서브커맨드 패턴을 따른다 (subparsers.add_parser + `if args.command ==`).

- [ ] **Step 1: 서브파서 등록**

`main.py` 의 `subparsers.add_parser("foreign-ranking", ...)` 다음 줄에 추가:

```python
    toss_parser = subparsers.add_parser(
        "toss-sync", help="토스 보유주식 → 포트폴리오 미러링 (launchd cron 용)",
    )
    toss_parser.add_argument("--dry-run", action="store_true", help="diff 만 출력, DB 무변경")
    toss_parser.add_argument("--user", type=str, default=None, help="대상 username (기본 TOSS_SYNC_USERNAME)")
```

- [ ] **Step 2: 핸들러 추가**

`main.py` 의 `if args.command == "foreign-ranking":` 블록 다음에 추가
(`if args.web:` 위):

```python
    if args.command == "toss-sync":
        from src import toss_sync, portfolio as _pf
        _pf.init_db()
        user = args.user or os.environ.get("TOSS_SYNC_USERNAME", "admin")
        try:
            result = toss_sync.run_sync(user, dry_run=args.dry_run)
            logger.info("toss-sync 완료 (user=%s): %s", user, result)
        except toss_sync.SyncAborted as e:
            logger.warning("toss-sync 중단: %s", e)
            sys.exit(2)
        except Exception as e:
            logger.exception("toss-sync 실패: %s", e)
            sys.exit(1)
        return
```

- [ ] **Step 3: import 검증 + dry-run 스모크**

Run: `.venv/bin/python main.py toss-sync --help`
Expected: 서브커맨드 help 출력 (에러 없음)

Run (자격증명 있을 때): `.venv/bin/python main.py toss-sync --dry-run`
Expected: `toss-sync 완료 ... would_add=[...]` 로그 (DB 무변경)

- [ ] **Step 4: Commit**

```bash
git add main.py
git commit -m "feat(toss): toss-sync CLI 서브커맨드 (--dry-run/--user)"
```

---

## Task 8: web_app.py — sync 버튼 + 라우트

**Files:**
- Modify: `src/web_app.py` (`POST /portfolio/sync` 라우트 + `/portfolio` 페이지 버튼)

기존 `/portfolio/add` 라우트의 CSRF 검증 + flash + redirect 패턴을 따른다.

- [ ] **Step 1: 라우트 추가**

`src/web_app.py` 의 `@app.route("/portfolio/add", methods=["POST"])` 정의 직전에 추가:

```python
@app.route("/portfolio/sync", methods=["POST"])
def portfolio_sync():
    """토스 보유주식 → 현재 로그인 사용자 포트폴리오 미러링."""
    _csrf_validate()
    from src import toss_sync
    user = _current_user()
    try:
        res = toss_sync.run_sync(user)
        flash(f"토스 동기화 완료 — 추가 {res['added']} · 갱신 {res['updated']} · "
              f"제거 {res['removed']} · 건너뜀 {res['skipped']}", "success")
    except toss_sync.SyncAborted as e:
        flash(f"동기화 중단: {e}", "warning")
    except Exception as e:
        app.logger.exception("portfolio_sync 실패: %s", e)
        flash(f"동기화 실패: {e}", "error")
    return redirect(url_for("portfolio_view"))
```

> 구현 메모: redirect 대상 엔드포인트 함수명은 `/portfolio` 라우트의 실제 함수명으로
> 맞춘다. `grep -n '@app.route("/portfolio")' src/web_app.py` 로 확인 후 `url_for` 인자 교체.

- [ ] **Step 2: 버튼 추가**

`/portfolio` 페이지 HTML 의 "종목 추가" 폼 근처에 sync 버튼 추가
(`grep -n 'action="/portfolio/add"' src/web_app.py` 로 위치 확인). CSRF 입력 포함:

```python
        f'<form method="post" action="/portfolio/sync" style="display:inline;">'
        f'{_csrf_input()}'
        f'<button type="submit" class="badge" '
        f'onclick="return confirm(\'토스 계좌 보유종목으로 포트폴리오를 덮어씁니다. 진행할까요?\')">'
        f'📥 토스 동기화</button></form>'
```

- [ ] **Step 3: 라우트 등록 검증**

Run: `.venv/bin/python -c "from src.web_app import app; print('/portfolio/sync' in [r.rule for r in app.url_map.iter_rules()])"`
Expected: `True`

- [ ] **Step 4: Flask test_client 로 인증 redirect 확인**

Run:
```bash
.venv/bin/python -c "
from src.web_app import app
with app.test_client() as c:
    r = c.post('/portfolio/sync')
    print(r.status_code)  # 302 (login) 또는 400 (CSRF) — 라우트 존재 확인
"
```
Expected: `302` 또는 `400` (라우트 매칭됨)

- [ ] **Step 5: Commit**

```bash
git add src/web_app.py
git commit -m "feat(toss): /portfolio/sync 라우트 + 동기화 버튼"
```

---

## Task 9: launchd plist + 배포

**Files:**
- Create: `scripts/toss-sync.plist.template`

기존 `scripts/foreign-ranking.plist.template` 패턴을 따른다.

- [ ] **Step 1: plist 템플릿 작성**

`scripts/toss-sync.plist.template`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>ai.stock-analyzer.toss-sync</string>
    <key>ProgramArguments</key>
    <array>
        <string>__PROJECT_ROOT__/.venv/bin/python</string>
        <string>__PROJECT_ROOT__/main.py</string>
        <string>toss-sync</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>15</integer>
        <key>Minute</key>
        <integer>50</integer>
    </dict>
    <key>WorkingDirectory</key>
    <string>__PROJECT_ROOT__</string>
    <key>RunAtLoad</key>
    <false/>
    <key>KeepAlive</key>
    <false/>
    <key>StandardOutPath</key>
    <string>__PROJECT_ROOT__/logs/toss-sync.out</string>
    <key>StandardErrorPath</key>
    <string>__PROJECT_ROOT__/logs/toss-sync.err</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>__PROJECT_ROOT__/.venv/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
</dict>
</plist>
```

- [ ] **Step 2: Commit + push**

```bash
git add scripts/toss-sync.plist.template
git commit -m "feat(toss): launchd plist (매일 15:50 KST)"
git push origin main
```

- [ ] **Step 3: 서버 배포 — .env 자격증명 (사용자 직접)**

서버 `~/Projects/stock-analyzer/.env` 에 토스 자격증명 추가 (사용자가 직접, 채팅 노출 금지):
```
TOSS_CLIENT_ID=...
TOSS_CLIENT_SECRET=...
TOSS_SYNC_USERNAME=admin
```

- [ ] **Step 4: 서버 pull + plist 등록**

```bash
ssh 100.87.151.104 'set -e
  PROJ=/Users/sykim/Projects/stock-analyzer
  cd "$PROJ" && git pull --ff-only origin main
  sed "s|__PROJECT_ROOT__|$PROJ|g" "$PROJ/scripts/toss-sync.plist.template" \
    > ~/Library/LaunchAgents/ai.stock-analyzer.toss-sync.plist
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.stock-analyzer.toss-sync.plist 2>&1 || true
  launchctl list | grep toss-sync
'
```

- [ ] **Step 5: 서버 dry-run 검증**

```bash
ssh 100.87.151.104 'cd ~/Projects/stock-analyzer && .venv/bin/python main.py toss-sync --dry-run 2>&1 | tail -5'
```
Expected: `toss-sync 완료 ... would_add=[...]` (DB 무변경)

- [ ] **Step 6: 실제 1회 sync + 웹 확인**

```bash
ssh 100.87.151.104 'cd ~/Projects/stock-analyzer && .venv/bin/python main.py toss-sync 2>&1 | tail -5'
ssh 100.87.151.104 'launchctl kickstart -k gui/$(id -u)/ai.stock-analyzer.web'
```
브라우저에서 `/portfolio` 확인 — 토스 보유종목이 미러링됐는지.

---

## Self-Review (작성자 체크)

- **Spec coverage**: §2 API(Task 2,3) · §5 symbol 변환(Task 4) · §5b qty REAL(Task 1) ·
  §6 미러링(Task 5) · §7 안전장치(Task 5) · §8 트리거 CLI/웹/launchd(Task 6,7,9) ·
  §9 에러처리(Task 3 401재시도, Task 6 가드) · §10 테스트(각 Task) — 전부 커버.
- **Placeholder**: 없음. url_for 엔드포인트명/버튼 위치는 grep 확인 지시 + 실제 코드 제공.
- **Type 일관성**: `run_sync`/`mirror_to_portfolio`/`_to_sa_symbol`/`SyncAborted`/`TossClient`
  시그니처가 Task 간 일치. `add_holding(username, symbol, avg_price, qty)` 시그니처 Task 1 과 5 일치.
