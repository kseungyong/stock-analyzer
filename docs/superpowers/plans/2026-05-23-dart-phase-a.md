# DART 공시 통합 Phase A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 한국 종목 분석 카드에 DART 공시 분석 섹션 추가. 매일 KST 19:30 cron 이 DS005 + DS004 공시 일괄 fetch → 단일 critical=규칙 / 복수 critical=Gemini LLM 요약 → 카드에 "공시정보분석" 섹션 표시.

**Architecture:** 신규 5개 모듈 (`dart_client`/`dart_rules`/`dart_cache`/`dart_llm`/`log_filter`) + 별도 `dart_summaries` 테이블 (analysis_cache 미수정). atomic batch commit 으로 cron 중간 실패 대비.

**Tech Stack:** Python 3.14, requests, google.generativeai (기존), sqlite3 (기존)

**Spec:** `docs/superpowers/specs/2026-05-23-dart-phase-a-design.md` (commit 36f3b67)

---

## File Structure

**Create:**
- `src/log_filter.py` — `SecretFilter` (DART_API_KEY 마스킹)
- `src/dart_client.py` — DART HTTP wrapper, corp_code 매핑, fetch_disclosures
- `src/dart_rules.py` — classify_disclosures, render_template (단일 critical)
- `src/dart_cache.py` — corp_codes / disclosures / dart_summaries 테이블 + atomic batch
- `src/dart_llm.py` — Gemini summarize (복수 critical)
- `tests/test_log_filter.py` (2 tests)
- `tests/test_dart_client.py` (8 tests)
- `tests/test_dart_rules.py` (6 tests)
- `tests/test_dart_cache.py` (5 tests)
- `tests/test_dart_llm.py` (5 tests)

**Modify:**
- `main.py` — `dart-refresh` subcommand + handler
- `src/report_generator.py` — `_render_dart_section()` + `_render_stock_card` 호출
- `src/web_app.py` — home/portfolio 카드 공시 배지 + dart_summaries 조회
- `src/templates/report.css` — `.dart-section` 스타일
- `tests/test_report_generator.py` — 4 tests 추가 (TestRenderDartSection)
- `tests/test_main.py` — 2 tests 추가 (TestDartRefresh)

**No new dependencies** — `requests`, `google.generativeai` 모두 기존 사용 중.

---

## Task 1: log_filter — SecretFilter

**Files:**
- Create: `src/log_filter.py`
- Test: `tests/test_log_filter.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_log_filter.py`:

```python
"""src/log_filter.py 단위 테스트."""
import logging
import pytest
from src.log_filter import SecretFilter, install_secret_filter


class TestSecretFilter:
    def test_redacts_api_key_in_message(self):
        f = SecretFilter(["super-secret-key-12345"])
        record = logging.LogRecord(
            name="test", level=logging.WARNING, pathname="", lineno=0,
            msg="fetch failed: https://api.example.com/?key=super-secret-key-12345&q=1",
            args=(), exc_info=None,
        )
        f.filter(record)
        assert "super-secret-key-12345" not in record.getMessage()
        assert "***" in record.getMessage()

    def test_passthrough_when_no_secret(self):
        f = SecretFilter(["api-key-abc"])
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="normal log without any secret",
            args=(), exc_info=None,
        )
        f.filter(record)
        assert record.getMessage() == "normal log without any secret"
```

- [ ] **Step 2: Run tests to verify failure**

```bash
cd /Users/sykim/Projects/stock-analyzer
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_log_filter.py -v
```

Expected: 2 FAIL — `ImportError: No module named 'src.log_filter'`

- [ ] **Step 3: Implement**

Create `src/log_filter.py`:

```python
"""log_filter — Python logging filter 로 시크릿 (API key 등) 자동 마스킹.

사용:
    from src.log_filter import install_secret_filter
    install_secret_filter([os.environ["DART_API_KEY"]])

이후 모든 logger 의 메시지/args 에서 해당 문자열이 자동으로 *** 로 치환.
URL query param, 예외 메시지, format 인자 등 모든 출력 경로 커버.
"""
from __future__ import annotations

import logging


class SecretFilter(logging.Filter):
    """logging.Filter 구현 — 로그 메시지에서 secret 문자열을 *** 로 치환."""

    def __init__(self, secrets: list[str]):
        super().__init__()
        # 빈 문자열은 무한 루프 위험 → 제외
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        # record.msg + args 모두 처리. getMessage() 호출 시점에 적용.
        msg = str(record.msg)
        for secret in self._secrets:
            msg = msg.replace(secret, "***")
        record.msg = msg
        if record.args:
            new_args = []
            for a in record.args:
                s = str(a)
                for secret in self._secrets:
                    s = s.replace(secret, "***")
                new_args.append(s)
            record.args = tuple(new_args)
        return True


def install_secret_filter(secrets: list[str]) -> None:
    """root logger 에 SecretFilter 적용. 모든 child logger 도 영향 받음."""
    flt = SecretFilter(secrets)
    logging.getLogger().addFilter(flt)
```

- [ ] **Step 4: Run tests to verify pass**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_log_filter.py -v
```

Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add src/log_filter.py tests/test_log_filter.py
git commit -m "feat(log_filter): SecretFilter — API key 로그 마스킹 (DART_API_KEY 누출 방지)"
```

---

## Task 2: corp_codes 다운로드 + DB

**Files:**
- Create: `src/dart_cache.py` (corp_codes 부분만)
- Create: `src/dart_client.py` (corp_code 다운로드/조회 부분만)
- Create: `tests/test_dart_cache.py` (1 test)
- Create: `tests/test_dart_client.py` (3 tests, corp_code 관련)
- Create: `tests/fixtures/CORPCODE_sample.xml`

- [ ] **Step 1: Create fixture**

Create `tests/fixtures/CORPCODE_sample.xml`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<result>
    <list>
        <corp_code>00126380</corp_code>
        <corp_name>삼성전자</corp_name>
        <stock_code>005930</stock_code>
        <modify_date>20240501</modify_date>
    </list>
    <list>
        <corp_code>00164779</corp_code>
        <corp_name>SK하이닉스</corp_name>
        <stock_code>000660</stock_code>
        <modify_date>20240502</modify_date>
    </list>
    <list>
        <corp_code>99999991</corp_code>
        <corp_name>비상장회사</corp_name>
        <stock_code></stock_code>
        <modify_date>20240503</modify_date>
    </list>
</result>
```

- [ ] **Step 2: Write failing tests**

Create `tests/test_dart_cache.py`:

```python
"""src/dart_cache.py 단위 테스트."""
import time
import pytest
from src import dart_cache


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    db = tmp_path / "test_predictions.db"
    monkeypatch.setattr(dart_cache, "_DB_PATH", db)
    dart_cache.init_db()
    yield


class TestCorpCodes:
    def test_upsert_dedup(self):
        rows = [
            {"corp_code": "00126380", "corp_name": "삼성전자",
             "stock_code": "005930", "modify_date": "20240501"},
        ]
        dart_cache.upsert_corp_codes(rows)
        # 같은 corp_code 두 번 → 1 row
        rows2 = [
            {"corp_code": "00126380", "corp_name": "삼성전자(주)",
             "stock_code": "005930", "modify_date": "20240601"},
        ]
        dart_cache.upsert_corp_codes(rows2)
        result = dart_cache.get_corp_code_by_stock("005930")
        assert result == "00126380"
        # corp_name 도 최신 값 (last writer wins)
```

Create `tests/test_dart_client.py`:

```python
"""src/dart_client.py 단위 테스트."""
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from src import dart_client


_FIXTURE_DIR = Path(__file__).parent / "fixtures"


class TestCorpCodeXmlParse:
    def test_parse_extracts_records(self):
        xml_text = (_FIXTURE_DIR / "CORPCODE_sample.xml").read_text(encoding="utf-8")
        rows = dart_client._parse_corp_code_xml(xml_text)
        assert len(rows) == 3
        samsung = next(r for r in rows if r["corp_code"] == "00126380")
        assert samsung["corp_name"] == "삼성전자"
        assert samsung["stock_code"] == "005930"
        # 비상장 (stock_code 빈 문자열) 도 포함
        unlisted = next(r for r in rows if r["corp_code"] == "99999991")
        assert unlisted["stock_code"] == ""


class TestGetCorpCode:
    @patch("src.dart_client.dart_cache.get_corp_code_by_stock")
    def test_finds_listed_stock(self, mock_get):
        mock_get.return_value = "00126380"
        result = dart_client.get_corp_code("005930.KS")
        assert result == "00126380"
        mock_get.assert_called_once_with("005930")

    @patch("src.dart_client.dart_cache.get_corp_code_by_stock")
    def test_returns_none_for_unknown(self, mock_get):
        mock_get.return_value = None
        assert dart_client.get_corp_code("999999.KS") is None
```

- [ ] **Step 3: Run tests to verify failure**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_dart_cache.py tests/test_dart_client.py -v
```

Expected: FAIL — `ImportError: No module named 'src.dart_cache'` / `'src.dart_client'`

- [ ] **Step 4: Implement dart_cache (corp_codes only)**

Create `src/dart_cache.py`:

```python
"""dart_cache — DART corp_codes + disclosures + dart_summaries DB layer.

기존 data/predictions.db 재사용 (analysis_cache 등과 같은 파일).
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "predictions.db"
_writer_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS corp_codes (
    corp_code   TEXT PRIMARY KEY,
    corp_name   TEXT NOT NULL,
    stock_code  TEXT,
    modify_date TEXT
);
CREATE INDEX IF NOT EXISTS idx_corp_codes_stock_code ON corp_codes(stock_code);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _writer_lock:
        with closing(_connect()) as conn:
            conn.executescript(_SCHEMA)
    logger.info("dart_cache DB 초기화 완료: %s", _DB_PATH)


def upsert_corp_codes(rows: list[dict]) -> int:
    """INSERT OR REPLACE — 동일 corp_code 는 덮어쓴다. 반환: 처리된 row 수."""
    if not rows:
        return 0
    with _writer_lock:
        with closing(_connect()) as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO corp_codes "
                "(corp_code, corp_name, stock_code, modify_date) VALUES (?, ?, ?, ?)",
                [(r["corp_code"], r["corp_name"], r["stock_code"], r["modify_date"])
                 for r in rows],
            )
            conn.commit()
    return len(rows)


def get_corp_code_by_stock(stock_code: str) -> str | None:
    """주식 종목코드 → DART corp_code. 없으면 None."""
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT corp_code FROM corp_codes WHERE stock_code = ? LIMIT 1",
            (stock_code,),
        ).fetchone()
    return row[0] if row else None


def corp_codes_last_modify_date() -> str | None:
    """corp_codes 테이블의 최대 modify_date. 없으면 None (=재다운로드 필요)."""
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT MAX(modify_date) FROM corp_codes"
        ).fetchone()
    return row[0] if row and row[0] else None
```

- [ ] **Step 5: Implement dart_client (corp_code part only)**

Create `src/dart_client.py`:

```python
"""dart_client — DART OpenAPI HTTP wrapper.

corp_code 매핑 + Phase A 9개 endpoint (DS001 list + DS005 6 + DS004 2).
"""
from __future__ import annotations

import io
import logging
import os
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime

import requests

from src import dart_cache

logger = logging.getLogger(__name__)

_DART_BASE = "https://opendart.fss.or.kr/api"
_CORP_CODE_URL = _DART_BASE + "/corpCode.xml"
_HTTP_TIMEOUT = 30


def _api_key() -> str:
    key = os.environ.get("DART_API_KEY")
    if not key:
        raise RuntimeError("DART_API_KEY 환경변수 없음")
    return key


def _parse_corp_code_xml(xml_text: str) -> list[dict]:
    """CORPCODE.xml → [{corp_code, corp_name, stock_code, modify_date}, ...]"""
    root = ET.fromstring(xml_text)
    rows = []
    for elem in root.findall(".//list"):
        rows.append({
            "corp_code": (elem.findtext("corp_code") or "").strip(),
            "corp_name": (elem.findtext("corp_name") or "").strip(),
            "stock_code": (elem.findtext("stock_code") or "").strip(),
            "modify_date": (elem.findtext("modify_date") or "").strip(),
        })
    return rows


def _to_krx_code(symbol: str) -> str:
    """'005930.KS' → '005930' (suffix 제거)."""
    return symbol.split(".")[0].zfill(6)


def get_corp_code(symbol: str) -> str | None:
    """yfinance 심볼 → DART corp_code. 캐시 조회 only."""
    krx = _to_krx_code(symbol)
    return dart_cache.get_corp_code_by_stock(krx)


def download_corp_codes() -> int:
    """corpCode.xml ZIP 다운로드 → XML parse → corp_codes 테이블 UPSERT.

    Returns: 갱신된 row 수.
    """
    url = _CORP_CODE_URL
    try:
        resp = requests.get(
            url, params={"crtfc_key": _api_key()}, timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
    except Exception as e:
        logger.warning("corpCode.xml 다운로드 실패: %s", e)
        return 0
    try:
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            xml_text = zf.read("CORPCODE.xml").decode("utf-8")
    except (zipfile.BadZipFile, KeyError) as e:
        logger.warning("corpCode.xml ZIP 파싱 실패: %s", e)
        return 0
    rows = _parse_corp_code_xml(xml_text)
    return dart_cache.upsert_corp_codes(rows)


def refresh_corp_codes_if_stale(days: int = 7) -> int:
    """마지막 modify_date 가 N일 이전이면 download. 아니면 0 반환."""
    last = dart_cache.corp_codes_last_modify_date()
    if last:
        try:
            last_dt = datetime.strptime(last, "%Y%m%d")
            age_days = (datetime.now() - last_dt).days
            if age_days < days:
                logger.info("corp_codes %d일 stale (< %d) — skip download", age_days, days)
                return 0
        except ValueError:
            logger.warning("corp_codes modify_date 파싱 실패: %s — 재다운로드", last)
    return download_corp_codes()
```

- [ ] **Step 6: Run tests to verify pass**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_dart_cache.py tests/test_dart_client.py -v
```

Expected: 4 PASS (1 cache + 3 client)

- [ ] **Step 7: Commit**

```bash
git add src/dart_cache.py src/dart_client.py tests/test_dart_cache.py tests/test_dart_client.py tests/fixtures/CORPCODE_sample.xml
git commit -m "feat(dart): corp_code 매핑 (XML parse + DB cache + 7일 stale)"
```

---

## Task 3: DART API fetch_disclosures (9 endpoints)

**Files:**
- Modify: `src/dart_client.py` (add fetch_disclosures + endpoint helpers)
- Modify: `tests/test_dart_client.py` (add TestFetchDisclosures + TestRefreshCorpCodes — 5 tests)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_dart_client.py`:

```python
class TestFetchDisclosures:
    @patch("src.dart_client.requests.get")
    def test_aggregates_all_endpoints(self, mock_get):
        # 9 endpoint 모두 성공 응답
        def fake_get(url, **kwargs):
            return MagicMock(status_code=200, json=lambda: {"status": "000", "list": [
                {"rcept_no": f"{url[-15:]}_1", "rcept_dt": "20260520"},
            ]})
        mock_get.side_effect = fake_get
        result = dart_client.fetch_disclosures("00126380", days=30)
        # 8 critical 카테고리 키 모두 존재 (list 는 별도)
        for key in ("list", "capital_increase", "capital_decrease",
                    "treasury_acquire", "treasury_dispose", "merger",
                    "major_holders", "exec_holders"):
            assert key in result
            assert len(result[key]) == 1

    @patch("src.dart_client.requests.get")
    def test_partial_failure_returns_partial(self, mock_get):
        # 1번째 호출은 실패, 나머지는 성공
        calls = [0]
        def fake_get(url, **kwargs):
            calls[0] += 1
            if calls[0] == 1:
                raise Exception("transient network error")
            return MagicMock(status_code=200, json=lambda: {"status": "000", "list": []})
        mock_get.side_effect = fake_get
        result = dart_client.fetch_disclosures("00126380", days=30)
        # 결과 dict 가 반환됨 (전체 None 이 아님)
        assert isinstance(result, dict)
        assert "list" in result

    @patch("src.dart_client.requests.get")
    def test_rate_limit_sleep(self, mock_get, monkeypatch):
        mock_get.return_value = MagicMock(
            status_code=200, json=lambda: {"status": "000", "list": []},
        )
        sleep_calls = []
        monkeypatch.setattr(dart_client.time, "sleep", lambda s: sleep_calls.append(s))
        dart_client.fetch_disclosures("00126380", days=30)
        # 9 endpoint 사이에 sleep 8회 (마지막 호출 후엔 안 함)
        assert len(sleep_calls) >= 8
        assert all(s == dart_client._RATE_LIMIT_SLEEP for s in sleep_calls)

    @patch("src.dart_client.requests.get")
    def test_dart_status_013_treated_as_no_data(self, mock_get):
        # DART 의 "013" = 조회 데이터 없음 → 정상 (warn 없음, 빈 list)
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"status": "013", "message": "조회된 데이타가 없습니다", "list": []},
        )
        result = dart_client.fetch_disclosures("00126380", days=30)
        for key in ("capital_increase", "treasury_acquire"):
            assert result[key] == []


class TestRefreshCorpCodes:
    @patch("src.dart_client.download_corp_codes")
    @patch("src.dart_client.dart_cache.corp_codes_last_modify_date")
    def test_skips_if_recent(self, mock_last, mock_download):
        # 어제 갱신됨 → skip
        from datetime import datetime, timedelta
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
        mock_last.return_value = yesterday
        result = dart_client.refresh_corp_codes_if_stale(days=7)
        assert result == 0
        mock_download.assert_not_called()
```

- [ ] **Step 2: Run tests to verify failure**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_dart_client.py::TestFetchDisclosures tests/test_dart_client.py::TestRefreshCorpCodes -v
```

Expected: 5 FAIL — `AttributeError: module 'src.dart_client' has no attribute 'fetch_disclosures'`

- [ ] **Step 3: Implement fetch_disclosures**

In `src/dart_client.py`, append:

```python
_RATE_LIMIT_SLEEP = 0.5  # 1초당 10회 limit 안전 마진


# Phase A 9개 endpoint — (key, url_suffix) 쌍
_ENDPOINTS = [
    ("list",              "/list.json"),               # DS001 공시 list
    ("capital_increase",  "/piicDecsn.json"),          # DS005 유상증자
    ("capital_decrease",  "/crDecsn.json"),            # DS005 감자
    ("treasury_acquire",  "/tsstkAqDecsn.json"),       # DS005 자기주식 취득
    ("treasury_dispose",  "/tsstkDpDecsn.json"),       # DS005 자기주식 처분
    ("merger",            "/cmpMgDecsn.json"),         # DS005 합병
    ("major_holders",     "/majorstock.json"),         # DS004 대량보유
    ("exec_holders",      "/elestock.json"),           # DS004 임원 소유
    ("free_increase",     "/pifricDecsn.json"),        # DS005 무상증자
]


def _call_endpoint(url_suffix: str, params: dict) -> list[dict]:
    """단일 endpoint 호출. 정상=list, status 013=빈 list, 그 외 실패=빈 list+warn.

    예외는 caller 가 catch (한 endpoint 실패가 다른 endpoint 막지 않게).
    """
    url = _DART_BASE + url_suffix
    try:
        resp = requests.get(url, params=params, timeout=_HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning("DART %s 호출 실패: %s", url_suffix, e)
        return []
    status = data.get("status", "")
    if status == "013":
        # "조회된 데이타가 없습니다" — 정상 응답
        return []
    if status not in ("000", "013"):
        logger.warning("DART %s status=%s message=%s",
                       url_suffix, status, data.get("message"))
        return []
    return data.get("list") or []


def fetch_disclosures(corp_code: str, days: int = 30) -> dict:
    """9 endpoint 일괄 호출. 호출 간 sleep(_RATE_LIMIT_SLEEP).

    Returns: {key: list[dict]} — 9개 key 모두 존재 (실패해도 빈 list).
    """
    today = datetime.now()
    end_de = today.strftime("%Y%m%d")
    bgn_de = (today.replace(day=1) if days >= 30 else today).strftime("%Y%m%d")
    # 단순화: days 일 전부터
    from datetime import timedelta
    bgn_de = (today - timedelta(days=days)).strftime("%Y%m%d")

    base_params = {
        "crtfc_key": _api_key(),
        "corp_code": corp_code,
        "bgn_de": bgn_de,
        "end_de": end_de,
    }

    result: dict[str, list[dict]] = {}
    for i, (key, suffix) in enumerate(_ENDPOINTS):
        params = dict(base_params)
        # DS001 list 는 pblntf_detail_ty 추가
        if key == "list":
            params["pblntf_detail_ty"] = "PBL"
        try:
            result[key] = _call_endpoint(suffix, params)
        except Exception as e:
            logger.warning("fetch_disclosures %s exception: %s", key, e)
            result[key] = []
        # 마지막 endpoint 후엔 sleep 안 함
        if i < len(_ENDPOINTS) - 1:
            time.sleep(_RATE_LIMIT_SLEEP)
    return result
```

- [ ] **Step 4: Run tests to verify pass**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_dart_client.py -v
```

Expected: 8 PASS (3 from Task 2 + 5 new)

- [ ] **Step 5: Commit**

```bash
git add src/dart_client.py tests/test_dart_client.py
git commit -m "feat(dart): fetch_disclosures — 9 endpoint + rate limit + status 013 처리"
```

---

## Task 4: dart_rules — classify + template

**Files:**
- Create: `src/dart_rules.py`
- Create: `tests/test_dart_rules.py` (6 tests)

- [ ] **Step 1: Write failing tests**

Create `tests/test_dart_rules.py`:

```python
"""src/dart_rules.py 단위 테스트."""
import pytest
from src import dart_rules


def _disclosures(**overrides):
    """모든 key 가 빈 list 인 baseline + 일부 채우기."""
    base = {
        "list": [], "capital_increase": [], "capital_decrease": [],
        "treasury_acquire": [], "treasury_dispose": [], "merger": [],
        "major_holders": [], "exec_holders": [], "free_increase": [],
    }
    base.update(overrides)
    return base


class TestClassifyDisclosures:
    def test_treasury_acquire_is_tier1_critical(self):
        disc = _disclosures(treasury_acquire=[{"rcept_no": "X", "aqpln_amount": "20000000000"}])
        result = dart_rules.classify_disclosures(disc)
        assert result["count"] == 1
        assert result["critical_events"][0]["type"] == "treasury_acquire"
        assert result["critical_events"][0]["tier"] == "high"
        assert result["should_call_llm"] is False  # count == 1

    def test_exec_holders_below_threshold_excluded(self):
        # 임원 1주 매수 (1000주 미만) → 제외
        disc = _disclosures(exec_holders=[
            {"rcept_no": "X", "stkqy": "1", "stkrt": "0.001"}
        ])
        result = dart_rules.classify_disclosures(disc)
        assert result["count"] == 0

    def test_exec_holders_above_threshold_included(self):
        # 임원 5000주 매수 → critical
        disc = _disclosures(exec_holders=[
            {"rcept_no": "X", "stkqy": "5000", "stkrt": "0.05"}
        ])
        result = dart_rules.classify_disclosures(disc)
        assert result["count"] == 1
        assert result["critical_events"][0]["type"] == "exec_holders"
        assert result["critical_events"][0]["tier"] == "medium"

    def test_major_holders_below_threshold_excluded(self):
        # 변동 0.1%p (< 0.5%p) → 제외
        disc = _disclosures(major_holders=[
            {"rcept_no": "X", "stkrt": "5.1", "stkrt_irds": "0.1"}
        ])
        result = dart_rules.classify_disclosures(disc)
        assert result["count"] == 0

    def test_should_call_llm_true_when_count_ge_2(self):
        disc = _disclosures(
            treasury_acquire=[{"rcept_no": "A", "aqpln_amount": "1"}],
            capital_increase=[{"rcept_no": "B", "nstk_ostk_qy": "1"}],
        )
        result = dart_rules.classify_disclosures(disc)
        assert result["count"] == 2
        assert result["should_call_llm"] is True


class TestRenderTemplate:
    def test_treasury_acquire_returns_buy_view(self):
        event = {
            "type": "treasury_acquire", "tier": "high",
            "raw": {"rcept_no": "20260520000001", "aqpln_amount": "20000000000"},
        }
        result = dart_rules.render_template(event)
        assert result["sentiment"] == "긍정"
        assert "매수" in result["trading_view"]
        assert "자기주식" in result["summary"]
        assert len(result["key_events"]) >= 1
        assert result["model"] == "rule_based"
        assert "generated_at" in result
```

- [ ] **Step 2: Run tests to verify failure**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_dart_rules.py -v
```

Expected: 6 FAIL — `ImportError`

- [ ] **Step 3: Implement**

Create `src/dart_rules.py`:

```python
"""dart_rules — DART 공시 critical event 분류 + 단일 case 규칙 기반 template.

Hybrid 전략:
- count == 0: empty marker
- count == 1: render_template (LLM 호출 X)
- count >= 2: caller 가 dart_llm.summarize_disclosures 호출
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

# tier 1 — 임계치 없이 무조건 critical
_TIER1_KEYS = (
    "capital_increase", "capital_decrease",
    "treasury_acquire", "treasury_dispose",
    "merger",
)

# tier 2 — 임계치 적용
_MAJOR_HOLDERS_MIN_DELTA_PP = 0.5      # 변동 비율 0.5%p 이상
_MAJOR_HOLDERS_MIN_HOLDING_PCT = 5.0   # 보유 비율 5% 이상
_EXEC_HOLDERS_MIN_QTY = 1000           # 변동 주식수 1000주 이상

# 단일 case template — {type: (sentiment, trading_view, summary_template)}
_TEMPLATES = {
    "treasury_acquire": (
        "긍정", "매수 — 자사주 매입은 EPS 상승 + 회사 자신감 표명",
        "자기주식 취득 결정 — 주주환원 시그널",
    ),
    "treasury_dispose": (
        "부정", "매도 — 자사주 매도는 유통량 증가 + 주가 압박",
        "자기주식 처분 결정 — 유통량 증가 우려",
    ),
    "capital_increase": (
        "부정", "매도 — 신주 발행으로 기존 주주 지분 희석",
        "유상증자 결정 — 신주 발행에 따른 희석",
    ),
    "capital_decrease": (
        "부정", "매도 — 감자는 일반적으로 부정 시그널 (재무 악화 가능)",
        "감자 결정 — 자본 감소",
    ),
    "merger": (
        "중립", "관망 — 합병 효과 분석 필요 (시너지 vs 통합 비용)",
        "합병 결정 — 시너지/통합 비용 분석 필요",
    ),
    "major_holders": (
        "중립", "관망 — 대량보유 변동, 보유자 의도 분석 필요",
        "대량보유 변동 (5%+)",
    ),
    "exec_holders": (
        "긍정", "매수 — 임원 자사주 매수는 회사 내부 자신감 시그널",
        "임원/주요주주 매수",
    ),
}


def _safe_float(s, default=0.0):
    try:
        return float(str(s).replace(",", ""))
    except (ValueError, TypeError):
        return default


def _safe_int(s, default=0):
    try:
        return int(float(str(s).replace(",", "")))
    except (ValueError, TypeError):
        return default


def classify_disclosures(disclosures: dict) -> dict:
    """critical event 분류.

    Returns:
        {
            "critical_events": [{"type", "tier", "raw"}, ...],
            "count": int,
            "should_call_llm": bool,   # count >= 2
        }
    """
    events: list[dict] = []

    for key in _TIER1_KEYS:
        for raw in disclosures.get(key) or []:
            events.append({"type": key, "tier": "high", "raw": raw})

    # tier 2 - major_holders
    for raw in disclosures.get("major_holders") or []:
        delta_pp = abs(_safe_float(raw.get("stkrt_irds", 0)))
        holding = _safe_float(raw.get("stkrt", 0))
        if delta_pp >= _MAJOR_HOLDERS_MIN_DELTA_PP and holding >= _MAJOR_HOLDERS_MIN_HOLDING_PCT:
            events.append({"type": "major_holders", "tier": "medium", "raw": raw})

    # tier 2 - exec_holders
    for raw in disclosures.get("exec_holders") or []:
        qty = abs(_safe_int(raw.get("stkqy", 0)))
        if qty >= _EXEC_HOLDERS_MIN_QTY:
            events.append({"type": "exec_holders", "tier": "medium", "raw": raw})

    return {
        "critical_events": events,
        "count": len(events),
        "should_call_llm": len(events) >= 2,
    }


def render_template(event: dict) -> dict:
    """단일 critical event 를 규칙 기반 요약 dict 로 변환.

    Returns: {summary, sentiment, key_events, trading_view, model, generated_at}
    """
    event_type = event["type"]
    raw = event.get("raw") or {}
    sentiment, trading_view, summary_template = _TEMPLATES.get(
        event_type, ("중립", "관망 — 규칙 미정의", "공시 발생"),
    )
    rcept_no = raw.get("rcept_no", "")
    key_event = f"[{rcept_no}] {summary_template}"
    return {
        "summary": summary_template,
        "sentiment": sentiment,
        "key_events": [key_event],
        "trading_view": trading_view,
        "model": "rule_based",
        "generated_at": int(time.time()),
    }
```

- [ ] **Step 4: Run tests to verify pass**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_dart_rules.py -v
```

Expected: 6 PASS

- [ ] **Step 5: Commit**

```bash
git add src/dart_rules.py tests/test_dart_rules.py
git commit -m "feat(dart): classify_disclosures + render_template (단일 critical case)"
```

---

## Task 5: dart_llm — Gemini summarize (복수 critical)

**Files:**
- Create: `src/dart_llm.py`
- Create: `tests/test_dart_llm.py` (5 tests)

- [ ] **Step 1: Write failing tests**

Create `tests/test_dart_llm.py`:

```python
"""src/dart_llm.py 단위 테스트."""
from unittest.mock import MagicMock, patch
import pytest

from src import dart_llm


def _classified(count=2):
    return {
        "count": count,
        "should_call_llm": count >= 2,
        "critical_events": [
            {"type": "treasury_acquire", "tier": "high",
             "raw": {"rcept_no": "20260520000001", "aqpln_amount": "20000000000"}},
            {"type": "capital_increase", "tier": "high",
             "raw": {"rcept_no": "20260521000002", "nstk_ostk_qy": "1000000"}},
        ],
    }


class TestSummarizeDisclosures:
    @patch("src.dart_llm._get_model")
    def test_parses_gemini_json(self, mock_model_fn):
        mock_model = MagicMock()
        mock_resp = MagicMock()
        mock_resp.text = (
            '{"summary": "자기주식 취득 + 유상증자 동시 발생",'
            ' "sentiment": "중립",'
            ' "key_events": ["[20260520000001] 자기주식 200억", "[20260521000002] 유상증자"],'
            ' "trading_view": "관망 — 상반된 시그널"}'
        )
        mock_model.generate_content.return_value = mock_resp
        mock_model_fn.return_value = mock_model

        result = dart_llm.summarize_disclosures("005930.KS", "삼성전자", _classified())
        assert result is not None
        assert result["summary"].startswith("자기주식")
        assert result["sentiment"] == "중립"
        assert len(result["key_events"]) == 2
        assert "관망" in result["trading_view"]
        assert result["model"] == "gemini-2.5-flash"
        assert "generated_at" in result

    @patch("src.dart_llm._get_model")
    def test_validates_sentiment_enum(self, mock_model_fn):
        # Gemini 가 "긍정적" 같은 변형 반환 → "중립" fallback
        mock_model = MagicMock()
        mock_resp = MagicMock()
        mock_resp.text = (
            '{"summary": "...", "sentiment": "긍정적",'
            ' "key_events": ["[X] e1"], "trading_view": "매수 — 근거"}'
        )
        mock_model.generate_content.return_value = mock_resp
        mock_model_fn.return_value = mock_model

        result = dart_llm.summarize_disclosures("005930.KS", "삼성전자", _classified())
        assert result["sentiment"] == "중립"  # fallback

    @patch("src.dart_llm._get_model")
    def test_validates_trading_view_prefix(self, mock_model_fn):
        # trading_view 가 "강한매수" → "관망 — ..." fallback
        mock_model = MagicMock()
        mock_resp = MagicMock()
        mock_resp.text = (
            '{"summary": "...", "sentiment": "긍정",'
            ' "key_events": ["[X] e1"], "trading_view": "강한매수 — 근거"}'
        )
        mock_model.generate_content.return_value = mock_resp
        mock_model_fn.return_value = mock_model

        result = dart_llm.summarize_disclosures("005930.KS", "삼성전자", _classified())
        assert result["trading_view"].startswith("관망")
        assert "LLM 응답 형식 오류" in result["trading_view"]

    @patch("src.dart_llm._get_model")
    def test_parse_failure_falls_back_to_raw(self, mock_model_fn):
        mock_model = MagicMock()
        mock_resp = MagicMock()
        mock_resp.text = "this is not json at all"
        mock_model.generate_content.return_value = mock_resp
        mock_model_fn.return_value = mock_model

        result = dart_llm.summarize_disclosures("005930.KS", "삼성전자", _classified())
        assert result is not None
        assert result["sentiment"] == "중립"
        assert "관망" in result["trading_view"]

    @patch("src.dart_llm._get_model")
    def test_api_error_returns_none(self, mock_model_fn):
        mock_model = MagicMock()
        mock_model.generate_content.side_effect = Exception("API timeout")
        mock_model_fn.return_value = mock_model

        result = dart_llm.summarize_disclosures("005930.KS", "삼성전자", _classified())
        assert result is None
```

- [ ] **Step 2: Run tests to verify failure**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_dart_llm.py -v
```

Expected: 5 FAIL — `ImportError`

- [ ] **Step 3: Implement**

Create `src/dart_llm.py`:

```python
"""dart_llm — Gemini 2.5 Flash 기반 DART 공시 종합 해석 (hybrid 복수 case 전용).

count >= 2 일 때만 호출. count == 0/1 은 dart_rules 가 처리.
"""
from __future__ import annotations

import json
import logging
import os
import time

import google.generativeai as genai  # type: ignore

logger = logging.getLogger(__name__)

_MODEL_NAME = "gemini-2.5-flash"
_TIMEOUT_S = 30

_VALID_SENTIMENT = ("긍정", "부정", "중립")
_VALID_TRADING_PREFIX = ("매수", "매도", "관망")

_SYSTEM_INSTRUCTION = (
    "당신은 한국 주식 시장의 공시 분석 전문가다. 입력으로 받은 critical events 를 "
    "사실 기반으로 종합 해석한다. 출력은 반드시 strict JSON, 다른 텍스트 금지. "
    "key_events 의 각 항목은 [rcept_no] 형식으로 사실 인용 필수. 추정/과장 금지."
)

_PROMPT_TEMPLATE = """종목: {name} ({symbol})

최근 30일 주요 공시 (분류된 critical events):
{events_json}

다음 규칙을 엄격히 지켜:
1. key_events 의 각 항목은 입력에 있는 rcept_no 를 [접수번호] 형식으로 시작.
2. sentiment 는 "긍정" / "부정" / "중립" 중 하나만 (정확한 enum).
3. trading_view 는 "매수" / "매도" / "관망" 중 하나로 시작하고, " — " 다음에 1줄 근거.

응답은 아래 JSON 형식만:

{{
  "summary": "2-3문장 종합 해석 (여러 공시의 상호 영향)",
  "sentiment": "긍정 또는 부정 또는 중립",
  "key_events": ["[rcept_no] 사실 인용 1", "[rcept_no] 사실 인용 2"],
  "trading_view": "매수|매도|관망 — 1줄 근거"
}}
"""


def _get_model():  # noqa: ANN202
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY 환경변수 없음")
    genai.configure(api_key=api_key)
    return genai.GenerativeModel(
        model_name=_MODEL_NAME,
        system_instruction=_SYSTEM_INSTRUCTION,
    )


def _validate_and_normalize(parsed: dict) -> dict:
    """sentiment / trading_view enum 검증 + fallback."""
    sentiment = parsed.get("sentiment", "")
    if sentiment not in _VALID_SENTIMENT:
        logger.warning("dart_llm sentiment enum 위반: %r → 중립", sentiment)
        sentiment = "중립"

    trading_view = (parsed.get("trading_view") or "").strip()
    if not any(trading_view.startswith(p) for p in _VALID_TRADING_PREFIX):
        logger.warning("dart_llm trading_view prefix 위반: %r → 관망 fallback", trading_view)
        trading_view = "관망 — LLM 응답 형식 오류"

    return {
        "summary": str(parsed.get("summary", ""))[:1000],
        "sentiment": sentiment,
        "key_events": [str(e) for e in (parsed.get("key_events") or [])][:5],
        "trading_view": trading_view,
        "model": _MODEL_NAME,
        "generated_at": int(time.time()),
    }


def summarize_disclosures(symbol: str, name: str, classified: dict) -> dict | None:
    """복수 critical events 종합 요약. count < 2 면 호출 금지.

    Returns:
        dict — 정상 또는 parse 실패 시 fallback
        None — API 호출 실패 (timeout/429/etc)
    """
    if not classified.get("should_call_llm"):
        logger.warning("dart_llm: should_call_llm=False — caller 가 잘못 호출")
        return None

    events_json = json.dumps(
        classified["critical_events"], ensure_ascii=False, indent=2,
    )[:4000]  # 토큰 절약
    prompt = _PROMPT_TEMPLATE.format(
        name=name, symbol=symbol, events_json=events_json,
    )
    gen_cfg = {
        "temperature": 0.3,
        "max_output_tokens": 1024,
        "response_mime_type": "application/json",
    }

    try:
        model = _get_model()
        resp = model.generate_content(
            prompt,
            generation_config=gen_cfg,
            request_options={"timeout": _TIMEOUT_S},
        )
        raw = getattr(resp, "text", "") or ""
    except Exception as e:
        logger.warning("dart_llm API 호출 실패: %s", e)
        return None

    try:
        parsed = json.loads(raw.strip())
    except json.JSONDecodeError:
        logger.warning("dart_llm JSON parse 실패 — fallback")
        return {
            "summary": raw[:200],
            "sentiment": "중립",
            "key_events": [],
            "trading_view": "관망 — LLM 응답 형식 오류",
            "model": _MODEL_NAME,
            "generated_at": int(time.time()),
        }

    return _validate_and_normalize(parsed)
```

- [ ] **Step 4: Run tests to verify pass**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_dart_llm.py -v
```

Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add src/dart_llm.py tests/test_dart_llm.py
git commit -m "feat(dart): dart_llm — Gemini 2.5 Flash 요약 (복수 critical + enum 검증)"
```

---

## Task 6: dart_summaries DB + disclosures + main.py dart-refresh

**Files:**
- Modify: `src/dart_cache.py` (disclosures + dart_summaries 추가)
- Modify: `main.py` (dart-refresh subcommand)
- Modify: `tests/test_dart_cache.py` (4 tests 추가)
- Modify: `tests/test_main.py` (2 tests 추가)

- [ ] **Step 1: Write failing tests for dart_cache**

Append to `tests/test_dart_cache.py`:

```python
class TestDisclosures:
    def test_insert_dedup_by_rcept_no(self):
        rows = [
            {"rcept_no": "X1", "rcept_dt": "20260520", "raw_json": '{"a":1}'},
        ]
        dart_cache.insert_disclosures("005930", "00126380", "treasury_acquire", rows)
        # 동일 rcept_no 다시 → 1 row 유지 (INSERT OR IGNORE)
        dart_cache.insert_disclosures("005930", "00126380", "treasury_acquire", rows)
        count = dart_cache.count_disclosures("005930")
        assert count == 1

    def test_purge_old_disclosures(self):
        import time as _t
        now = int(_t.time())
        # 30일 전 row (old) + 1일 전 row (new)
        old_rows = [{"rcept_no": "OLD", "rcept_dt": "20240101", "raw_json": "{}"}]
        new_rows = [{"rcept_no": "NEW", "rcept_dt": "20260522", "raw_json": "{}"}]
        dart_cache.insert_disclosures("005930", "X", "treasury_acquire", old_rows,
                                       fetched_at=now - 30 * 86400)
        dart_cache.insert_disclosures("005930", "X", "treasury_acquire", new_rows,
                                       fetched_at=now - 86400)
        deleted = dart_cache.purge_old(days=14)
        assert deleted == 1
        assert dart_cache.count_disclosures("005930") == 1


class TestDartSummaries:
    def test_upsert_atomic(self):
        # INSERT
        dart_cache.upsert_summary(
            symbol="005930.KS", summary_json='{"summary":"x"}',
            sentiment="긍정", critical_count=1, model="rule_based", source="rule",
        )
        # 동일 symbol UPDATE
        dart_cache.upsert_summary(
            symbol="005930.KS", summary_json='{"summary":"y"}',
            sentiment="부정", critical_count=2, model="gemini-2.5-flash", source="llm",
        )
        result = dart_cache.get_summary("005930.KS")
        assert result["sentiment"] == "부정"
        assert result["critical_count"] == 2
        assert result["source"] == "llm"

    def test_list_summaries_returns_dict_keyed_by_symbol(self):
        dart_cache.upsert_summary(
            symbol="005930.KS", summary_json='{"a":1}',
            sentiment="긍정", critical_count=1, model="rule_based", source="rule",
        )
        dart_cache.upsert_summary(
            symbol="AAPL", summary_json='{"a":2}',
            sentiment="중립", critical_count=0, model=None, source="empty",
        )
        result = dart_cache.list_summaries()
        assert "005930.KS" in result
        assert "AAPL" in result
        assert result["005930.KS"]["sentiment"] == "긍정"
```

- [ ] **Step 2: Run tests to verify failure**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_dart_cache.py -v
```

Expected: 4 new tests FAIL — `AttributeError: module 'src.dart_cache' has no attribute 'insert_disclosures'`

- [ ] **Step 3: Extend dart_cache.py schema + functions**

In `src/dart_cache.py`, update `_SCHEMA` to include all 3 tables:

```python
_SCHEMA = """
CREATE TABLE IF NOT EXISTS corp_codes (
    corp_code   TEXT PRIMARY KEY,
    corp_name   TEXT NOT NULL,
    stock_code  TEXT,
    modify_date TEXT
);
CREATE INDEX IF NOT EXISTS idx_corp_codes_stock_code ON corp_codes(stock_code);

CREATE TABLE IF NOT EXISTS disclosures (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    corp_code       TEXT NOT NULL,
    stock_code      TEXT NOT NULL,
    disclosure_type TEXT NOT NULL,
    rcept_no        TEXT,
    rcept_dt        TEXT,
    raw_json        TEXT NOT NULL,
    fetched_at      INTEGER NOT NULL,
    UNIQUE(corp_code, disclosure_type, rcept_no)
);
CREATE INDEX IF NOT EXISTS idx_disclosures_stock ON disclosures(stock_code, rcept_dt DESC);

CREATE TABLE IF NOT EXISTS dart_summaries (
    symbol           TEXT PRIMARY KEY,
    summary_json     TEXT NOT NULL,
    sentiment        TEXT,
    critical_count   INTEGER NOT NULL,
    generated_at     INTEGER NOT NULL,
    model            TEXT,
    source           TEXT NOT NULL
);
"""
```

Append new functions:

```python
import time


def insert_disclosures(
    stock_code: str, corp_code: str, disclosure_type: str,
    rows: list[dict], fetched_at: int | None = None,
) -> int:
    """INSERT OR IGNORE — UNIQUE 위반 (동일 rcept_no) 은 silent skip."""
    if not rows:
        return 0
    ts = fetched_at if fetched_at is not None else int(time.time())
    import json as _json
    with _writer_lock:
        with closing(_connect()) as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO disclosures "
                "(corp_code, stock_code, disclosure_type, rcept_no, rcept_dt, raw_json, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(corp_code, stock_code, disclosure_type,
                  r.get("rcept_no") or "", r.get("rcept_dt") or "",
                  _json.dumps(r, ensure_ascii=False), ts) for r in rows],
            )
            conn.commit()
    return len(rows)


def count_disclosures(stock_code: str) -> int:
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM disclosures WHERE stock_code = ?",
            (stock_code,),
        ).fetchone()
    return row[0] if row else 0


def purge_old(days: int = 14) -> int:
    """fetched_at < now - days*86400 인 row 삭제. 반환: 삭제 row 수."""
    cutoff = int(time.time()) - days * 86400
    with _writer_lock:
        with closing(_connect()) as conn:
            cur = conn.execute(
                "DELETE FROM disclosures WHERE fetched_at < ?", (cutoff,),
            )
            conn.commit()
            return cur.rowcount


def upsert_summary(
    symbol: str, summary_json: str, sentiment: str | None,
    critical_count: int, model: str | None, source: str,
) -> None:
    """INSERT ... ON CONFLICT(symbol) DO UPDATE — atomic."""
    now = int(time.time())
    with _writer_lock:
        with closing(_connect()) as conn:
            conn.execute(
                "INSERT INTO dart_summaries "
                "(symbol, summary_json, sentiment, critical_count, generated_at, model, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(symbol) DO UPDATE SET "
                "  summary_json = excluded.summary_json, "
                "  sentiment = excluded.sentiment, "
                "  critical_count = excluded.critical_count, "
                "  generated_at = excluded.generated_at, "
                "  model = excluded.model, "
                "  source = excluded.source",
                (symbol, summary_json, sentiment, critical_count, now, model, source),
            )
            conn.commit()


def get_summary(symbol: str) -> dict | None:
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT symbol, summary_json, sentiment, critical_count, "
            "generated_at, model, source FROM dart_summaries WHERE symbol = ?",
            (symbol,),
        ).fetchone()
    if not row:
        return None
    return {
        "symbol": row[0], "summary_json": row[1], "sentiment": row[2],
        "critical_count": row[3], "generated_at": row[4],
        "model": row[5], "source": row[6],
    }


def list_summaries() -> dict[str, dict]:
    """{symbol: row_dict} — web/report 가 한 번에 fetch."""
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT symbol, summary_json, sentiment, critical_count, "
            "generated_at, model, source FROM dart_summaries"
        ).fetchall()
    return {
        r[0]: {
            "symbol": r[0], "summary_json": r[1], "sentiment": r[2],
            "critical_count": r[3], "generated_at": r[4],
            "model": r[5], "source": r[6],
        }
        for r in rows
    }
```

- [ ] **Step 4: Run dart_cache tests to verify pass**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_dart_cache.py -v
```

Expected: 5 PASS (1 from Task 2 + 4 new)

- [ ] **Step 5: Write failing tests for main.py dart-refresh**

Append to `tests/test_main.py`:

```python
class TestDartRefresh:
    def test_exits_when_api_key_missing(self, monkeypatch):
        """DART_API_KEY 미설정 시 sys.exit(1)."""
        monkeypatch.delenv("DART_API_KEY", raising=False)
        import main, sys
        # argparse 시뮬레이션
        old_argv = sys.argv
        sys.argv = ["main.py", "dart-refresh"]
        try:
            with pytest.raises(SystemExit) as exc_info:
                main.main()
            assert exc_info.value.code == 1
        finally:
            sys.argv = old_argv
```

- [ ] **Step 6: Add main.py dart-refresh subcommand**

In `main.py`, find the subparsers block and add:

```python
    subparsers.add_parser("dart-refresh", help="DART 공시 갱신 + 요약 (cron)")
```

Add handler before `if args.web:`:

```python
    if args.command == "dart-refresh":
        import json as _json
        if not os.environ.get("DART_API_KEY"):
            logger.error("DART_API_KEY 미설정 — cron 중단")
            sys.exit(1)
        from src.log_filter import install_secret_filter
        install_secret_filter([os.environ["DART_API_KEY"]])

        from src import dart_client, dart_cache, dart_rules, dart_llm
        dart_cache.init_db()

        n = dart_client.refresh_corp_codes_if_stale(days=7)
        logger.info("corp_codes 갱신: %d row", n)

        config = load_config()
        stocks = config.get("stocks", {}).get("korea", [])
        logger.info("dart-refresh 시작 — n=%d", len(stocks))

        # Phase 1: 모든 종목 fetch + classify (메모리 누적, DB write 0)
        pending: list[tuple] = []  # (stock, classified)
        for stock in stocks:
            try:
                corp_code = dart_client.get_corp_code(stock["symbol"])
                if not corp_code:
                    logger.warning("corp_code 없음 — skip: %s", stock["symbol"])
                    continue
                disclosures = dart_client.fetch_disclosures(corp_code, days=30)
                # raw 저장 (디버깅용)
                stock_code = stock["symbol"].split(".")[0]
                for dtype, rows in disclosures.items():
                    if rows:
                        dart_cache.insert_disclosures(stock_code, corp_code, dtype, rows)
                classified = dart_rules.classify_disclosures(disclosures)
                pending.append((stock, classified))
            except Exception as e:
                logger.exception("dart-refresh fetch 오류 — %s: %s", stock["symbol"], e)

        # Phase 2: summary 생성 + atomic batch UPSERT
        llm_count = rule_count = empty_count = 0
        for stock, classified in pending:
            try:
                count = classified["count"]
                if count == 0:
                    summary = {"empty": True, "generated_at": int(time.time())}
                    source = "empty"
                    sentiment = None
                    model = None
                    empty_count += 1
                elif count == 1:
                    summary = dart_rules.render_template(classified["critical_events"][0])
                    source = "rule"
                    sentiment = summary.get("sentiment")
                    model = summary.get("model")
                    rule_count += 1
                else:
                    summary = dart_llm.summarize_disclosures(
                        stock["symbol"], stock["name"], classified,
                    )
                    if summary is None:
                        continue  # LLM 실패 — 이전 값 유지
                    source = "llm"
                    sentiment = summary.get("sentiment")
                    model = summary.get("model")
                    llm_count += 1
                dart_cache.upsert_summary(
                    symbol=stock["symbol"],
                    summary_json=_json.dumps(summary, ensure_ascii=False),
                    sentiment=sentiment, critical_count=count,
                    model=model, source=source,
                )
            except Exception as e:
                logger.exception("dart-refresh summary 오류 — %s: %s", stock["symbol"], e)

        purged = dart_cache.purge_old(days=14)
        logger.info("dart-refresh 완료 — llm=%d rule=%d empty=%d purged=%d",
                    llm_count, rule_count, empty_count, purged)
        return
```

Add `import time` if not at top.

- [ ] **Step 7: Run all tests**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_dart_cache.py tests/test_main.py::TestDartRefresh -v
```

Expected: all PASS.

- [ ] **Step 8: Smoke test (local, no real API call — just argparse)**

```bash
cd /Users/sykim/Projects/stock-analyzer
unset DART_API_KEY
/Users/sykim/Projects/stock-analyzer/.venv/bin/python main.py dart-refresh
echo "exit=$?"
```

Expected: `DART_API_KEY 미설정 — cron 중단`, `exit=1`.

- [ ] **Step 9: Commit**

```bash
git add src/dart_cache.py main.py tests/test_dart_cache.py tests/test_main.py
git commit -m "feat(dart): dart-refresh subcommand — atomic batch + hybrid summary"
```

---

## Task 7: Report 카드 + web 배지 + CSS

**Files:**
- Modify: `src/report_generator.py` (_render_dart_section + _render_stock_card 호출)
- Modify: `src/web_app.py` (home/portfolio 카드 dart 배지)
- Modify: `src/templates/report.css` (.dart-section 스타일)
- Modify: `tests/test_report_generator.py` (4 tests 추가)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_report_generator.py`:

```python
class TestRenderDartSection:
    def test_with_summary_renders_html(self):
        from src.report_generator import _render_dart_section
        summary = {
            "summary": "자기주식 200억 취득",
            "sentiment": "긍정",
            "key_events": ["[20260520000001] 자기주식 200억"],
            "trading_view": "매수 — 자사주 매입은 EPS 상승",
            "model": "rule_based",
            "generated_at": 1779562800,
        }
        html = _render_dart_section(summary)
        assert "공시정보분석" in html
        assert "자기주식 200억 취득" in html
        assert "매수" in html
        assert "trading-view-positive" in html
        assert "출처: DART" in html

    def test_empty_marker_renders_no_news_text(self):
        from src.report_generator import _render_dart_section
        html = _render_dart_section({"empty": True, "generated_at": 1779562800})
        assert "최근 30일" in html
        assert "공시 없음" in html
        # 정상 dart-section CSS 가 적용되지만 sentiment 색상은 없음
        assert "trading-view-positive" not in html
        assert "trading-view-negative" not in html

    def test_none_returns_empty_string(self):
        from src.report_generator import _render_dart_section
        assert _render_dart_section(None) == ""

    def test_escapes_user_content(self):
        # LLM 출력에 <script> 들어와도 escape
        from src.report_generator import _render_dart_section
        summary = {
            "summary": "<script>alert('xss')</script>",
            "sentiment": "중립",
            "key_events": ["<img src=x>"],
            "trading_view": "관망 — <b>bold</b>",
            "model": "test", "generated_at": 1779562800,
        }
        html = _render_dart_section(summary)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html
        assert "<img" not in html
```

- [ ] **Step 2: Run tests to verify failure**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_report_generator.py::TestRenderDartSection -v
```

Expected: 4 FAIL — `ImportError: cannot import name '_render_dart_section'`

- [ ] **Step 3: Implement _render_dart_section**

In `src/report_generator.py`, find the section after `_render_rel_perf` and add:

```python
_DART_SENTIMENT_CLASS = {
    "긍정": "positive",
    "부정": "negative",
    "중립": "neutral",
}


def _render_dart_section(summary: dict | None) -> str:
    """DART 공시 분석 섹션 HTML. None/빈 dict → 빈 문자열.

    summary 형식:
    - {"empty": True, "generated_at": int} — 공시 없음 marker
    - {"summary", "sentiment", "key_events", "trading_view", "model", "generated_at"}
    """
    if not summary:
        return ""

    if summary.get("empty"):
        return (
            '<div class="dart-section">'
            '<h4 class="section-title">📋 공시정보분석</h4>'
            '<p class="dart-summary">최근 30일 critical 공시 없음.</p>'
            '<p class="dart-asof">출처: DART (금감원 전자공시)</p>'
            '</div>'
        )

    sentiment = summary.get("sentiment", "중립")
    sentiment_cls = _DART_SENTIMENT_CLASS.get(sentiment, "neutral")
    summary_text = html.escape(str(summary.get("summary", "")))
    trading_view = html.escape(str(summary.get("trading_view", "")))
    key_events = summary.get("key_events") or []
    model = html.escape(str(summary.get("model", "")))

    events_html = "".join(
        f'<li>{html.escape(str(e))}</li>' for e in key_events
    )

    return (
        f'<div class="dart-section">'
        f'<h4 class="section-title">📋 공시정보분석</h4>'
        f'<p class="dart-summary">{summary_text}</p>'
        f'<ul class="dart-events">{events_html}</ul>'
        f'<p class="dart-trading">'
        f'<strong class="trading-view-{sentiment_cls}">{trading_view}</strong>'
        f'</p>'
        f'<p class="dart-asof">model: {model} | 출처: DART (금감원 전자공시)</p>'
        f'</div>'
    )
```

- [ ] **Step 4: Wire into _render_stock_card**

In `src/report_generator.py`, find `_render_stock_card`. After `rel_perf_html = _render_rel_perf(item.get("rel_perf"))` line, add:

```python
    dart_summary_raw = item.get("dart_summary")
    # str (json) 이면 parse, dict 이면 그대로
    if isinstance(dart_summary_raw, str):
        try:
            import json as _json
            dart_summary = _json.loads(dart_summary_raw)
        except (ValueError, TypeError):
            dart_summary = None
    else:
        dart_summary = dart_summary_raw
    dart_html = _render_dart_section(dart_summary)
```

In the same function, in the f-string return, insert `{dart_html}` between `{rel_perf_html}` and `{sentiment_html}`.

- [ ] **Step 5: Add CSS**

In `src/templates/report.css`, append at end:

```css
/* DART 공시 분석 */
.dart-section {
  background: #f8f9fa;
  padding: 10px 12px;
  border-left: 3px solid #6c757d;
  margin: 10px 0;
  border-radius: 4px;
}

.dart-summary {
  font-size: 0.9em;
  color: #333;
  margin: 4px 0;
}

.dart-events {
  font-size: 0.85em;
  margin: 6px 0;
  padding-left: 20px;
}

.dart-trading {
  margin: 6px 0;
  font-size: 0.9em;
}

.trading-view-positive { color: #28a745; font-weight: 600; }
.trading-view-negative { color: #dc3545; font-weight: 600; }
.trading-view-neutral  { color: #6c757d; }

.dart-asof {
  color: #999;
  font-size: 0.75em;
  margin: 4px 0 0;
}
```

- [ ] **Step 6: Run tests to verify pass**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_report_generator.py -v
```

Expected: all PASS (기존 + 4 new).

- [ ] **Step 7: Wire web home/portfolio card badges (light touch)**

In `src/web_app.py`, find `home()` route. Before the card-render loop, add a 1-time fetch:

```python
    # DART 공시 요약 — 한번에 fetch
    try:
        from src import dart_cache as _dart_cache
        dart_summaries = _dart_cache.list_summaries()
    except Exception:
        dart_summaries = {}
```

In each card-render block (home + portfolio), inside the badges div near `signal_badge_html`, add:

```python
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
```

Insert `{dart_badge_html}` in the badges div near `{alpha_badge_html}` (before `{pattern_badge_html}`).

Repeat in `portfolio_view()` route's card-render block.

Also pass `dart_summary` into analyze result before generate_report (so /stock/{symbol} page shows it). Find `def stock_detail(symbol)` route, after retrieving cache_row, inject:

```python
    if cache_row:
        try:
            from src import dart_cache as _dart_cache
            ds = _dart_cache.get_summary(symbol)
            if ds:
                # report_generator 의 generate_report 가 직접 받지 않으니
                # cache_row.result_html 에 이미 들어가 있어야 함
                # → dart-refresh cron 다음 auto-analyze cron 에서 자동 합성
                pass
        except Exception:
            pass
```

(Note: 카드 페이지 dart 표시는 auto-analyze cron 이 다음 사이클에 generate_report 호출 시 dart_summaries 를 dict 에 inject 해야 함 — Task 8 또는 별도 fix.)

For now: home/portfolio 배지만 동작, /stock/{symbol} 상세 페이지의 dart-section 은 다음 auto-analyze cron 후 표시.

- [ ] **Step 8: Inject dart_summary into auto_analyze_market**

In `main.py`, in `auto_analyze_market()` function, after `result = analyze_stock(...)` and before `html = _rg.generate_report([result])`, add:

```python
            # DART 요약 inject (있으면)
            try:
                from src import dart_cache as _dc
                ds = _dc.get_summary(s["symbol"])
                if ds:
                    result["dart_summary"] = ds.get("summary_json")
            except Exception as e:
                logger.warning("dart_summary inject 실패: %s", e)
```

- [ ] **Step 9: Commit**

```bash
git add src/report_generator.py src/web_app.py src/templates/report.css main.py tests/test_report_generator.py
git commit -m "feat(dart): _render_dart_section + 카드 배지 + auto_analyze 통합"
```

---

## Task 8: macmini 배포 + 메모리 기록

**Files:** 운영 (코드 변경 없음 — push + plist)

- [ ] **Step 1: Merge to main + push**

```bash
cd /Users/sykim/Projects/stock-analyzer
git fetch origin --quiet
git merge worktree-feat-dart-phase-a --no-ff -m "Merge: DART 공시 통합 Phase A

DS005 + DS004 일괄 fetch + hybrid summary (단일=rule / 복수=Gemini LLM).
신규: dart_client/dart_rules/dart_cache/dart_llm/log_filter 5개 모듈.
별도 dart_summaries 테이블 (analysis_cache 미수정).
KST 19:30 cron + DART_API_KEY redaction + atomic batch.

총 32 신규 테스트.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
git push origin main 2>&1 | tail -3
```

- [ ] **Step 2: macmini pull**

```bash
ssh -i ~/.ssh/sykim-macmini -o IdentitiesOnly=yes sykim@100.87.151.104 "cd ~/Projects/stock-analyzer && git stash push -m s config/settings.yaml 2>/dev/null; git pull origin main 2>&1 | tail -5 && git stash pop 2>&1 | tail -2"
```

Expected: Fast-forward, 5 신규 파일 + 5 신규 test 파일.

- [ ] **Step 3: Register DART_API_KEY on macmini**

User 가 직접 환경 변수 등록 (보안). 다음 1줄 실행:

```bash
ssh -i ~/.ssh/sykim-macmini -o IdentitiesOnly=yes sykim@100.87.151.104
# (macmini 안에서)
echo "DART_API_KEY=<YOUR_KEY>" >> ~/Projects/stock-analyzer/.env
```

또는 launchd plist 의 EnvironmentVariables 에 직접 등록 (다음 step).

- [ ] **Step 4: Install launchd plist**

```bash
ssh -i ~/.ssh/sykim-macmini -o IdentitiesOnly=yes sykim@100.87.151.104 "cat > ~/Library/LaunchAgents/ai.stock-analyzer.dart-refresh.plist << 'PLIST_EOF'
<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\">
<dict>
    <key>Label</key>
    <string>ai.stock-analyzer.dart-refresh</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/sykim/Projects/stock-analyzer/.venv/bin/python</string>
        <string>main.py</string>
        <string>dart-refresh</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/sykim/Projects/stock-analyzer</string>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>19</integer>
        <key>Minute</key><integer>30</integer>
    </dict>
    <key>RunAtLoad</key><false/>
    <key>KeepAlive</key><false/>
    <key>StandardOutPath</key>
    <string>/Users/sykim/Projects/stock-analyzer/logs/dart-refresh.out.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/sykim/Projects/stock-analyzer/logs/dart-refresh.err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>DART_API_KEY</key><string>REPLACE_ME</string>
        <key>GEMINI_API_KEY</key><string>REPLACE_ME</string>
    </dict>
</dict>
</plist>
PLIST_EOF
echo OK"
```

User 가 macmini 에서 `REPLACE_ME` 부분을 실제 키로 교체.

- [ ] **Step 5: Bootstrap launchd**

```bash
ssh -i ~/.ssh/sykim-macmini -o IdentitiesOnly=yes sykim@100.87.151.104 "launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/ai.stock-analyzer.dart-refresh.plist 2>&1 && launchctl list | grep dart-refresh"
```

Expected: `ai.stock-analyzer.dart-refresh` 잡 등록.

- [ ] **Step 6: Manual smoke test on macmini**

```bash
ssh -i ~/.ssh/sykim-macmini -o IdentitiesOnly=yes sykim@100.87.151.104 "launchctl start ai.stock-analyzer.dart-refresh 2>&1 && sleep 5 && launchctl list | grep dart-refresh"
```

5분 후 결과 확인:

```bash
ssh -i ~/.ssh/sykim-macmini -o IdentitiesOnly=yes sykim@100.87.151.104 "tail -10 ~/Projects/stock-analyzer/logs/dart-refresh.err.log && echo '---' && sqlite3 ~/Projects/stock-analyzer/data/predictions.db 'SELECT COUNT(*) FROM dart_summaries; SELECT source, COUNT(*) FROM dart_summaries GROUP BY source;'"
```

Expected: 65종목 dart_summaries row, source 분포 (empty/rule/llm).

- [ ] **Step 7: Update memory**

In `/Users/sykim/.claude/projects/-Users-sykim/memory/reference_sykim_macmini.md`, append after the auto-cleanup section:

```
## dart-refresh cron (2026-05-23 추가)

**잡**: `ai.stock-analyzer.dart-refresh` — 매일 KST 19:30 (DS005 공시 18-19시 집중 접수 회피).

**역할**: 65 한국 종목 × DART API 9 endpoint (DS005 + DS004) 일괄 fetch +
critical event 분류 + hybrid summary (단일=rule / 복수=Gemini LLM) →
dart_summaries 테이블 atomic upsert.

**환경 변수**: DART_API_KEY, GEMINI_API_KEY (둘 다 plist EnvironmentVariables 에 등록).

**로그**: `logs/dart-refresh.{out,err}.log`. SecretFilter 로 DART_API_KEY 자동 마스킹.

**plist 재생성** (macmini 재설치 시):
- 위치: `~/Library/LaunchAgents/ai.stock-analyzer.dart-refresh.plist`
- ProgramArguments: `python main.py dart-refresh`
- StartCalendarInterval: Hour=19, Minute=30
- EnvironmentVariables: PATH, DART_API_KEY, GEMINI_API_KEY (ML 환경변수 불필요)
- 설치: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.stock-analyzer.dart-refresh.plist`
```

---

## 완료 체크

- [ ] `log_filter.SecretFilter` — DART_API_KEY 자동 마스킹
- [ ] `dart_client._parse_corp_code_xml` + `download_corp_codes` + `get_corp_code`
- [ ] `dart_client.fetch_disclosures` — 9 endpoint + rate limit + status 013
- [ ] `dart_rules.classify_disclosures` — tier1/tier2 + 임계치
- [ ] `dart_rules.render_template` — 단일 critical case
- [ ] `dart_llm.summarize_disclosures` — Gemini + enum 검증 + fallback
- [ ] `dart_cache.upsert_summary` — atomic
- [ ] `main.py dart-refresh` — atomic batch (fetch all → summary all → batch commit)
- [ ] `_render_dart_section` — HTML + XSS escape + 출처 표기
- [ ] web 카드 배지 (🟢/🔴/🟡 공시)
- [ ] macmini cron 등록 + 메모리 기록
- [ ] 모든 신규 테스트 PASS (~32개)
