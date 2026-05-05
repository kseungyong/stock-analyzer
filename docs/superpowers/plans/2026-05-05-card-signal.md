# 종목 카드 매수/매도/관망 시그널 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 대시보드 종목 카드 header 영역에 매수/매도/관망 시그널 + score 정수 뱃지를 표시. `analysis_cache` 테이블에 `signal_value` / `signal_score` 컬럼을 추가하고, 분석 worker 3개가 매번 시그널을 함께 저장한다.

**Architecture:** `analysis_cache` 스키마 확장 + 멱등 마이그레이션 (`PRAGMA table_info` → 조건부 `ALTER TABLE`). `put` 시그니처에 keyword-only `signal_value` / `signal_score` 추가. 3 worker (`_run_analysis_bg`, `_run_full_analysis_bg`, `auto_analyze_market`) 가 `result["signal"]` 에서 값 추출 후 cache.put 에 전달. `_render_signal_badge` 헬퍼와 카드 마크업 변경, CSS 추가.

**Tech Stack:** Python 3.10+, SQLite (stdlib), Flask, pytest, CSS3

**Spec:** `docs/superpowers/specs/2026-05-05-card-signal-design.md`

---

## 파일 구조

| 파일 | 변경 |
|---|---|
| `src/analysis_cache.py` | 수정 — `_SCHEMA` 갱신, `_migrate(conn)` 추가, `put` keyword-only signal 매개변수, `get`/`list_symbols` SELECT 확장 |
| `src/web_app.py` | 수정 — `_SIGNAL_CLASS` 상수, `_render_signal_badge` 헬퍼, `index` 카드 마크업 변경, CSS append, `_run_analysis_bg` / `_run_full_analysis_bg` 가 signal 전달 |
| `main.py` | 수정 — `auto_analyze_market` 가 signal 전달 |
| `tests/test_analysis_cache.py` | 보강 — `TestMigrateAddsSignalColumns` (3), `TestPutGetSignal` (4) |
| `tests/test_web_app.py` | 보강 — `TestRenderSignalBadge` (5), `TestIndexCardSignal` (3), `TestAnalyzeBgSavesSignal` (1), `TestFullAnalysisSavesSignal` (1) |
| `tests/test_main.py` | 보강 — `TestAutoAnalyzeMarketSavesSignal` (1) |

---

## Phase 1 — `analysis_cache` 스키마 + 마이그레이션

### Task 1: `_SCHEMA` 갱신 + `_migrate` 멱등 마이그레이션

**Files:**
- Modify: `src/analysis_cache.py` (`_SCHEMA` 상수, `init_db`, 신규 `_migrate` 함수)
- Modify: `tests/test_analysis_cache.py` (append `TestMigrateAddsSignalColumns`)

- [ ] **Step 1.1: 테스트 작성 (TDD)**

`tests/test_analysis_cache.py` 끝에 추가:

```python
class TestMigrateAddsSignalColumns:
    def test_new_db_has_signal_columns(self, tmp_db):
        ph_ac.init_db()  # ac alias for analysis_cache module
        import sqlite3
        with sqlite3.connect(tmp_db) as conn:
            cur = conn.execute("PRAGMA table_info(analysis_cache)")
            cols = {row[1] for row in cur.fetchall()}
        assert "signal_value" in cols
        assert "signal_score" in cols

    def test_migrate_is_idempotent(self, tmp_db):
        ph_ac.init_db()
        ph_ac.init_db()  # 두 번째 호출도 오류 없음
        import sqlite3
        with sqlite3.connect(tmp_db) as conn:
            cur = conn.execute("PRAGMA table_info(analysis_cache)")
            names = [row[1] for row in cur.fetchall()]
        # signal_value 가 정확히 1개만
        assert names.count("signal_value") == 1
        assert names.count("signal_score") == 1

    def test_migrate_adds_columns_to_legacy_db(self, tmp_db):
        """기존 (signal 컬럼 없는) DB 시뮬레이션 → _migrate 후 컬럼 존재."""
        import sqlite3
        # legacy schema 만 직접 생성 (signal 컬럼 없이)
        legacy_schema = """
        CREATE TABLE IF NOT EXISTS analysis_cache (
            cache_key      TEXT PRIMARY KEY,
            market         TEXT NOT NULL,
            result_html    TEXT NOT NULL,
            generated_at   INTEGER NOT NULL,
            source         TEXT NOT NULL
        );
        """
        tmp_db.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(tmp_db) as conn:
            conn.executescript(legacy_schema)
            conn.execute(
                """INSERT INTO analysis_cache
                   (cache_key, market, result_html, generated_at, source)
                   VALUES ('AAPL', 'us', '<p/>', 1700000000, 'manual')"""
            )
        # init_db 호출 → _migrate 가 컬럼 추가
        ph_ac.init_db()
        with sqlite3.connect(tmp_db) as conn:
            cur = conn.execute("PRAGMA table_info(analysis_cache)")
            cols = {row[1] for row in cur.fetchall()}
            # 기존 row 도 보존
            row = conn.execute("SELECT cache_key, signal_value FROM analysis_cache WHERE cache_key='AAPL'").fetchone()
        assert "signal_value" in cols
        assert "signal_score" in cols
        assert row == ("AAPL", None)  # 기존 row 의 signal 은 NULL
```

기존 import 영역에 `from src import analysis_cache as ph_ac` 가 있으면 그대로 사용. 없으면 파일 상단의 기존 `from src import analysis_cache as ac` 와 같은 alias 활용 (테스트 파일이 `ac` 를 쓰는지 `ph_ac` 를 쓰는지 확인 후 일관 사용).

> **alias 확인 방법**: `tests/test_analysis_cache.py` 상단의 `from src import analysis_cache as XXX` 라인에서 alias 이름 그대로 사용. 다른 alias 면 위 `ph_ac` 를 그 이름으로 일괄 치환.

- [ ] **Step 1.2: 테스트 실행 — FAIL**

Run from `/Users/sykim/Projects/stock-analyzer/.worktrees/card-signal`:
```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_analysis_cache.py::TestMigrateAddsSignalColumns -v
```
Expected: 3 fail — `signal_value` 컬럼 없음

- [ ] **Step 1.3: `_SCHEMA` 갱신 + `_migrate` 추가**

`src/analysis_cache.py` 의 `_SCHEMA` 상수 변경:

```python
_SCHEMA = """
CREATE TABLE IF NOT EXISTS analysis_cache (
    cache_key      TEXT PRIMARY KEY,
    market         TEXT NOT NULL,
    result_html    TEXT NOT NULL,
    generated_at   INTEGER NOT NULL,
    source         TEXT NOT NULL,
    signal_value   TEXT,
    signal_score   INTEGER
);

CREATE INDEX IF NOT EXISTS idx_analysis_cache_market
    ON analysis_cache(market);
"""
```

`init_db` 함수에 `_migrate(conn)` 호출 추가:

```python
def init_db() -> None:
    """첫 호출 시 DB 파일과 부모 디렉토리 생성, 스키마 적용. 멱등."""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _writer_lock:
        with closing(_connect()) as conn:
            conn.executescript(_SCHEMA)
            _migrate(conn)
    logger.info("analysis_cache DB 초기화 완료: %s", _DB_PATH)
```

`init_db` 다음에 `_migrate` 함수 추가:

```python
def _migrate(conn: sqlite3.Connection) -> None:
    """기존 DB 에 누락된 컬럼을 추가하는 멱등 마이그레이션.

    PRAGMA table_info 로 컬럼 존재 확인 후 조건부 ALTER. SQLite 의 ALTER TABLE
    ADD COLUMN 은 IF NOT EXISTS 미지원이라 명시적 체크 필요.
    """
    cur = conn.execute("PRAGMA table_info(analysis_cache)")
    cols = {row[1] for row in cur.fetchall()}
    if "signal_value" not in cols:
        conn.execute("ALTER TABLE analysis_cache ADD COLUMN signal_value TEXT")
    if "signal_score" not in cols:
        conn.execute("ALTER TABLE analysis_cache ADD COLUMN signal_score INTEGER")
```

- [ ] **Step 1.4: 테스트 실행 — PASS**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_analysis_cache.py::TestMigrateAddsSignalColumns -v
```
Expected: 3 passed

- [ ] **Step 1.5: 회귀**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_analysis_cache.py -v
```
Expected: 모든 기존 + 3 신규 PASS

- [ ] **Step 1.6: 커밋**

```bash
git add src/analysis_cache.py tests/test_analysis_cache.py
git commit -m "feat(analysis_cache): signal_value/signal_score 컬럼 + 멱등 마이그레이션"
```

---

### Task 2: `put` keyword-only signal 매개변수 + UPSERT

**Files:**
- Modify: `src/analysis_cache.py` (`put` 함수)
- Modify: `tests/test_analysis_cache.py`

- [ ] **Step 2.1: 테스트 작성**

`tests/test_analysis_cache.py` 끝에 추가:

```python
class TestPutGetSignal:
    def test_put_with_signal_then_get(self, tmp_db):
        ph_ac.init_db()
        ph_ac.put("AAPL", "us", "<p/>", "manual",
                  signal_value="매수", signal_score=3)
        row = ph_ac.get("AAPL")
        assert row["signal_value"] == "매수"
        assert row["signal_score"] == 3

    def test_put_default_signal_is_none(self, tmp_db):
        ph_ac.init_db()
        ph_ac.put("AAPL", "us", "<p/>", "manual")
        row = ph_ac.get("AAPL")
        assert row["signal_value"] is None
        assert row["signal_score"] is None

    def test_put_signal_is_keyword_only(self, tmp_db):
        ph_ac.init_db()
        with pytest.raises(TypeError):
            # 5번째 positional 으로 signal_value 전달 시도 → keyword-only 라 TypeError
            ph_ac.put("AAPL", "us", "<p/>", "manual", "매수", 3)

    def test_upsert_overwrites_signal_with_none(self, tmp_db):
        """signal 있던 row 를 signal 없이 UPSERT → NULL 로 덮어쓰기."""
        ph_ac.init_db()
        ph_ac.put("AAPL", "us", "<p/>", "manual",
                  signal_value="매수", signal_score=3)
        # 두 번째 put — signal 매개변수 없이
        ph_ac.put("AAPL", "us", "<p>v2</p>", "manual")
        row = ph_ac.get("AAPL")
        assert row["result_html"] == "<p>v2</p>"
        assert row["signal_value"] is None  # NULL 로 덮어씌워짐
        assert row["signal_score"] is None
```

- [ ] **Step 2.2: 테스트 실행 — FAIL**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_analysis_cache.py::TestPutGetSignal -v
```
Expected: 4 fail — `put()` 가 signal_value 매개변수 모름

- [ ] **Step 2.3: `put` 시그니처 + UPSERT 변경**

`src/analysis_cache.py` 의 `put` 함수 교체:

```python
def put(
    cache_key: str,
    market: str,
    result_html: str,
    source: str,
    *,
    signal_value: str | None = None,
    signal_score: int | None = None,
) -> None:
    """analysis_cache UPSERT. 같은 cache_key 존재 시 덮어쓴다.

    signal_value/signal_score 가 None 이면 NULL 저장 — UPSERT 시 기존 값을 NULL 로
    덮어쓰는 효과 (호출자가 명시적으로 전달해야 보존).
    """
    now_unix = int(time.time())
    with _writer_lock:
        with closing(_connect()) as conn:
            conn.execute("BEGIN")
            try:
                conn.execute(
                    """INSERT INTO analysis_cache
                       (cache_key, market, result_html, generated_at, source,
                        signal_value, signal_score)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(cache_key) DO UPDATE SET
                         market       = excluded.market,
                         result_html  = excluded.result_html,
                         generated_at = excluded.generated_at,
                         source       = excluded.source,
                         signal_value = excluded.signal_value,
                         signal_score = excluded.signal_score""",
                    (cache_key, market, result_html, now_unix, source,
                     signal_value, signal_score),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
```

- [ ] **Step 2.4: `get` 갱신 — 두 컬럼 SELECT 추가**

`src/analysis_cache.py` 의 `get` 함수 교체:

```python
def get(cache_key: str) -> dict | None:
    """cache_key 의 row 를 dict 로 반환. 없으면 None."""
    with closing(_connect()) as conn:
        row = conn.execute(
            """SELECT cache_key, market, result_html, generated_at, source,
                      signal_value, signal_score
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
    }
```

- [ ] **Step 2.5: `list_symbols` 도 SELECT 확장**

`src/analysis_cache.py` 의 `list_symbols` 함수 교체:

```python
def list_symbols() -> list[dict]:
    """종목별 row 만 (market != 'all') market·cache_key 순으로 반환."""
    with closing(_connect()) as conn:
        rows = conn.execute(
            """SELECT cache_key, market, result_html, generated_at, source,
                      signal_value, signal_score
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
        }
        for r in rows
    ]
```

- [ ] **Step 2.6: 테스트 실행 — PASS**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_analysis_cache.py -v
```
Expected: 모든 기존 + 4 신규 PASS, 회귀 없음

- [ ] **Step 2.7: 커밋**

```bash
git add src/analysis_cache.py tests/test_analysis_cache.py
git commit -m "feat(analysis_cache): put/get/list_symbols 가 signal_value/signal_score 처리"
```

---

## Phase 2 — Worker 가 signal 전달

### Task 3: `_run_analysis_bg` (수동 단일) signal 전달

**Files:**
- Modify: `src/web_app.py` (`_run_analysis_bg` 함수)
- Modify: `tests/test_web_app.py`

- [ ] **Step 3.1: 테스트 작성**

`tests/test_web_app.py` 끝에 추가:

```python
class TestAnalyzeBgSavesSignal:
    def test_run_analysis_bg_passes_signal_to_cache(self, client, monkeypatch):
        import src.web_app as wa
        from src import analysis_cache as ac

        captured = {}

        def fake_analyze_stock(symbol, name):
            return {
                "name": name, "symbol": symbol,
                "df": None, "prediction": None, "news": [], "sentiment": None,
                "signal": {"signal": "매수", "score": 3, "reasons": []},
            }

        def fake_generate_report(analyses):
            return "<p>fake</p>"

        def fake_put(cache_key, market, result_html, source, *,
                     signal_value=None, signal_score=None):
            captured["signal_value"] = signal_value
            captured["signal_score"] = signal_score

        monkeypatch.setattr("main.analyze_stock", fake_analyze_stock)
        monkeypatch.setattr("src.report_generator.generate_report", fake_generate_report)
        monkeypatch.setattr(ac, "put", fake_put)

        wa._jobs.clear()
        wa._jobs["jobsig1"] = {
            "status": "running", "symbol": "AAPL", "name": "Apple",
            "result_html": None, "error": None, "started_at": "00:00:00",
        }
        wa._run_analysis_bg("jobsig1", "AAPL", "Apple")

        assert captured["signal_value"] == "매수"
        assert captured["signal_score"] == 3
```

- [ ] **Step 3.2: 테스트 실행 — FAIL**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_web_app.py::TestAnalyzeBgSavesSignal -v
```
Expected: 1 fail — captured 가 비어있음 (현재 코드는 signal 전달 안 함)

- [ ] **Step 3.3: `_run_analysis_bg` 변경**

`src/web_app.py` 에서 `def _run_analysis_bg(job_id` 검색. `analysis_cache.put` 호출하는 라인을 다음으로 교체:

기존:
```python
            try:
                market = _market_of(symbol)
                analysis_cache.put(symbol, market, html, source="manual")
            except Exception as e:
                logger.warning("analysis_cache.put 실패 (job 결과는 정상): %s", e)
```

변경 후:
```python
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
```

- [ ] **Step 3.4: 테스트 실행 — PASS**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_web_app.py::TestAnalyzeBgSavesSignal -v
```
Expected: PASS

- [ ] **Step 3.5: 커밋**

```bash
git add src/web_app.py tests/test_web_app.py
git commit -m "feat(web): _run_analysis_bg 가 signal_value/signal_score 캐시 저장"
```

---

### Task 4: `_run_full_analysis_bg` (수동 전체) signal 전달

**Files:**
- Modify: `src/web_app.py` (`_run_full_analysis_bg` 함수)
- Modify: `tests/test_web_app.py`

- [ ] **Step 4.1: 테스트 작성**

`tests/test_web_app.py` 끝에 추가:

```python
class TestFullAnalysisSavesSignal:
    def test_run_full_analysis_bg_passes_signal_per_symbol(self, client, monkeypatch):
        """종목별 put 에 signal 전달, ALL put 은 signal 없이."""
        import src.web_app as wa
        from src import analysis_cache as ac

        fake_analyses = [
            {"symbol": "AAPL", "name": "Apple",
             "signal": {"signal": "매수", "score": 3}},
            {"symbol": "005930.KS", "name": "삼성전자",
             "signal": {"signal": "매도", "score": -2}},
        ]
        fake_config = {"stocks": {
            "us":    [{"symbol": "AAPL", "name": "Apple"}],
            "korea": [{"symbol": "005930.KS", "name": "삼성전자"}],
        }}
        monkeypatch.setattr("main.collect_analyses", lambda cfg: fake_analyses)
        monkeypatch.setattr("main.load_config", lambda: fake_config)
        monkeypatch.setattr("src.report_generator.generate_report",
                            lambda items: f"<p>{len(items)}</p>")

        captured = []
        monkeypatch.setattr(ac, "put",
                            lambda *a, **k: captured.append((a, k)))

        wa._jobs.clear()
        wa._jobs["fsig1"] = {
            "status": "running", "symbol": "ALL", "name": "전체 종목",
            "result_html": None, "error": None, "started_at": "00:00:00",
        }
        wa._run_full_analysis_bg("fsig1")

        # 종목별 put 두 번 — signal kwargs 포함
        symbol_calls = {c[0][0]: c[1] for c in captured if c[0][0] != "ALL"}
        assert symbol_calls["AAPL"]["signal_value"] == "매수"
        assert symbol_calls["AAPL"]["signal_score"] == 3
        assert symbol_calls["005930.KS"]["signal_value"] == "매도"
        assert symbol_calls["005930.KS"]["signal_score"] == -2
        # ALL put 은 signal kwargs 없음 (또는 None)
        all_calls = [c[1] for c in captured if c[0][0] == "ALL"]
        assert len(all_calls) == 1
        assert all_calls[0].get("signal_value") is None
        assert all_calls[0].get("signal_score") is None
```

- [ ] **Step 4.2: 테스트 실행 — FAIL**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_web_app.py::TestFullAnalysisSavesSignal -v
```

- [ ] **Step 4.3: `_run_full_analysis_bg` 변경**

`src/web_app.py` 의 `_run_full_analysis_bg` 안에서 종목별 `analysis_cache.put` 호출하는 부분을 찾아 (현재 다음 형태):

```python
        for r in analyses:
            sym = r["symbol"]
            try:
                ind_html = generate_report([r])
                analysis_cache.put(
                    sym, symbol_to_market.get(sym, "us"), ind_html, source="manual"
                )
                cached += 1
            except Exception as e:
                logger.warning("종목별 cache.put 실패 — %s: %s", sym, e)
```

다음으로 교체:

```python
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
```

ALL row 의 put 은 변경하지 않습니다 (signal 없이 그대로):
```python
        try:
            analysis_cache.put("ALL", "all", full_html, source="manual")
        except Exception as e:
            logger.warning("analysis_cache.put('ALL') 실패: %s", e)
```

- [ ] **Step 4.4: 테스트 실행 — PASS**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_web_app.py::TestFullAnalysisSavesSignal -v
```

- [ ] **Step 4.5: 커밋**

```bash
git add src/web_app.py tests/test_web_app.py
git commit -m "feat(web): _run_full_analysis_bg 종목별 put 에 signal 전달"
```

---

### Task 5: `auto_analyze_market` (cron) signal 전달

**Files:**
- Modify: `main.py` (`auto_analyze_market` 함수)
- Modify: `tests/test_main.py`

- [ ] **Step 5.1: 테스트 작성**

`tests/test_main.py` 끝에 추가:

```python
class TestAutoAnalyzeMarketSavesSignal:
    def test_signal_passed_to_cache_put(self, monkeypatch):
        import main
        from src import analysis_cache as ac

        fake_config = {"stocks": {
            "us": [{"symbol": "AAPL", "name": "Apple"}],
        }, "schedule": {}, "email": {}}
        monkeypatch.setattr(main, "load_config", lambda: fake_config)

        def fake_analyze(symbol, name):
            return {
                "name": name, "symbol": symbol,
                "df": None, "prediction": None, "news": [], "sentiment": None,
                "signal": {"signal": "매수", "score": 4},
            }

        monkeypatch.setattr(main, "analyze_stock", fake_analyze)
        monkeypatch.setattr("src.report_generator.generate_report",
                            lambda a: "<p/>")

        captured = []
        monkeypatch.setattr(ac, "put", lambda **kw: captured.append(kw))

        main.auto_analyze_market("us")

        assert len(captured) == 1
        assert captured[0]["signal_value"] == "매수"
        assert captured[0]["signal_score"] == 4
```

- [ ] **Step 5.2: 테스트 실행 — FAIL**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_main.py::TestAutoAnalyzeMarketSavesSignal -v
```

- [ ] **Step 5.3: `auto_analyze_market` 변경**

`main.py` 의 `auto_analyze_market` 함수 안 `analysis_cache.put` 호출 부분 (현재):

```python
            html = _rg.generate_report([result])
            analysis_cache.put(
                cache_key=s["symbol"],
                market=market,
                result_html=html,
                source="auto_cron",
            )
```

(또는 그 근처 — `from src import report_generator as _rg` 있는 형태). 다음으로 교체:

```python
            html = _rg.generate_report([result])
            sig = result.get("signal") or {}
            analysis_cache.put(
                cache_key=s["symbol"],
                market=market,
                result_html=html,
                source="auto_cron",
                signal_value=sig.get("signal"),
                signal_score=sig.get("score"),
            )
```

- [ ] **Step 5.4: 테스트 실행 — PASS**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_main.py::TestAutoAnalyzeMarketSavesSignal -v
```

- [ ] **Step 5.5: 커밋**

```bash
git add main.py tests/test_main.py
git commit -m "feat(main): auto_analyze_market 가 signal 캐시 저장"
```

---

## Phase 3 — 카드 렌더링

### Task 6: `_SIGNAL_CLASS` + `_render_signal_badge` 헬퍼

**Files:**
- Modify: `src/web_app.py`
- Modify: `tests/test_web_app.py`

- [ ] **Step 6.1: 테스트 작성**

`tests/test_web_app.py` 끝에 추가:

```python
class TestRenderSignalBadge:
    def test_none_value_returns_empty_string(self, client):
        from src.web_app import _render_signal_badge
        assert _render_signal_badge(None, None) == ""
        assert _render_signal_badge(None, 5) == ""
        assert _render_signal_badge("", 0) == ""

    def test_buy_with_positive_score(self, client):
        from src.web_app import _render_signal_badge
        html = _render_signal_badge("매수", 3)
        assert "signal-badge" in html
        assert "signal-buy" in html
        assert "매수 +3" in html

    def test_sell_with_negative_score(self, client):
        from src.web_app import _render_signal_badge
        html = _render_signal_badge("매도", -2)
        assert "signal-sell" in html
        assert "매도 -2" in html

    def test_hold_with_positive_score(self, client):
        from src.web_app import _render_signal_badge
        html = _render_signal_badge("관망", 1)
        assert "signal-hold" in html
        assert "관망 +1" in html

    def test_score_zero_no_sign(self, client):
        from src.web_app import _render_signal_badge
        html = _render_signal_badge("관망", 0)
        # "+0" 또는 "-0" 가 아니라 그냥 "0"
        assert "관망 0" in html
        assert "+0" not in html
```

- [ ] **Step 6.2: 테스트 실행 — FAIL**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_web_app.py::TestRenderSignalBadge -v
```

- [ ] **Step 6.3: 헬퍼 구현**

`src/web_app.py` 에 추가 — 다른 `_render_*` 헬퍼 영역 (`_render_signal_badge` 가 카드에서 호출될 위치이므로 `index` 함수 근처보다는 다른 헬퍼들과 함께):

```python
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
```

- [ ] **Step 6.4: 테스트 실행 — PASS**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_web_app.py::TestRenderSignalBadge -v
```

- [ ] **Step 6.5: 커밋**

```bash
git add src/web_app.py tests/test_web_app.py
git commit -m "feat(web): _SIGNAL_CLASS + _render_signal_badge 헬퍼"
```

---

### Task 7: `index` 카드 마크업 + CSS

**Files:**
- Modify: `src/web_app.py` (`index` 함수의 카드 루프 + `_CSS` append)
- Modify: `tests/test_web_app.py`

- [ ] **Step 7.1: 테스트 작성**

`tests/test_web_app.py` 끝에 추가:

```python
class TestIndexCardSignal:
    def test_card_shows_signal_badge_when_cache_has_signal(self, client):
        from src import analysis_cache as ac
        ac.init_db()
        ac.put("AAPL", "us", "<p/>", "auto_cron",
               signal_value="매수", signal_score=3)
        resp = client.get("/")
        assert b"signal-badge" in resp.data
        assert "매수 +3".encode() in resp.data

    def test_card_no_signal_when_signal_value_null(self, client):
        from src import analysis_cache as ac
        ac.init_db()
        ac.put("AAPL", "us", "<p/>", "auto_cron")  # signal 없이
        resp = client.get("/")
        assert b"signal-badge" not in resp.data

    def test_card_no_signal_when_no_cache_row(self, client):
        from src import analysis_cache as ac
        ac.init_db()
        # AAPL 캐시 row 자체 없음 (다른 테스트로부터 잔여 가능 — 명시적 삭제)
        import sqlite3
        with sqlite3.connect(ac._DB_PATH) as conn:
            conn.execute("DELETE FROM analysis_cache WHERE cache_key = 'AAPL'")
        resp = client.get("/")
        assert b"signal-badge" not in resp.data
```

- [ ] **Step 7.2: 테스트 실행 — FAIL**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_web_app.py::TestIndexCardSignal -v
```
Expected: 첫 케이스 fail (현재 카드에 signal-badge 마크업 없음). 나머지 두 케이스는 PASS (이미 마크업 없음).

- [ ] **Step 7.3: `index` 카드 마크업 변경**

`src/web_app.py` 의 `index` 함수 안 카드 생성 루프를 찾으세요 (`cache_row = _safe_cache_get(s["symbol"])` 줄이 단서). 카드 HTML 안 `<div class="stock-card-header">` 부분이 다음 형태일 것:

```python
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
            ...
          </div>
        </div>""")
```

이를 다음으로 교체 — `signal_badge` 변수를 카드 위에서 산출 후 `stock-card-badges` 컨테이너에 넣음:

```python
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
            ...
          </div>
        </div>""")
```

`...` 부분 (actions) 은 변경하지 않습니다 — 기존 그대로 유지. signal_badge_html 계산 라인은 `cards.append(...)` 직전에 위치.

- [ ] **Step 7.4: CSS append**

`src/web_app.py` 의 `_CSS = """ ... """` 상수 끝부분 (closing `"""` 직전) 에 다음 CSS 추가:

```css
/* ── 카드 시그널 뱃지 ─────────────────────────────────────────────────── */
.stock-card-badges {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 6px;
}

.signal-badge {
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
```

- [ ] **Step 7.5: 테스트 실행 — PASS**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_web_app.py::TestIndexCardSignal -v
```

- [ ] **Step 7.6: 전체 회귀**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/ --ignore=tests/test_ml_predictor.py --ignore=tests/test_data_fetcher.py --ignore=tests/test_backtest.py -q
```
Expected: 모든 기존 + 신규 PASS

- [ ] **Step 7.7: 커밋**

```bash
git add src/web_app.py tests/test_web_app.py
git commit -m "feat(web): 카드 header 영역 매수/매도/관망 시그널 뱃지 + CSS"
```

---

## Phase 4 — 배포

### Task 8: 서버 배포 + 시각 확인

**Files:** 없음 (서버 운영 명령)

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

- [ ] **Step 8.4: 마이그레이션 적용 확인**

```bash
ssh sykim@100.87.151.104 'sqlite3 ~/Projects/stock-analyzer/data/predictions.db ".schema analysis_cache"'
```
Expected: schema 출력에 `signal_value TEXT`, `signal_score INTEGER` 포함

- [ ] **Step 8.5: smoke test**

```bash
ssh sykim@100.87.151.104 'sleep 3; curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/'
```
Expected: 401

- [ ] **Step 8.6: 시각 확인**

브라우저로 https://sykim-macmini.tail8d6ef7.ts.net/ 로그인 후:
- 대시보드의 종목 카드에서 시장 뱃지 옆에 **시그널 뱃지가 표시되지 않음** 을 확인 (기존 row 의 signal=NULL)
- 종목 하나 골라 **재분석** 클릭 → 분석 완료 후 대시보드로 돌아오면 그 종목 카드에 `매수 +N` / `매도 -N` / `관망 N` 뱃지 표시됨
- 매수면 초록, 매도면 빨강, 관망이면 회색
- 한 번 재분석 안 한 다른 종목은 시그널 뱃지 없음 — 정상 (기존 row NULL)

자동 cron (KST 16:00 / 06:00) 이 한 번 돌고 나면 모든 종목에 자동으로 시그널이 채워집니다. 또는 "전체 종목 일괄 분석" 한 번 클릭하면 즉시 모든 카드에 시그널 표시.

---

## Self-Review

스펙 (`docs/superpowers/specs/2026-05-05-card-signal-design.md`) 의 §2 정책표 각 항목 → plan task 매핑:

| 스펙 항목 | 구현 task |
|---|---|
| 시그널 출처 (generate_signal) | T3/T4/T5 — `result["signal"]` 에서 추출 |
| 강도 표시 (라벨 + score 정수) | T6 — `_render_signal_badge` 의 sign 처리 |
| 카드 위치 (시장 뱃지 옆) | T7 — `stock-card-badges` 컨테이너 |
| 데이터 저장 (signal_value/signal_score) | T1 — `_SCHEMA`, T2 — `put` 시그니처 |
| 마이그레이션 (멱등 ALTER) | T1 — `_migrate` |
| 시그널 갱신 (3 worker) | T3, T4, T5 |
| 기존 row 호환 (signal NULL) | T1 (NULL 허용), T7 (`_render_signal_badge` 빈 문자열) |
| score=0 표시 (sign 없이) | T6 (`else: score_part = " 0"`) |

스펙 §7 에러 케이스 → 모두 구현됨:
- 기존 row signal NULL → T7 (`signal_badge_html` 가 빈 문자열)
- `result["signal"]` 가 dict 아님 → T3/T4/T5 (`result.get("signal") or {}`)
- 정의 외 signal_value 문자열 → T6 (`_SIGNAL_CLASS.get(..., "signal-hold")`)
- score=0 표시 → T6
- ALL row signal NULL → T4 (ALL put 시 signal 매개변수 없이)
- 마이그레이션 멱등 → T1 (`_migrate` 의 PRAGMA 체크)

타입 일관성:
- `put` 시그니처 (T2) → T3/T4/T5 호출 모두 `signal_value=`, `signal_score=` keyword 사용 ✓
- `get` 반환 dict 새 키 (T2) → T7 카드 렌더가 `cache_row.get("signal_value")` / `.get("signal_score")` 동일 ✓
- `_render_signal_badge` 시그니처 (T6) → T7 호출 인자 일관 ✓
- `_SIGNAL_CLASS` 키 ("매수"/"매도"/"관망") → `generate_signal` 반환값 (technical_analysis.py:174-178) 와 일치 ✓

Placeholder 스캔: TBD/TODO/"add appropriate" 패턴 없음 ✓

스펙 §11 비목표 → plan 에 의도적으로 빠짐 (ML 합성, 5단계, 시그널 알림) ✓
