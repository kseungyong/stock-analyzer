# 종목 추가 자동완성 검색 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 대시보드의 "종목 추가" 폼에 회사명/심볼 자동완성 검색 기능을 추가해, 사용자가 한국·미국 종목을 검색해 클릭만으로 폼을 채울 수 있게 한다.

**Architecture:** 백엔드는 `src/stock_search.py` 새 모듈에서 한국(FDR KRX 24h 캐시) + 미국(yfinance Search) 통합 검색을 제공한다. Flask 라우트 `GET /api/stocks/search`가 이를 노출하고, 프런트엔드는 vanilla JS로 300ms debounce 후 결과를 드롭다운에 렌더링한다.

**Tech Stack:** Python 3 / Flask / FinanceDataReader / yfinance / pytest / vanilla JS

**Spec:** `docs/superpowers/specs/2026-05-04-stock-search-autocomplete-design.md`

---

## File Structure

| 파일 | 역할 |
|------|------|
| `src/stock_search.py` (신규) | 한국·미국 통합 종목 검색. KRX 캐시 관리. |
| `src/validators.py` (수정) | 검색 쿼리 화이트리스트 검증 함수 추가. |
| `src/web_app.py` (수정) | `/api/stocks/search` 라우트, 폼 마크업, CSS, JS. |
| `tests/test_stock_search.py` (신규) | 검색 모듈 단위 테스트. |
| `tests/test_validators.py` (수정) | 검색 쿼리 검증 테스트. |
| `tests/test_web_app.py` (수정) | 검색 API 테스트. |

---

## Task 1: 검색 쿼리 검증 함수

**Files:**
- Modify: `src/validators.py` (append)
- Test: `tests/test_validators.py` (append)

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_validators.py` 끝에 추가:

```python
from src.validators import is_valid_search_query


class TestIsValidSearchQuery:
    def test_korean(self):
        assert is_valid_search_query("삼성") is True
        assert is_valid_search_query("삼성전자") is True

    def test_english(self):
        assert is_valid_search_query("Apple") is True
        assert is_valid_search_query("aapl") is True

    def test_mixed_with_spaces(self):
        assert is_valid_search_query("LG 화학") is True

    def test_with_dot_and_hyphen(self):
        assert is_valid_search_query("BRK-A") is True
        assert is_valid_search_query("005930.KS") is True

    def test_empty_or_whitespace(self):
        assert is_valid_search_query("") is False
        assert is_valid_search_query("   ") is False

    def test_too_long(self):
        assert is_valid_search_query("a" * 51) is False
        assert is_valid_search_query("a" * 50) is True

    def test_special_chars_rejected(self):
        assert is_valid_search_query("DROP TABLE;") is False
        assert is_valid_search_query("a<b>") is False
        assert is_valid_search_query("a/b") is False
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `pytest tests/test_validators.py::TestIsValidSearchQuery -v`
Expected: FAIL — `cannot import name 'is_valid_search_query'`

- [ ] **Step 3: 구현 추가**

`src/validators.py` 끝에 추가:

```python
SEARCH_QUERY_MAX_LEN = 50
_SEARCH_QUERY_PATTERN = re.compile(r'^[A-Za-z0-9가-힣 .\-]+$')


def is_valid_search_query(query: str) -> bool:
    """검색 쿼리의 유효성을 검증한다.

    허용: 영문자/숫자/한글/공백/점(.)/하이픈(-). 길이 1-50자(strip 후).
    """
    if not query or not isinstance(query, str):
        return False
    stripped = query.strip()
    if not stripped or len(stripped) > SEARCH_QUERY_MAX_LEN:
        return False
    return bool(_SEARCH_QUERY_PATTERN.match(stripped))
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `pytest tests/test_validators.py::TestIsValidSearchQuery -v`
Expected: PASS (8 tests)

- [ ] **Step 5: 커밋**

```bash
git add src/validators.py tests/test_validators.py
git commit -m "feat(validators): is_valid_search_query 추가"
```

---

## Task 2: stock_search 모듈 — 짧은 쿼리 처리

**Files:**
- Create: `src/stock_search.py`
- Test: `tests/test_stock_search.py`

- [ ] **Step 1: 실패 테스트 작성**

새 파일 `tests/test_stock_search.py`:

```python
"""src/stock_search.py 단위 테스트."""
from src.stock_search import search_stocks


class TestShortQuery:
    def test_empty_returns_empty_list(self):
        assert search_stocks("") == []

    def test_one_char_returns_empty_list(self):
        assert search_stocks("a") == []

    def test_whitespace_only_returns_empty_list(self):
        assert search_stocks("   ") == []
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `pytest tests/test_stock_search.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: 모듈 스켈레톤 작성**

새 파일 `src/stock_search.py`:

```python
"""한국·미국 종목 통합 검색."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_MIN_QUERY_LEN = 2


def search_stocks(query: str, limit: int = 10) -> list[dict]:
    """회사명 또는 심볼로 한국·미국 종목을 검색한다.

    Args:
        query: 검색어 (한글/영문/숫자, 2자 이상)
        limit: 반환할 최대 결과 수

    Returns:
        [{"symbol": str, "name": str, "market": "korea"|"us"}, ...].
        한국 결과 우선, 심볼 기준 중복 제거.
    """
    q = query.strip() if query else ""
    if len(q) < _MIN_QUERY_LEN:
        return []
    return []
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `pytest tests/test_stock_search.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: 커밋**

```bash
git add src/stock_search.py tests/test_stock_search.py
git commit -m "feat(stock_search): 모듈 스켈레톤과 짧은 쿼리 가드"
```

---

## Task 3: KRX 캐시 + 한국 검색

**Files:**
- Modify: `src/stock_search.py`
- Test: `tests/test_stock_search.py` (append)

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_stock_search.py`에 추가:

```python
import pytest
import pandas as pd
from unittest.mock import patch


_FAKE_KRX_DF = pd.DataFrame([
    {"Code": "005930", "Name": "삼성전자", "Market": "KOSPI"},
    {"Code": "000660", "Name": "SK하이닉스", "Market": "KOSPI"},
    {"Code": "035720", "Name": "카카오", "Market": "KOSPI"},
    {"Code": "247540", "Name": "에코프로비엠", "Market": "KOSDAQ"},
])


@pytest.fixture(autouse=True)
def reset_krx_cache():
    """각 테스트 사이에 KRX 캐시를 초기화."""
    import src.stock_search as ss
    ss._krx_cache["loaded_at"] = None
    ss._krx_cache["data"] = []
    yield


class TestKoreaSearch:
    @patch("src.stock_search._fetch_krx_listing")
    @patch("src.stock_search._search_us", return_value=[])
    def test_substring_match_korean_name(self, _us, fetch_mock):
        fetch_mock.return_value = _FAKE_KRX_DF
        results = search_stocks("삼성")
        symbols = [r["symbol"] for r in results]
        assert "005930.KS" in symbols
        assert all(r["market"] == "korea" for r in results)

    @patch("src.stock_search._fetch_krx_listing")
    @patch("src.stock_search._search_us", return_value=[])
    def test_kosdaq_uses_kq_suffix(self, _us, fetch_mock):
        fetch_mock.return_value = _FAKE_KRX_DF
        results = search_stocks("에코프로")
        assert any(r["symbol"] == "247540.KQ" for r in results)

    @patch("src.stock_search._fetch_krx_listing")
    @patch("src.stock_search._search_us", return_value=[])
    def test_symbol_prefix_match(self, _us, fetch_mock):
        fetch_mock.return_value = _FAKE_KRX_DF
        results = search_stocks("00593")
        assert any(r["symbol"] == "005930.KS" for r in results)

    @patch("src.stock_search._fetch_krx_listing", side_effect=RuntimeError("net"))
    @patch("src.stock_search._search_us", return_value=[])
    def test_fetch_failure_returns_empty(self, _us, _fetch):
        assert search_stocks("삼성") == []

    @patch("src.stock_search._fetch_krx_listing")
    @patch("src.stock_search._search_us", return_value=[])
    def test_cache_reused_within_ttl(self, _us, fetch_mock):
        fetch_mock.return_value = _FAKE_KRX_DF
        search_stocks("삼성")
        search_stocks("카카오")
        assert fetch_mock.call_count == 1
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `pytest tests/test_stock_search.py::TestKoreaSearch -v`
Expected: FAIL — `_fetch_krx_listing` / `_krx_cache` / `_search_us` 미정의

- [ ] **Step 3: 구현**

`src/stock_search.py` 전체를 다음으로 교체:

```python
"""한국·미국 종목 통합 검색."""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

_MIN_QUERY_LEN = 2
_KRX_TTL_SECONDS = 24 * 3600

_krx_cache: dict = {"loaded_at": None, "data": []}
_krx_lock = threading.Lock()


def _fetch_krx_listing() -> pd.DataFrame:
    """FinanceDataReader에서 전체 KRX 종목 목록을 받는다 (외부 호출 격리용)."""
    import FinanceDataReader as fdr
    return fdr.StockListing("KRX")


def _load_krx_cache() -> list[dict]:
    """KRX 캐시를 TTL 내면 재사용, 만료되면 새로 받는다.

    Returns:
        [{"symbol": "005930.KS", "name": "삼성전자", "market": "korea"}, ...]
        — 실패 시 빈 리스트.
    """
    now = time.time()
    loaded_at = _krx_cache["loaded_at"]
    if loaded_at is not None and (now - loaded_at) < _KRX_TTL_SECONDS:
        return _krx_cache["data"]

    with _krx_lock:
        loaded_at = _krx_cache["loaded_at"]
        if loaded_at is not None and (now - loaded_at) < _KRX_TTL_SECONDS:
            return _krx_cache["data"]
        try:
            df = _fetch_krx_listing()
            data = []
            for _, row in df.iterrows():
                code = str(row.get("Code", "")).strip()
                name = str(row.get("Name", "")).strip()
                market = str(row.get("Market", "")).strip()
                if not code or not name:
                    continue
                suffix = ".KQ" if "KOSDAQ" in market.upper() else ".KS"
                data.append({"symbol": f"{code}{suffix}", "name": name, "market": "korea"})
            _krx_cache["data"] = data
            _krx_cache["loaded_at"] = now
            logger.info("KRX 캐시 로드 완료: %d 종목", len(data))
            return data
        except Exception as e:
            logger.warning("KRX 캐시 로드 실패: %s", e)
            return []


def _search_kr(query: str, limit: int) -> list[dict]:
    """KRX 캐시에서 종목명 substring 또는 심볼 prefix 매칭."""
    data = _load_krx_cache()
    if not data:
        return []
    q_lower = query.lower()
    results = []
    for item in data:
        name = item["name"]
        symbol = item["symbol"]
        code = symbol.split(".")[0]
        if q_lower in name.lower() or code.startswith(query):
            results.append(item)
            if len(results) >= limit:
                break
    return results


def _search_us(query: str, limit: int) -> list[dict]:
    """미국 종목 검색 (Task 4에서 구현)."""
    return []


def search_stocks(query: str, limit: int = 10) -> list[dict]:
    """회사명 또는 심볼로 한국·미국 종목을 검색한다.

    Returns:
        [{"symbol": str, "name": str, "market": "korea"|"us"}, ...].
        한국 결과 우선.
    """
    q = query.strip() if query else ""
    if len(q) < _MIN_QUERY_LEN:
        return []
    kr = _search_kr(q, limit)
    us = _search_us(q, limit)
    seen = set()
    combined = []
    for item in (*kr, *us):
        if item["symbol"] in seen:
            continue
        seen.add(item["symbol"])
        combined.append(item)
        if len(combined) >= limit:
            break
    return combined
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `pytest tests/test_stock_search.py -v`
Expected: PASS (8 tests — 3 짧은 쿼리 + 5 한국 검색)

- [ ] **Step 5: 커밋**

```bash
git add src/stock_search.py tests/test_stock_search.py
git commit -m "feat(stock_search): KRX 24h 캐시와 한국 검색 추가"
```

---

## Task 4: 미국 검색 (yfinance Search)

**Files:**
- Modify: `src/stock_search.py`
- Test: `tests/test_stock_search.py` (append)

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_stock_search.py`에 추가:

```python
class _FakeSearch:
    """yf.Search mock — `quotes` 속성을 노출."""
    def __init__(self, quotes):
        self.quotes = quotes


class TestUSSearch:
    @patch("src.stock_search._fetch_yf_search")
    @patch("src.stock_search._fetch_krx_listing", return_value=pd.DataFrame())
    def test_us_quote_returned(self, _krx, yf_mock):
        yf_mock.return_value = _FakeSearch([
            {"symbol": "AAPL", "shortname": "Apple Inc.", "quoteType": "EQUITY", "exchange": "NMS"},
        ])
        results = search_stocks("apple")
        assert results == [{"symbol": "AAPL", "name": "Apple Inc.", "market": "us"}]

    @patch("src.stock_search._fetch_yf_search")
    @patch("src.stock_search._fetch_krx_listing", return_value=pd.DataFrame())
    def test_excludes_non_equity_etf(self, _krx, yf_mock):
        yf_mock.return_value = _FakeSearch([
            {"symbol": "BTC-USD", "shortname": "Bitcoin", "quoteType": "CRYPTOCURRENCY", "exchange": "CCC"},
            {"symbol": "SPY", "shortname": "SPDR S&P 500", "quoteType": "ETF", "exchange": "PCX"},
        ])
        results = search_stocks("spy")
        symbols = [r["symbol"] for r in results]
        assert "SPY" in symbols
        assert "BTC-USD" not in symbols

    @patch("src.stock_search._fetch_yf_search")
    @patch("src.stock_search._fetch_krx_listing", return_value=pd.DataFrame())
    def test_excludes_korean_exchange(self, _krx, yf_mock):
        yf_mock.return_value = _FakeSearch([
            {"symbol": "005930.KS", "shortname": "Samsung Electronics", "quoteType": "EQUITY", "exchange": "KSC"},
            {"symbol": "AAPL", "shortname": "Apple Inc.", "quoteType": "EQUITY", "exchange": "NMS"},
        ])
        results = search_stocks("app")
        symbols = [r["symbol"] for r in results]
        assert "AAPL" in symbols
        assert "005930.KS" not in symbols

    @patch("src.stock_search._fetch_yf_search", side_effect=RuntimeError("api"))
    @patch("src.stock_search._fetch_krx_listing", return_value=pd.DataFrame())
    def test_us_failure_returns_empty(self, _krx, _yf):
        assert search_stocks("apple") == []

    @patch("src.stock_search._fetch_yf_search")
    @patch("src.stock_search._fetch_krx_listing")
    def test_korea_first_then_us(self, krx_mock, yf_mock):
        krx_mock.return_value = _FAKE_KRX_DF
        yf_mock.return_value = _FakeSearch([
            {"symbol": "AAPL", "shortname": "Apple Inc.", "quoteType": "EQUITY", "exchange": "NMS"},
        ])
        # 'ap' — KRX fake 데이터엔 매칭 없음, yfinance만 응답 → US 결과만 반환
        results = search_stocks("ap")
        assert results == [{"symbol": "AAPL", "name": "Apple Inc.", "market": "us"}]

    @patch("src.stock_search._fetch_yf_search")
    @patch("src.stock_search._fetch_krx_listing")
    def test_dedup_by_symbol(self, krx_mock, yf_mock):
        # KRX와 yfinance 양쪽 모두 005930.KS 반환 — 한 번만 등장
        krx_mock.return_value = _FAKE_KRX_DF
        yf_mock.return_value = _FakeSearch([
            {"symbol": "005930.KS", "shortname": "Samsung Electronics", "quoteType": "EQUITY", "exchange": "KSC"},
        ])
        results = search_stocks("삼성")
        samsung = [r for r in results if r["symbol"] == "005930.KS"]
        assert len(samsung) == 1
        assert samsung[0]["market"] == "korea"
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `pytest tests/test_stock_search.py::TestUSSearch -v`
Expected: FAIL — `_fetch_yf_search` 미정의

- [ ] **Step 3: 구현**

`src/stock_search.py`의 `_search_us` 함수를 다음으로 교체하고, `_fetch_yf_search` 헬퍼를 추가:

```python
_US_ALLOWED_TYPES = {"EQUITY", "ETF"}
_KR_EXCHANGES = {"KSC", "KOE"}  # 한국 거래소 코드


def _fetch_yf_search(query: str, max_results: int):
    """yfinance Search 호출 격리. quotes 속성을 가진 객체 반환."""
    import yfinance as yf
    return yf.Search(query, max_results=max_results)


def _search_us(query: str, limit: int) -> list[dict]:
    """yfinance Search로 미국 주식/ETF 검색. 한국 거래소 결과는 제외."""
    try:
        search = _fetch_yf_search(query, max_results=max(limit, 8))
        quotes = getattr(search, "quotes", None) or []
    except Exception as e:
        logger.warning("yfinance Search 실패: %s", e)
        return []

    results = []
    for q in quotes:
        if not isinstance(q, dict):
            continue
        symbol = str(q.get("symbol", "")).strip()
        if not symbol:
            continue
        qtype = str(q.get("quoteType", "")).upper()
        if qtype not in _US_ALLOWED_TYPES:
            continue
        exchange = str(q.get("exchange", "")).upper()
        if exchange in _KR_EXCHANGES or symbol.endswith((".KS", ".KQ")):
            continue
        name = str(q.get("shortname") or q.get("longname") or symbol).strip()
        results.append({"symbol": symbol, "name": name, "market": "us"})
        if len(results) >= limit:
            break
    return results
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `pytest tests/test_stock_search.py -v`
Expected: PASS (전체 14 tests)

- [ ] **Step 5: 커밋**

```bash
git add src/stock_search.py tests/test_stock_search.py
git commit -m "feat(stock_search): yfinance 기반 미국 종목 검색 추가"
```

---

## Task 5: `/api/stocks/search` 라우트

**Files:**
- Modify: `src/web_app.py` (추가 라우트)
- Test: `tests/test_web_app.py` (append)

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_web_app.py` 끝에 추가:

```python
from unittest.mock import patch


class TestApiStocksSearch:
    def test_returns_results(self, client):
        with patch("src.web_app.search_stocks") as m:
            m.return_value = [{"symbol": "AAPL", "name": "Apple Inc.", "market": "us"}]
            resp = client.get("/api/stocks/search?q=apple")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data == [{"symbol": "AAPL", "name": "Apple Inc.", "market": "us"}]
            m.assert_called_once_with("apple", limit=10)

    def test_short_query_returns_empty(self, client):
        with patch("src.web_app.search_stocks") as m:
            resp = client.get("/api/stocks/search?q=a")
            assert resp.status_code == 200
            assert resp.get_json() == []
            m.assert_not_called()

    def test_invalid_chars_returns_empty(self, client):
        with patch("src.web_app.search_stocks") as m:
            resp = client.get("/api/stocks/search?q=DROP%20TABLE%3B")
            assert resp.status_code == 200
            assert resp.get_json() == []
            m.assert_not_called()

    def test_too_long_query_returns_empty(self, client):
        with patch("src.web_app.search_stocks") as m:
            resp = client.get("/api/stocks/search?q=" + "a" * 51)
            assert resp.status_code == 200
            assert resp.get_json() == []
            m.assert_not_called()

    def test_missing_q_returns_empty(self, client):
        resp = client.get("/api/stocks/search")
        assert resp.status_code == 200
        assert resp.get_json() == []
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `pytest tests/test_web_app.py::TestApiStocksSearch -v`
Expected: FAIL — 404 또는 import 오류

- [ ] **Step 3: 구현**

`src/web_app.py` import 블록에 추가 (line 23 부근, 기존 validators import 옆):

```python
from src.validators import validate_stock_symbol, validate_stock_name, sanitize_stock_symbol, is_valid_search_query
from src.stock_search import search_stocks
```

`src/web_app.py`의 `api_job_status` 라우트 정의 직후(예: 약 829행)에 새 라우트 추가:

```python
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
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `pytest tests/test_web_app.py::TestApiStocksSearch -v`
Expected: PASS (5 tests)

- [ ] **Step 5: 전체 회귀 테스트**

Run: `pytest tests/test_web_app.py tests/test_stock_search.py tests/test_validators.py -v`
Expected: 모두 PASS

- [ ] **Step 6: 커밋**

```bash
git add src/web_app.py tests/test_web_app.py
git commit -m "feat(web): /api/stocks/search 라우트 추가"
```

---

## Task 6: 폼 마크업 + 자동완성 CSS

**Files:**
- Modify: `src/web_app.py` (CSS 추가, `index()`의 add_form 마크업 변경)

- [ ] **Step 1: CSS 추가**

`src/web_app.py`의 `_CSS` 상수 끝, `/* ── Responsive ── */` 직전에 다음 블록을 삽입:

```css
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
```

- [ ] **Step 2: 폼 마크업 교체**

`src/web_app.py`의 `index()` 안 `add_form` f-string에서 기존 심볼 필드 블록을 다음으로 교체:

기존 (약 611-614행):
```python
          <div class="field">
            <label>심볼</label>
            <input name="symbol" placeholder="예: AAPL, 005930" required style="width:160px;">
          </div>
```

교체 후:
```python
          <div class="field autocomplete-wrap">
            <label>검색</label>
            <input name="symbol" id="stock-search-input"
                   placeholder="종목명 또는 심볼 검색" autocomplete="off"
                   required style="width:240px;">
            <div id="autocomplete-list" class="autocomplete-list"></div>
          </div>
```

- [ ] **Step 3: 페이지 응답 수동 확인**

Flask dev server 실행:

```bash
python main.py --web --port 8080
```

브라우저에서 `http://localhost:8080/` 접속. "검색" 라벨, placeholder "종목명 또는 심볼 검색", `id="autocomplete-list"` 빈 div가 보이는지 DOM에서 확인. (이 단계에서는 JS가 없어 드롭다운은 안 뜸.)

서버 종료(Ctrl+C).

- [ ] **Step 4: 회귀 테스트**

Run: `pytest tests/test_web_app.py -v`
Expected: 기존 테스트 모두 PASS (마크업 변경이 기존 라우트 응답을 깨지 않아야 함)

- [ ] **Step 5: 커밋**

```bash
git add src/web_app.py
git commit -m "feat(web): 종목 추가 폼에 자동완성 마크업·CSS 추가"
```

---

## Task 7: 자동완성 JavaScript

**Files:**
- Modify: `src/web_app.py` (`_page` 함수 또는 인덱스 응답에 `<script>` 삽입)

- [ ] **Step 1: 스크립트 상수 추가**

`src/web_app.py`의 `_CSS` 상수 정의 직후, SVG 아이콘 상수들 위에 다음 추가:

```python
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

  function close() {
    list.classList.remove('open');
    list.innerHTML = '';
    activeIdx = -1;
    items = [];
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
    });
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
    items = results;
    activeIdx = -1;
  }

  input.addEventListener('input', () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 2) { close(); return; }
    timer = setTimeout(async () => {
      try {
        const res = await fetch('/api/stocks/search?q=' + encodeURIComponent(q));
        if (!res.ok) { close(); return; }
        const data = await res.json();
        render(Array.isArray(data) ? data : []);
      } catch { close(); }
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
```

- [ ] **Step 2: 인덱스 페이지에서 스크립트 사용**

`src/web_app.py`의 `index()` 라우트 끝부분, `refresh = "<script>setTimeout(...)</script>"` 라인을 다음으로 교체:

기존:
```python
    refresh = "<script>setTimeout(()=>location.reload(),5000);</script>" if running else ""
    return _page("대시보드", body, refresh)
```

교체 후:
```python
    refresh_script = "<script>setTimeout(()=>location.reload(),5000);</script>" if running else ""
    return _page("대시보드", body, refresh_script + _AUTOCOMPLETE_JS)
```

- [ ] **Step 3: 회귀 테스트**

Run: `pytest tests/test_web_app.py -v`
Expected: 모두 PASS

- [ ] **Step 4: 수동 검증 — 미국 검색**

```bash
python main.py --web --port 8080
```

브라우저에서 `http://localhost:8080/` 접속:

1. 검색 input에 `apple` 입력 → 0.3초 후 드롭다운에 AAPL 등 표시 확인.
2. 결과 클릭 → 심볼 input에 `AAPL`, 종목명에 `Apple Inc.`, 시장에 `미국`이 자동 채워지는지 확인.
3. "추가" 버튼 클릭 → 폼이 정상 제출되어 종목이 추가되는지 확인.

- [ ] **Step 5: 수동 검증 — 한국 검색**

같은 서버에서:

1. 검색 input을 비우고 `삼성` 입력 → 드롭다운에 삼성전자/삼성SDI 등 표시 확인.
2. 결과 클릭 → 심볼이 `005930.KS`, 종목명이 `삼성전자`, 시장이 `한국`으로 채워지는지 확인.
3. 추가 → 정상 처리 확인.

- [ ] **Step 6: 수동 검증 — 키보드/엣지 케이스**

1. 검색 input에 `app` 입력 → ↓ 키로 결과 이동, Enter로 선택되는지.
2. Esc 키로 드롭다운이 닫히는지.
3. `<script>` 같은 특수문자 입력 → API가 빈 배열 반환, 드롭다운에 "검색 결과 없음" 표시.
4. 외부 클릭 시 드롭다운 닫힘.

서버 종료.

- [ ] **Step 7: 커밋**

```bash
git add src/web_app.py
git commit -m "feat(web): 종목 추가 자동완성 JS 추가"
```

---

## Task 8: 최종 회귀 + 정리

- [ ] **Step 1: 전체 테스트 스위트**

Run: `pytest -v`
Expected: 모두 PASS

- [ ] **Step 2: lint 점검 (선택)**

Run: `python -m py_compile src/stock_search.py src/web_app.py src/validators.py`
Expected: 오류 없음

- [ ] **Step 3: git status 확인**

Run: `git status`
Expected: clean (수정 사항 모두 커밋됨)

- [ ] **Step 4: 변경 요약 commit log**

Run: `git log --oneline -10`
Expected: Task 1-7 커밋이 시간 역순으로 보임

---

## 자체 검증 체크리스트

| Spec 요구 사항 | 구현 위치 |
|---------------|----------|
| 인라인 자동완성 (드롭다운) | Task 6, 7 |
| 시장 무관 통합 검색 | Task 3, 4 |
| 2자 이상 + 300ms debounce | Task 7 (input 핸들러) |
| 최대 10개 결과 | Task 5 (`limit=10`) |
| 클릭 시 심볼/종목명/시장 자동 채움 | Task 7 (`pick()`) |
| 키보드 ↑↓/Enter/Esc | Task 7 (`keydown` 핸들러) |
| KRX 24h 캐시 | Task 3 |
| 한국 우선 정렬 | Task 3 (병합 순서) |
| 심볼 기준 dedup | Task 3 |
| 한국 거래소 quote 제외 (US 결과) | Task 4 |
| EQUITY/ETF만 필터 | Task 4 |
| 한쪽 실패 시 다른쪽 결과 반환 | Task 3, 4 (try/except) |
| 검색 쿼리 화이트리스트 sanitize | Task 1, 5 |
| XSS 안전 렌더링 (textContent) | Task 7 |
| CSRF 미적용 (GET 읽기) | Task 5 |
