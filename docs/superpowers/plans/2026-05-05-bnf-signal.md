# BNF 스타일 시그널 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 기존 `generate_signal` 옆에 `generate_bnf_signal` (mean reversion + 시장 패닉 매수) 별도 함수 추가, 시장 인덱스 (KOSPI/S&P500) TTL 캐시, `analysis_cache` 컬럼 확장 + 자동 마이그레이션, 카드에 Tech + BNF 두 시그널 뱃지를 동시 표시한다.

**Architecture:** `technical_analysis.py` 에 `_MARKET_INDEX` 상수 + `_market_cache` 모듈 변수 + `fetch_market_df` (TTL 15분 메모리 캐시) + `generate_bnf_signal(df, market_df=None)` 추가. `analyze_stock(symbol, name, market=None)` 시그니처 확장 + `result["bnf_signal"]` 추가. `analysis_cache` 의 `_migrate` 가 `bnf_signal_value` / `bnf_signal_score` 컬럼 자동 추가, `put`/`get`/`list_symbols` 시그니처/SELECT 확장. 3 worker 가 BNF signal 도 cache.put 에 전달. `_render_signal_badge` 가 `prefix=` 매개변수 추가 → 카드 header `stock-card-badges` 컨테이너에 Tech + BNF + 시장 3 뱃지 세로 정렬.

**Tech Stack:** Python 3.10+, pandas, yfinance (`fetch_stock_data`), SQLite (stdlib), Flask, pytest

**Spec:** `docs/superpowers/specs/2026-05-05-bnf-signal-design.md`

---

## 파일 구조

| 파일 | 변경 |
|---|---|
| `src/technical_analysis.py` | 추가 — `_MARKET_INDEX` 상수, `_market_cache` 모듈 변수, `fetch_market_df()`, `generate_bnf_signal()`, `import time/logging` |
| `src/analysis_cache.py` | 수정 — `_SCHEMA` 갱신, `_migrate` 두 컬럼 추가, `put` keyword-only 매개변수 + INSERT/UPDATE 확장, `get`/`list_symbols` SELECT 확장 |
| `main.py` | 수정 — `analyze_stock(symbol, name, market=None)` 시그니처, `result["bnf_signal"]` 추가, `collect_analyses` 가 market 전달, `auto_analyze_market` 가 market 전달 + bnf_signal cache.put |
| `src/web_app.py` | 수정 — `_render_signal_badge` `prefix` 매개변수, `index` 카드 두 번째 뱃지, `_run_analysis_bg` / `_run_full_analysis_bg` 가 bnf_signal cache.put + market 전달 |
| `tests/test_technical_analysis.py` | 신규/보강 — `TestGenerateBnfSignal` (8), `TestFetchMarketDf` (4) |
| `tests/test_analysis_cache.py` | 보강 — `TestMigrateAddsBnfColumns` (2), `TestPutGetBnfSignal` (3) |
| `tests/test_main.py` | 보강 — `TestAnalyzeStockBnfSignal` (2) |
| `tests/test_web_app.py` | 보강 — `TestRenderSignalBadgeBnfPrefix` (2), `TestIndexCardBnfBadge` (3), `TestWorkerBnfSignal` (1) |

총 신규 테스트 25건.

---

## Phase 1 — `analysis_cache` 스키마 확장

### Task 1: `_SCHEMA` + `_migrate` BNF 컬럼 추가

**Files:**
- Modify: `src/analysis_cache.py`
- Modify: `tests/test_analysis_cache.py`

- [ ] **Step 1.1: 테스트 작성**

`tests/test_analysis_cache.py` 끝에 추가:

```python
class TestMigrateAddsBnfColumns:
    def test_new_db_has_bnf_columns(self, tmp_db):
        ac.init_db()
        import sqlite3
        with sqlite3.connect(tmp_db) as conn:
            cur = conn.execute("PRAGMA table_info(analysis_cache)")
            cols = {row[1] for row in cur.fetchall()}
        assert "bnf_signal_value" in cols
        assert "bnf_signal_score" in cols

    def test_migrate_adds_bnf_columns_to_post_signal_legacy_db(self, tmp_db):
        """기존 (signal_value/score 만 있고 bnf_* 없는) DB → _migrate 후 bnf 컬럼 추가."""
        import sqlite3
        legacy_schema = """
        CREATE TABLE IF NOT EXISTS analysis_cache (
            cache_key      TEXT PRIMARY KEY,
            market         TEXT NOT NULL,
            result_html    TEXT NOT NULL,
            generated_at   INTEGER NOT NULL,
            source         TEXT NOT NULL,
            signal_value   TEXT,
            signal_score   INTEGER
        );
        """
        tmp_db.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(tmp_db) as conn:
            conn.executescript(legacy_schema)
            conn.execute(
                """INSERT INTO analysis_cache
                   (cache_key, market, result_html, generated_at, source,
                    signal_value, signal_score)
                   VALUES ('AAPL', 'us', '<p/>', 1700000000, 'manual', '매수', 3)"""
            )
        ac.init_db()
        with sqlite3.connect(tmp_db) as conn:
            cur = conn.execute("PRAGMA table_info(analysis_cache)")
            cols = {row[1] for row in cur.fetchall()}
            row = conn.execute(
                "SELECT signal_value, signal_score, bnf_signal_value, bnf_signal_score "
                "FROM analysis_cache WHERE cache_key='AAPL'"
            ).fetchone()
        assert "bnf_signal_value" in cols
        assert "bnf_signal_score" in cols
        # 기존 row 의 signal_* 보존 + bnf_* 는 NULL
        assert row == ("매수", 3, None, None)
```

- [ ] **Step 1.2: 테스트 실행 — FAIL**

```bash
cd /Users/sykim/Projects/stock-analyzer/.worktrees/bnf-signal
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_analysis_cache.py::TestMigrateAddsBnfColumns -v
```
Expected: 2 fail (bnf_signal_value 컬럼 없음)

- [ ] **Step 1.3: `_SCHEMA` 갱신**

`src/analysis_cache.py` 의 `_SCHEMA` 변경 — bnf 두 컬럼 추가:

```python
_SCHEMA = """
CREATE TABLE IF NOT EXISTS analysis_cache (
    cache_key         TEXT PRIMARY KEY,
    market            TEXT NOT NULL,
    result_html       TEXT NOT NULL,
    generated_at      INTEGER NOT NULL,
    source            TEXT NOT NULL,
    signal_value      TEXT,
    signal_score      INTEGER,
    bnf_signal_value  TEXT,
    bnf_signal_score  INTEGER
);

CREATE INDEX IF NOT EXISTS idx_analysis_cache_market
    ON analysis_cache(market);
"""
```

- [ ] **Step 1.4: `_migrate` 확장**

`_migrate(conn)` 함수에 bnf 컬럼 체크 추가:

```python
def _migrate(conn: sqlite3.Connection) -> None:
    """기존 DB 에 누락된 컬럼을 추가하는 멱등 마이그레이션."""
    cur = conn.execute("PRAGMA table_info(analysis_cache)")
    cols = {row[1] for row in cur.fetchall()}
    if "signal_value" not in cols:
        conn.execute("ALTER TABLE analysis_cache ADD COLUMN signal_value TEXT")
    if "signal_score" not in cols:
        conn.execute("ALTER TABLE analysis_cache ADD COLUMN signal_score INTEGER")
    if "bnf_signal_value" not in cols:
        conn.execute("ALTER TABLE analysis_cache ADD COLUMN bnf_signal_value TEXT")
    if "bnf_signal_score" not in cols:
        conn.execute("ALTER TABLE analysis_cache ADD COLUMN bnf_signal_score INTEGER")
```

- [ ] **Step 1.5: 테스트 실행 — PASS**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_analysis_cache.py::TestMigrateAddsBnfColumns -v
```

- [ ] **Step 1.6: 회귀**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_analysis_cache.py -v
```

- [ ] **Step 1.7: 커밋**

```bash
git add src/analysis_cache.py tests/test_analysis_cache.py
git commit -m "feat(analysis_cache): bnf_signal_value/bnf_signal_score 컬럼 + 마이그레이션"
```

---

### Task 2: `put`/`get`/`list_symbols` BNF 매개변수

**Files:**
- Modify: `src/analysis_cache.py`
- Modify: `tests/test_analysis_cache.py`

- [ ] **Step 2.1: 테스트 작성**

```python
class TestPutGetBnfSignal:
    def test_put_with_bnf_then_get(self, tmp_db):
        ac.init_db()
        ac.put("AAPL", "us", "<p/>", "manual",
               signal_value="매수", signal_score=3,
               bnf_signal_value="매도", bnf_signal_score=-2)
        row = ac.get("AAPL")
        assert row["signal_value"] == "매수"
        assert row["signal_score"] == 3
        assert row["bnf_signal_value"] == "매도"
        assert row["bnf_signal_score"] == -2

    def test_put_default_bnf_is_none(self, tmp_db):
        ac.init_db()
        ac.put("AAPL", "us", "<p/>", "manual")
        row = ac.get("AAPL")
        assert row["bnf_signal_value"] is None
        assert row["bnf_signal_score"] is None

    def test_upsert_overwrites_bnf_with_none(self, tmp_db):
        ac.init_db()
        ac.put("AAPL", "us", "<p/>", "manual",
               bnf_signal_value="매수", bnf_signal_score=3)
        ac.put("AAPL", "us", "<p>v2</p>", "manual")  # bnf 없이
        row = ac.get("AAPL")
        assert row["bnf_signal_value"] is None
        assert row["bnf_signal_score"] is None
```

- [ ] **Step 2.2: 테스트 실행 — FAIL**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_analysis_cache.py::TestPutGetBnfSignal -v
```

- [ ] **Step 2.3: `put` 시그니처 + UPSERT 확장**

`src/analysis_cache.py` 의 `put` 교체:

```python
def put(
    cache_key: str,
    market: str,
    result_html: str,
    source: str,
    *,
    signal_value: str | None = None,
    signal_score: int | None = None,
    bnf_signal_value: str | None = None,
    bnf_signal_score: int | None = None,
) -> None:
    """analysis_cache UPSERT. 같은 cache_key 존재 시 덮어쓴다.

    signal/bnf_signal 매개변수 None 이면 NULL 저장 (UPSERT 시 기존 값을 NULL 로 덮어쓰는 효과).
    """
    now_unix = int(time.time())
    with _writer_lock:
        with closing(_connect()) as conn:
            conn.execute("BEGIN")
            try:
                conn.execute(
                    """INSERT INTO analysis_cache
                       (cache_key, market, result_html, generated_at, source,
                        signal_value, signal_score,
                        bnf_signal_value, bnf_signal_score)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(cache_key) DO UPDATE SET
                         market           = excluded.market,
                         result_html      = excluded.result_html,
                         generated_at     = excluded.generated_at,
                         source           = excluded.source,
                         signal_value     = excluded.signal_value,
                         signal_score     = excluded.signal_score,
                         bnf_signal_value = excluded.bnf_signal_value,
                         bnf_signal_score = excluded.bnf_signal_score""",
                    (cache_key, market, result_html, now_unix, source,
                     signal_value, signal_score,
                     bnf_signal_value, bnf_signal_score),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
```

- [ ] **Step 2.4: `get` 갱신**

```python
def get(cache_key: str) -> dict | None:
    with closing(_connect()) as conn:
        row = conn.execute(
            """SELECT cache_key, market, result_html, generated_at, source,
                      signal_value, signal_score,
                      bnf_signal_value, bnf_signal_score
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
        "signal_value": row[5],
        "signal_score": row[6],
        "bnf_signal_value": row[7],
        "bnf_signal_score": row[8],
    }
```

- [ ] **Step 2.5: `list_symbols` 갱신**

```python
def list_symbols() -> list[dict]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            """SELECT cache_key, market, result_html, generated_at, source,
                      signal_value, signal_score,
                      bnf_signal_value, bnf_signal_score
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
            "signal_value": r[5],
            "signal_score": r[6],
            "bnf_signal_value": r[7],
            "bnf_signal_score": r[8],
        }
        for r in rows
    ]
```

- [ ] **Step 2.6: 테스트 PASS + 회귀**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_analysis_cache.py -v
```

- [ ] **Step 2.7: 커밋**

```bash
git add src/analysis_cache.py tests/test_analysis_cache.py
git commit -m "feat(analysis_cache): put/get/list_symbols 가 bnf_signal_value/score 처리"
```

---

## Phase 2 — `technical_analysis` BNF 로직

### Task 3: `fetch_market_df` + TTL 캐시

**Files:**
- Modify: `src/technical_analysis.py`
- Create: `tests/test_technical_analysis.py` (없으면 신규)

- [ ] **Step 3.1: 테스트 작성**

`tests/test_technical_analysis.py` 에 추가 (없으면 신규 파일):

```python
"""src/technical_analysis.py 단위 테스트."""
import time
import pandas as pd
import pytest

from src import technical_analysis as ta_mod


@pytest.fixture(autouse=True)
def _clear_market_cache():
    """각 테스트 시작 시 _market_cache 비움 (모듈 변수 격리)."""
    ta_mod._market_cache.clear()
    yield
    ta_mod._market_cache.clear()


def _fake_df():
    """간단한 OHLCV df — compute_indicators 가 작동할 정도의 길이."""
    import numpy as np
    n = 100
    idx = pd.date_range("2026-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "Open":  np.linspace(100, 110, n),
        "High":  np.linspace(102, 112, n),
        "Low":   np.linspace(98, 108, n),
        "Close": np.linspace(100, 110, n),
        "Volume": [1_000_000] * n,
    }, index=idx)


class TestFetchMarketDf:
    def test_korea_fetches_kospi_index(self, monkeypatch):
        captured = []
        def fake_fetch(symbol):
            captured.append(symbol)
            return _fake_df()
        monkeypatch.setattr("src.data_fetcher.fetch_stock_data", fake_fetch)
        result = ta_mod.fetch_market_df("korea")
        assert result is not None
        assert captured == ["^KS11"]

    def test_us_fetches_sp500_index(self, monkeypatch):
        captured = []
        def fake_fetch(symbol):
            captured.append(symbol)
            return _fake_df()
        monkeypatch.setattr("src.data_fetcher.fetch_stock_data", fake_fetch)
        result = ta_mod.fetch_market_df("us")
        assert result is not None
        assert captured == ["^GSPC"]

    def test_fetch_failure_returns_none(self, monkeypatch, caplog):
        def fake_fetch(symbol):
            raise RuntimeError("network down")
        monkeypatch.setattr("src.data_fetcher.fetch_stock_data", fake_fetch)
        result = ta_mod.fetch_market_df("us")
        assert result is None

    def test_ttl_cache_hits_only_one_fetch(self, monkeypatch):
        captured = []
        def fake_fetch(symbol):
            captured.append(symbol)
            return _fake_df()
        monkeypatch.setattr("src.data_fetcher.fetch_stock_data", fake_fetch)
        # 첫 호출 — fetch
        r1 = ta_mod.fetch_market_df("us")
        # 두 번째 호출 — 캐시 hit
        r2 = ta_mod.fetch_market_df("us")
        assert len(captured) == 1
        assert r1 is r2  # 같은 객체 (캐시)
```

- [ ] **Step 3.2: 테스트 실행 — FAIL**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_technical_analysis.py::TestFetchMarketDf -v
```

- [ ] **Step 3.3: `fetch_market_df` 구현**

`src/technical_analysis.py` 상단에 import 추가:

```python
import logging
import time
from src.data_fetcher import fetch_stock_data
```

(`fetch_stock_data` import 가 순환 의존 일으키면 함수 안으로 이동)

모듈 상단 (compute_indicators 위) 에 상수 + 캐시 변수 + 함수 추가:

```python
logger = logging.getLogger(__name__)


_MARKET_INDEX = {
    "korea": "^KS11",   # KOSPI
    "us":    "^GSPC",   # S&P 500
}

_market_cache: dict = {}  # {index: (df, cached_at_unix)}
_MARKET_CACHE_TTL = 15 * 60  # 15분


def fetch_market_df(market: str) -> "pd.DataFrame | None":
    """시장 인덱스 데이터 fetch + 15분 TTL 메모리 캐시.

    market: "korea" 또는 "us". 그 외/None/fetch 실패 시 None.
    """
    index = _MARKET_INDEX.get(market)
    if not index:
        return None
    cached = _market_cache.get(index)
    if cached and (time.time() - cached[1] < _MARKET_CACHE_TTL):
        return cached[0]
    try:
        # 함수 안 import 로 순환 의존 회피
        from src.data_fetcher import fetch_stock_data as _fetch
        df = _fetch(index)
        df = compute_indicators(df)
        _market_cache[index] = (df, time.time())
        return df
    except Exception as e:
        logger.warning("시장 데이터 fetch 실패 (%s): %s", index, e)
        # stale 데이터 정리 (이전 성공 후 만료된 entry)
        _market_cache.pop(index, None)
        return None
```

- [ ] **Step 3.4: 테스트 실행 — PASS**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_technical_analysis.py::TestFetchMarketDf -v
```

- [ ] **Step 3.5: 커밋**

```bash
git add src/technical_analysis.py tests/test_technical_analysis.py
git commit -m "feat(technical_analysis): fetch_market_df + 15분 TTL 메모리 캐시"
```

---

### Task 4: `generate_bnf_signal` — 점수 로직

**Files:**
- Modify: `src/technical_analysis.py`
- Modify: `tests/test_technical_analysis.py`

- [ ] **Step 4.1: 테스트 작성**

`tests/test_technical_analysis.py` 끝에 추가:

```python
def _build_df_for_disparity(disparity_pct: float, rsi: float = 50.0,
                             volume_ratio: float = 1.0, green: bool = True):
    """MA20 이격율 + RSI + 거래량 비율 + 양/음봉을 의도된 값으로 가지는 df 생성."""
    import numpy as np
    n = 100
    base = 100.0
    # MA20 이 base 와 같도록 동일 close 100, 마지막만 base*(1+disparity_pct/100) 로 설정
    closes = [base] * (n - 1) + [base * (1 + disparity_pct / 100)]
    # 양/음봉 — 마지막 행의 open vs close
    last_open = closes[-1] - 1 if green else closes[-1] + 1
    opens = closes.copy()
    opens[-1] = last_open
    # 거래량
    volumes = [1_000_000] * (n - 1) + [int(1_000_000 * volume_ratio)]
    idx = pd.date_range("2026-01-01", periods=n, freq="D")
    df = pd.DataFrame({
        "Open": opens, "High": [c + 2 for c in closes],
        "Low": [c - 2 for c in closes], "Close": closes, "Volume": volumes,
    }, index=idx)
    df = ta_mod.compute_indicators(df)
    # RSI 강제 (덮어쓰기 — compute_indicators 결과 위에)
    df.loc[df.index[-1], "RSI"] = rsi
    return df


class TestGenerateBnfSignal:
    def test_strong_oversold_buy(self):
        df = _build_df_for_disparity(disparity_pct=-12, rsi=25)
        result = ta_mod.generate_bnf_signal(df)
        assert result["signal"] == "매수"
        assert result["score"] >= 2

    def test_strong_overbought_sell(self):
        df = _build_df_for_disparity(disparity_pct=+12, rsi=75)
        result = ta_mod.generate_bnf_signal(df)
        assert result["signal"] == "매도"
        assert result["score"] <= -2

    def test_neutral_hold(self):
        df = _build_df_for_disparity(disparity_pct=0, rsi=50)
        result = ta_mod.generate_bnf_signal(df)
        assert result["signal"] == "관망"
        assert -2 < result["score"] < 2

    def test_panic_volume_with_red_candle_adds_buy_point(self):
        df = _build_df_for_disparity(disparity_pct=-8, rsi=50,
                                      volume_ratio=2.5, green=False)
        result = ta_mod.generate_bnf_signal(df)
        assert "거래량" in " ".join(result["reasons"])
        # 종합 점수에 거래량+음봉 +1 가산
        assert result["score"] >= 1

    def test_volume_surge_no_buy_on_green(self):
        # 양봉 + 거래량 급증 + 이격율 0 → 거래량 점수 0
        df = _build_df_for_disparity(disparity_pct=0, rsi=50,
                                      volume_ratio=2.5, green=True)
        result = ta_mod.generate_bnf_signal(df)
        # 거래량 reason 없어야 함 (양봉 추격 매수 안 함)
        assert all("거래량" not in r for r in result["reasons"])

    def test_market_panic_amplifies_buy(self):
        stock_df = _build_df_for_disparity(disparity_pct=-11, rsi=50)
        market_df = _build_df_for_disparity(disparity_pct=-4, rsi=50)
        result = ta_mod.generate_bnf_signal(stock_df, market_df=market_df)
        # market_disparity 필드 존재
        assert result["market_disparity"] is not None
        assert result["market_disparity"] < -3
        # 시장 +1 가산되어 score 더 큼
        assert result["score"] >= 3

    def test_market_overheat_amplifies_sell(self):
        stock_df = _build_df_for_disparity(disparity_pct=+8, rsi=50)
        market_df = _build_df_for_disparity(disparity_pct=+6, rsi=50)
        result = ta_mod.generate_bnf_signal(stock_df, market_df=market_df)
        assert result["market_disparity"] > 5
        assert result["score"] <= -2

    def test_market_df_none_gives_no_market_score(self):
        stock_df = _build_df_for_disparity(disparity_pct=-11, rsi=50)
        result = ta_mod.generate_bnf_signal(stock_df, market_df=None)
        assert result["market_disparity"] is None
        # 시장 항목 없이 종목 단독 — disparity -11% +2, RSI 50 +0
        assert result["score"] == 2
```

- [ ] **Step 4.2: 테스트 실행 — FAIL**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_technical_analysis.py::TestGenerateBnfSignal -v
```

- [ ] **Step 4.3: `generate_bnf_signal` 구현**

`src/technical_analysis.py` 끝에 추가:

```python
def generate_bnf_signal(df: pd.DataFrame, market_df: "pd.DataFrame | None" = None) -> dict:
    """BNF 스타일 매수/매도/관망 시그널 — mean reversion + 시장 패닉 매수.

    점수 항목:
    - MA20 이격율: <= -10% +2, <= -5% +1, >= +7% -1, >= +10% -2
    - RSI: <= 30 +1, >= 70 -1
    - 거래량 ≥2배 + 음봉: +1 (BNF 는 추격 매수 안 함, 양봉은 0)
    - 시장 이격율 (market_df 있을 때): 시장<=-3% AND 종목<=-10% → +1,
                                          시장>=+5% AND 종목>=+7% → -1

    임계값: score >= 2 매수, <= -2 매도, 그 외 관망.
    """
    latest = df.dropna().iloc[-1]
    score = 0
    reasons: list[str] = []
    indicators: list[dict] = []

    close = float(latest["Close"])
    ma20 = float(latest["MA20"])
    disparity = (close - ma20) / ma20 * 100 if pd.notna(ma20) and ma20 != 0 else 0.0

    # 1) MA20 이격율
    if disparity <= -10:
        score += 2
        reasons.append(f"MA20 {disparity:.1f}% 강한 과매도")
        d_comment = "강한 과매도 — 평균회귀 반발 매수 후보"
    elif disparity <= -5:
        score += 1
        reasons.append(f"MA20 {disparity:.1f}% 과매도")
        d_comment = "과매도 — 반발 가능성"
    elif disparity >= 10:
        score -= 2
        reasons.append(f"MA20 +{disparity:.1f}% 강한 과열")
        d_comment = "강한 과열 — 평균회귀 매도 후보"
    elif disparity >= 7:
        score -= 1
        reasons.append(f"MA20 +{disparity:.1f}% 과열")
        d_comment = "과열 — 조정 가능성"
    else:
        d_comment = "이격 적정 범위"
    indicators.append({
        "name": "MA20 이격율", "value": f"{disparity:.1f}%", "comment": d_comment,
    })

    # 2) RSI
    rsi_val = float(latest["RSI"]) if pd.notna(latest.get("RSI")) else 50.0
    if rsi_val <= 30:
        score += 1
        reasons.append(f"RSI {rsi_val:.0f} 과매도")
    elif rsi_val >= 70:
        score -= 1
        reasons.append(f"RSI {rsi_val:.0f} 과매수")
    indicators.append({"name": "RSI", "value": round(rsi_val, 1), "comment": "BNF 보조"})

    # 3) 거래량 + 음봉 (양봉은 0)
    vol_ratio = float(latest.get("Volume_Ratio", 1.0)) if pd.notna(latest.get("Volume_Ratio")) else 1.0
    open_val = float(latest["Open"])
    is_red = close < open_val
    if vol_ratio >= 2.0 and is_red:
        score += 1
        reasons.append(f"거래량 {vol_ratio:.1f}배 음봉 — 패닉 매도 후 반발 가능")
        v_comment = f"급증 음봉 — 반발 매수 후보 ({vol_ratio:.1f}배)"
    elif vol_ratio >= 2.0:
        v_comment = f"급증 양봉 — BNF 는 추격 매수 안 함 ({vol_ratio:.1f}배)"
    else:
        v_comment = f"평이 ({vol_ratio:.1f}배)"
    indicators.append({"name": "거래량+캔들", "value": f"{vol_ratio:.1f}배",
                        "comment": v_comment})

    # 4) 시장 이격율 (옵션)
    market_disparity = None
    if market_df is not None:
        m_latest = market_df.dropna().iloc[-1]
        m_close = float(m_latest["Close"])
        m_ma20 = float(m_latest["MA20"])
        if pd.notna(m_ma20) and m_ma20 != 0:
            market_disparity = (m_close - m_ma20) / m_ma20 * 100
            if market_disparity <= -3 and disparity <= -10:
                score += 1
                reasons.append(f"시장 {market_disparity:.1f}% + 종목 패닉")
                m_comment = "시장 패닉 + 종목 과매도 — BNF 매수 강화"
            elif market_disparity >= 5 and disparity >= 7:
                score -= 1
                reasons.append(f"시장 +{market_disparity:.1f}% + 종목 과열")
                m_comment = "시장 과열 + 종목 과열 — 조정 강화"
            else:
                m_comment = f"시장 이격 {market_disparity:.1f}%"
            indicators.append({
                "name": "시장 이격율",
                "value": f"{market_disparity:.1f}%",
                "comment": m_comment,
            })

    if score >= 2:
        signal = "매수"
    elif score <= -2:
        signal = "매도"
    else:
        signal = "관망"

    return {
        "signal": signal,
        "score": score,
        "reasons": reasons,
        "indicators": indicators,
        "disparity": round(disparity, 1),
        "market_disparity": round(market_disparity, 1) if market_disparity is not None else None,
    }
```

- [ ] **Step 4.4: 테스트 실행 — PASS**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_technical_analysis.py -v
```

- [ ] **Step 4.5: 커밋**

```bash
git add src/technical_analysis.py tests/test_technical_analysis.py
git commit -m "feat(technical_analysis): generate_bnf_signal — mean reversion + 시장 통합"
```

---

## Phase 3 — `analyze_stock` 통합 + 3 worker

### Task 5: `analyze_stock` 시그니처 + bnf_signal 통합

**Files:**
- Modify: `main.py`
- Modify: `tests/test_main.py`

- [ ] **Step 5.1: 테스트 작성**

`tests/test_main.py` 끝에 추가:

```python
class TestAnalyzeStockBnfSignal:
    def test_analyze_stock_includes_bnf_signal_with_market(self, monkeypatch):
        """analyze_stock(market='us') → result['bnf_signal'] dict 존재."""
        import main
        from src import technical_analysis as ta_mod
        # fetch + ML stub
        import pandas as pd, numpy as np
        n = 100
        idx = pd.date_range("2026-01-01", periods=n, freq="D")
        fake_df = pd.DataFrame({
            "Open": np.linspace(100, 110, n), "High": np.linspace(102, 112, n),
            "Low": np.linspace(98, 108, n), "Close": np.linspace(100, 110, n),
            "Volume": [1_000_000] * n,
        }, index=idx)
        monkeypatch.setattr(main, "fetch_stock_data", lambda s, **k: fake_df)
        monkeypatch.setattr(main, "fetch_news", lambda s: [])
        monkeypatch.setattr(main._engine, "run", lambda df, sym: {})
        monkeypatch.setattr("src.ml_predictor.analyze_sentiment", lambda news: {})
        monkeypatch.setattr(ta_mod, "fetch_market_df", lambda mkt: fake_df)

        result = main.analyze_stock("AAPL", "Apple", market="us")
        assert result is not None
        assert "bnf_signal" in result
        assert result["bnf_signal"] is not None
        assert "signal" in result["bnf_signal"]

    def test_analyze_stock_market_none_bnf_uses_no_market(self, monkeypatch):
        """market=None → bnf_signal 의 market_disparity 가 None."""
        import main
        from src import technical_analysis as ta_mod
        import pandas as pd, numpy as np
        n = 100
        idx = pd.date_range("2026-01-01", periods=n, freq="D")
        fake_df = pd.DataFrame({
            "Open": np.linspace(100, 110, n), "High": np.linspace(102, 112, n),
            "Low": np.linspace(98, 108, n), "Close": np.linspace(100, 110, n),
            "Volume": [1_000_000] * n,
        }, index=idx)
        monkeypatch.setattr(main, "fetch_stock_data", lambda s, **k: fake_df)
        monkeypatch.setattr(main, "fetch_news", lambda s: [])
        monkeypatch.setattr(main._engine, "run", lambda df, sym: {})
        monkeypatch.setattr("src.ml_predictor.analyze_sentiment", lambda news: {})

        # market=None — fetch_market_df 호출 안 됨
        called = []
        monkeypatch.setattr(ta_mod, "fetch_market_df",
                            lambda mkt: (called.append(mkt), fake_df)[1])

        result = main.analyze_stock("AAPL", "Apple")  # market 인자 없이
        assert result is not None
        assert result["bnf_signal"] is not None
        assert result["bnf_signal"]["market_disparity"] is None
        assert called == []  # market=None 이라 fetch 시도 안 함
```

- [ ] **Step 5.2: 테스트 실행 — FAIL**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_main.py::TestAnalyzeStockBnfSignal -v
```

- [ ] **Step 5.3: `analyze_stock` 변경**

`main.py` 의 import 영역에 `generate_bnf_signal`, `fetch_market_df` 추가:

```python
from src.technical_analysis import compute_indicators, generate_signal, generate_bnf_signal, fetch_market_df
```

`analyze_stock` 시그니처 + 본문 변경:

```python
def analyze_stock(symbol: str, name: str, market: str | None = None) -> dict | None:
    """단일 종목 분석을 수행한다.

    Args:
        symbol: 주식 심볼 (예: AAPL, 005930.KS)
        name: 종목명
        market: 'korea' 또는 'us'. None 이면 BNF 시그널은 시장 통합 없이 종목 단독.

    Returns:
        분석 결과 딕셔너리 또는 실패 시 None
    """
    symbol = sanitize_stock_symbol(symbol)
    if not validate_stock_symbol(symbol):
        logger.error("유효하지 않은 심볼: %s", symbol)
        return None

    try:
        df = fetch_stock_data(symbol)
        df = compute_indicators(df)

        try:
            prediction_history.backfill_inline(symbol, df)
        except Exception as e:
            logger.warning("backfill_inline 실패 (분석은 계속): %s", e)

        signal = generate_signal(df)

        # BNF 시그널 — 실패해도 분석 본체에 영향 없음
        bnf_signal = None
        try:
            market_df = fetch_market_df(market) if market else None
            bnf_signal = generate_bnf_signal(df, market_df=market_df)
        except Exception as e:
            logger.warning("generate_bnf_signal 실패 (분석은 계속): %s", e)

        from src.ml_predictor import analyze_sentiment
        with ThreadPoolExecutor(max_workers=2) as ex:
            fut_pred = ex.submit(_engine.run, df, symbol)
            fut_news = ex.submit(fetch_news, symbol)
            prediction = fut_pred.result()
            news = fut_news.result()

        try:
            last_close = float(df["Close"].iloc[-1])
            target_date = _next_business_day_unix(df.index[-1])
            prediction_history.insert_live(symbol, prediction, last_close, target_date)
        except Exception as e:
            logger.warning("insert_live 실패 (분석 결과는 정상 반환): %s", e)

        sentiment = analyze_sentiment(news)

        return {
            "name": name,
            "symbol": symbol,
            "df": df,
            "signal": signal,
            "bnf_signal": bnf_signal,
            "prediction": prediction,
            "news": news,
            "sentiment": sentiment,
        }
    except Exception as e:
        logger.error("분석 실패 — %s (%s): %s", name, symbol, e)
        return None
```

- [ ] **Step 5.4: 테스트 실행 — PASS**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_main.py::TestAnalyzeStockBnfSignal -v
```

- [ ] **Step 5.5: 회귀**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_main.py -v
```

- [ ] **Step 5.6: 커밋**

```bash
git add main.py tests/test_main.py
git commit -m "feat(main): analyze_stock(market=...) + result['bnf_signal'] 통합"
```

---

### Task 6: 3 worker 가 BNF signal cache.put + market 전달

**Files:**
- Modify: `src/web_app.py` (`_run_analysis_bg`, `_run_full_analysis_bg`)
- Modify: `main.py` (`auto_analyze_market`, `collect_analyses`)
- Modify: `tests/test_web_app.py`

- [ ] **Step 6.1: 테스트 작성**

`tests/test_web_app.py` 끝에 추가:

```python
class TestWorkerBnfSignal:
    def test_run_analysis_bg_passes_bnf_signal_to_cache(self, client, monkeypatch):
        import src.web_app as wa
        from src import analysis_cache as ac

        captured = {}

        def fake_analyze_stock(symbol, name, market=None):
            return {
                "name": name, "symbol": symbol,
                "df": None, "prediction": None, "news": [], "sentiment": None,
                "signal": {"signal": "매수", "score": 3},
                "bnf_signal": {"signal": "매도", "score": -2},
            }

        monkeypatch.setattr("main.analyze_stock", fake_analyze_stock)
        monkeypatch.setattr("src.report_generator.generate_report", lambda a: "<p/>")

        def fake_put(*a, **k):
            captured.update(k)

        monkeypatch.setattr(ac, "put", fake_put)

        wa._jobs.clear()
        wa._jobs["jobbnf1"] = {
            "status": "running", "symbol": "AAPL", "name": "Apple",
            "result_html": None, "error": None, "started_at": "00:00:00",
        }
        wa._run_analysis_bg("jobbnf1", "AAPL", "Apple")

        assert captured["signal_value"] == "매수"
        assert captured["signal_score"] == 3
        assert captured["bnf_signal_value"] == "매도"
        assert captured["bnf_signal_score"] == -2
```

- [ ] **Step 6.2: 테스트 실행 — FAIL**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_web_app.py::TestWorkerBnfSignal -v
```

- [ ] **Step 6.3: `_run_analysis_bg` 변경 (web_app.py)**

`src/web_app.py` 의 `_run_analysis_bg` 안 cache.put 호출 부분 (현재):

```python
            try:
                market = _market_of(symbol)
                sig = result.get("signal") or {}
                analysis_cache.put(
                    symbol, market, html, source="manual",
                    signal_value=sig.get("signal"),
                    signal_score=sig.get("score"),
                )
```

다음으로 교체 (`market` 을 `analyze_stock` 호출에 전달 + bnf 추가):

```python
        from main import analyze_stock
        market = _market_of(symbol)
        result = analyze_stock(symbol, name, market=market)  # market 명시 전달
        ...
        if result is None:
            ...
        else:
            html = generate_report([result])
            _jobs_set(job_id, status="done", result_html=html)
            try:
                sig = result.get("signal") or {}
                bnf = result.get("bnf_signal") or {}
                analysis_cache.put(
                    symbol, market, html, source="manual",
                    signal_value=sig.get("signal"),
                    signal_score=sig.get("score"),
                    bnf_signal_value=bnf.get("signal"),
                    bnf_signal_score=bnf.get("score"),
                )
            except Exception as e:
                logger.warning("analysis_cache.put 실패: %s", e)
```

`market = _market_of(symbol)` 호출 위치를 result is None 체크 전으로 옮기고, `analyze_stock(... market=market)` 으로 전달.

- [ ] **Step 6.4: `_run_full_analysis_bg` 변경 (web_app.py)**

종목별 cache.put 부분에 bnf 추가:

```python
        for r in analyses:
            sym = r["symbol"]
            try:
                ind_html = generate_report([r])
                sig = r.get("signal") or {}
                bnf = r.get("bnf_signal") or {}
                analysis_cache.put(
                    sym, symbol_to_market.get(sym, "us"), ind_html, source="manual",
                    signal_value=sig.get("signal"),
                    signal_score=sig.get("score"),
                    bnf_signal_value=bnf.get("signal"),
                    bnf_signal_score=bnf.get("score"),
                )
                cached += 1
            except Exception as e:
                logger.warning("종목별 cache.put 실패 — %s: %s", sym, e)
```

- [ ] **Step 6.5: `collect_analyses` 가 market 전달 (main.py)**

`main.py` 의 `collect_analyses` 함수에서 `analyze_stock` 호출 시 market 전달. 현재:

```python
def collect_analyses(config: dict) -> list[dict]:
    stocks = get_all_stocks(config)
    ...
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        future_to_stock = {
            ex.submit(analyze_stock, s["symbol"], s["name"]): s
            for s in stocks
        }
```

`get_all_stocks` 가 market 없이 종목 dict 반환. 신규 헬퍼 또는 직접 lookup:

```python
def collect_analyses(config: dict) -> list[dict]:
    """전체 종목 분석을 병렬 실행하고 성공한 결과 list 를 반환한다."""
    # market 별로 종목 모음 → (symbol, name, market) 튜플
    stocks_with_market: list[tuple] = []
    for market, group in config.get("stocks", {}).items():
        for s in group:
            stocks_with_market.append((s["symbol"], s["name"], market))
    logger.info("분석 시작: %d개 종목", len(stocks_with_market))

    analyses: list[dict] = []
    max_workers = min(len(stocks_with_market), 3)
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        future_to_stock = {
            ex.submit(analyze_stock, sym, nm, mk): (sym, nm)
            for (sym, nm, mk) in stocks_with_market
        }
        for future in as_completed(future_to_stock):
            sym, nm = future_to_stock[future]
            logger.info("분석 중: %s (%s)", nm, sym)
            result = future.result()
            if result:
                analyses.append(result)
    return analyses
```

- [ ] **Step 6.6: `auto_analyze_market` 가 market 전달 + bnf cache.put (main.py)**

```python
def auto_analyze_market(market: str) -> None:
    """시장의 모든 종목을 차례로 분석하고 analysis_cache 에 UPSERT."""
    from src import report_generator as _rg

    config = load_config()
    stocks = config.get("stocks", {}).get(market, [])
    logger.info("자동분석 시작 — market=%s n=%d", market, len(stocks))
    success = 0
    for s in stocks:
        try:
            result = analyze_stock(s["symbol"], s["name"], market=market)
            if result is None:
                logger.warning("자동분석 실패(결과 없음): %s", s["symbol"])
                continue
            html = _rg.generate_report([result])
            sig = result.get("signal") or {}
            bnf = result.get("bnf_signal") or {}
            analysis_cache.put(
                cache_key=s["symbol"],
                market=market,
                result_html=html,
                source="auto_cron",
                signal_value=sig.get("signal"),
                signal_score=sig.get("score"),
                bnf_signal_value=bnf.get("signal"),
                bnf_signal_score=bnf.get("score"),
            )
            success += 1
        except Exception as e:
            logger.exception("자동분석 오류 — %s: %s", s["symbol"], e)
    logger.info("자동분석 완료 — market=%s ok=%d/%d", market, success, len(stocks))
```

- [ ] **Step 6.7: 테스트 실행 — PASS**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_web_app.py::TestWorkerBnfSignal tests/test_main.py -v
```

- [ ] **Step 6.8: 회귀**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_web_app.py tests/test_main.py -q
```

- [ ] **Step 6.9: 커밋**

```bash
git add src/web_app.py main.py tests/test_web_app.py
git commit -m "feat(workers): 3 worker 가 bnf_signal cache.put + market 전달"
```

---

## Phase 4 — UI 카드 두 뱃지

### Task 7: `_render_signal_badge` prefix + 카드 BNF 뱃지

**Files:**
- Modify: `src/web_app.py`
- Modify: `tests/test_web_app.py`

- [ ] **Step 7.1: 테스트 작성**

`tests/test_web_app.py` 끝에 추가:

```python
class TestRenderSignalBadgeBnfPrefix:
    def test_prefix_bnf(self, client):
        from src.web_app import _render_signal_badge
        html = _render_signal_badge("매수", 3, prefix="BNF ")
        assert "BNF 매수 +3" in html
        assert "signal-buy" in html

    def test_default_prefix_unchanged(self, client):
        from src.web_app import _render_signal_badge
        html = _render_signal_badge("매도", -2)
        assert "매도 -2" in html
        assert "BNF" not in html


class TestIndexCardBnfBadge:
    def test_card_shows_bnf_badge(self, client):
        from src import analysis_cache as ac
        ac.init_db()
        ac.put("AAPL", "us", "<p/>", "auto_cron",
               signal_value="매수", signal_score=3,
               bnf_signal_value="매도", bnf_signal_score=-2)
        resp = client.get("/")
        # 두 뱃지 모두 존재
        assert "매수 +3".encode() in resp.data
        assert "BNF 매도 -2".encode() in resp.data

    def test_card_no_bnf_badge_when_null(self, client):
        from src import analysis_cache as ac
        ac.init_db()
        ac.put("AAPL", "us", "<p/>", "auto_cron",
               signal_value="매수", signal_score=3)  # bnf 없이
        resp = client.get("/")
        # signal 뱃지는 있고 BNF 뱃지는 없음
        assert "매수 +3".encode() in resp.data
        assert b"BNF " not in resp.data

    def test_card_no_badges_when_no_cache_row(self, client):
        from src import analysis_cache as ac
        ac.init_db()
        import sqlite3
        with sqlite3.connect(ac._DB_PATH) as conn:
            conn.execute("DELETE FROM analysis_cache WHERE cache_key = 'AAPL'")
        resp = client.get("/")
        assert b"BNF " not in resp.data
```

- [ ] **Step 7.2: 테스트 실행 — FAIL**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_web_app.py::TestRenderSignalBadgeBnfPrefix tests/test_web_app.py::TestIndexCardBnfBadge -v
```

- [ ] **Step 7.3: `_render_signal_badge` 시그니처 확장**

`src/web_app.py` 의 `_render_signal_badge` 교체:

```python
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
```

- [ ] **Step 7.4: `index` 카드 마크업 변경**

`src/web_app.py` 의 `index` 함수 안 카드 루프에서 `signal_badge_html` 산출 부분 (현재):

```python
        signal_badge_html = _render_signal_badge(
            cache_row.get("signal_value") if cache_row else None,
            cache_row.get("signal_score") if cache_row else None,
        )
```

다음 추가 (signal_badge_html 산출 직후):

```python
        bnf_badge_html = _render_signal_badge(
            cache_row.get("bnf_signal_value") if cache_row else None,
            cache_row.get("bnf_signal_score") if cache_row else None,
            prefix="BNF ",
        )
```

`stock-card-badges` 컨테이너 마크업 (현재):

```python
            <div class="stock-card-badges">
              {signal_badge_html}
              <span class="badge {badge_cls}">{market_label}</span>
            </div>
```

다음으로 교체 (BNF 뱃지 추가):

```python
            <div class="stock-card-badges">
              {signal_badge_html}
              {bnf_badge_html}
              <span class="badge {badge_cls}">{market_label}</span>
            </div>
```

- [ ] **Step 7.5: 테스트 실행 — PASS**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_web_app.py::TestRenderSignalBadgeBnfPrefix tests/test_web_app.py::TestIndexCardBnfBadge -v
```

- [ ] **Step 7.6: 전체 회귀**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/ --ignore=tests/test_ml_predictor.py --ignore=tests/test_data_fetcher.py --ignore=tests/test_backtest.py -q
```

- [ ] **Step 7.7: 커밋**

```bash
git add src/web_app.py tests/test_web_app.py
git commit -m "feat(web): _render_signal_badge prefix + 카드 BNF 뱃지 (Tech + BNF + 시장)"
```

---

## Phase 5 — 배포

### Task 8: 서버 배포 + 시각 확인

**Files:** 없음 (서버 운영)

- [ ] **Step 8.1: push to origin/main**

```bash
git push origin main
```

- [ ] **Step 8.2: 서버 git pull**

```bash
ssh sykim@100.87.151.104 'cd ~/Projects/stock-analyzer && git pull --ff-only origin main'
```

- [ ] **Step 8.3: web + scheduler 재시작**

```bash
ssh sykim@100.87.151.104 'launchctl kickstart -k gui/$(id -u)/ai.stock-analyzer.web'
ssh sykim@100.87.151.104 'launchctl kickstart -k gui/$(id -u)/ai.stock-analyzer.scheduler'
```

- [ ] **Step 8.4: 마이그레이션 자동 적용 확인**

```bash
ssh sykim@100.87.151.104 'sqlite3 ~/Projects/stock-analyzer/data/predictions.db ".schema analysis_cache"'
```
Expected: schema 출력에 `signal_value TEXT`, `signal_score INTEGER`, `bnf_signal_value TEXT`, `bnf_signal_score INTEGER` 모두 포함

- [ ] **Step 8.5: smoke**

```bash
ssh sykim@100.87.151.104 'sleep 3; curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/'
```
Expected: 401

- [ ] **Step 8.6: 시각 확인**

브라우저로 https://sykim-macmini.tail8d6ef7.ts.net/ 로그인 후:
- 종목 카드 한 개 골라 **재분석** → 분석 완료 후 대시보드로 돌아오면:
  - Tech 뱃지 (예: `매수 +3`)
  - BNF 뱃지 (예: `BNF 관망 +1`)
  - 시장 뱃지 (한국/미국)
  세 뱃지가 세로 정렬로 표시
- 색상으로 의견 합의/분기 즉시 확인 (둘 다 초록 → 강한 매수, 분기 → 신중)
- 시장 인덱스 fetch — 첫 분석 시 KOSPI/S&P500 fetch 1회, 이후 15분간 캐시

자동 cron (KST 16:00 / 06:00) 한 번 돌면 모든 종목에 자동으로 두 시그널 채워짐.

---

## Self-Review

스펙 (`docs/superpowers/specs/2026-05-05-bnf-signal-design.md`) 의 §2 정책 ↔ plan task 매핑:

| 스펙 항목 | 구현 task |
|---|---|
| 별도 `generate_bnf_signal` | T4 |
| 시장 센티먼트 통합 | T3 (`fetch_market_df`), T4 (`generate_bnf_signal` 의 시장 점수 항목) |
| 메모리 TTL 15분 캐시 | T3 |
| 임계값 (>=2 매수 / <=-2 매도) | T4 |
| 카드에 두 시그널 동시 표시 | T7 |
| `analysis_cache` 컬럼 추가 | T1 |
| `put`/`get`/`list_symbols` 시그니처 확장 | T2 |
| `analyze_stock(market=...)` | T5 |
| 3 worker BNF signal 전달 | T6 |
| `_render_signal_badge` prefix | T7 |
| Graceful degradation (시장 fetch 실패) | T3 (warning + None), T4 (market_df=None 분기), T5 (try/except 가 catch) |
| 자동 마이그레이션 | T1 (`_migrate` 확장), T8 (배포 시 init_db 자동) |

스펙 §9 에러 케이스 → 모두 구현됨:
- 시장 fetch 실패 → T3 (None 반환)
- TTL 캐시 stale 후 fetch 실패 → T3 (`_market_cache.pop` 으로 stale 정리)
- `analyze_stock(market=None)` → T5 (`market_df=None` → 시장 점수 0)
- 기존 row (bnf 없음) → T2 (NULL 허용), T7 (빈 문자열 반환)
- `generate_bnf_signal` 예외 → T5 (try/except → bnf_signal=None)
- MA20 NaN → T4 (`pd.notna` 체크)
- 시장 외 종목 → T3 (`_MARKET_INDEX.get` → None)
- 두 시그널 의견 분기 → T7 (색상 다른 두 뱃지)

타입 일관성:
- `put` 시그니처 (T2) → T6 호출자 모두 keyword 사용 ✓
- `get` 반환 dict 새 키 (T2) → T7 카드 렌더가 같은 키 ✓
- `_render_signal_badge` prefix 매개변수 (T7) → T7 카드 호출 일관 ✓
- `_MARKET_INDEX` 키 ("korea"/"us") → `_market_of` lookup 결과와 일치 ✓
- `generate_bnf_signal` 반환 dict 키 (signal/score/disparity 등) → T5 `analyze_stock` 가 그대로 result["bnf_signal"] 에 저장, T6 worker 가 sig.get("signal")/sig.get("score") 으로 추출 ✓

Placeholder 스캔: TBD/TODO 없음 ✓

스펙 §13 비목표 → plan 에 의도적으로 빠짐 (백테스트, 결과 페이지 BNF 섹션, 토글, 알림, intraday) ✓
