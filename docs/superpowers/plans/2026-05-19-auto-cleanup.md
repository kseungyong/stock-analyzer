# Auto Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 매일 KST 23:30 cron이 composite < -5가 7일 연속인 종목을 settings.yaml에서 자동 제거. ETF/portfolio/pinned/note 보호, safety limit 10종목.

**Architecture:** 신규 `composite_history` 테이블에 매 auto-analyze 시점 composite score 기록. 신규 `cleanup` 모듈이 7일 연속 약세 + 5개 조건 모두 만족하는 후보를 찾아 settings.yaml에서 제거하고 git commit/push.

**Tech Stack:** Python 3, SQLite, pyyaml, pytest, launchd

**Spec:** `docs/superpowers/specs/2026-05-19-auto-cleanup-design.md`

---

## File Structure

**Create:**
- `src/composite_history.py` — DB wrapper (init / insert / recent / purge_old)
- `src/cleanup.py` — ETF 식별, 조건 판정, settings.yaml 수정 + git commit
- `tests/test_composite_history.py` — composite_history 단위 테스트
- `tests/test_cleanup.py` — cleanup 단위 테스트

**Modify:**
- `main.py` — `composite_history.init_db()` 모듈 로드 시점 호출, `auto_analyze_market` 에 1줄 insert 추가, `cleanup` subcommand 추가
- `~/Library/LaunchAgents/ai.stock-analyzer.cleanup.plist` (macmini, git 추적 안 됨)

**Reference (수정 안 함, 참고만):**
- `src/analysis_cache.py` — DB connection 패턴
- `src/web_app.py:1470-1476` — `_composite_score` 공식 (Tech + BNF + Pattern×0.5)
- `src/web_app.py:2683-2697` — 기존 수동 삭제 (settings.yaml 수정 패턴)

---

## Task 1: composite_history DB wrapper

**Files:**
- Create: `src/composite_history.py`
- Test: `tests/test_composite_history.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_composite_history.py`:

```python
"""src/composite_history.py 단위 테스트."""
import time
import pytest
from src import composite_history as ch


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """매 테스트마다 임시 DB 파일 사용."""
    db = tmp_path / "test_predictions.db"
    monkeypatch.setattr(ch, "_DB_PATH", db)
    ch.init_db()
    yield


class TestCompositeHistory:
    def test_init_db_idempotent(self):
        ch.init_db()
        ch.init_db()  # 2회 호출도 OK

    def test_insert_and_recent(self):
        ch.insert("AAPL", -3.5)
        rows = ch.recent("AAPL", days=7)
        assert len(rows) == 1
        recorded_at, composite = rows[0]
        assert composite == pytest.approx(-3.5)
        assert recorded_at > int(time.time()) - 10

    def test_recent_returns_newest_first(self):
        now = int(time.time())
        ch.insert("AAPL", -1.0, recorded_at=now - 86400 * 3)
        ch.insert("AAPL", -2.0, recorded_at=now - 86400)
        ch.insert("AAPL", -3.0, recorded_at=now)
        rows = ch.recent("AAPL", days=7)
        assert [r[1] for r in rows] == [-3.0, -2.0, -1.0]

    def test_recent_respects_days_window(self):
        now = int(time.time())
        ch.insert("AAPL", -1.0, recorded_at=now - 86400 * 10)  # 10일 전 (창 밖)
        ch.insert("AAPL", -2.0, recorded_at=now - 86400 * 3)   # 3일 전 (창 안)
        rows = ch.recent("AAPL", days=7)
        assert len(rows) == 1
        assert rows[0][1] == pytest.approx(-2.0)

    def test_recent_empty_symbol(self):
        assert ch.recent("UNKNOWN", days=7) == []

    def test_insert_explicit_timestamp(self):
        ch.insert("AAPL", 1.5, recorded_at=1700000000)
        rows = ch.recent("AAPL", days=365 * 10)
        assert rows[0] == (1700000000, 1.5)

    def test_insert_same_timestamp_replaces(self):
        """PRIMARY KEY (symbol, recorded_at) — 동일 키는 덮어쓴다."""
        ch.insert("AAPL", -5.0, recorded_at=1700000000)
        ch.insert("AAPL", -6.0, recorded_at=1700000000)
        rows = ch.recent("AAPL", days=365 * 10)
        assert len(rows) == 1
        assert rows[0][1] == pytest.approx(-6.0)

    def test_purge_old_removes_only_old(self):
        now = int(time.time())
        ch.insert("AAPL", -1.0, recorded_at=now - 86400 * 100)  # 100일 전 — 삭제
        ch.insert("AAPL", -2.0, recorded_at=now - 86400 * 30)   # 30일 전 — 유지
        deleted = ch.purge_old(days=90)
        assert deleted == 1
        rows = ch.recent("AAPL", days=365)
        assert len(rows) == 1
        assert rows[0][1] == pytest.approx(-2.0)

    def test_purge_old_returns_zero_when_nothing_old(self):
        ch.insert("AAPL", -1.0)
        assert ch.purge_old(days=90) == 0
```

- [ ] **Step 2: Run tests to verify failure**

```bash
cd /Users/sykim/Projects/stock-analyzer
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_composite_history.py -v
```

Expected: 9 FAIL — `ImportError: No module named 'src.composite_history'`

- [ ] **Step 3: Implement composite_history**

Create `src/composite_history.py`:

```python
"""composite_history — 종목별 일일 composite score 시계열 저장.

cleanup 모듈이 '7일 연속 < -5' 같은 지속성 판정에 사용.
"""
from __future__ import annotations

import logging
import sqlite3
import time
import threading
from contextlib import closing
from pathlib import Path

logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "predictions.db"
_writer_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS composite_history (
    symbol       TEXT NOT NULL,
    recorded_at  INTEGER NOT NULL,
    composite    REAL NOT NULL,
    PRIMARY KEY (symbol, recorded_at)
);

CREATE INDEX IF NOT EXISTS idx_ch_symbol_date
    ON composite_history(symbol, recorded_at DESC);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    """스키마 적용. 멱등."""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _writer_lock:
        with closing(_connect()) as conn:
            conn.executescript(_SCHEMA)
    logger.info("composite_history DB 초기화 완료: %s", _DB_PATH)


def insert(symbol: str, composite: float, recorded_at: int | None = None) -> None:
    """recorded_at 생략 시 현재 시각. 동일 (symbol, recorded_at) 은 덮어쓴다."""
    ts = recorded_at if recorded_at is not None else int(time.time())
    with _writer_lock:
        with closing(_connect()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO composite_history "
                "(symbol, recorded_at, composite) VALUES (?, ?, ?)",
                (symbol, ts, float(composite)),
            )
            conn.commit()


def recent(symbol: str, days: int = 7) -> list[tuple[int, float]]:
    """최근 N일간 (recorded_at, composite) 리스트. 최신순."""
    cutoff = int(time.time()) - days * 86400
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT recorded_at, composite FROM composite_history "
            "WHERE symbol = ? AND recorded_at >= ? "
            "ORDER BY recorded_at DESC",
            (symbol, cutoff),
        ).fetchall()
    return [(int(r[0]), float(r[1])) for r in rows]


def purge_old(days: int = 90) -> int:
    """N일 이전 row 삭제. 삭제된 row 수 반환."""
    cutoff = int(time.time()) - days * 86400
    with _writer_lock:
        with closing(_connect()) as conn:
            cur = conn.execute(
                "DELETE FROM composite_history WHERE recorded_at < ?",
                (cutoff,),
            )
            conn.commit()
            return cur.rowcount
```

- [ ] **Step 4: Run tests to verify pass**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_composite_history.py -v
```

Expected: 9 PASS

- [ ] **Step 5: Commit**

```bash
git add src/composite_history.py tests/test_composite_history.py
git commit -m "feat(cleanup): composite_history DB wrapper (insert/recent/purge_old)"
```

---

## Task 2: composite_history.init_db() 호출 + auto_analyze_market 통합

**Files:**
- Modify: `main.py` (import + init_db + insert 호출)

- [ ] **Step 1: Add import and init_db at module load**

In `main.py`, find the existing block (around line 60-65):

```python
# 모듈 로드 시점에 1회 — DB 파일/스키마 보장
prediction_history.init_db()
analysis_cache.init_db()
from src import portfolio as _portfolio_init
_portfolio_init.init_db()
```

Add `composite_history.init_db()` and the import. Replace with:

```python
# 모듈 로드 시점에 1회 — DB 파일/스키마 보장
prediction_history.init_db()
analysis_cache.init_db()
from src import portfolio as _portfolio_init
_portfolio_init.init_db()
from src import composite_history as _composite_history
_composite_history.init_db()
```

- [ ] **Step 2: Add composite_history.insert call in auto_analyze_market**

In `main.py`, in `auto_analyze_market` function, find the block after `analysis_cache.put(...)` (around lines 252-253):

```python
                rel_perf_json=_json.dumps(result["rel_perf"], ensure_ascii=False)
                              if result.get("rel_perf") else None,
            )
            success += 1
```

Replace with (add composite_history.insert between put and success += 1):

```python
                rel_perf_json=_json.dumps(result["rel_perf"], ensure_ascii=False)
                              if result.get("rel_perf") else None,
            )
            # composite_history 기록 — cleanup 모듈의 7일 연속 판정용
            try:
                tech = (sig.get("score") or 0)
                bnf_sc = (bnf.get("score") or 0)
                pat_sc = (pat_summary.get("score") or 0)
                composite = float(tech) + float(bnf_sc) + float(pat_sc) * 0.5
                _composite_history.insert(s["symbol"], composite)
            except Exception as e:
                logger.warning("composite_history.insert 실패 (분석은 계속): %s", e)
            success += 1
```

- [ ] **Step 3: Verify py_compile**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m py_compile main.py
```

Expected: no output

- [ ] **Step 4: Smoke test — module import**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -c "
from src import composite_history
composite_history.init_db()
composite_history.insert('TEST', -1.5)
rows = composite_history.recent('TEST', days=1)
print('inserted:', rows)
# cleanup
import sqlite3
conn = sqlite3.connect(composite_history._DB_PATH)
conn.execute(\"DELETE FROM composite_history WHERE symbol='TEST'\")
conn.commit()
print('cleanup OK')
"
```

Expected:
```
inserted: [(<unix_ts>, -1.5)]
cleanup OK
```

- [ ] **Step 5: Commit**

```bash
git add main.py
git commit -m "feat(cleanup): auto_analyze_market에서 composite_history 기록"
```

---

## Task 3: cleanup 모듈 — is_etf + should_remove

**Files:**
- Create: `src/cleanup.py` (helper 함수만, apply는 Task 5)
- Test: `tests/test_cleanup.py`

- [ ] **Step 1: Write failing tests (Task 3 부분)**

Create `tests/test_cleanup.py`:

```python
"""src/cleanup.py 단위 테스트."""
import pytest
from src import cleanup


class TestIsEtf:
    def test_kodex_kr(self):
        assert cleanup.is_etf("069500.KS", "KODEX 200") is True

    def test_tiger_kr(self):
        assert cleanup.is_etf("102110.KS", "TIGER 200") is True

    def test_us_spy(self):
        assert cleanup.is_etf("SPY", "SPDR S&P 500") is True

    def test_us_qqq(self):
        assert cleanup.is_etf("QQQ", "Invesco QQQ") is True

    def test_us_koru(self):
        assert cleanup.is_etf("KORU", "Direxion Daily South Korea Bull") is True

    def test_regular_kr_stock(self):
        assert cleanup.is_etf("005930.KS", "삼성전자") is False

    def test_regular_us_stock(self):
        assert cleanup.is_etf("AAPL", "Apple") is False

    def test_arirang_kr(self):
        assert cleanup.is_etf("XXX.KS", "ARIRANG 신흥국MSCI") is True


class TestShouldRemove:
    """5개 조건 AND 판정."""
    def _rows_seven_days(self, value: float):
        """7개 row (값 모두 동일) 만들기."""
        import time
        now = int(time.time())
        return [(now - i * 86400, value) for i in range(7)]

    def test_all_seven_days_below_threshold(self):
        assert cleanup.should_remove(
            symbol="005930.KS", name="삼성전자",
            history_rows=self._rows_seven_days(-6.0),
            is_held=False, is_pinned_or_noted=False,
        ) is True

    def test_one_recovery_day_protects(self):
        rows = self._rows_seven_days(-6.0)
        rows[0] = (rows[0][0], 2.0)  # 최근 1일 +2 회복
        assert cleanup.should_remove(
            symbol="005930.KS", name="삼성전자",
            history_rows=rows,
            is_held=False, is_pinned_or_noted=False,
        ) is False

    def test_insufficient_history_protects(self):
        rows = self._rows_seven_days(-6.0)[:3]  # 3 rows만
        assert cleanup.should_remove(
            symbol="005930.KS", name="삼성전자",
            history_rows=rows,
            is_held=False, is_pinned_or_noted=False,
        ) is False

    def test_etf_protected(self):
        assert cleanup.should_remove(
            symbol="069500.KS", name="KODEX 200",
            history_rows=self._rows_seven_days(-7.0),
            is_held=False, is_pinned_or_noted=False,
        ) is False

    def test_portfolio_protected(self):
        assert cleanup.should_remove(
            symbol="005930.KS", name="삼성전자",
            history_rows=self._rows_seven_days(-6.0),
            is_held=True, is_pinned_or_noted=False,
        ) is False

    def test_pinned_protected(self):
        assert cleanup.should_remove(
            symbol="005930.KS", name="삼성전자",
            history_rows=self._rows_seven_days(-6.0),
            is_held=False, is_pinned_or_noted=True,
        ) is False

    def test_threshold_boundary_minus_5_protects(self):
        """composite == -5 는 threshold 미달 — 보호."""
        assert cleanup.should_remove(
            symbol="005930.KS", name="삼성전자",
            history_rows=self._rows_seven_days(-5.0),
            is_held=False, is_pinned_or_noted=False,
        ) is False
```

- [ ] **Step 2: Run tests to verify failure**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_cleanup.py::TestIsEtf tests/test_cleanup.py::TestShouldRemove -v
```

Expected: 15 FAIL — `ImportError: No module named 'src.cleanup'`

- [ ] **Step 3: Implement cleanup helpers (is_etf + should_remove only)**

Create `src/cleanup.py`:

```python
"""cleanup — 관심 가치 떨어진 종목 자동 정리.

조건 (모두 AND):
1. composite < -5 (Tech + BNF + Pattern×0.5)
2. 최근 7일 (5+ row) 모두 조건 1 만족
3. ETF/인덱스 아님
4. portfolio 보유 중 아님
5. settings.yaml 에 pinned 또는 note 없음
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_ETF_PREFIXES = ("KODEX", "TIGER", "ARIRANG", "KBSTAR", "HANARO")
_ETF_SYMBOLS = {
    "SPY", "QQQ", "VTI", "VOO", "IWM",
    "KORU", "QLD", "TECL", "USD", "SOXL", "TQQQ",
}

_COMPOSITE_THRESHOLD = -5.0
_MIN_HISTORY_ROWS = 5
_SAFETY_LIMIT = 10


def is_etf(symbol: str, name: str) -> bool:
    """ETF/인덱스 식별. symbol 화이트리스트 또는 name prefix 매칭."""
    sym_upper = symbol.upper()
    if sym_upper in _ETF_SYMBOLS:
        return True
    name_upper = name.upper()
    return any(name_upper.startswith(p) for p in _ETF_PREFIXES)


def should_remove(
    symbol: str,
    name: str,
    history_rows: list[tuple[int, float]],
    is_held: bool,
    is_pinned_or_noted: bool,
) -> bool:
    """5개 조건 AND 판정. True면 삭제 후보."""
    if is_etf(symbol, name):
        return False
    if is_held:
        return False
    if is_pinned_or_noted:
        return False
    if len(history_rows) < _MIN_HISTORY_ROWS:
        return False
    return all(composite < _COMPOSITE_THRESHOLD for _, composite in history_rows)
```

- [ ] **Step 4: Run tests to verify pass**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_cleanup.py::TestIsEtf tests/test_cleanup.py::TestShouldRemove -v
```

Expected: 15 PASS

- [ ] **Step 5: Commit**

```bash
git add src/cleanup.py tests/test_cleanup.py
git commit -m "feat(cleanup): is_etf + should_remove 5조건 판정"
```

---

## Task 4: find_candidates — settings.yaml 순회 + held set + history 조회

**Files:**
- Modify: `src/cleanup.py` (find_candidates 추가)
- Modify: `tests/test_cleanup.py` (TestFindCandidates 추가)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_cleanup.py`:

```python
import time
from src import composite_history as ch


@pytest.fixture
def _isolated_history_db(tmp_path, monkeypatch):
    """find_candidates 가 composite_history.recent 를 호출하므로 격리."""
    db = tmp_path / "test_predictions.db"
    monkeypatch.setattr(ch, "_DB_PATH", db)
    ch.init_db()
    yield ch


class TestFindCandidates:
    def _seed_history(self, ch_mod, symbol: str, value: float, days: int = 7):
        now = int(time.time())
        for i in range(days):
            ch_mod.insert(symbol, value, recorded_at=now - i * 86400)

    def test_finds_simple_candidate(self, _isolated_history_db):
        ch_mod = _isolated_history_db
        self._seed_history(ch_mod, "BAD.KS", -7.0, days=7)
        config = {
            "stocks": {"korea": [{"name": "잡주", "symbol": "BAD.KS"}]}
        }
        result = cleanup.find_candidates(config, held_symbols=set())
        assert len(result) == 1
        assert result[0]["symbol"] == "BAD.KS"
        assert result[0]["name"] == "잡주"
        assert result[0]["market"] == "korea"
        assert result[0]["composite_avg"] == pytest.approx(-7.0)
        assert result[0]["days"] == 7

    def test_excludes_etf(self, _isolated_history_db):
        ch_mod = _isolated_history_db
        self._seed_history(ch_mod, "069500.KS", -7.0, days=7)
        config = {
            "stocks": {"korea": [{"name": "KODEX 200", "symbol": "069500.KS"}]}
        }
        assert cleanup.find_candidates(config, held_symbols=set()) == []

    def test_excludes_held(self, _isolated_history_db):
        ch_mod = _isolated_history_db
        self._seed_history(ch_mod, "BAD.KS", -7.0, days=7)
        config = {
            "stocks": {"korea": [{"name": "잡주", "symbol": "BAD.KS"}]}
        }
        assert cleanup.find_candidates(config, held_symbols={"BAD.KS"}) == []

    def test_excludes_pinned(self, _isolated_history_db):
        ch_mod = _isolated_history_db
        self._seed_history(ch_mod, "BAD.KS", -7.0, days=7)
        config = {
            "stocks": {"korea": [
                {"name": "잡주", "symbol": "BAD.KS", "pinned": True}
            ]}
        }
        assert cleanup.find_candidates(config, held_symbols=set()) == []

    def test_excludes_noted(self, _isolated_history_db):
        ch_mod = _isolated_history_db
        self._seed_history(ch_mod, "BAD.KS", -7.0, days=7)
        config = {
            "stocks": {"korea": [
                {"name": "잡주", "symbol": "BAD.KS", "note": "장기 보유 의도"}
            ]}
        }
        assert cleanup.find_candidates(config, held_symbols=set()) == []

    def test_multi_market(self, _isolated_history_db):
        ch_mod = _isolated_history_db
        self._seed_history(ch_mod, "BAD.KS", -7.0, days=7)
        self._seed_history(ch_mod, "TRASH", -8.0, days=7)
        self._seed_history(ch_mod, "GOOD.KS", 5.0, days=7)
        config = {
            "stocks": {
                "korea": [
                    {"name": "잡주", "symbol": "BAD.KS"},
                    {"name": "좋은주", "symbol": "GOOD.KS"},
                ],
                "us": [
                    {"name": "쓰레기", "symbol": "TRASH"},
                ],
            }
        }
        result = cleanup.find_candidates(config, held_symbols=set())
        syms = sorted(c["symbol"] for c in result)
        assert syms == ["BAD.KS", "TRASH"]
```

- [ ] **Step 2: Run tests to verify failure**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_cleanup.py::TestFindCandidates -v
```

Expected: 6 FAIL — `AttributeError: module 'src.cleanup' has no attribute 'find_candidates'`

- [ ] **Step 3: Implement find_candidates**

In `src/cleanup.py`, append:

```python
def find_candidates(config: dict, held_symbols: set[str]) -> list[dict]:
    """settings.yaml 의 모든 종목을 검사하여 삭제 후보 list 반환.

    Args:
        config: yaml.safe_load 된 settings.yaml dict
        held_symbols: portfolio 보유 종목 symbol set

    Returns:
        [{"symbol", "name", "market", "composite_avg", "days"}, ...]
    """
    from src import composite_history

    candidates = []
    stocks_by_market = config.get("stocks", {})
    for market, group in stocks_by_market.items():
        for stock in group:
            symbol = stock["symbol"]
            name = stock["name"]
            is_held = symbol in held_symbols
            is_pinned_or_noted = bool(
                stock.get("pinned") or stock.get("note")
            )
            history_rows = composite_history.recent(symbol, days=7)
            if not should_remove(
                symbol, name, history_rows, is_held, is_pinned_or_noted,
            ):
                continue
            composites = [c for _, c in history_rows]
            candidates.append({
                "symbol": symbol,
                "name": name,
                "market": market,
                "composite_avg": sum(composites) / len(composites),
                "days": len(history_rows),
            })
    return candidates
```

- [ ] **Step 4: Run tests to verify pass**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_cleanup.py::TestFindCandidates -v
```

Expected: 6 PASS

- [ ] **Step 5: Commit**

```bash
git add src/cleanup.py tests/test_cleanup.py
git commit -m "feat(cleanup): find_candidates — settings.yaml 순회 + 후보 list 반환"
```

---

## Task 5: apply — settings.yaml 수정 + 로그 + git commit

**Files:**
- Modify: `src/cleanup.py` (apply 추가)
- Modify: `tests/test_cleanup.py` (TestApply 추가)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_cleanup.py`:

```python
import subprocess
from unittest.mock import MagicMock, patch


class TestApply:
    def _make_config_file(self, tmp_path):
        yaml_path = tmp_path / "settings.yaml"
        yaml_path.write_text(
            "stocks:\n"
            "  korea:\n"
            "    - name: 좋은주\n"
            "      symbol: GOOD.KS\n"
            "    - name: 잡주\n"
            "      symbol: BAD.KS\n",
            encoding="utf-8",
        )
        return yaml_path

    def test_dry_run_no_file_change(self, tmp_path):
        yaml_path = self._make_config_file(tmp_path)
        original = yaml_path.read_text(encoding="utf-8")
        log_path = tmp_path / "auto_remove.log"
        candidates = [
            {"symbol": "BAD.KS", "name": "잡주", "market": "korea",
             "composite_avg": -6.5, "days": 7},
        ]
        result = cleanup.apply(
            candidates, config_path=yaml_path, log_path=log_path,
            dry_run=True,
        )
        assert result["removed"] == 0
        assert result["dry_run"] is True
        assert yaml_path.read_text(encoding="utf-8") == original
        assert not log_path.exists()

    def test_apply_removes_and_writes_log(self, tmp_path):
        yaml_path = self._make_config_file(tmp_path)
        log_path = tmp_path / "auto_remove.log"
        candidates = [
            {"symbol": "BAD.KS", "name": "잡주", "market": "korea",
             "composite_avg": -6.5, "days": 7},
        ]
        with patch("src.cleanup._git_commit_push") as mock_git:
            mock_git.return_value = True
            result = cleanup.apply(
                candidates, config_path=yaml_path, log_path=log_path,
                dry_run=False,
            )
        assert result["removed"] == 1
        assert result["limited"] is False
        # settings.yaml — BAD.KS 제거 확인
        import yaml
        config = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        symbols = [s["symbol"] for s in config["stocks"]["korea"]]
        assert "GOOD.KS" in symbols
        assert "BAD.KS" not in symbols
        # 로그 1줄 추가 확인
        log_lines = log_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(log_lines) == 1
        assert "BAD.KS" in log_lines[0]
        assert "잡주" in log_lines[0]
        # git commit 호출 확인
        mock_git.assert_called_once()

    def test_safety_limit_aborts(self, tmp_path):
        yaml_path = self._make_config_file(tmp_path)
        log_path = tmp_path / "auto_remove.log"
        original = yaml_path.read_text(encoding="utf-8")
        # 11 candidates (>10 limit)
        candidates = [
            {"symbol": f"X{i}.KS", "name": f"잡주{i}", "market": "korea",
             "composite_avg": -6.0, "days": 7}
            for i in range(11)
        ]
        with patch("src.cleanup._git_commit_push") as mock_git:
            result = cleanup.apply(
                candidates, config_path=yaml_path, log_path=log_path,
                dry_run=False,
            )
        assert result["removed"] == 0
        assert result["limited"] is True
        # 파일 변경 없음 + git 호출 없음
        assert yaml_path.read_text(encoding="utf-8") == original
        assert not log_path.exists()
        mock_git.assert_not_called()

    def test_empty_candidates_noop(self, tmp_path):
        yaml_path = self._make_config_file(tmp_path)
        log_path = tmp_path / "auto_remove.log"
        original = yaml_path.read_text(encoding="utf-8")
        with patch("src.cleanup._git_commit_push") as mock_git:
            result = cleanup.apply(
                [], config_path=yaml_path, log_path=log_path, dry_run=False,
            )
        assert result["removed"] == 0
        assert yaml_path.read_text(encoding="utf-8") == original
        assert not log_path.exists()
        mock_git.assert_not_called()
```

- [ ] **Step 2: Run tests to verify failure**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_cleanup.py::TestApply -v
```

Expected: 4 FAIL — `AttributeError: module 'src.cleanup' has no attribute 'apply'`

- [ ] **Step 3: Implement apply + _git_commit_push helpers**

In `src/cleanup.py`, append:

```python
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def _git_commit_push(config_path: Path, log_path: Path,
                     candidates: list[dict]) -> bool:
    """git add + commit + push. push 실패는 warn only.

    Returns: commit 성공 여부 (push 무관).
    """
    repo_root = config_path.resolve().parent.parent
    try:
        body_lines = "\n".join(
            f"- {c['symbol']} {c['name']} (composite_avg={c['composite_avg']:.2f})"
            for c in candidates
        )
        msg = (
            f"chore(cleanup): 자동 제거 {len(candidates)}종목 "
            "(composite < -5, 7일 연속)\n\n"
            f"{body_lines}\n\n"
            "Triggered by: ai.stock-analyzer.cleanup launchd cron\n"
            "Restore: git revert <this-sha>"
        )
        subprocess.run(
            ["git", "add", str(config_path), str(log_path)],
            cwd=repo_root, check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", msg],
            cwd=repo_root, check=True,
        )
    except subprocess.CalledProcessError as e:
        logger.warning("cleanup git commit 실패: %s", e)
        return False
    try:
        subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=repo_root, check=True,
            timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        logger.warning("cleanup git push 실패 (로컬 commit 유지): %s", e)
    return True


def apply(
    candidates: list[dict],
    config_path: Path | str,
    log_path: Path | str,
    dry_run: bool = False,
) -> dict:
    """settings.yaml 수정 + 로그 + git commit.

    Returns: {"removed": N, "limited": bool, "dry_run": bool}
    """
    import yaml

    config_path = Path(config_path)
    log_path = Path(log_path)

    result = {"removed": 0, "limited": False, "dry_run": dry_run}

    if not candidates:
        return result

    if len(candidates) > _SAFETY_LIMIT:
        logger.warning(
            "cleanup safety limit 초과 (%d > %d) — abort. 의심스러운 대량 삭제 감지.",
            len(candidates), _SAFETY_LIMIT,
        )
        result["limited"] = True
        return result

    if dry_run:
        for c in candidates:
            logger.info(
                "[DRY-RUN] would remove: %s %s composite_avg=%.2f days=%d",
                c["symbol"], c["name"], c["composite_avg"], c["days"],
            )
        return result

    # settings.yaml 수정
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    remove_set = {c["symbol"] for c in candidates}
    for market in list(config.get("stocks", {}).keys()):
        config["stocks"][market] = [
            s for s in config["stocks"][market]
            if s["symbol"] not in remove_set
        ]
    config_path.write_text(
        yaml.dump(config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    # 로그 append
    now_kst = datetime.now(ZoneInfo("Asia/Seoul")).isoformat(timespec="seconds")
    log_lines = "\n".join(
        f"{now_kst}\t{c['symbol']}\t{c['name']}\t"
        f"composite_avg={c['composite_avg']:.2f}\tdays={c['days']}"
        for c in candidates
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(log_lines + "\n")

    # git commit + push
    _git_commit_push(config_path, log_path, candidates)
    result["removed"] = len(candidates)
    return result
```

- [ ] **Step 4: Run tests to verify pass**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_cleanup.py -v
```

Expected: all PASS (Task 3+4+5 모든 테스트)

- [ ] **Step 5: Commit**

```bash
git add src/cleanup.py tests/test_cleanup.py
git commit -m "feat(cleanup): apply — settings.yaml 수정 + 로그 + git commit/push + safety limit"
```

---

## Task 6: main.py cleanup subcommand

**Files:**
- Modify: `main.py` (argparse + handler)

- [ ] **Step 1: Add cleanup subcommand parser**

In `main.py` `main()` function, find the subparsers section (around line 380):

```python
    parser = argparse.ArgumentParser(description="주식시장 분석 시스템")
    subparsers = parser.add_subparsers(dest="command")

    # scan 서브커맨드
    scan_parser = subparsers.add_parser("scan", help="외인/기관 수급 스캐너")
```

Add cleanup subcommand definition. After the existing `subparsers.add_parser("leaders-refresh", ...)` line (around line 391-395):

Locate:
```python
    subparsers.add_parser("leaders-refresh", help="주도주 발굴 cron (launchd)")
```

Add after it:
```python
    cleanup_parser = subparsers.add_parser(
        "cleanup", help="자동 종목 정리 (composite < -5, 7일 연속)"
    )
    cleanup_parser.add_argument(
        "--dry-run", action="store_true",
        help="후보만 출력, 변경 없음",
    )
    cleanup_parser.add_argument(
        "--apply", action="store_true",
        help="실제 settings.yaml 수정 + git commit + push",
    )
```

- [ ] **Step 2: Add cleanup handler**

In `main.py`, find the section where subcommands are dispatched (around line 418-420):

```python
    if args.command == "leaders-refresh":
        sys.exit(leaders_refresh())
```

Add after it:
```python
    if args.command == "cleanup":
        from src import cleanup as _cleanup, composite_history as _ch
        from src import portfolio as _pf

        if not (args.dry_run or args.apply):
            print("cleanup: --dry-run 또는 --apply 중 하나 필요")
            sys.exit(2)

        # 90일 이전 history 정리
        purged = _ch.purge_old(days=90)
        logger.info("composite_history 정리: %d row 삭제", purged)

        # held symbols
        try:
            holdings = _pf.list_holdings_with_pnl("default")
            held = {h["symbol"] for h in holdings}
        except Exception as e:
            logger.warning("portfolio 조회 실패 — held 보호 비활성: %s", e)
            held = set()

        config = load_config()
        candidates = _cleanup.find_candidates(config, held)
        logger.info("cleanup 후보: %d 종목", len(candidates))

        log_path = Path(__file__).parent / "logs" / "auto_remove.log"
        result = _cleanup.apply(
            candidates,
            config_path=CONFIG_PATH,
            log_path=log_path,
            dry_run=args.dry_run,
        )
        logger.info("cleanup 결과: %s", result)
        return
```

- [ ] **Step 3: Verify py_compile**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m py_compile main.py
```

Expected: no output

- [ ] **Step 4: Dry-run smoke test (no changes)**

```bash
cd /Users/sykim/Projects/stock-analyzer
/Users/sykim/Projects/stock-analyzer/.venv/bin/python main.py cleanup --dry-run 2>&1 | tail -20
```

Expected: `cleanup 후보: 0 종목` 와 같은 로그 (로컬 mac엔 composite_history 데이터 없음). Settings.yaml 변경 없음 확인:

```bash
git status config/settings.yaml
```

Expected: `nothing to commit, working tree clean` (또는 무관 변경만)

- [ ] **Step 5: Commit**

```bash
git add main.py
git commit -m "feat(cleanup): main.py cleanup subcommand (--dry-run / --apply)"
```

---

## Task 7: launchd plist + 메모리 기록 (macmini)

**Files:**
- Create (macmini only, NOT git-tracked): `~/Library/LaunchAgents/ai.stock-analyzer.cleanup.plist`
- Modify: `/Users/sykim/.claude/projects/-Users-sykim/memory/reference_sykim_macmini.md`

- [ ] **Step 1: Push commits to origin**

```bash
git push origin main
```

Expected: `main -> main` push successful

- [ ] **Step 2: macmini pull**

```bash
ssh sykim-macmini "cd ~/Projects/stock-analyzer && git stash push -m s config/settings.yaml 2>/dev/null; git pull origin main 2>&1 | tail -3 && git stash pop 2>&1 | tail -2"
```

Expected: Fast-forward 갱신.

- [ ] **Step 3: Create cleanup plist on macmini**

Run this command on the local mac (ssh wraps):

```bash
ssh sykim-macmini "cat > ~/Library/LaunchAgents/ai.stock-analyzer.cleanup.plist << 'PLIST_EOF'
<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\">
<dict>
    <key>Label</key>
    <string>ai.stock-analyzer.cleanup</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/sykim/Projects/stock-analyzer/.venv/bin/python</string>
        <string>main.py</string>
        <string>cleanup</string>
        <string>--apply</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/sykim/Projects/stock-analyzer</string>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>23</integer>
        <key>Minute</key><integer>30</integer>
    </dict>
    <key>RunAtLoad</key><false/>
    <key>KeepAlive</key><false/>
    <key>StandardOutPath</key>
    <string>/Users/sykim/Projects/stock-analyzer/logs/cleanup.out.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/sykim/Projects/stock-analyzer/logs/cleanup.err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
</dict>
</plist>
PLIST_EOF
echo OK"
```

Expected: `OK` 출력

- [ ] **Step 4: Bootstrap launchd**

```bash
ssh sykim-macmini "launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/ai.stock-analyzer.cleanup.plist 2>&1 && launchctl list | grep cleanup"
```

Expected: `ai.stock-analyzer.cleanup` 잡 등장 (PID -).

- [ ] **Step 5: Dry-run test on macmini**

```bash
ssh sykim-macmini "cd ~/Projects/stock-analyzer && .venv/bin/python main.py cleanup --dry-run 2>&1 | tail -10"
```

Expected: `cleanup 후보: 0 종목` 또는 합리적 후보 수 (history 부족이라 보통 0). 에러 없음.

- [ ] **Step 6: Update memory reference**

Add to `/Users/sykim/.claude/projects/-Users-sykim/memory/reference_sykim_macmini.md` after the existing plist section. Use the Edit tool:

Find the section ending with:
```
**Trade-off**: 알파/signal/BNF/pattern 은 정상 계산, ML 예측만 `{"skipped": True}` dummy.
근본 fix (libomp+fork) 가 되면 두 SKIP_ 변수 제거.
```

Append after it:
```
## auto-cleanup cron (2026-05-19 추가)

**잡**: `ai.stock-analyzer.cleanup` — 매일 KST 23:30 자동 실행.

**역할**: composite < -5 가 7일 연속인 종목을 settings.yaml에서 자동 제거 + git commit/push.

**보호 종목**: ETF (KODEX/TIGER/SPY/QQQ/VTI 등), portfolio 보유, `pinned: true`/`note` 필드.

**Safety limit**: 한 번에 최대 10종목. 초과 시 abort.

**로그**: `logs/auto_remove.log` (제거 history), `logs/cleanup.{out,err}.log` (cron 실행).

**복원**: 잘못 제거됐을 경우 `git revert <commit-sha>` 또는 settings.yaml 수동 추가.

**plist 없으면 재생성** (macmini 재설치 시):
- 위치: `~/Library/LaunchAgents/ai.stock-analyzer.cleanup.plist`
- ProgramArguments: `python main.py cleanup --apply`
- StartCalendarInterval: Hour=23, Minute=30
- EnvironmentVariables: PATH (git 호출용), ML 환경변수 불필요
```

- [ ] **Step 7: Verify memory file syntax**

```bash
head -1 /Users/sykim/.claude/projects/-Users-sykim/memory/reference_sykim_macmini.md
# 첫 줄이 ---로 시작하는지 확인 (frontmatter)
```

Expected: `---` (frontmatter 유지)

---

## 완료 체크

- [ ] `composite_history` DB 스키마/insert/recent/purge_old 정상 동작
- [ ] `auto_analyze_market` 매 종목마다 composite_history 기록
- [ ] `cleanup.is_etf` 5가지 케이스 (한국 ETF, 미국 ETF, 일반 주식) 정상
- [ ] `cleanup.should_remove` 5조건 + threshold boundary + grace period 정상
- [ ] `cleanup.find_candidates` settings.yaml 순회 + 5가지 제외 사유 정상
- [ ] `cleanup.apply` dry-run/apply/safety_limit/empty 모두 정상
- [ ] `main.py cleanup --dry-run` smoke test 통과
- [ ] launchd `ai.stock-analyzer.cleanup` 잡 등록 + dry-run smoke test
- [ ] 메모리 `reference_sykim_macmini.md` 에 auto-cleanup 섹션 추가
- [ ] 모든 신규 테스트 PASS (composite_history 9개 + cleanup ~25개)
