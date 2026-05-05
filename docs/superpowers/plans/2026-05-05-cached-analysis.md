# 분석 결과 캐시 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 종목별 + 전체 분석 결과를 SQLite에 캐시하고, 시장별 자동분석(KST 16:00/06:00) cron을 추가하며, 결과 페이지에서 인라인 재분석을 가능하게 한다.

**Architecture:** 신규 `src/analysis_cache.py` 모듈이 `prediction_history` 와 동일한 SQLite 파일에 `analysis_cache` 테이블을 추가한다. `cache_key` PK + UPSERT 패턴으로 종목별 1 row 유지. Flask `/stock/<symbol>` 가 캐시 조회 진입점이 되고 `POST /analyze/<symbol>` 가 `return_to` 폼 필드로 인라인/카드 흐름을 분기한다. `main.py` 의 `extra_jobs` 에 자동분석 cron 2개를 추가하고 기존 `daily_job` 을 캐시-읽기 전용 `daily_email_job` 으로 교체한다.

**Tech Stack:** Python 3.10+, Flask, SQLite (stdlib `sqlite3`), APScheduler, pytest, Asia/Seoul · America/New_York timezone via `zoneinfo`

**Spec:** `docs/superpowers/specs/2026-05-05-cached-analysis-design.md`

---

## 파일 구조

| 파일 | 변경 |
|---|---|
| `src/analysis_cache.py` | **신규** — DB 액세스 (init_db, get, put, is_fresh, list_symbols, _next_market_open_kst) |
| `src/web_app.py` | 수정 — `/stock/*` 신규, `/analyze/<symbol>` POST 변경, 카드/메타바 렌더, 폴링 JS |
| `src/email_sender.py` | 수정 — `render_email_digest` 추가, `utc_to_kst` 유틸 |
| `main.py` | 수정 — `auto_analyze_market`, `daily_email_job` 추가, `extra_jobs` 에 cron 등록, `daily_job` 교체 |
| `tests/conftest.py` | 수정 — `analysis_cache._DB_PATH` 도 임시 경로로 redirect |
| `tests/test_analysis_cache.py` | **신규** |
| `tests/test_web_app.py` | 보강 — `/stock/*`, `/analyze` POST, 인라인 갱신 |
| `tests/test_email_sender.py` | 보강 — `render_email_digest` |
| `tests/test_main.py` | 보강 — `auto_analyze_market`, `daily_email_job` |

---

## Phase 1 — `analysis_cache` 모듈

### Task 1: 스키마 + `init_db`

**Files:**
- Create: `src/analysis_cache.py`
- Test: `tests/test_analysis_cache.py`

- [ ] **Step 1.1: Test fixture + init_db 테스트**

`tests/test_analysis_cache.py` 생성:

```python
"""src/analysis_cache.py 단위 테스트."""
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src import analysis_cache as ac


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """임시 DB 경로 사용. 모듈 전역 상태 격리."""
    db_path = tmp_path / "predictions.db"
    monkeypatch.setattr(ac, "_DB_PATH", db_path)
    yield db_path


class TestInitDb:
    def test_creates_db_file(self, tmp_db):
        assert not tmp_db.exists()
        ac.init_db()
        assert tmp_db.exists()

    def test_creates_parent_dir(self, tmp_path, monkeypatch):
        db = tmp_path / "deep" / "nested" / "predictions.db"
        monkeypatch.setattr(ac, "_DB_PATH", db)
        ac.init_db()
        assert db.exists()

    def test_creates_analysis_cache_table(self, tmp_db):
        ac.init_db()
        with sqlite3.connect(tmp_db) as conn:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='analysis_cache'"
            )
            assert cur.fetchone() is not None

    def test_creates_market_index(self, tmp_db):
        ac.init_db()
        with sqlite3.connect(tmp_db) as conn:
            names = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()]
        assert "idx_analysis_cache_market" in names

    def test_idempotent(self, tmp_db):
        ac.init_db()
        ac.init_db()
        assert tmp_db.exists()
```

- [ ] **Step 1.2: 테스트 실행 — FAIL 확인**

Run: `pytest tests/test_analysis_cache.py -v`
Expected: ImportError (모듈 없음) 또는 AttributeError

- [ ] **Step 1.3: 모듈 구현**

`src/analysis_cache.py`:

```python
"""분석 결과 캐시 — 종목별 + 전체 분석 SQLite 영속화."""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "predictions.db"
_writer_lock = threading.Lock()

_KST = ZoneInfo("Asia/Seoul")
_NY = ZoneInfo("America/New_York")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS analysis_cache (
    cache_key      TEXT PRIMARY KEY,
    market         TEXT NOT NULL,
    result_html    TEXT NOT NULL,
    generated_at   INTEGER NOT NULL,
    source         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_analysis_cache_market
    ON analysis_cache(market);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    """첫 호출 시 DB 파일과 부모 디렉토리 생성, 스키마 적용. 멱등."""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _writer_lock:
        with closing(_connect()) as conn:
            conn.executescript(_SCHEMA)
    logger.info("analysis_cache DB 초기화 완료: %s", _DB_PATH)
```

- [ ] **Step 1.4: 테스트 실행 — PASS 확인**

Run: `pytest tests/test_analysis_cache.py::TestInitDb -v`
Expected: 모든 테스트 PASS

- [ ] **Step 1.5: 커밋**

```bash
git add src/analysis_cache.py tests/test_analysis_cache.py
git commit -m "feat(analysis_cache): 스키마 + init_db (멱등)"
```

---

### Task 2: `put` + `get` (UPSERT)

**Files:**
- Modify: `src/analysis_cache.py`
- Modify: `tests/test_analysis_cache.py`

- [ ] **Step 2.1: 테스트 작성**

`tests/test_analysis_cache.py` 에 추가:

```python
class TestPutGet:
    def test_put_then_get_roundtrip(self, tmp_db):
        ac.init_db()
        ac.put(cache_key="AAPL", market="us",
               result_html="<p>hi</p>", source="manual")
        row = ac.get("AAPL")
        assert row is not None
        assert row["cache_key"] == "AAPL"
        assert row["market"] == "us"
        assert row["result_html"] == "<p>hi</p>"
        assert row["source"] == "manual"
        assert isinstance(row["generated_at"], int)

    def test_get_missing_returns_none(self, tmp_db):
        ac.init_db()
        assert ac.get("NOSUCH") is None

    def test_put_upsert_overwrites(self, tmp_db):
        ac.init_db()
        ac.put("AAPL", "us", "<p>v1</p>", "auto_cron")
        time.sleep(0.01)
        ac.put("AAPL", "us", "<p>v2</p>", "manual")
        row = ac.get("AAPL")
        assert row["result_html"] == "<p>v2</p>"
        assert row["source"] == "manual"
        # row 가 1개만 존재
        with sqlite3.connect(tmp_db) as conn:
            (n,) = conn.execute(
                "SELECT COUNT(*) FROM analysis_cache WHERE cache_key='AAPL'"
            ).fetchone()
            assert n == 1

    def test_put_all_key(self, tmp_db):
        ac.init_db()
        ac.put("ALL", "all", "<p>full</p>", "manual")
        row = ac.get("ALL")
        assert row["market"] == "all"
```

- [ ] **Step 2.2: 테스트 실행 — FAIL 확인**

Run: `pytest tests/test_analysis_cache.py::TestPutGet -v`
Expected: AttributeError (`put`/`get` 미정의)

- [ ] **Step 2.3: `put` + `get` 구현**

`src/analysis_cache.py` 끝에 추가:

```python
def put(cache_key: str, market: str, result_html: str, source: str) -> None:
    """analysis_cache UPSERT. 같은 cache_key 존재 시 덮어쓴다."""
    now_unix = int(time.time())
    with _writer_lock:
        with closing(_connect()) as conn:
            conn.execute("BEGIN")
            try:
                conn.execute(
                    """INSERT INTO analysis_cache
                       (cache_key, market, result_html, generated_at, source)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(cache_key) DO UPDATE SET
                         market       = excluded.market,
                         result_html  = excluded.result_html,
                         generated_at = excluded.generated_at,
                         source       = excluded.source""",
                    (cache_key, market, result_html, now_unix, source),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise


def get(cache_key: str) -> dict | None:
    """cache_key 의 row 를 dict 로 반환. 없으면 None."""
    with closing(_connect()) as conn:
        row = conn.execute(
            """SELECT cache_key, market, result_html, generated_at, source
               FROM analysis_cache WHERE cache_key = ?""",
            (cache_key,),
        ).fetchone()
    if row is None:
        return None
    return {
        "cache_key": row[0],
        "market": row[1],
        "result_html": row[2],
        "generated_at": row[3],
        "source": row[4],
    }
```

- [ ] **Step 2.4: 테스트 실행 — PASS**

Run: `pytest tests/test_analysis_cache.py::TestPutGet -v`
Expected: PASS

- [ ] **Step 2.5: 커밋**

```bash
git add src/analysis_cache.py tests/test_analysis_cache.py
git commit -m "feat(analysis_cache): put UPSERT + get"
```

---

### Task 3: `list_symbols`

**Files:**
- Modify: `src/analysis_cache.py`
- Modify: `tests/test_analysis_cache.py`

- [ ] **Step 3.1: 테스트 작성**

```python
class TestListSymbols:
    def test_returns_only_symbol_rows(self, tmp_db):
        ac.init_db()
        ac.put("AAPL", "us", "<p>a</p>", "auto_cron")
        ac.put("005930.KS", "korea", "<p>k</p>", "auto_cron")
        ac.put("ALL", "all", "<p>full</p>", "manual")
        rows = ac.list_symbols()
        keys = [r["cache_key"] for r in rows]
        assert "AAPL" in keys
        assert "005930.KS" in keys
        assert "ALL" not in keys

    def test_empty_db_returns_empty_list(self, tmp_db):
        ac.init_db()
        assert ac.list_symbols() == []

    def test_sorted_by_market_then_key(self, tmp_db):
        ac.init_db()
        ac.put("NVDA", "us", "<p>n</p>", "auto_cron")
        ac.put("AAPL", "us", "<p>a</p>", "auto_cron")
        ac.put("005930.KS", "korea", "<p>k</p>", "auto_cron")
        rows = ac.list_symbols()
        assert [(r["market"], r["cache_key"]) for r in rows] == [
            ("korea", "005930.KS"),
            ("us", "AAPL"),
            ("us", "NVDA"),
        ]
```

- [ ] **Step 3.2: 테스트 실행 — FAIL**

Run: `pytest tests/test_analysis_cache.py::TestListSymbols -v`

- [ ] **Step 3.3: 구현**

`src/analysis_cache.py` 에 추가:

```python
def list_symbols() -> list[dict]:
    """종목별 row 만 (market != 'all') market·cache_key 순으로 반환."""
    with closing(_connect()) as conn:
        rows = conn.execute(
            """SELECT cache_key, market, result_html, generated_at, source
               FROM analysis_cache
               WHERE market != 'all'
               ORDER BY market, cache_key"""
        ).fetchall()
    return [
        {
            "cache_key": r[0],
            "market": r[1],
            "result_html": r[2],
            "generated_at": r[3],
            "source": r[4],
        }
        for r in rows
    ]
```

- [ ] **Step 3.4: 테스트 실행 — PASS**

- [ ] **Step 3.5: 커밋**

```bash
git add src/analysis_cache.py tests/test_analysis_cache.py
git commit -m "feat(analysis_cache): list_symbols (ALL row 제외)"
```

---

### Task 4: `_next_market_open_kst` (한국)

**Files:**
- Modify: `src/analysis_cache.py`
- Modify: `tests/test_analysis_cache.py`

- [ ] **Step 4.1: 테스트 작성**

```python
def _kst_unix(year, month, day, hour, minute) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("Asia/Seoul")).timestamp())


class TestNextMarketOpenKoreaKst:
    def test_after_close_same_day(self, tmp_db):
        # 2026-05-05 16:00 KST 분석 → 다음 09:00 KST 만료
        gen = _kst_unix(2026, 5, 5, 16, 0)
        nxt = ac._next_market_open_kst("korea", gen)
        assert nxt == _kst_unix(2026, 5, 6, 9, 0)

    def test_before_open_same_day(self, tmp_db):
        # 2026-05-06 03:00 KST 분석 (가상) → 같은 날 09:00 만료
        gen = _kst_unix(2026, 5, 6, 3, 0)
        nxt = ac._next_market_open_kst("korea", gen)
        assert nxt == _kst_unix(2026, 5, 6, 9, 0)

    def test_at_open_exact(self, tmp_db):
        # 09:00 정각이면 그 다음 영업일 09:00 (이미 만료)
        gen = _kst_unix(2026, 5, 6, 9, 0)
        nxt = ac._next_market_open_kst("korea", gen)
        assert nxt == _kst_unix(2026, 5, 7, 9, 0)
```

- [ ] **Step 4.2: 테스트 실행 — FAIL**

- [ ] **Step 4.3: 한국 분기 구현**

`src/analysis_cache.py` 에 추가:

```python
def _next_market_open_kst(market: str, generated_at_unix: int) -> int:
    """generated_at 이후 다음 시장 시작 시각의 unix epoch (UTC) 를 반환.

    market='korea' → 한국시간 09:00 (KOSPI 정규장 시작)
    market='us'    → 미국 동부 09:30 → KST 환산 (서머타임 자동 처리)
    """
    gen_dt = datetime.fromtimestamp(generated_at_unix, tz=_KST)

    if market == "korea":
        candidate = gen_dt.replace(hour=9, minute=0, second=0, microsecond=0)
        if candidate <= gen_dt:
            candidate = candidate.replace(day=candidate.day) + _ONE_DAY
        return int(candidate.timestamp())

    raise ValueError(f"Unknown market: {market}")
```

`from datetime import timedelta` 추가 + 모듈 상단에:
```python
_ONE_DAY = timedelta(days=1)
```

- [ ] **Step 4.4: 테스트 실행 — PASS**

Run: `pytest tests/test_analysis_cache.py::TestNextMarketOpenKoreaKst -v`

- [ ] **Step 4.5: 커밋**

```bash
git add src/analysis_cache.py tests/test_analysis_cache.py
git commit -m "feat(analysis_cache): _next_market_open_kst — 한국 분기"
```

---

### Task 5: `_next_market_open_kst` (미국, 서머타임)

**Files:**
- Modify: `src/analysis_cache.py`
- Modify: `tests/test_analysis_cache.py`

- [ ] **Step 5.1: 테스트 작성**

```python
class TestNextMarketOpenUsKst:
    def test_us_standard_time_winter(self, tmp_db):
        # 2026-01-15 06:00 KST 분석 (겨울, 표준시 — UTC-5)
        # 미국 09:30 ET = KST 23:30 (당일)
        gen = _kst_unix(2026, 1, 15, 6, 0)
        nxt = ac._next_market_open_kst("us", gen)
        assert nxt == _kst_unix(2026, 1, 15, 23, 30)

    def test_us_daylight_time_summer(self, tmp_db):
        # 2026-07-15 06:00 KST 분석 (여름, 서머타임 — UTC-4)
        # 미국 09:30 ET = KST 22:30 (당일)
        gen = _kst_unix(2026, 7, 15, 6, 0)
        nxt = ac._next_market_open_kst("us", gen)
        assert nxt == _kst_unix(2026, 7, 15, 22, 30)

    def test_us_already_past_open_winter(self, tmp_db):
        # 2026-01-15 23:35 KST (겨울 시장 이미 시작) → 다음날 23:30
        gen = _kst_unix(2026, 1, 15, 23, 35)
        nxt = ac._next_market_open_kst("us", gen)
        assert nxt == _kst_unix(2026, 1, 16, 23, 30)

    def test_us_already_past_open_summer(self, tmp_db):
        # 2026-07-15 22:35 KST (여름 시장 이미 시작) → 다음날 22:30
        gen = _kst_unix(2026, 7, 15, 22, 35)
        nxt = ac._next_market_open_kst("us", gen)
        assert nxt == _kst_unix(2026, 7, 16, 22, 30)
```

- [ ] **Step 5.2: 테스트 실행 — FAIL** (`Unknown market: us`)

- [ ] **Step 5.3: 미국 분기 구현**

`src/analysis_cache.py` 의 `_next_market_open_kst` 에 추가:

```python
def _next_market_open_kst(market: str, generated_at_unix: int) -> int:
    gen_dt = datetime.fromtimestamp(generated_at_unix, tz=_KST)

    if market == "korea":
        candidate = gen_dt.replace(hour=9, minute=0, second=0, microsecond=0)
        if candidate <= gen_dt:
            candidate = candidate + _ONE_DAY
        return int(candidate.timestamp())

    if market == "us":
        # NY 09:30 을 두 번 후보로 검사 (gen_dt 와 같은 NY 날짜, 다음 NY 날짜)
        gen_ny = gen_dt.astimezone(_NY)
        candidate_ny = gen_ny.replace(hour=9, minute=30, second=0, microsecond=0)
        if candidate_ny <= gen_ny:
            candidate_ny = candidate_ny + _ONE_DAY
        return int(candidate_ny.astimezone(_KST).timestamp())

    raise ValueError(f"Unknown market: {market}")
```

- [ ] **Step 5.4: 테스트 실행 — PASS**

Run: `pytest tests/test_analysis_cache.py::TestNextMarketOpenUsKst -v`

- [ ] **Step 5.5: 커밋**

```bash
git add src/analysis_cache.py tests/test_analysis_cache.py
git commit -m "feat(analysis_cache): _next_market_open_kst — 미국 (서머타임)"
```

---

### Task 6: `is_fresh` (종목별 + ALL)

**Files:**
- Modify: `src/analysis_cache.py`
- Modify: `tests/test_analysis_cache.py`

- [ ] **Step 6.1: 테스트 작성**

```python
class TestIsFreshKorea:
    def test_fresh_before_next_open(self, tmp_db):
        gen = _kst_unix(2026, 5, 5, 16, 0)
        now = _kst_unix(2026, 5, 6, 8, 59)
        row = {"market": "korea", "generated_at": gen}
        assert ac.is_fresh(row, now) is True

    def test_stale_after_next_open(self, tmp_db):
        gen = _kst_unix(2026, 5, 5, 16, 0)
        now = _kst_unix(2026, 5, 6, 9, 1)
        row = {"market": "korea", "generated_at": gen}
        assert ac.is_fresh(row, now) is False


class TestIsFreshUs:
    def test_fresh_winter(self, tmp_db):
        # 2026-01-15 06:00 KST 분석 → 23:30 까지 fresh
        gen = _kst_unix(2026, 1, 15, 6, 0)
        now = _kst_unix(2026, 1, 15, 22, 0)
        assert ac.is_fresh({"market": "us", "generated_at": gen}, now) is True

    def test_stale_winter(self, tmp_db):
        gen = _kst_unix(2026, 1, 15, 6, 0)
        now = _kst_unix(2026, 1, 15, 23, 31)
        assert ac.is_fresh({"market": "us", "generated_at": gen}, now) is False

    def test_fresh_summer(self, tmp_db):
        # 2026-07-15 06:00 KST → 22:30 까지 fresh
        gen = _kst_unix(2026, 7, 15, 6, 0)
        now = _kst_unix(2026, 7, 15, 22, 0)
        assert ac.is_fresh({"market": "us", "generated_at": gen}, now) is True

    def test_stale_summer(self, tmp_db):
        gen = _kst_unix(2026, 7, 15, 6, 0)
        now = _kst_unix(2026, 7, 15, 22, 31)
        assert ac.is_fresh({"market": "us", "generated_at": gen}, now) is False


class TestIsFreshAll:
    def test_all_fresh_when_every_symbol_fresh(self, tmp_db):
        ac.init_db()
        gen = _kst_unix(2026, 5, 5, 16, 0)
        now = _kst_unix(2026, 5, 6, 8, 30)  # 한국 만료 전
        # 한국·미국 종목 모두 직전 자동분석 시점에 분석됐다고 가정
        ac.put("AAPL", "us", "<p/>", "auto_cron")
        ac.put("005930.KS", "korea", "<p/>", "auto_cron")
        # generated_at 을 명시 시각으로 덮어쓰기 위해 직접 UPDATE
        with sqlite3.connect(tmp_db) as conn:
            conn.execute("UPDATE analysis_cache SET generated_at = ?", (gen,))
        all_row = {"market": "all", "generated_at": gen}
        # ALL 의 신선도는 종목별 row 가 모두 fresh 인지로 판단
        assert ac.is_fresh(all_row, now) is True

    def test_all_stale_when_any_symbol_stale(self, tmp_db):
        ac.init_db()
        fresh_gen = _kst_unix(2026, 5, 5, 16, 0)
        stale_gen = _kst_unix(2026, 5, 4, 16, 0)
        ac.put("AAPL", "us", "<p/>", "auto_cron")
        ac.put("005930.KS", "korea", "<p/>", "auto_cron")
        with sqlite3.connect(tmp_db) as conn:
            conn.execute("UPDATE analysis_cache SET generated_at = ? WHERE cache_key = 'AAPL'", (fresh_gen,))
            conn.execute("UPDATE analysis_cache SET generated_at = ? WHERE cache_key = '005930.KS'", (stale_gen,))
        now = _kst_unix(2026, 5, 6, 8, 30)
        all_row = {"market": "all", "generated_at": fresh_gen}
        assert ac.is_fresh(all_row, now) is False

    def test_all_with_no_symbol_rows_is_stale(self, tmp_db):
        ac.init_db()
        gen = _kst_unix(2026, 5, 5, 16, 0)
        now = _kst_unix(2026, 5, 5, 17, 0)
        all_row = {"market": "all", "generated_at": gen}
        assert ac.is_fresh(all_row, now) is False
```

- [ ] **Step 6.2: 테스트 실행 — FAIL**

- [ ] **Step 6.3: `is_fresh` 구현**

`src/analysis_cache.py` 에 추가:

```python
def is_fresh(row: dict, now_unix: int) -> bool:
    """row 가 만료 전인지 판단.

    market='korea'/'us' → _next_market_open_kst 와 비교.
    market='all'        → 모든 종목 row 가 fresh 일 때만 True.
    """
    market = row["market"]
    if market in ("korea", "us"):
        return now_unix < _next_market_open_kst(market, row["generated_at"])

    if market == "all":
        symbol_rows = list_symbols()
        if not symbol_rows:
            return False
        return all(is_fresh(r, now_unix) for r in symbol_rows)

    raise ValueError(f"Unknown market: {market}")
```

- [ ] **Step 6.4: 테스트 실행 — PASS**

Run: `pytest tests/test_analysis_cache.py -v`
Expected: 모든 테스트 PASS

- [ ] **Step 6.5: 커밋**

```bash
git add src/analysis_cache.py tests/test_analysis_cache.py
git commit -m "feat(analysis_cache): is_fresh — 시장별 + ALL 집계"
```

---

### Task 7: conftest 격리 + 모듈 로드 시 `init_db`

**Files:**
- Modify: `tests/conftest.py`
- Modify: `main.py:60-61`

- [ ] **Step 7.1: conftest 갱신**

`tests/conftest.py` 를 갱신:

```python
"""세션 전체 fixture — 테스트 시 실제 predictions.db 오염 방지."""
import tempfile
from pathlib import Path


def pytest_configure(config):
    """pytest collection 단계 (`import main`)이 일어나기 전에
    prediction_history와 analysis_cache 의 _DB_PATH 를
    임시 경로로 redirect 한다.
    """
    from src import prediction_history as ph
    from src import analysis_cache as ac

    tmp_dir = Path(tempfile.mkdtemp(prefix="pytest_predictions_"))
    config._predictions_tmp_dir = tmp_dir
    db_path = tmp_dir / "predictions.db"
    ph._DB_PATH = db_path
    ac._DB_PATH = db_path  # 동일 파일 공유


def pytest_unconfigure(config):
    import shutil
    tmp_dir = getattr(config, "_predictions_tmp_dir", None)
    if tmp_dir and tmp_dir.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)
```

- [ ] **Step 7.2: `main.py` 모듈 로드 시 init_db 호출 추가**

`main.py` 의 60–61 라인 근처:

```python
# 변경 전
prediction_history.init_db()

# 변경 후
prediction_history.init_db()
from src import analysis_cache
analysis_cache.init_db()
```

상단 import 섹션에는 아직 추가하지 않음 — 모듈 로드 시점 1회 호출이라 함수 내부와 동일 효과.
대신 깔끔하게 상단에 `from src import analysis_cache` 를 추가하고 호출만 그 줄에:

```python
# 상단 import (29번째 줄 근처)
from src import prediction_history, analysis_cache

# 60번째 줄 근처
prediction_history.init_db()
analysis_cache.init_db()
```

- [ ] **Step 7.3: 전체 테스트 실행 — 회귀 없음 확인**

Run: `pytest tests/ -v --tb=short`
Expected: 기존 테스트 + 신규 analysis_cache 테스트 모두 PASS

- [ ] **Step 7.4: 커밋**

```bash
git add tests/conftest.py main.py
git commit -m "feat(main): analysis_cache.init_db 모듈 로드 시 호출 + conftest 격리"
```

---

## Phase 2 — Web 라우트 변경

### Task 8: `/analyze/<symbol>` GET → POST 변경 + `return_to`

**Files:**
- Modify: `src/web_app.py:831-859, 700-828`
- Modify: `tests/test_web_app.py`

- [ ] **Step 8.1: 테스트 작성**

`tests/test_web_app.py` 에 클래스 추가:

```python
class TestAnalyzePost:
    def test_post_with_return_to_jobs_redirects_to_jobs(self, client, monkeypatch):
        # _run_analysis_bg 를 즉시 종료 stub 으로 교체
        import src.web_app as wa
        monkeypatch.setattr(wa, "_run_analysis_bg", lambda *a, **k: None)
        resp = _post(client, "/analyze/AAPL", {"return_to": "jobs"})
        assert resp.status_code == 303
        assert resp.headers["Location"].startswith("/jobs/")

    def test_post_with_return_to_stock_redirects_to_stock(self, client, monkeypatch):
        import src.web_app as wa
        monkeypatch.setattr(wa, "_run_analysis_bg", lambda *a, **k: None)
        resp = _post(client, "/analyze/AAPL", {"return_to": "stock"})
        assert resp.status_code == 303
        loc = resp.headers["Location"]
        assert loc.startswith("/stock/AAPL?job=")

    def test_post_default_return_to_is_jobs(self, client, monkeypatch):
        import src.web_app as wa
        monkeypatch.setattr(wa, "_run_analysis_bg", lambda *a, **k: None)
        resp = _post(client, "/analyze/AAPL", {})
        assert resp.status_code == 303
        assert resp.headers["Location"].startswith("/jobs/")

    def test_post_without_csrf_returns_403(self, client):
        resp = client.post("/analyze/AAPL", data={"return_to": "jobs"})
        assert resp.status_code == 403

    def test_get_method_not_allowed(self, client):
        resp = client.get("/analyze/AAPL")
        assert resp.status_code == 405
```

- [ ] **Step 8.2: 테스트 실행 — FAIL** (현재 GET 라우트라 POST가 405)

Run: `pytest tests/test_web_app.py::TestAnalyzePost -v`

- [ ] **Step 8.3: 라우트 변경**

`src/web_app.py:831-859` 의 `analyze` 함수 교체:

```python
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
    if return_to == "stock":
        return redirect(f"/stock/{symbol}?job={job_id}", code=303)
    return redirect(f"/jobs/{job_id}", code=303)
```

`src/web_app.py:735-737` 의 카드 분석 버튼을 폼으로 교체:

```python
# 변경 전
analyze_btn = f'<a class="btn btn-primary btn-sm" href="/analyze/{s["symbol"]}">{_ICON_PLAY} 분석</a>'

# 변경 후 — POST 폼
analyze_btn = f'''
<form method="post" action="/analyze/{s["symbol"]}" style="display:inline; margin:0;">
  {_csrf_input()}
  <input type="hidden" name="return_to" value="jobs">
  <button type="submit" class="btn btn-primary btn-sm">{_ICON_PLAY} 분석</button>
</form>'''
```

- [ ] **Step 8.4: 테스트 실행 — PASS**

Run: `pytest tests/test_web_app.py::TestAnalyzePost -v`
Expected: 모든 테스트 PASS

- [ ] **Step 8.5: 커밋**

```bash
git add src/web_app.py tests/test_web_app.py
git commit -m "feat(web): /analyze/<symbol> GET→POST + return_to 분기"
```

---

### Task 9: `_run_analysis_bg` 종료 시 `analysis_cache.put`

**Files:**
- Modify: `src/web_app.py:127-146`
- Modify: `tests/test_web_app.py`

- [ ] **Step 9.1: 테스트 작성**

`tests/test_web_app.py` 에 추가 (TestAnalyzeBgCachePut 클래스):

```python
class TestAnalyzeBgCachePut:
    def test_successful_analysis_puts_cache(self, client, monkeypatch, tmp_path):
        """_run_analysis_bg 성공 → analysis_cache.put 호출 확인."""
        import src.web_app as wa
        from src import analysis_cache as ac

        # analysis_cache._DB_PATH 임시 경로 (conftest 가 이미 처리하지만 명시)
        captured = {}

        def fake_analyze_stock(symbol, name):
            return {"name": name, "symbol": symbol, "df": None,
                    "signal": None, "prediction": None, "news": [], "sentiment": None}

        def fake_generate_report(analyses):
            return "<p>fake report</p>"

        def fake_put(cache_key, market, result_html, source):
            captured["cache_key"] = cache_key
            captured["market"] = market
            captured["result_html"] = result_html
            captured["source"] = source

        monkeypatch.setattr("main.analyze_stock", fake_analyze_stock)
        monkeypatch.setattr("src.report_generator.generate_report", fake_generate_report)
        monkeypatch.setattr(ac, "put", fake_put)

        # config 의 AAPL → market="us" 로 결정
        wa._jobs.clear()
        job_id = "testjob1"
        wa._jobs[job_id] = {
            "status": "running", "symbol": "AAPL", "name": "Apple",
            "result_html": None, "error": None,
            "started_at": "00:00:00",
        }
        wa._run_analysis_bg(job_id, "AAPL", "Apple")

        assert captured["cache_key"] == "AAPL"
        assert captured["market"] == "us"
        assert captured["result_html"] == "<p>fake report</p>"
        assert captured["source"] == "manual"

    def test_failed_analysis_does_not_put_cache(self, client, monkeypatch):
        import src.web_app as wa
        from src import analysis_cache as ac

        called = {"put": False}
        monkeypatch.setattr(ac, "put", lambda *a, **k: called.__setitem__("put", True))
        monkeypatch.setattr("main.analyze_stock", lambda s, n: None)

        wa._jobs.clear()
        wa._jobs["job2"] = {
            "status": "running", "symbol": "AAPL", "name": "Apple",
            "result_html": None, "error": None, "started_at": "00:00:00",
        }
        wa._run_analysis_bg("job2", "AAPL", "Apple")
        assert called["put"] is False
```

- [ ] **Step 9.2: 테스트 실행 — FAIL**

- [ ] **Step 9.3: `_run_analysis_bg` 갱신**

`src/web_app.py` 상단 import 에 추가:
```python
from src import analysis_cache
```

`src/web_app.py:127-146` 의 `_run_analysis_bg` 교체:

```python
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
                analysis_cache.put(symbol, market, html, source="manual")
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
```

- [ ] **Step 9.4: 테스트 실행 — PASS**

Run: `pytest tests/test_web_app.py::TestAnalyzeBgCachePut -v`

- [ ] **Step 9.5: 커밋**

```bash
git add src/web_app.py tests/test_web_app.py
git commit -m "feat(web): _run_analysis_bg 종료 시 analysis_cache.put"
```

---

### Task 10: `GET /stock/<symbol>` — 캐시 hit/miss

**Files:**
- Modify: `src/web_app.py` (라우트 추가, 헬퍼 추가)
- Modify: `tests/test_web_app.py`

- [ ] **Step 10.1: 테스트 작성**

```python
class TestStockGet:
    def test_cache_hit_shows_result_html(self, client):
        from src import analysis_cache as ac
        ac.init_db()
        ac.put("AAPL", "us", "<p>cached body</p>", "auto_cron")
        resp = client.get("/stock/AAPL")
        assert resp.status_code == 200
        assert b"cached body" in resp.data

    def test_cache_hit_shows_meta_bar(self, client):
        from src import analysis_cache as ac
        ac.init_db()
        ac.put("AAPL", "us", "<p>cached</p>", "auto_cron")
        resp = client.get("/stock/AAPL")
        # 메타바: "분석 시각" 텍스트 포함
        assert "분석 시각".encode() in resp.data
        # 재분석 폼: return_to=stock
        assert b'name="return_to" value="stock"' in resp.data

    def test_cache_miss_shows_start_button(self, client):
        from src import analysis_cache as ac
        ac.init_db()
        # 캐시 비어있음
        resp = client.get("/stock/AAPL")
        assert resp.status_code == 200
        assert "분석 이력".encode() in resp.data or "분석 시작".encode() in resp.data
        assert b'name="return_to"' in resp.data  # 폼 존재

    def test_invalid_symbol_returns_400(self, client):
        resp = client.get("/stock/<script>")
        assert resp.status_code in (400, 404)
```

- [ ] **Step 10.2: 테스트 실행 — FAIL**

- [ ] **Step 10.3: 라우트 + 렌더 헬퍼 구현**

`src/web_app.py` 에 추가 (라우트 섹션):

```python
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
    <form method="post" action="/analyze/{row["cache_key"]}" style="margin:8px 0 16px 0;">
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
    <form method="post" action="/analyze/{symbol}" style="margin:16px 0;">
      {_csrf_input()}
      <input type="hidden" name="return_to" value="stock">
      <button type="submit" class="btn btn-primary">▶ 분석 시작</button>
    </form>'''


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

    row = analysis_cache.get(symbol)
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
    body = _render_meta_bar(row, fresh, name) + f'<div class="card result-frame">{row["result_html"]}</div>'
    return _page(f"{name} 분석 결과", body)
```

`src/web_app.py` 상단:
```python
import time
```

`_render_stock_with_overlay` 는 Task 11 에서 구현 — 일단 stub 추가:

```python
def _render_stock_with_overlay(symbol: str, name: str, row: dict | None, job_id: str) -> str:
    """진행 중 오버레이 + 폴링 — Task 11 에서 구현."""
    raise NotImplementedError("Task 11 에서 구현")
```

- [ ] **Step 10.4: 테스트 실행 — PASS**

Run: `pytest tests/test_web_app.py::TestStockGet -v`

- [ ] **Step 10.5: 커밋**

```bash
git add src/web_app.py tests/test_web_app.py
git commit -m "feat(web): /stock/<symbol> GET — 캐시 hit/miss + 메타바"
```

---

### Task 11: `/stock/<symbol>?job=<id>` 인라인 갱신

**Files:**
- Modify: `src/web_app.py` (`_render_stock_with_overlay` 구현)
- Modify: `tests/test_web_app.py`

- [ ] **Step 11.1: 테스트 작성**

```python
class TestStockInlinePolling:
    def test_running_job_renders_overlay_and_polling_script(self, client):
        import src.web_app as wa
        from src import analysis_cache as ac
        ac.init_db()
        ac.put("AAPL", "us", "<p>old</p>", "auto_cron")

        wa._jobs.clear()
        wa._jobs["abc12345"] = {
            "status": "running", "symbol": "AAPL", "name": "Apple",
            "result_html": None, "error": None, "started_at": "16:00:00",
        }

        resp = client.get("/stock/AAPL?job=abc12345")
        assert resp.status_code == 200
        # 오버레이
        assert "재분석 중".encode() in resp.data or "분석 진행 중".encode() in resp.data
        # 폴링 JS — jobId 가 const 로 삽입되고 fetch 가 /api/jobs/ 호출
        assert b'const jobId = "abc12345"' in resp.data
        assert b"/api/jobs/" in resp.data
        # 기존 캐시는 흐리게 보여줌
        assert b"old" in resp.data

    def test_completed_job_redirects_to_clean_url(self, client):
        import src.web_app as wa
        wa._jobs.clear()
        wa._jobs["done1234"] = {
            "status": "done", "symbol": "AAPL", "name": "Apple",
            "result_html": "<p>x</p>", "error": None, "started_at": "16:00:00",
        }
        resp = client.get("/stock/AAPL?job=done1234")
        assert resp.status_code == 303
        assert resp.headers["Location"] == "/stock/AAPL"

    def test_unknown_job_id_redirects(self, client):
        resp = client.get("/stock/AAPL?job=unknown1")
        assert resp.status_code == 303
        assert resp.headers["Location"] == "/stock/AAPL"
```

- [ ] **Step 11.2: 테스트 실행 — FAIL**

- [ ] **Step 11.3: `_render_stock_with_overlay` 구현**

`src/web_app.py` 의 stub 교체:

```python
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
```

- [ ] **Step 11.4: 테스트 실행 — PASS**

Run: `pytest tests/test_web_app.py::TestStockInlinePolling -v`

- [ ] **Step 11.5: 커밋**

```bash
git add src/web_app.py tests/test_web_app.py
git commit -m "feat(web): /stock/<symbol>?job=<id> 인라인 오버레이 + 폴링"
```

---

### Task 12: `GET /stock/all` + `/analyze-all` 캐시 UPSERT

**Files:**
- Modify: `src/web_app.py` (`/stock/all` 추가, `_run_full_analysis_bg` UPSERT)
- Modify: `tests/test_web_app.py`

- [ ] **Step 12.1: 테스트 작성**

```python
class TestStockAll:
    def test_cache_hit(self, client):
        from src import analysis_cache as ac
        ac.init_db()
        ac.put("ALL", "all", "<p>full digest</p>", "manual")
        resp = client.get("/stock/all")
        assert resp.status_code == 200
        assert b"full digest" in resp.data

    def test_cache_miss_shows_start_button(self, client):
        from src import analysis_cache as ac
        ac.init_db()
        resp = client.get("/stock/all")
        assert resp.status_code == 200
        assert "전체 분석".encode() in resp.data


class TestAnalyzeAllUpsert:
    def test_full_analysis_puts_all_cache(self, client, monkeypatch):
        import src.web_app as wa
        from src import analysis_cache as ac

        captured = {}
        monkeypatch.setattr("main.run_full_analysis", lambda cfg: "<p>digest</p>")
        monkeypatch.setattr(ac, "put", lambda *a, **k: captured.update({"args": a, "kwargs": k}))

        wa._jobs.clear()
        wa._jobs["full1"] = {
            "status": "running", "symbol": "ALL", "name": "전체 종목",
            "result_html": None, "error": None, "started_at": "16:00:00",
        }
        wa._run_full_analysis_bg("full1")
        # put("ALL", "all", "<p>digest</p>", source="manual")
        args = captured["args"]
        assert args[0] == "ALL"
        assert args[1] == "all"
        assert args[2] == "<p>digest</p>"
        assert captured["kwargs"].get("source") == "manual"
```

- [ ] **Step 12.2: 테스트 실행 — FAIL**

- [ ] **Step 12.3: `/stock/all` 라우트 + `_run_full_analysis_bg` UPSERT**

`src/web_app.py` 의 `_run_full_analysis_bg` (149–168 라인 근처) 교체:

```python
def _run_full_analysis_bg(job_id: str) -> None:
    """백그라운드 스레드에서 전체 분석 실행. 성공 시 analysis_cache.put('ALL')."""
    logger.info("전체 분석 시작: job_id=%s", job_id)
    try:
        from main import run_full_analysis, load_config

        config = load_config()
        html = run_full_analysis(config)
        if html is None:
            logger.warning("전체 분석 결과 없음: job_id=%s", job_id)
            _jobs_set(job_id, status="error", error="분석 결과 없음")
        else:
            _jobs_set(job_id, status="done", result_html=html)
            try:
                analysis_cache.put("ALL", "all", html, source="manual")
            except Exception as e:
                logger.warning("analysis_cache.put('ALL') 실패: %s", e)
            logger.info("전체 분석 완료: job_id=%s", job_id)
    except Exception as e:
        logger.exception("전체 분석 오류: job_id=%s error=%s", job_id, e)
        _jobs_set(job_id, status="error", error=str(e))
    finally:
        _trim_jobs()
```

`/stock/all` 라우트 추가 (`stock_view` 아래):

```python
@app.route("/stock/all")
def stock_all_view():
    row = analysis_cache.get("ALL")
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
```

- [ ] **Step 12.4: 테스트 실행 — PASS**

Run: `pytest tests/test_web_app.py::TestStockAll tests/test_web_app.py::TestAnalyzeAllUpsert -v`

- [ ] **Step 12.5: 커밋**

```bash
git add src/web_app.py tests/test_web_app.py
git commit -m "feat(web): /stock/all 라우트 + /analyze-all 캐시 UPSERT"
```

---

### Task 13: 대시보드 카드 신선도 표시 + 링크 변경

**Files:**
- Modify: `src/web_app.py:700-828` (`index` 함수 — 카드 렌더 부분)
- Modify: `tests/test_web_app.py`

- [ ] **Step 13.1: 테스트 작성**

```python
class TestIndexFreshness:
    def test_card_shows_no_history_when_no_cache(self, client):
        from src import analysis_cache as ac
        ac.init_db()
        resp = client.get("/")
        assert b"AAPL" in resp.data
        # 분석 이력 없음 안내가 카드에 표시
        assert "분석 이력".encode() in resp.data

    def test_card_shows_fresh_badge(self, client):
        from src import analysis_cache as ac
        ac.init_db()
        ac.put("AAPL", "us", "<p>x</p>", "auto_cron")
        resp = client.get("/")
        # fresh 마크 (🟢)
        assert b"\xf0\x9f\x9f\xa2" in resp.data  # 🟢 utf-8

    def test_card_links_to_stock_view(self, client):
        from src import analysis_cache as ac
        ac.init_db()
        ac.put("AAPL", "us", "<p>x</p>", "auto_cron")
        resp = client.get("/")
        assert b'href="/stock/AAPL"' in resp.data

    def test_card_has_reanalyze_form(self, client):
        from src import analysis_cache as ac
        ac.init_db()
        ac.put("AAPL", "us", "<p>x</p>", "auto_cron")
        resp = client.get("/")
        # 재분석 폼 (return_to=jobs) 존재
        assert b'name="return_to"' in resp.data
```

- [ ] **Step 13.2: 테스트 실행 — FAIL**

- [ ] **Step 13.3: 카드 렌더 변경**

`src/web_app.py:725-757` 의 카드 생성 루프 교체:

```python
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
        cache_row = analysis_cache.get(s["symbol"])
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

        cards.append(f"""
        <div class="stock-card">
          <div class="stock-card-header">
            <div class="stock-card-info">
              <h3>{escape(s['name'])}</h3>
              <div class="symbol">{escape(s['symbol'])}</div>
            </div>
            <span class="badge {badge_cls}">{market_label}</span>
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
```

- [ ] **Step 13.4: 테스트 실행 — PASS**

Run: `pytest tests/test_web_app.py::TestIndexFreshness -v`

- [ ] **Step 13.5: 커밋**

```bash
git add src/web_app.py tests/test_web_app.py
git commit -m "feat(web): 대시보드 카드 신선도 뱃지 + /stock 링크 + 재분석 버튼"
```

---

## Phase 3 — 이메일 + 스케줄러

### Task 14: `render_email_digest`

**Files:**
- Modify: `src/email_sender.py`
- Modify: `tests/test_email_sender.py`

- [ ] **Step 14.1: 테스트 작성**

`tests/test_email_sender.py` 에 추가 (기존 파일 끝):

```python
class TestRenderEmailDigest:
    def test_empty_rows_returns_header_only(self):
        from src.email_sender import render_email_digest
        html = render_email_digest([])
        assert "<h1>" in html
        assert "다이제스트" in html

    def test_single_symbol_row(self, monkeypatch):
        from src.email_sender import render_email_digest
        from src import analysis_cache as ac
        # is_fresh 결정성을 위해 stub
        monkeypatch.setattr(ac, "is_fresh", lambda row, now: True)
        rows = [{
            "cache_key": "AAPL",
            "market": "us",
            "result_html": "<p>aapl body</p>",
            "generated_at": 1715000000,
            "source": "auto_cron",
        }]
        html = render_email_digest(rows)
        assert "AAPL" in html
        assert "aapl body" in html
        assert "🟢" in html

    def test_stale_row_shows_yellow_mark(self, monkeypatch):
        from src.email_sender import render_email_digest
        from src import analysis_cache as ac
        monkeypatch.setattr(ac, "is_fresh", lambda row, now: False)
        rows = [{
            "cache_key": "AAPL", "market": "us", "result_html": "<p/>",
            "generated_at": 1, "source": "auto_cron",
        }]
        html = render_email_digest(rows)
        assert "🟡" in html
```

- [ ] **Step 14.2: 테스트 실행 — FAIL**

- [ ] **Step 14.3: 구현**

`src/email_sender.py` 끝에 추가:

```python
def render_email_digest(rows: list[dict]) -> str:
    """analysis_cache row 리스트를 받아 이메일용 HTML 합성한다.

    Args:
        rows: list_symbols() 가 반환하는 딕셔너리 리스트.
              (cache_key, market, result_html, generated_at, source)

    Returns:
        완성된 HTML 문서 문자열 (`<html><body>...</body></html>`).
    """
    import time as _time
    from datetime import datetime
    from html import escape
    from zoneinfo import ZoneInfo

    from src import analysis_cache

    now_ts = int(_time.time())
    parts = ["<h1>일일 시장 분석 다이제스트</h1>"]
    for row in rows:
        gen_kst = datetime.fromtimestamp(
            row["generated_at"], tz=ZoneInfo("Asia/Seoul")
        ).strftime("%Y-%m-%d %H:%M")
        fresh = "🟢 최근" if analysis_cache.is_fresh(row, now_ts) else "🟡 오래됨"
        parts.append(
            f'<section><h2>{escape(row["cache_key"])} '
            f'<small>{fresh} · 분석 {gen_kst} KST</small></h2>'
            f'{row["result_html"]}</section>'
        )
    return "<html><body>" + "".join(parts) + "</body></html>"
```

- [ ] **Step 14.4: 테스트 실행 — PASS**

Run: `pytest tests/test_email_sender.py::TestRenderEmailDigest -v`

- [ ] **Step 14.5: 커밋**

```bash
git add src/email_sender.py tests/test_email_sender.py
git commit -m "feat(email): render_email_digest — analysis_cache row → HTML"
```

---

### Task 15: `main.auto_analyze_market`

**Files:**
- Modify: `main.py`
- Modify: `tests/test_main.py`

- [ ] **Step 15.1: 테스트 작성**

`tests/test_main.py` 에 추가 (없으면 클래스부터 생성):

```python
class TestAutoAnalyzeMarket:
    def test_processes_only_target_market(self, monkeypatch, tmp_path):
        import main
        from src import analysis_cache as ac

        # config 를 한국 1개 + 미국 1개로 stub
        fake_config = {
            "stocks": {
                "korea": [{"symbol": "005930.KS", "name": "삼성전자"}],
                "us":    [{"symbol": "AAPL", "name": "Apple"}],
            },
            "schedule": {"hour": 8, "minute": 30, "timezone": "Asia/Seoul"},
            "email": {},
        }
        monkeypatch.setattr(main, "load_config", lambda: fake_config)

        analyzed = []
        def fake_analyze(symbol, name):
            analyzed.append(symbol)
            return {"name": name, "symbol": symbol, "df": None,
                    "signal": None, "prediction": None, "news": [], "sentiment": None}
        monkeypatch.setattr(main, "analyze_stock", fake_analyze)
        monkeypatch.setattr("src.report_generator.generate_report", lambda a: "<p/>")

        puts = []
        monkeypatch.setattr(ac, "put", lambda **kw: puts.append(kw))

        main.auto_analyze_market("korea")
        assert analyzed == ["005930.KS"]
        assert len(puts) == 1
        assert puts[0]["cache_key"] == "005930.KS"
        assert puts[0]["market"] == "korea"
        assert puts[0]["source"] == "auto_cron"

    def test_skips_failed_symbols(self, monkeypatch):
        import main
        from src import analysis_cache as ac

        fake_config = {"stocks": {"us": [
            {"symbol": "BAD", "name": "Bad"},
            {"symbol": "GOOD", "name": "Good"},
        ]}, "schedule": {}, "email": {}}
        monkeypatch.setattr(main, "load_config", lambda: fake_config)

        def fake_analyze(symbol, name):
            if symbol == "BAD":
                return None  # fetch 실패 시뮬레이션
            return {"name": name, "symbol": symbol, "df": None,
                    "signal": None, "prediction": None, "news": [], "sentiment": None}
        monkeypatch.setattr(main, "analyze_stock", fake_analyze)
        monkeypatch.setattr("src.report_generator.generate_report", lambda a: "<p/>")
        puts = []
        monkeypatch.setattr(ac, "put", lambda **kw: puts.append(kw))

        main.auto_analyze_market("us")
        # GOOD 만 캐시
        assert [p["cache_key"] for p in puts] == ["GOOD"]
```

- [ ] **Step 15.2: 테스트 실행 — FAIL**

- [ ] **Step 15.3: `auto_analyze_market` 구현**

`main.py` 에 추가 (run_full_analysis 다음 위치):

```python
def auto_analyze_market(market: str) -> None:
    """시장의 모든 종목을 차례로 분석하고 analysis_cache 에 UPSERT.

    cron (KST 16:00 한국 / KST 06:00 미국) 에서 호출된다.
    """
    config = load_config()
    stocks = config.get("stocks", {}).get(market, [])
    logger.info("자동분석 시작 — market=%s n=%d", market, len(stocks))
    success = 0
    for s in stocks:
        try:
            result = analyze_stock(s["symbol"], s["name"])
            if result is None:
                logger.warning("자동분석 실패(결과 없음): %s", s["symbol"])
                continue
            html = generate_report([result])
            analysis_cache.put(
                cache_key=s["symbol"],
                market=market,
                result_html=html,
                source="auto_cron",
            )
            success += 1
        except Exception as e:
            logger.exception("자동분석 오류 — %s: %s", s["symbol"], e)
    logger.info("자동분석 완료 — market=%s ok=%d/%d", market, success, len(stocks))
```

(`from src.report_generator import generate_report` 는 main.py 에 이미 import 됨)
(`analysis_cache` 는 Task 7 에서 import 추가됨)

- [ ] **Step 15.4: 테스트 실행 — PASS**

Run: `pytest tests/test_main.py::TestAutoAnalyzeMarket -v`

- [ ] **Step 15.5: 커밋**

```bash
git add main.py tests/test_main.py
git commit -m "feat(main): auto_analyze_market — 시장별 일괄 분석 + 캐시 UPSERT"
```

---

### Task 16: `daily_email_job` (캐시 재사용)

**Files:**
- Modify: `main.py:160-165` (`daily_job` 교체)
- Modify: `tests/test_main.py`

- [ ] **Step 16.1: 테스트 작성**

```python
class TestDailyEmailJob:
    def test_skips_email_when_cache_empty(self, monkeypatch):
        import main
        from src import analysis_cache as ac

        monkeypatch.setattr(main, "load_config", lambda: {"email": {}})
        monkeypatch.setattr(ac, "list_symbols", lambda: [])

        sent = []
        monkeypatch.setattr(main, "send_report", lambda html, cfg: sent.append(html))
        main.daily_email_job()
        assert sent == []

    def test_sends_email_when_cache_has_rows(self, monkeypatch):
        import main
        from src import analysis_cache as ac

        monkeypatch.setattr(main, "load_config", lambda: {"email": {"recipients": ["a@b"]}})
        monkeypatch.setattr(ac, "list_symbols", lambda: [
            {"cache_key": "AAPL", "market": "us", "result_html": "<p/>",
             "generated_at": 1, "source": "auto_cron"}
        ])
        monkeypatch.setattr(ac, "is_fresh", lambda row, now: True)

        sent = []
        monkeypatch.setattr(main, "send_report", lambda html, cfg: sent.append(html))
        main.daily_email_job()
        assert len(sent) == 1
        assert "AAPL" in sent[0]
```

- [ ] **Step 16.2: 테스트 실행 — FAIL**

- [ ] **Step 16.3: `daily_email_job` + `daily_job` 교체**

`main.py:160-165` 의 `daily_job` 교체:

```python
def daily_email_job() -> None:
    """캐시에서 종목별 결과를 모아 이메일 발송. 분석 재실행하지 않는다."""
    from src.email_sender import render_email_digest

    config = load_config()
    rows = analysis_cache.list_symbols()
    if not rows:
        logger.warning("이메일 발송 스킵 — analysis_cache 가 비어있음")
        return
    html = render_email_digest(rows)
    send_report(html, config["email"])


# 호환을 위해 기존 이름도 유지 (cron 등록부에서 새 이름으로 교체됨)
daily_job = daily_email_job
```

- [ ] **Step 16.4: 테스트 실행 — PASS**

Run: `pytest tests/test_main.py::TestDailyEmailJob -v`

- [ ] **Step 16.5: 커밋**

```bash
git add main.py tests/test_main.py
git commit -m "feat(main): daily_email_job — 캐시 재사용, 분석 재실행 안 함"
```

---

### Task 17: 자동분석 cron 등록 + `--start-scheduler` 갱신

**Files:**
- Modify: `main.py:241-253` (`extra_jobs` 정의)

- [ ] **Step 17.1: extra_jobs 갱신**

`main.py:241-253` 의 `--start-scheduler` 분기 교체:

```python
    if args.start_scheduler:
        from apscheduler.triggers.cron import CronTrigger
        extra_jobs = {
            "auto_analyze_korea": {
                "func": lambda: auto_analyze_market("korea"),
                "trigger": CronTrigger(
                    hour=16, minute=0, timezone="Asia/Seoul"
                ),
                "name": "Korea Auto Analysis",
            },
            "auto_analyze_us": {
                "func": lambda: auto_analyze_market("us"),
                "trigger": CronTrigger(
                    hour=6, minute=0, timezone="Asia/Seoul"
                ),
                "name": "US Auto Analysis (post-close)",
            },
            "backfill_daily": {
                "func": lambda: prediction_history.backfill_all(fetch_fn=fetch_stock_data),
                "trigger": CronTrigger(
                    hour=18, minute=0, timezone="Asia/Seoul"
                ),
                "name": "Daily Prediction Backfill",
            },
        }
        start_scheduler(daily_email_job, config["schedule"], extra_jobs=extra_jobs)
        return
```

- [ ] **Step 17.2: 회귀 테스트**

Run: `pytest tests/ -v --tb=short`
Expected: 모든 테스트 PASS (회귀 없음)

- [ ] **Step 17.3: 수동 검증 (로컬)**

```bash
# 모듈 import 검증 (스케줄러는 blocking 이라 즉시 종료)
python -c "import main; print('imports ok')"
```
Expected: `imports ok` 출력, 예외 없음

- [ ] **Step 17.4: 커밋**

```bash
git add main.py
git commit -m "feat(main): 자동분석 cron 등록 (KST 16:00 한국 / 06:00 미국)"
```

---

## Phase 4 — 통합 검증 + 문서

### Task 18: README/CHANGELOG 업데이트 (선택)

**Files:**
- Modify: `README.md` (운영 노트 섹션이 있다면)

- [ ] **Step 18.1: README 의 스케줄러/Cron 설명 갱신**

`README.md` 에서 cron 설명 섹션을 찾아 다음을 반영:

```markdown
## 스케줄러 작업

| 시각 (KST) | 작업 | 설명 |
|---|---|---|
| 06:00 | `auto_analyze_us` | 미국 종목 자동분석 → analysis_cache UPSERT |
| 08:30 | `daily_email_job` | analysis_cache 에서 다이제스트 이메일 발송 (분석 재실행 X) |
| 16:00 | `auto_analyze_korea` | 한국 종목 자동분석 → analysis_cache UPSERT |
| 18:00 | `backfill_daily` | 예측 이력 actual_close 백필 |

분석 결과는 SQLite `analysis_cache` 테이블에 저장되어 재시작에도 유지됩니다.
캐시 만료 시각: 한국 종목 KST 09:00, 미국 종목 NYSE 09:30 ET (서머타임 자동 처리).
```

(README 에 cron 표가 없으면 운영 노트로 적절히 추가하거나 본 task 스킵)

- [ ] **Step 18.2: 커밋**

```bash
git add README.md
git commit -m "docs(readme): 자동분석 cron + analysis_cache 운영 설명 갱신"
```

---

### Task 19: 통합 회귀 테스트

- [ ] **Step 19.1: 전체 테스트 실행**

Run: `pytest tests/ -v`
Expected: 모든 테스트 PASS (기존 + 신규)

- [ ] **Step 19.2: 로컬 웹 서버 수동 검증**

```bash
python main.py --web --port 8080
```

브라우저에서 `http://localhost:8080` 열고 다음 시나리오 확인:
1. 카드의 "분석 시작" 클릭 → /jobs/<id> 진행 페이지 → 완료
2. /stock/<symbol> 직접 접속 → 캐시 결과 표시 + 메타바
3. /stock/<symbol> 의 "재분석" 클릭 → 같은 페이지 머무름 + spinner → 자동 reload
4. 만료 시각 시뮬레이션은 sqlite 직접 수정으로 generated_at 을 옛날로 → 노란 뱃지 확인

```bash
sqlite3 data/predictions.db "UPDATE analysis_cache SET generated_at = generated_at - 86400 WHERE cache_key='AAPL'"
```

- [ ] **Step 19.3: 검증 결과를 사용자에게 보고**

각 시나리오 OK/FAIL 결과 정리.

---

## Self-Review 체크리스트 (구현자용)

- [ ] 모든 새 함수는 단일 책임. 한 파일이 200~300 라인 넘으면 분할 고려
- [ ] `_run_analysis_bg`/`_run_full_analysis_bg` 의 `analysis_cache.put` 실패가 job 결과를 깨뜨리지 않음 (try/except + warning)
- [ ] CSRF: 모든 POST 라우트 `_csrf_validate()` 또는 `_csrf_input()` 으로 토큰 처리
- [ ] timezone: KST/NY 변환은 `zoneinfo` 만 사용, naive datetime 사용 금지
- [ ] sqlite: `closing(_connect())` 패턴 + `BEGIN/COMMIT/ROLLBACK` (write 작업)
- [ ] 테스트: `tmp_db` fixture 또는 `conftest._DB_PATH` 격리만 사용, 실제 `data/predictions.db` 미오염
- [ ] 모든 task 사이 커밋 — 단일 task 안 다중 변경 금지
