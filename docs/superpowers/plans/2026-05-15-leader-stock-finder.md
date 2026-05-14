# Leader Stock Finder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** stock-analyzer 에 KOSPI200+KOSDAQ150 50종 universe 에서 "주도주 5조건" 부합 후보를 자동 발굴하고 종목별 상세 페이지(`/leaders` + `/leaders/<symbol>`) 를 제공하는 신규 모듈을 구축한다.

**Architecture:** 정량 hard filter (1·2·3번, yfinance) + 정성 LLM 분석 (4·5번, Gemini 2.5 Flash JSON) 의 hybrid. `predictions.db` 의 `leaders` 테이블 하나에 정량/LLM/사용자 수정본을 분리 저장. launchd cron 매일 16:30 KST.

**Tech Stack:** Python 3.11+, Flask, SQLite3, yfinance, google-generativeai (신규), pytest, jinja2.

**Spec:** `docs/superpowers/specs/2026-05-15-leader-stock-finder-design.md`

---

## Task 1: 의존성 + 환경변수 (스캐폴딩)

**Files:**
- Modify: `requirements.txt`
- Modify: `.env.example`

- [ ] **Step 1: requirements.txt 에 google-generativeai 추가**

`requirements.txt` 의 마지막 줄 다음에 추가:

```
google-generativeai>=0.8.0
```

- [ ] **Step 2: .env.example 에 신규 변수 3개 추가**

`.env.example` 의 마지막에 추가:

```
# Leader Stock Finder (주도주 발굴 페이지)
# Gemini 2.5 Flash API 키 (https://aistudio.google.com/apikey)
GEMINI_API_KEY=
# 일일 LLM 호출 cap (universe 50종 기준 5종 통과 가정 × 4배 여유)
LEADER_LLM_DAILY_LIMIT=20
# auto-trader universe.yaml 경로 (read-only 참조)
AUTO_TRADER_UNIVERSE_PATH=../auto-trader/config/universe.yaml
```

- [ ] **Step 3: 의존성 설치 + 검증**

Run: `.venv/bin/pip install -r requirements.txt && .venv/bin/python -c "import google.generativeai as genai; print(genai.__version__)"`
Expected: 버전 출력 (예: `0.8.x`)

- [ ] **Step 4: Commit**

```bash
git add requirements.txt .env.example
git commit -m "feat(leaders): add google-generativeai dependency + env vars"
```

---

## Task 2: leader_cache.py — SQLite 스키마 + CRUD (다른 모듈의 의존성)

**Files:**
- Create: `src/leader_cache.py`
- Test: `tests/test_leader_cache.py`

- [ ] **Step 1: Failing test 작성 — init_db 가 테이블 생성**

Create `tests/test_leader_cache.py`:

```python
"""leader_cache: SQLite CRUD + display 헬퍼."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from src import leader_cache


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """예측 DB 를 tmp_path 로 이동, init_db 호출."""
    p = tmp_path / "predictions.db"
    monkeypatch.setattr(leader_cache, "_DB_PATH", str(p))
    leader_cache.init_db()
    return p


def test_init_db_creates_leaders_table(db_path: Path):
    with sqlite3.connect(str(db_path)) as conn:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )]
    assert "leaders" in names
```

- [ ] **Step 2: Test 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_leader_cache.py::test_init_db_creates_leaders_table -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.leader_cache'`

- [ ] **Step 3: leader_cache.py 의 init_db + 스키마 구현**

Create `src/leader_cache.py`:

```python
"""leader_cache: SQLite 영속화 — 정량/LLM/사용자 수정본 분리 저장.

Spec §6: leaders 테이블 하나에 cond1/cond2 통과 여부, LLM 초안 (llm_*),
사용자 수정본 (user_*) 모두 저장. 표시 시 user_* 우선, NULL 이면 llm_* fallback.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

_DB_PATH = str(Path(__file__).parent.parent / "data" / "predictions.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS leaders (
    symbol              TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    market              TEXT NOT NULL,
    sector              TEXT,
    industry            TEXT,

    last_close          REAL NOT NULL,
    market_cap          INTEGER,
    market_cap_quintile INTEGER,
    near_high_pct       REAL,
    return_1y_pct       REAL,
    index_return_1y_pct REAL,
    rel_return_pp       REAL,
    trailing_eps        REAL,
    forward_eps         REAL,
    eps_growth_yoy      REAL,
    trailing_pe         REAL,
    pe_quintile         INTEGER,

    cond1_passed        BOOLEAN NOT NULL,
    cond2_passed        BOOLEAN NOT NULL,
    cond3_score         INTEGER,
    passed              BOOLEAN NOT NULL,

    llm_tam_narrative        TEXT,
    llm_narrative_expansion  TEXT,
    llm_bottleneck           TEXT,
    llm_moat                 TEXT,
    llm_raw_response         TEXT,
    llm_generated_at         INTEGER,
    llm_model                TEXT,
    llm_error                TEXT,

    user_tam_narrative       TEXT,
    user_narrative_expansion TEXT,
    user_bottleneck          TEXT,
    user_moat                TEXT,
    user_edited_at           INTEGER,
    user_edited_by           TEXT,

    status              TEXT NOT NULL DEFAULT 'active',
    is_stale            BOOLEAN NOT NULL DEFAULT 0,
    refreshed_at        INTEGER NOT NULL,
    created_at          INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_leaders_passed_status ON leaders(passed, status);
CREATE INDEX IF NOT EXISTS idx_leaders_market ON leaders(market);
"""

_LLM_FIELDS = ("tam_narrative", "narrative_expansion", "bottleneck", "moat")
_STALE_SECONDS = 7 * 24 * 60 * 60  # 7일


def init_db() -> None:
    Path(_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        conn.commit()
    logger.info("leader_cache DB 초기화 완료: %s", _DB_PATH)


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()
```

- [ ] **Step 4: Test pass 확인**

Run: `.venv/bin/python -m pytest tests/test_leader_cache.py -v`
Expected: PASS (1 test)

- [ ] **Step 5: list_active / get / upsert_quantitative 테스트 + 구현**

`tests/test_leader_cache.py` 에 추가:

```python
def _sample_candidate(symbol: str = "005930.KS", passed: bool = True) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "name": "삼성전자",
        "market": "KOSPI",
        "sector": "Tech",
        "industry": "Semiconductors",
        "last_close": 70000.0,
        "market_cap": 400_000_000_000_000,
        "market_cap_quintile": 1,
        "near_high_pct": 0.92,
        "return_1y_pct": 0.45,
        "index_return_1y_pct": 0.15,
        "rel_return_pp": 0.30,
        "trailing_eps": 5000.0,
        "forward_eps": 6000.0,
        "eps_growth_yoy": 0.2,
        "trailing_pe": 14.0,
        "pe_quintile": 3,
        "cond1_passed": passed,
        "cond2_passed": passed,
        "cond3_score": 3,
        "passed": passed,
    }


def test_upsert_quantitative_inserts_row(db_path: Path):
    leader_cache.upsert_quantitative([_sample_candidate()])
    rows = leader_cache.list_active()
    assert len(rows) == 1
    assert rows[0]["symbol"] == "005930.KS"
    assert rows[0]["passed"] == 1


def test_list_active_excludes_dropped(db_path: Path):
    leader_cache.upsert_quantitative([_sample_candidate()])
    leader_cache.mark_dropped(["005930.KS"])
    rows = leader_cache.list_active()
    assert rows == []


def test_get_returns_dropped_row(db_path: Path):
    leader_cache.upsert_quantitative([_sample_candidate()])
    leader_cache.mark_dropped(["005930.KS"])
    row = leader_cache.get("005930.KS")
    assert row is not None
    assert row["status"] == "dropped"
```

`src/leader_cache.py` 에 추가:

```python
def upsert_quantitative(candidates: list[dict[str, Any]]) -> None:
    """정량 컬럼 + cond_passed + meta(refreshed_at, status='active') 갱신.

    LLM 컬럼과 user_* 는 건드리지 않음 (UPSERT 의 SET 절 명시).
    """
    if not candidates:
        return
    now = int(time.time())
    cols = [
        "symbol", "name", "market", "sector", "industry",
        "last_close", "market_cap", "market_cap_quintile",
        "near_high_pct", "return_1y_pct", "index_return_1y_pct", "rel_return_pp",
        "trailing_eps", "forward_eps", "eps_growth_yoy", "trailing_pe", "pe_quintile",
        "cond1_passed", "cond2_passed", "cond3_score", "passed",
    ]
    placeholders = ",".join("?" * (len(cols) + 3))  # +status,refreshed_at,created_at
    sql_cols = ",".join(cols) + ",status,refreshed_at,created_at"
    set_clause = ",".join(f"{c}=excluded.{c}" for c in cols) + (
        ",status='active',refreshed_at=excluded.refreshed_at"
    )
    sql = (
        f"INSERT INTO leaders({sql_cols}) VALUES ({placeholders}) "
        f"ON CONFLICT(symbol) DO UPDATE SET {set_clause}"
    )
    with _connect() as conn:
        for c in candidates:
            params = [c[k] for k in cols] + ["active", now, now]
            conn.execute(sql, params)
        conn.commit()


def list_active() -> list[sqlite3.Row]:
    with _connect() as conn:
        return list(conn.execute(
            "SELECT * FROM leaders WHERE passed=1 AND status='active' "
            "ORDER BY rel_return_pp DESC NULLS LAST"
        ))


def get(symbol: str) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM leaders WHERE symbol=?", (symbol,)
        ).fetchone()


def mark_dropped(symbols: list[str]) -> None:
    if not symbols:
        return
    placeholders = ",".join("?" * len(symbols))
    with _connect() as conn:
        conn.execute(
            f"UPDATE leaders SET status='dropped' WHERE symbol IN ({placeholders})",
            symbols,
        )
        conn.commit()
```

- [ ] **Step 6: Run new tests, expect PASS**

Run: `.venv/bin/python -m pytest tests/test_leader_cache.py -v`
Expected: 4 passed

- [ ] **Step 7: LLM upsert + user fields + stale 테스트 + 구현**

`tests/test_leader_cache.py` 에 추가:

```python
def test_upsert_llm_preserves_user_fields(db_path: Path):
    leader_cache.upsert_quantitative([_sample_candidate()])
    leader_cache.update_user_fields(
        "005930.KS",
        {"tam_narrative": "사용자 메모"},
        "sykim",
    )
    leader_cache.upsert_llm("005930.KS", {
        "tam_narrative": "LLM 메모",
        "narrative_expansion": "LLM 확장",
        "bottleneck": "LLM 병목",
        "moat": "LLM 해자",
    }, model="gemini-2.5-flash", raw="{...}")
    row = leader_cache.get("005930.KS")
    assert row["user_tam_narrative"] == "사용자 메모"
    assert row["llm_tam_narrative"] == "LLM 메모"


def test_display_field_prefers_user(db_path: Path):
    leader_cache.upsert_quantitative([_sample_candidate()])
    leader_cache.upsert_llm("005930.KS", {
        "tam_narrative": "LLM",
        "narrative_expansion": "LLM",
        "bottleneck": "LLM",
        "moat": "LLM",
    }, model="gemini-2.5-flash", raw="")
    leader_cache.update_user_fields(
        "005930.KS", {"tam_narrative": "USER"}, "sykim"
    )
    row = leader_cache.get("005930.KS")
    assert leader_cache.display_field(row, "tam_narrative") == "USER"
    assert leader_cache.display_field(row, "moat") == "LLM"


def test_recompute_stale_marks_old_llm(db_path: Path):
    leader_cache.upsert_quantitative([_sample_candidate()])
    # 8일 전 LLM 분석
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "UPDATE leaders SET llm_generated_at=? WHERE symbol=?",
            (int(time.time()) - 8 * 86400, "005930.KS"),
        )
        conn.commit()
    leader_cache.recompute_stale()
    row = leader_cache.get("005930.KS")
    assert row["is_stale"] == 1
```

`src/leader_cache.py` 에 추가:

```python
def upsert_llm(
    symbol: str,
    fields: dict[str, str],
    *,
    model: str,
    raw: str,
    error: str | None = None,
) -> None:
    """LLM 4필드 + 메타 갱신. user_* 는 건드리지 않음."""
    now = int(time.time())
    with _connect() as conn:
        conn.execute(
            "UPDATE leaders SET "
            "llm_tam_narrative=?, llm_narrative_expansion=?, "
            "llm_bottleneck=?, llm_moat=?, "
            "llm_raw_response=?, llm_generated_at=?, llm_model=?, llm_error=?, "
            "is_stale=0 "
            "WHERE symbol=?",
            (
                fields.get("tam_narrative"),
                fields.get("narrative_expansion"),
                fields.get("bottleneck"),
                fields.get("moat"),
                raw, now, model, error,
                symbol,
            ),
        )
        conn.commit()


def update_user_fields(symbol: str, fields: dict[str, str], user: str) -> None:
    """사용자 수정 — 4 필드 중 명시된 것만 user_* 컬럼 덮어쓰기."""
    allowed = set(_LLM_FIELDS)
    sets: list[str] = []
    params: list[Any] = []
    for k, v in fields.items():
        if k not in allowed:
            raise ValueError(f"invalid field: {k}")
        sets.append(f"user_{k}=?")
        params.append(v)
    if not sets:
        return
    sets.extend(["user_edited_at=?", "user_edited_by=?"])
    params.extend([int(time.time()), user, symbol])
    with _connect() as conn:
        conn.execute(
            f"UPDATE leaders SET {','.join(sets)} WHERE symbol=?", params,
        )
        conn.commit()


def recompute_stale() -> None:
    """llm_generated_at 이 7일 초과면 is_stale=1."""
    threshold = int(time.time()) - _STALE_SECONDS
    with _connect() as conn:
        conn.execute(
            "UPDATE leaders SET is_stale=1 "
            "WHERE llm_generated_at IS NOT NULL AND llm_generated_at < ?",
            (threshold,),
        )
        conn.commit()


def display_field(row: sqlite3.Row, name: str) -> str:
    """user_<name> 우선, NULL 이면 llm_<name> fallback, 둘 다 NULL 이면 '(분석 대기 중)'."""
    if name not in _LLM_FIELDS:
        raise ValueError(f"invalid field: {name}")
    user_val = row[f"user_{name}"]
    if user_val:
        return str(user_val)
    llm_val = row[f"llm_{name}"]
    if llm_val:
        return str(llm_val)
    return "(분석 대기 중)"


def diff_with_existing(symbols: list[str]) -> dict[str, list[str]]:
    """이번 cron 의 통과 종목 vs 기존 row 비교.

    Returns:
        {"new": [...], "stale": [...], "kept": [...], "dropped": [...]}
    """
    with _connect() as conn:
        existing = {
            r["symbol"]: r for r in conn.execute(
                "SELECT symbol, llm_generated_at, status FROM leaders"
            )
        }
    new_set = set(symbols)
    threshold = int(time.time()) - _STALE_SECONDS
    result = {"new": [], "stale": [], "kept": [], "dropped": []}
    for sym in symbols:
        row = existing.get(sym)
        if row is None or row["llm_generated_at"] is None:
            result["new"].append(sym)
        elif row["llm_generated_at"] < threshold:
            result["stale"].append(sym)
        else:
            result["kept"].append(sym)
    for sym, row in existing.items():
        if sym not in new_set and row["status"] == "active":
            result["dropped"].append(sym)
    return result
```

- [ ] **Step 8: Run all leader_cache tests**

Run: `.venv/bin/python -m pytest tests/test_leader_cache.py -v`
Expected: 7 passed

- [ ] **Step 9: Commit**

```bash
git add src/leader_cache.py tests/test_leader_cache.py
git commit -m "feat(leaders): leader_cache SQLite CRUD + display 헬퍼 + 7일 stale"
```

---

## Task 3: leader_filter.py — 정량 hard filter

**Files:**
- Create: `src/leader_filter.py`
- Test: `tests/test_leader_filter.py`

- [ ] **Step 1: LeaderCandidate dataclass + universe loader 테스트**

Create `tests/test_leader_filter.py`:

```python
"""leader_filter: 정량 hard filter (1·2·3번)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

from src import leader_filter


def _make_universe_yaml(tmp_path: Path) -> Path:
    """auto-trader 형식 minimal yaml 생성 (etf 섹션 포함 — filter 가 제외해야)."""
    p = tmp_path / "universe.yaml"
    p.write_text("""kospi200:
  - "005930"  # 삼성전자
  - "000660"  # SK하이닉스
kosdaq150:
  - "247540"  # 에코프로비엠
etf:
  - "069500"  # KODEX 200
""", encoding="utf-8")
    return p


def test_load_universe_excludes_etf(tmp_path: Path):
    p = _make_universe_yaml(tmp_path)
    syms = leader_filter.load_universe(str(p))
    assert ("005930.KS", "KOSPI") in syms
    assert ("000660.KS", "KOSPI") in syms
    assert ("247540.KQ", "KOSDAQ") in syms
    # ETF must be excluded
    assert all(not s[0].startswith("069500") for s in syms)


def test_load_universe_missing_section_ok(tmp_path: Path):
    p = tmp_path / "u.yaml"
    p.write_text("kospi200:\n  - \"005930\"\n", encoding="utf-8")
    syms = leader_filter.load_universe(str(p))
    assert syms == [("005930.KS", "KOSPI")]
```

- [ ] **Step 2: Test 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_leader_filter.py::test_load_universe_excludes_etf -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: leader_filter.py 의 universe loader 구현**

Create `src/leader_filter.py`:

```python
"""leader_filter: 정량 hard filter (Spec §4.1).

universe.yaml 의 kospi200 + kosdaq150 섹션 합산. ETF 섹션 제외.
3 hard filter (가격 a/b/c) + 2번 (이익) 모두 통과해야 leader 후보.
3번 (PER) 은 점수만 산출, filter 아님.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import asdict, dataclass
from typing import Any

import yaml

logger = logging.getLogger(__name__)


@dataclass
class LeaderCandidate:
    symbol: str
    name: str
    market: str            # 'KOSPI' | 'KOSDAQ'
    sector: str | None
    industry: str | None
    last_close: float
    market_cap: int | None
    market_cap_quintile: int | None
    near_high_pct: float | None
    return_1y_pct: float | None
    index_return_1y_pct: float | None
    rel_return_pp: float | None
    trailing_eps: float | None
    forward_eps: float | None
    eps_growth_yoy: float | None
    trailing_pe: float | None
    pe_quintile: int | None
    cond1_passed: bool
    cond2_passed: bool
    cond3_score: int | None
    passed: bool

    def as_row(self) -> dict[str, Any]:
        return asdict(self)


def load_universe(path: str) -> list[tuple[str, str]]:
    """universe.yaml 파싱 → [(symbol_with_suffix, market), ...].

    auto-trader yaml 의 6자리 코드를 yfinance suffix 가 붙은 형식으로 변환:
      kospi200 → '.KS', kosdaq150 → '.KQ'. etf 섹션은 제외.
    """
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    out: list[tuple[str, str]] = []
    for code in data.get("kospi200") or []:
        out.append((f"{code}.KS", "KOSPI"))
    for code in data.get("kosdaq150") or []:
        out.append((f"{code}.KQ", "KOSDAQ"))
    return out
```

- [ ] **Step 4: Test pass 확인**

Run: `.venv/bin/python -m pytest tests/test_leader_filter.py -v`
Expected: 2 passed

- [ ] **Step 5: 시장 지수 1년 수익률 fetch — 테스트 + 구현**

`tests/test_leader_filter.py` 에 추가:

```python
def test_compute_index_return_uses_first_last_close(monkeypatch: pytest.MonkeyPatch):
    """^KS11 의 1년 수익률 = (마지막 종가 / 첫 종가) - 1."""
    fake_hist = pd.DataFrame(
        {"Close": [3000.0, 3500.0]},
        index=pd.to_datetime(["2025-05-15", "2026-05-15"]),
    )
    fake_ticker = MagicMock()
    fake_ticker.history.return_value = fake_hist
    monkeypatch.setattr(leader_filter.yf, "Ticker", lambda s: fake_ticker)
    r = leader_filter.compute_index_return("^KS11")
    assert math.isclose(r, 500.0 / 3000.0, rel_tol=1e-6)


def test_compute_index_return_returns_none_when_empty(monkeypatch: pytest.MonkeyPatch):
    fake_ticker = MagicMock()
    fake_ticker.history.return_value = pd.DataFrame()
    monkeypatch.setattr(leader_filter.yf, "Ticker", lambda s: fake_ticker)
    assert leader_filter.compute_index_return("^KS11") is None
```

`src/leader_filter.py` 에 추가:

```python
import yfinance as yf  # type: ignore


def compute_index_return(symbol: str) -> float | None:
    """yfinance Ticker.history(period='1y') 의 첫/마지막 종가 비율 - 1."""
    try:
        hist = yf.Ticker(symbol).history(period="1y", auto_adjust=True)
    except Exception as e:
        logger.warning("index history fetch 실패 %s: %s", symbol, e)
        return None
    if hist.empty or len(hist) < 2:
        return None
    first = float(hist["Close"].iloc[0])
    last = float(hist["Close"].iloc[-1])
    if first <= 0:
        return None
    return (last / first) - 1.0
```

- [ ] **Step 6: Run new tests**

Run: `.venv/bin/python -m pytest tests/test_leader_filter.py -v`
Expected: 4 passed

- [ ] **Step 7: 단일 종목 fundamentals + filter 통과 테스트**

`tests/test_leader_filter.py` 에 추가:

```python
def _fake_ticker(info: dict, hist_closes: list[float]) -> MagicMock:
    t = MagicMock()
    t.info = info
    t.history.return_value = pd.DataFrame(
        {"Close": hist_closes, "High": [c * 1.1 for c in hist_closes]},
        index=pd.date_range("2025-05-15", periods=len(hist_closes), freq="D"),
    )
    return t


def test_evaluate_passes_when_all_3_conds_met():
    info = {
        "longName": "삼성전자", "sector": "Tech", "industry": "Semi",
        "marketCap": 400_000_000_000_000,
        "trailingEps": 5000.0, "forwardEps": 6000.0,
        "earningsGrowth": 0.2, "revenueGrowth": 0.18,
        "trailingPE": 14.0,
    }
    closes = [60000.0] * 252 + [80000.0]  # 1년 +33%
    cand = leader_filter._evaluate_single(
        symbol="005930.KS", market="KOSPI",
        ticker=_fake_ticker(info, closes),
        index_return_1y=0.10,        # KOSPI 1년 +10%
        market_cap_quintile=1,       # 시총 상위 20%
        pe_quintile=3,
    )
    assert cand is not None
    assert cand.cond1_passed is True
    assert cand.cond2_passed is True
    assert cand.passed is True


def test_evaluate_fails_cond1_when_below_high():
    info = {
        "longName": "X", "marketCap": 1e14, "trailingEps": 100.0,
        "forwardEps": 110.0, "trailingPE": 12.0,
    }
    # 52주 신고가 100, 현재 종가 80 → 80/100 = 0.80 < 0.85
    closes = [50.0] + [60.0] * 250 + [80.0]
    # High max = 60 * 1.1 = 66 → 80/66 > 1 (last_close > high) 안 됨. high 직접 설정.
    t = _fake_ticker(info, closes)
    t.history.return_value["High"] = [100.0] * len(closes)
    cand = leader_filter._evaluate_single(
        symbol="X.KS", market="KOSPI", ticker=t,
        index_return_1y=0.10, market_cap_quintile=1, pe_quintile=3,
    )
    assert cand.cond1_passed is False
    assert cand.passed is False


def test_evaluate_fails_cond1_when_smaller_than_market_plus_20pp():
    info = {"longName": "X", "marketCap": 1e14, "trailingEps": 100.0,
            "forwardEps": 110.0, "trailingPE": 12.0}
    closes = [100.0] * 252 + [125.0]  # 1년 +25%
    t = _fake_ticker(info, closes)
    t.history.return_value["High"] = [130.0] * len(closes)  # 125/130 = 0.96
    cand = leader_filter._evaluate_single(
        symbol="X.KS", market="KOSPI", ticker=t,
        index_return_1y=0.10,         # +10% → +20pp 가산 = +30% 필요. +25% < +30%.
        market_cap_quintile=1, pe_quintile=3,
    )
    assert cand.cond1_passed is False


def test_evaluate_fails_cond1_when_market_cap_below_top20():
    info = {"longName": "X", "marketCap": 1e11, "trailingEps": 100.0,
            "forwardEps": 110.0, "trailingPE": 12.0}
    closes = [100.0] * 252 + [200.0]  # 1년 +100%
    t = _fake_ticker(info, closes)
    t.history.return_value["High"] = [200.0] * len(closes)
    cand = leader_filter._evaluate_single(
        symbol="X.KS", market="KOSPI", ticker=t,
        index_return_1y=0.10, market_cap_quintile=3, pe_quintile=3,
    )
    assert cand.cond1_passed is False


def test_evaluate_passes_cond2_with_forward_growth_only():
    info = {"longName": "X", "marketCap": 1e14,
            "trailingEps": -100.0, "forwardEps": 50.0,  # 적자 → 흑자 전환
            "trailingPE": -10.0}
    closes = [100.0] * 252 + [200.0]
    t = _fake_ticker(info, closes)
    t.history.return_value["High"] = [200.0] * len(closes)
    cand = leader_filter._evaluate_single(
        symbol="X.KS", market="KOSPI", ticker=t,
        index_return_1y=0.10, market_cap_quintile=1, pe_quintile=5,
    )
    assert cand.cond2_passed is True
    assert cand.passed is True


def test_evaluate_ignores_cond3_pe():
    """PER 높아도 1·2번 만족하면 통과 (사용자 요구사항: PER 무관)."""
    info = {"longName": "X", "marketCap": 1e14, "trailingEps": 100.0,
            "forwardEps": 110.0, "trailingPE": 200.0}  # 매우 높은 PER
    closes = [100.0] * 252 + [200.0]
    t = _fake_ticker(info, closes)
    t.history.return_value["High"] = [200.0] * len(closes)
    cand = leader_filter._evaluate_single(
        symbol="X.KS", market="KOSPI", ticker=t,
        index_return_1y=0.10, market_cap_quintile=1, pe_quintile=5,  # PE 분위 최하
    )
    assert cand.passed is True
    assert cand.pe_quintile == 5  # 표시는 됨
```

`src/leader_filter.py` 에 추가:

```python
_NEAR_HIGH_THRESHOLD = 0.85       # 신고가 -15% 이내
_REL_RETURN_THRESHOLD = 0.20      # 시장 대비 +20%p
_TOP_QUINTILE = 1                 # 시총 상위 20%


def _evaluate_single(
    *,
    symbol: str,
    market: str,
    ticker: Any,
    index_return_1y: float,
    market_cap_quintile: int | None,
    pe_quintile: int | None,
) -> LeaderCandidate | None:
    """단일 종목의 cond1/cond2/cond3 계산.

    Returns None 만약 데이터 부족으로 평가 불가 (price history empty).
    실패한 조건은 cond*_passed=False 로 반환, row 자체는 보존.
    """
    info = getattr(ticker, "info", {}) or {}
    try:
        hist = ticker.history(period="1y", auto_adjust=True)
    except Exception as e:
        logger.warning("history fetch 실패 %s: %s", symbol, e)
        return None
    if hist.empty or len(hist) < 2:
        logger.warning("history 데이터 부족 %s", symbol)
        return None

    closes = hist["Close"].astype(float)
    highs = hist["High"].astype(float)
    last_close = float(closes.iloc[-1])
    first_close = float(closes.iloc[0])
    high_52w = float(highs.max())

    near_high = (last_close / high_52w) if high_52w > 0 else None
    return_1y = (last_close / first_close - 1.0) if first_close > 0 else None
    rel_return = (
        (return_1y - index_return_1y) if (return_1y is not None) else None
    )

    market_cap = info.get("marketCap")
    trailing_eps = info.get("trailingEps")
    forward_eps = info.get("forwardEps")
    eps_growth = info.get("earningsGrowth")
    trailing_pe = info.get("trailingPE")

    # 1번 가격 (a) 신고가 + (b) 시장대비 + (c) 시총 상위20%
    cond1a = near_high is not None and near_high >= _NEAR_HIGH_THRESHOLD
    cond1b = rel_return is not None and rel_return >= _REL_RETURN_THRESHOLD
    cond1c = market_cap_quintile == _TOP_QUINTILE
    cond1 = bool(cond1a and cond1b and cond1c)

    # 2번 이익: trailingEps > 0 OR forwardEps > trailingEps
    eps_t = float(trailing_eps) if trailing_eps is not None else None
    eps_f = float(forward_eps) if forward_eps is not None else None
    cond2 = (
        (eps_t is not None and eps_t > 0)
        or (eps_t is not None and eps_f is not None and eps_f > eps_t)
    )

    # 3번 점수 (참고용, 1~5 — pe_quintile 그대로)
    cond3_score = pe_quintile

    return LeaderCandidate(
        symbol=symbol,
        name=str(info.get("longName") or info.get("shortName") or symbol),
        market=market,
        sector=info.get("sector"),
        industry=info.get("industry"),
        last_close=last_close,
        market_cap=int(market_cap) if market_cap is not None else None,
        market_cap_quintile=market_cap_quintile,
        near_high_pct=near_high,
        return_1y_pct=return_1y,
        index_return_1y_pct=index_return_1y,
        rel_return_pp=rel_return,
        trailing_eps=eps_t,
        forward_eps=eps_f,
        eps_growth_yoy=float(eps_growth) if eps_growth is not None else None,
        trailing_pe=float(trailing_pe) if trailing_pe is not None else None,
        pe_quintile=pe_quintile,
        cond1_passed=cond1,
        cond2_passed=cond2,
        cond3_score=cond3_score,
        passed=bool(cond1 and cond2),
    )
```

- [ ] **Step 8: Run all filter tests**

Run: `.venv/bin/python -m pytest tests/test_leader_filter.py -v`
Expected: 10 passed

- [ ] **Step 9: run_filter (전체 흐름) 테스트 + 구현**

`tests/test_leader_filter.py` 에 추가:

```python
def test_run_filter_assigns_market_cap_quintile_globally(monkeypatch: pytest.MonkeyPatch):
    """모집단: universe 전체 단일 컷오프 (시장 분리 X)."""
    # 4종 — 시총 4 tier. top 20% = quintile 1 = 시총 1위
    universe = [
        ("A.KS", "KOSPI"), ("B.KS", "KOSPI"),
        ("C.KQ", "KOSDAQ"), ("D.KQ", "KOSDAQ"),
    ]
    fundamentals = {
        "A.KS": {"marketCap": 400e12, "trailingEps": 100, "forwardEps": 110,
                 "trailingPE": 14, "longName": "A"},
        "B.KS": {"marketCap": 100e12, "trailingEps": 100, "forwardEps": 110,
                 "trailingPE": 14, "longName": "B"},
        "C.KQ": {"marketCap": 50e12, "trailingEps": 100, "forwardEps": 110,
                 "trailingPE": 14, "longName": "C"},
        "D.KQ": {"marketCap": 10e12, "trailingEps": 100, "forwardEps": 110,
                 "trailingPE": 14, "longName": "D"},
    }
    closes_pass = [100.0] * 252 + [200.0]  # +100%

    def mk(sym):
        t = MagicMock()
        t.info = fundamentals[sym]
        t.history.return_value = pd.DataFrame(
            {"Close": closes_pass, "High": [200.0] * 253},
            index=pd.date_range("2025-05-15", periods=253, freq="D"),
        )
        return t

    monkeypatch.setattr(leader_filter.yf, "Ticker", mk)
    monkeypatch.setattr(leader_filter, "compute_index_return", lambda s: 0.10)

    cands = leader_filter.run_filter(universe)
    by_sym = {c.symbol: c for c in cands}
    assert by_sym["A.KS"].market_cap_quintile == 1
    assert by_sym["D.KQ"].market_cap_quintile == 5 or by_sym["D.KQ"].market_cap_quintile == 4
    # Only A (top quintile) passes cond1c
    passed_syms = {c.symbol for c in cands if c.passed}
    assert passed_syms == {"A.KS"}
```

`src/leader_filter.py` 에 추가:

```python
def _quintile(values: list[float | None], descending: bool = True) -> dict[int, int]:
    """idx → 1..5 분위 (1 = 상위 20%, 5 = 하위 20%).

    None 은 5 (최하) 로 처리.
    """
    indexed = [(i, v) for i, v in enumerate(values)]
    indexed.sort(key=lambda x: (x[1] is None, -(x[1] or 0.0) if descending else (x[1] or 0.0)))
    n = len(indexed)
    result: dict[int, int] = {}
    for rank, (i, _) in enumerate(indexed):
        # rank 0..n-1 → 1..5 분위
        q = min(5, (rank * 5) // max(n, 1) + 1)
        result[i] = q
    return result


def run_filter(universe: list[tuple[str, str]]) -> list[LeaderCandidate]:
    """전체 흐름: 시장 지수 fetch → 종목별 info+price fetch → 분위 → cond 평가.

    Returns 모든 종목 (passed True/False 모두). 호출자가 passed 로 필터링.
    """
    # 1. 시장 지수 1년 수익률
    kospi_r = compute_index_return("^KS11") or 0.0
    kosdaq_r = compute_index_return("^KQ11") or 0.0

    # 2. 종목별 ticker 객체 + info 수집
    tickers: list[Any] = []
    market_caps: list[float | None] = []
    pes: list[float | None] = []
    skipped: list[str] = []
    for sym, market in universe:
        try:
            t = yf.Ticker(sym)
            _ = t.info  # 미리 fetch 트리거
            tickers.append(t)
            market_caps.append(t.info.get("marketCap"))
            pes.append(t.info.get("trailingPE"))
        except Exception as e:
            logger.warning("ticker fetch 실패 %s: %s", sym, e)
            tickers.append(None)
            market_caps.append(None)
            pes.append(None)
            skipped.append(sym)

    # 3. universe 전체 단일 분위 (시장 분리 X)
    mc_q = _quintile([float(v) if v is not None else None for v in market_caps], descending=True)
    pe_q = _quintile(
        [float(v) if v is not None else None for v in pes], descending=False
    )  # PE 작은 게 1분위

    # 4. 종목별 평가
    out: list[LeaderCandidate] = []
    for i, (sym, market) in enumerate(universe):
        if tickers[i] is None:
            continue
        idx_r = kospi_r if market == "KOSPI" else kosdaq_r
        c = _evaluate_single(
            symbol=sym, market=market, ticker=tickers[i],
            index_return_1y=idx_r,
            market_cap_quintile=mc_q.get(i),
            pe_quintile=pe_q.get(i),
        )
        if c is not None:
            out.append(c)

    skip_pct = len(skipped) / max(len(universe), 1)
    logger.info(
        "leader_filter: universe=%d, evaluated=%d, passed=%d, skipped=%d (%.0f%%)",
        len(universe), len(out), sum(1 for c in out if c.passed), len(skipped),
        skip_pct * 100,
    )
    if skip_pct > 0.10:
        raise RuntimeError(f"skip 률 {skip_pct:.0%} > 10% 임계 초과")
    return out
```

- [ ] **Step 10: Run all filter tests**

Run: `.venv/bin/python -m pytest tests/test_leader_filter.py -v`
Expected: 11 passed

- [ ] **Step 11: Commit**

```bash
git add src/leader_filter.py tests/test_leader_filter.py
git commit -m "feat(leaders): leader_filter 정량 hard filter (1·2·3번 조건)"
```

---

## Task 4: leader_llm.py — Gemini wrapper

**Files:**
- Create: `src/leader_llm.py`
- Test: `tests/test_leader_llm.py`

- [ ] **Step 1: 모듈 인터페이스 + JSON 파싱 happy path 테스트**

Create `tests/test_leader_llm.py`:

```python
"""leader_llm: Gemini 2.5 Flash wrapper + retry + daily limit."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from src import leader_llm


@pytest.fixture
def fake_genai(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """google.generativeai 를 통째로 mock 으로 교체."""
    mock = MagicMock()
    monkeypatch.setattr(leader_llm, "genai", mock)
    monkeypatch.setattr(leader_llm, "_get_model", lambda: mock.model)
    monkeypatch.setattr(leader_llm, "_daily_count", lambda: 0)
    monkeypatch.setattr(leader_llm, "_increment_daily_count", lambda: None)
    return mock


def _input(symbol: str = "005930.KS") -> dict:
    return {
        "symbol": symbol, "name": "삼성전자", "market": "KOSPI",
        "sector": "Tech", "industry": "Semi",
        "market_cap": 400_000_000_000_000,
        "return_1y_pct": 0.45, "rel_return_pp": 0.30,
        "trailing_eps": 5000.0, "forward_eps": 6000.0,
        "revenue_growth_pct": 0.18, "trailing_pe": 14.0,
    }


def test_analyze_one_returns_parsed_json(fake_genai: MagicMock):
    payload = {
        "tam_narrative": "글로벌 반도체 TAM 1조 달러",
        "narrative_expansion": "GPU→메모리→전력 확장",
        "bottleneck": "HBM 생산 capa",
        "moat": "EUV 노광 노하우",
    }
    resp = MagicMock()
    resp.text = json.dumps(payload, ensure_ascii=False)
    fake_genai.model.generate_content.return_value = resp

    result = leader_llm.analyze_one(_input())
    assert result.fields == payload
    assert result.error is None
    assert result.raw == resp.text
```

- [ ] **Step 2: Test 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_leader_llm.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: leader_llm.py 기본 구현**

Create `src/leader_llm.py`:

```python
"""leader_llm: Gemini 2.5 Flash 기반 정성 분석 wrapper.

Spec §4.2: 종목당 1회 호출 → strict JSON 4필드 (tam_narrative, narrative_expansion,
bottleneck, moat). retry 1회 + 2초 backoff. daily limit cap.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import google.generativeai as genai  # type: ignore

logger = logging.getLogger(__name__)

_MODEL_NAME = "gemini-2.5-flash"
_TIMEOUT_S = 30
_RETRY_BACKOFF_S = 2
_DAILY_COUNT_FILE = Path(__file__).parent.parent / "data" / ".leader_llm_count"

_SYSTEM_INSTRUCTION = (
    "당신은 주식 시장의 주도주 분석 전문가다. 입력으로 받은 한국 종목에 대해 "
    "4가지 정성 조건을 산출한다. 출력은 반드시 strict JSON, 다른 텍스트 금지. "
    "데이터 부족 시 추정 금지 — '데이터 부족' 명시. 마케팅 어조 금지, 사실 기반 "
    "분석만."
)

_PROMPT_TEMPLATE = """종목: {name} ({symbol})
시장: {market}, 섹터: {sector}, 산업: {industry}
시가총액: {market_cap:,}원, 1년 수익률: {return_1y_pct:.1%}, 시장지수 대비 +{rel_return_pp:.1%}p
trailing EPS: {trailing_eps}, forward EPS: {forward_eps}, 매출 성장률: {revenue_growth_pct:.1%}
trailing PE: {trailing_pe}

아래 4가지를 분석해 JSON 으로만 응답:

{{
  "tam_narrative": "이 회사가 속한 글로벌 산업의 TAM 규모와 성장 동인. 3~5문장.",
  "narrative_expansion": "이 회사 이야기가 인접 섹터로 확장 가능한가 (예: GPU→전력→메모리). 2~3문장.",
  "bottleneck": "산업 밸류체인 내 반드시 거쳐야 하는 구간을 점유하는가. 2~3문장.",
  "moat": "그 구간 내 경쟁자 진입 장벽 (기술/특허/규모/네트워크). 2~3문장."
}}
"""


@dataclass
class LLMResult:
    fields: dict[str, str]      # tam_narrative / narrative_expansion / bottleneck / moat
    raw: str                    # 원본 응답 (디버그)
    error: str | None           # None=성공, 'parse_failed'/'timeout'/'rate_limit'/'over_limit' etc


def _get_model():  # noqa: ANN202 — Gemini 객체 타입 안정 X
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY 환경변수 없음")
    genai.configure(api_key=api_key)
    return genai.GenerativeModel(
        model_name=_MODEL_NAME,
        system_instruction=_SYSTEM_INSTRUCTION,
    )


def _daily_count() -> int:
    if not _DAILY_COUNT_FILE.exists():
        return 0
    try:
        date, n = _DAILY_COUNT_FILE.read_text().strip().split(":")
    except ValueError:
        return 0
    if date != time.strftime("%Y-%m-%d"):
        return 0
    return int(n)


def _increment_daily_count() -> None:
    today = time.strftime("%Y-%m-%d")
    n = _daily_count() + 1
    _DAILY_COUNT_FILE.parent.mkdir(parents=True, exist_ok=True)
    _DAILY_COUNT_FILE.write_text(f"{today}:{n}")


def _daily_limit() -> int:
    return int(os.environ.get("LEADER_LLM_DAILY_LIMIT", "20"))


def _format_eps(v: float | None) -> str:
    return f"{v:.0f}" if v is not None else "데이터 없음"


def analyze_one(inputs: dict[str, Any]) -> LLMResult:
    """단일 종목 → Gemini 호출 → LLMResult."""
    if _daily_count() >= _daily_limit():
        logger.warning(
            "LEADER_LLM_DAILY_LIMIT (%d) 초과 — %s skip",
            _daily_limit(), inputs.get("symbol"),
        )
        return LLMResult(fields={}, raw="", error="over_limit")

    prompt = _PROMPT_TEMPLATE.format(
        name=inputs["name"],
        symbol=inputs["symbol"],
        market=inputs.get("market", ""),
        sector=inputs.get("sector") or "데이터 없음",
        industry=inputs.get("industry") or "데이터 없음",
        market_cap=int(inputs.get("market_cap") or 0),
        return_1y_pct=float(inputs.get("return_1y_pct") or 0.0),
        rel_return_pp=float(inputs.get("rel_return_pp") or 0.0),
        trailing_eps=_format_eps(inputs.get("trailing_eps")),
        forward_eps=_format_eps(inputs.get("forward_eps")),
        revenue_growth_pct=float(inputs.get("revenue_growth_pct") or 0.0),
        trailing_pe=_format_eps(inputs.get("trailing_pe")),
    )

    model = _get_model()
    gen_cfg = {
        "temperature": 0.3,
        "max_output_tokens": 1024,
        "response_mime_type": "application/json",
    }
    last_err: str | None = None
    last_raw = ""
    for attempt in range(2):
        try:
            resp = model.generate_content(
                prompt,
                generation_config=gen_cfg,
                request_options={"timeout": _TIMEOUT_S},
            )
            last_raw = getattr(resp, "text", "") or ""
            _increment_daily_count()
            try:
                fields = json.loads(last_raw)
            except json.JSONDecodeError:
                return LLMResult(fields={}, raw=last_raw, error="parse_failed")
            required = {"tam_narrative", "narrative_expansion", "bottleneck", "moat"}
            if not required.issubset(fields):
                return LLMResult(fields=fields, raw=last_raw, error="missing_fields")
            return LLMResult(
                fields={k: str(fields[k]) for k in required}, raw=last_raw, error=None
            )
        except Exception as e:
            last_err = str(e)
            logger.warning(
                "Gemini 호출 실패 (attempt %d/2) %s: %s", attempt + 1, inputs.get("symbol"), e
            )
            if attempt == 0:
                time.sleep(_RETRY_BACKOFF_S)
    return LLMResult(fields={}, raw=last_raw, error=last_err or "unknown")
```

- [ ] **Step 4: Test pass 확인**

Run: `.venv/bin/python -m pytest tests/test_leader_llm.py::test_analyze_one_returns_parsed_json -v`
Expected: 1 passed

- [ ] **Step 5: retry / parse_failed / daily_limit 테스트 + 검증**

`tests/test_leader_llm.py` 에 추가:

```python
def test_analyze_one_retries_on_exception_once(fake_genai: MagicMock):
    """첫 호출 실패 → 2초 backoff → 두 번째 호출 성공."""
    payload = {"tam_narrative": "x", "narrative_expansion": "x",
               "bottleneck": "x", "moat": "x"}
    success_resp = MagicMock()
    success_resp.text = json.dumps(payload)
    fake_genai.model.generate_content.side_effect = [
        Exception("503 Service Unavailable"),
        success_resp,
    ]
    # backoff sleep mock
    import time as time_mod
    sleeps: list[float] = []
    fake_genai.model.generate_content.reset_mock()
    fake_genai.model.generate_content.side_effect = [
        Exception("503"), success_resp,
    ]
    leader_llm.time = MagicMock(strftime=time_mod.strftime, sleep=lambda s: sleeps.append(s))
    try:
        result = leader_llm.analyze_one(_input())
    finally:
        leader_llm.time = time_mod
    assert result.error is None
    assert fake_genai.model.generate_content.call_count == 2
    assert 2 in sleeps  # 2초 backoff 호출


def test_analyze_one_marks_parse_failed_on_non_json(fake_genai: MagicMock):
    resp = MagicMock()
    resp.text = "이것은 JSON 이 아님"
    fake_genai.model.generate_content.return_value = resp
    result = leader_llm.analyze_one(_input())
    assert result.error == "parse_failed"
    assert result.raw == "이것은 JSON 이 아님"


def test_analyze_one_respects_daily_limit(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(leader_llm, "_daily_count", lambda: 999)
    monkeypatch.setenv("LEADER_LLM_DAILY_LIMIT", "20")
    result = leader_llm.analyze_one(_input())
    assert result.error == "over_limit"
```

- [ ] **Step 6: Run all LLM tests**

Run: `.venv/bin/python -m pytest tests/test_leader_llm.py -v`
Expected: 4 passed

- [ ] **Step 7: Commit**

```bash
git add src/leader_llm.py tests/test_leader_llm.py
git commit -m "feat(leaders): leader_llm Gemini 2.5 Flash wrapper + retry + daily limit"
```

---

## Task 5: main.py — leaders-refresh CLI 서브커맨드

**Files:**
- Modify: `main.py`
- Test: `tests/test_leaders_e2e.py` (e2e cron 흐름)

- [ ] **Step 1: e2e 테스트 작성 (cron 흐름)**

Create `tests/test_leaders_e2e.py`:

```python
"""leaders-refresh cron 흐름 end-to-end (fake yfinance + fake Gemini)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest


@pytest.fixture
def patched_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """leader_cache._DB_PATH + universe yaml + yfinance + Gemini 모두 patch."""
    from src import leader_cache, leader_filter, leader_llm

    db = tmp_path / "predictions.db"
    monkeypatch.setattr(leader_cache, "_DB_PATH", str(db))
    leader_cache.init_db()

    universe = tmp_path / "universe.yaml"
    universe.write_text(
        'kospi200:\n  - "005930"\n  - "000660"\nkosdaq150:\n  - "247540"\netf:\n  - "069500"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("AUTO_TRADER_UNIVERSE_PATH", str(universe))

    # fake yfinance
    def fake_ticker(sym):
        t = MagicMock()
        info_by_sym = {
            "005930.KS": {"longName": "삼성전자", "sector": "Tech", "industry": "Semi",
                          "marketCap": 400e12, "trailingEps": 5000.0,
                          "forwardEps": 6000.0, "trailingPE": 14.0,
                          "earningsGrowth": 0.2, "revenueGrowth": 0.18},
            "000660.KS": {"longName": "SK하이닉스", "sector": "Tech", "industry": "Semi",
                          "marketCap": 100e12, "trailingEps": 3000.0,
                          "forwardEps": 4000.0, "trailingPE": 12.0,
                          "earningsGrowth": 0.3, "revenueGrowth": 0.25},
            "247540.KQ": {"longName": "에코프로비엠", "sector": "Materials",
                          "industry": "Battery", "marketCap": 10e12,
                          "trailingEps": -100.0, "forwardEps": 200.0,
                          "trailingPE": -50.0},
        }
        if sym in ("^KS11", "^KQ11"):
            t.history.return_value = pd.DataFrame(
                {"Close": [3000.0] * 252 + [3300.0]},
                index=pd.date_range("2025-05-15", periods=253, freq="D"),
            )
            return t
        t.info = info_by_sym.get(sym, {})
        closes = [100.0] * 252 + [200.0]  # +100%
        t.history.return_value = pd.DataFrame(
            {"Close": closes, "High": [200.0] * 253},
            index=pd.date_range("2025-05-15", periods=253, freq="D"),
        )
        return t

    monkeypatch.setattr(leader_filter.yf, "Ticker", fake_ticker)

    # fake Gemini
    payload = json.dumps({
        "tam_narrative": "T", "narrative_expansion": "N",
        "bottleneck": "B", "moat": "M",
    })
    fake_model = MagicMock()
    fake_resp = MagicMock()
    fake_resp.text = payload
    fake_model.generate_content.return_value = fake_resp
    monkeypatch.setattr(leader_llm, "_get_model", lambda: fake_model)
    monkeypatch.setattr(leader_llm, "_daily_count", lambda: 0)
    monkeypatch.setattr(leader_llm, "_increment_daily_count", lambda: None)

    return {"db": db, "universe": universe}


def test_leaders_refresh_e2e(patched_runtime, monkeypatch: pytest.MonkeyPatch):
    """e2e: load universe → filter → LLM analyze → cache upsert."""
    import main
    main.leaders_refresh()

    from src import leader_cache
    rows = leader_cache.list_active()
    # 삼성전자 (시총 1위, +100% > KOSPI +10% +20%p) 통과해야
    syms = {r["symbol"] for r in rows}
    assert "005930.KS" in syms
    samsung = next(r for r in rows if r["symbol"] == "005930.KS")
    assert samsung["llm_tam_narrative"] == "T"
    assert samsung["llm_moat"] == "M"
```

- [ ] **Step 2: Test 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_leaders_e2e.py -v`
Expected: FAIL — `AttributeError: module 'main' has no attribute 'leaders_refresh'`

- [ ] **Step 3: main.py 에 leaders_refresh 함수 + 서브커맨드 추가**

`main.py` 의 `subparsers.add_parser("daily-email", ...)` 다음에 추가:

```python
    subparsers.add_parser("leaders-refresh", help="주도주 발굴 cron (launchd)")
```

`main.py` 의 `if args.command == "daily-email":` 블록 다음에 추가:

```python
    if args.command == "leaders-refresh":
        leaders_refresh()
        return
```

`main.py` 의 `daily_email_job()` 함수 다음에 추가:

```python
def leaders_refresh() -> None:
    """주도주 발굴 cron 진입점 (Spec §5 Cron 흐름).

    1. universe.yaml 파싱
    2. leader_filter.run_filter → 정량 평가
    3. leader_cache.diff_with_existing → 신규/유지/stale/탈락
    4. leader_llm.analyze_one(신규 + stale) 순차 호출
    5. leader_cache.upsert_all (user_* 보존)
    6. mark_dropped + recompute_stale
    """
    import os
    from src import leader_cache, leader_filter, leader_llm

    leader_cache.init_db()
    path = os.environ.get(
        "AUTO_TRADER_UNIVERSE_PATH", "../auto-trader/config/universe.yaml"
    )
    logger.info("leaders-refresh 시작: universe=%s", path)
    universe = leader_filter.load_universe(path)
    candidates = leader_filter.run_filter(universe)
    passed = [c for c in candidates if c.passed]
    rows = [c.as_row() for c in candidates]
    leader_cache.upsert_quantitative(rows)

    passed_syms = [c.symbol for c in passed]
    diff = leader_cache.diff_with_existing(passed_syms)
    to_llm = diff["new"] + diff["stale"]
    by_sym = {c.symbol: c for c in passed}

    llm_calls = 0
    llm_errors = 0
    for sym in to_llm:
        c = by_sym.get(sym)
        if c is None:
            continue
        inputs = {
            "symbol": c.symbol, "name": c.name, "market": c.market,
            "sector": c.sector, "industry": c.industry,
            "market_cap": c.market_cap or 0,
            "return_1y_pct": c.return_1y_pct or 0.0,
            "rel_return_pp": c.rel_return_pp or 0.0,
            "trailing_eps": c.trailing_eps, "forward_eps": c.forward_eps,
            "revenue_growth_pct": c.eps_growth_yoy or 0.0,
            "trailing_pe": c.trailing_pe,
        }
        result = leader_llm.analyze_one(inputs)
        llm_calls += 1
        if result.error:
            llm_errors += 1
            leader_cache.upsert_llm(
                sym, {}, model="gemini-2.5-flash",
                raw=result.raw, error=result.error,
            )
        else:
            leader_cache.upsert_llm(
                sym, result.fields, model="gemini-2.5-flash", raw=result.raw,
            )

    leader_cache.mark_dropped(diff["dropped"])
    leader_cache.recompute_stale()
    logger.info(
        "leaders-refresh 완료: passed=%d llm_calls=%d errors=%d dropped=%d",
        len(passed), llm_calls, llm_errors, len(diff["dropped"]),
    )
```

- [ ] **Step 4: Test pass 확인**

Run: `.venv/bin/python -m pytest tests/test_leaders_e2e.py -v`
Expected: 1 passed

- [ ] **Step 5: CLI 매뉴얼 확인**

Run: `.venv/bin/python main.py --help 2>&1 | grep leaders-refresh`
Expected: `leaders-refresh   주도주 발굴 cron (launchd)`

- [ ] **Step 6: Commit**

```bash
git add main.py tests/test_leaders_e2e.py
git commit -m "feat(leaders): main.py leaders-refresh 서브커맨드 + e2e 테스트"
```

---

## Task 6: web_app.py — Flask 라우트 4개 + _current_username 헬퍼

**Files:**
- Modify: `src/web_app.py`
- Test: `tests/test_leaders_routes.py`

- [ ] **Step 1: 라우트 테스트 — GET /leaders (목록)**

Create `tests/test_leaders_routes.py`:

```python
"""leaders Flask 라우트 4개."""
from __future__ import annotations

from pathlib import Path

import pytest

from src import leader_cache


@pytest.fixture
def app_with_leader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """web_app + leader_cache 의 DB 를 tmp_path 로."""
    db = tmp_path / "predictions.db"
    monkeypatch.setattr(leader_cache, "_DB_PATH", str(db))
    leader_cache.init_db()
    # sample row
    leader_cache.upsert_quantitative([{
        "symbol": "005930.KS", "name": "삼성전자", "market": "KOSPI",
        "sector": "Tech", "industry": "Semi",
        "last_close": 70000.0, "market_cap": 400e12,
        "market_cap_quintile": 1, "near_high_pct": 0.92,
        "return_1y_pct": 0.45, "index_return_1y_pct": 0.15,
        "rel_return_pp": 0.30,
        "trailing_eps": 5000.0, "forward_eps": 6000.0,
        "eps_growth_yoy": 0.2, "trailing_pe": 14.0, "pe_quintile": 3,
        "cond1_passed": True, "cond2_passed": True, "cond3_score": 3,
        "passed": True,
    }])
    leader_cache.upsert_llm("005930.KS", {
        "tam_narrative": "T", "narrative_expansion": "N",
        "bottleneck": "B", "moat": "M",
    }, model="gemini-2.5-flash", raw="{}")

    from src import web_app
    web_app.app.config["TESTING"] = True
    monkeypatch.setenv("ENABLE_BASIC_AUTH", "0")
    with web_app.app.test_client() as client:
        yield client


def test_get_leaders_lists_passed_active(app_with_leader):
    r = app_with_leader.get("/leaders")
    assert r.status_code == 200
    assert "삼성전자".encode() in r.data


def test_get_leaders_detail_renders_5_axis(app_with_leader):
    r = app_with_leader.get("/leaders/005930.KS")
    assert r.status_code == 200
    body = r.data.decode()
    # 5축 + LLM 4필드 표시
    assert "삼성전자" in body
    assert "T" in body and "N" in body and "B" in body and "M" in body
```

- [ ] **Step 2: Test 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_leaders_routes.py::test_get_leaders_lists_passed_active -v`
Expected: FAIL — `404 NOT FOUND`

- [ ] **Step 3: web_app.py 에 라우트 + 헬퍼 추가**

`src/web_app.py` 의 `/api/universe/<path:symbol>` DELETE 라우트 다음에 추가:

```python
# ─── 주도주 발굴 (Leader Stock Finder, Spec 2026-05-15) ─────────────────────


def _current_username() -> str:
    """Session 인증 username 또는 Basic Auth username, 둘 다 없으면 'anonymous'."""
    from flask import session, request
    if session.get("username"):
        return str(session["username"])
    if request.authorization and request.authorization.username:
        return str(request.authorization.username)
    return "anonymous"


@app.route("/leaders")
def leaders_list():
    from src import leader_cache
    rows = leader_cache.list_active()
    return render_template("leaders.html", rows=rows)


@app.route("/leaders/<path:symbol>")
def leaders_detail(symbol: str):
    from src import leader_cache
    from src.validators import sanitize_stock_symbol, validate_stock_symbol
    symbol = sanitize_stock_symbol(symbol)
    if not validate_stock_symbol(symbol):
        return jsonify({"error": "invalid_symbol", "symbol": symbol}), 400
    row = leader_cache.get(symbol)
    if row is None:
        return render_template("leader_detail.html", row=None, symbol=symbol), 404
    return render_template(
        "leader_detail.html", row=row, symbol=symbol,
        display=leader_cache.display_field,
    )


@app.route("/leaders/<path:symbol>/edit", methods=["POST"])
def leaders_edit(symbol: str):
    _csrf_validate()
    from src import leader_cache
    from src.validators import sanitize_stock_symbol, validate_stock_symbol
    symbol = sanitize_stock_symbol(symbol)
    if not validate_stock_symbol(symbol):
        return jsonify({"error": "invalid_symbol"}), 400

    fields: dict[str, str] = {}
    for k in ("tam_narrative", "narrative_expansion", "bottleneck", "moat"):
        v = request.form.get(k)
        if v is not None and v.strip():
            fields[k] = v.strip()
    if not fields:
        return jsonify({"error": "no_fields"}), 400
    leader_cache.update_user_fields(symbol, fields, _current_username())
    return redirect(url_for("leaders_detail", symbol=symbol))


@app.route("/leaders/<path:symbol>/refresh", methods=["POST"])
def leaders_refresh_one(symbol: str):
    _csrf_validate()
    from src import leader_cache, leader_llm
    from src.validators import sanitize_stock_symbol, validate_stock_symbol
    symbol = sanitize_stock_symbol(symbol)
    if not validate_stock_symbol(symbol):
        return jsonify({"error": "invalid_symbol"}), 400
    row = leader_cache.get(symbol)
    if row is None:
        return jsonify({"error": "not_found"}), 404
    inputs = {
        "symbol": row["symbol"], "name": row["name"], "market": row["market"],
        "sector": row["sector"], "industry": row["industry"],
        "market_cap": row["market_cap"] or 0,
        "return_1y_pct": row["return_1y_pct"] or 0.0,
        "rel_return_pp": row["rel_return_pp"] or 0.0,
        "trailing_eps": row["trailing_eps"], "forward_eps": row["forward_eps"],
        "revenue_growth_pct": row["eps_growth_yoy"] or 0.0,
        "trailing_pe": row["trailing_pe"],
    }
    result = leader_llm.analyze_one(inputs)
    if result.error:
        leader_cache.upsert_llm(
            symbol, {}, model="gemini-2.5-flash", raw=result.raw, error=result.error,
        )
    else:
        leader_cache.upsert_llm(
            symbol, result.fields, model="gemini-2.5-flash", raw=result.raw,
        )
    return redirect(url_for("leaders_detail", symbol=symbol))
```

(필요한 import 가 web_app.py 상단에 이미 다 있어야 함: `jsonify`, `redirect`, `url_for`, `render_template`, `request`. 누락 시 추가.)

- [ ] **Step 4: 두 템플릿 minimal 작성**

Create `src/templates/leaders.html`:

```html
<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>주도주 발굴 (Leaders)</title>
<style>
body { font-family: -apple-system, sans-serif; margin: 2em; }
table { border-collapse: collapse; width: 100%; }
th, td { padding: 8px 12px; border-bottom: 1px solid #ddd; text-align: right; }
th:first-child, td:first-child { text-align: left; }
.stale { color: #c97; font-size: 0.85em; }
.user { color: #2a7; font-size: 0.85em; }
</style>
</head>
<body>
<h1>주도주 발굴 — {{ rows|length }}종</h1>
<p>주도주 5조건 hard filter 통과 + Gemini LLM 분석 후보 (KOSPI200 + KOSDAQ150 사용자 선별 50종 풀)</p>
<table>
<thead><tr>
  <th>종목</th><th>시장</th><th>종가</th><th>1년 수익률</th>
  <th>시장 대비</th><th>신고가</th><th>EPS</th><th>PE</th><th>LLM</th>
</tr></thead>
<tbody>
{% for r in rows %}
<tr>
  <td><a href="/leaders/{{ r.symbol }}">{{ r.name }}</a><br><small>{{ r.symbol }}</small></td>
  <td>{{ r.market }}</td>
  <td>{{ '{:,.0f}'.format(r.last_close) }}</td>
  <td>{{ '{:+.1%}'.format(r.return_1y_pct) if r.return_1y_pct is not none else '-' }}</td>
  <td>{{ '{:+.1%}p'.format(r.rel_return_pp) if r.rel_return_pp is not none else '-' }}</td>
  <td>{{ '{:.0%}'.format(r.near_high_pct) if r.near_high_pct is not none else '-' }}</td>
  <td>{{ '{:,.0f}'.format(r.trailing_eps) if r.trailing_eps is not none else '-' }}</td>
  <td>{{ '{:.1f}'.format(r.trailing_pe) if r.trailing_pe is not none else '-' }}</td>
  <td>
    {% if r.is_stale %}<span class="stale">stale</span>{% endif %}
    {% if r.user_edited_at %}<span class="user">edited</span>{% endif %}
  </td>
</tr>
{% endfor %}
</tbody>
</table>
</body>
</html>
```

Create `src/templates/leader_detail.html`:

```html
<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>{{ row.name if row else symbol }} — 주도주 분석</title>
<style>
body { font-family: -apple-system, sans-serif; margin: 2em; max-width: 800px; }
.score-card { display: grid; grid-template-columns: repeat(5, 1fr); gap: 1em; margin: 1em 0; }
.axis { padding: 1em; border: 1px solid #ccc; border-radius: 6px; text-align: center; }
.axis.pass { background: #efe; }
.axis.fail { background: #fee; }
.axis.score { background: #eef; }
.narrative { margin-top: 2em; }
.narrative h3 { margin-bottom: 0.3em; }
.narrative .source { font-size: 0.8em; color: #888; }
textarea { width: 100%; min-height: 4em; font-family: inherit; }
button { padding: 0.4em 1em; margin-top: 0.5em; cursor: pointer; }
.stale-warn { background: #fc6; padding: 0.5em; }
</style>
</head>
<body>
{% if row is none %}
  <h1>{{ symbol }} — 해당 종목 없음</h1>
  <p><a href="/leaders">← 목록으로</a></p>
{% else %}
<p><a href="/leaders">← 목록으로</a></p>
<h1>{{ row.name }} ({{ row.symbol }})</h1>
<p>{{ row.market }} / {{ row.sector or '-' }} / {{ row.industry or '-' }}</p>

{% if row.is_stale %}
<p class="stale-warn">⚠ LLM 분석이 7일 이상 지났습니다.</p>
{% endif %}

<div class="score-card">
  <div class="axis {{ 'pass' if row.cond1_passed else 'fail' }}">
    <strong>1. 가격</strong><br>
    {{ '신고가 ' + ('{:.0%}'.format(row.near_high_pct)) if row.near_high_pct else '-' }}<br>
    {{ '시장 ' + ('{:+.1%}p'.format(row.rel_return_pp)) if row.rel_return_pp is not none else '-' }}<br>
    시총 {{ row.market_cap_quintile }}분위
  </div>
  <div class="axis {{ 'pass' if row.cond2_passed else 'fail' }}">
    <strong>2. 이익</strong><br>
    EPS {{ '{:,.0f}'.format(row.trailing_eps) if row.trailing_eps is not none else '-' }}<br>
    forward {{ '{:,.0f}'.format(row.forward_eps) if row.forward_eps is not none else '-' }}
  </div>
  <div class="axis score">
    <strong>3. 밸류</strong><br>
    PE {{ '{:.1f}'.format(row.trailing_pe) if row.trailing_pe is not none else '-' }}<br>
    {{ row.pe_quintile }}분위 (참고용)
  </div>
  <div class="axis {{ 'pass' if row.llm_tam_narrative else 'fail' }}">
    <strong>4. 글로벌 트렌드</strong><br>
    TAM / 내러티브
  </div>
  <div class="axis {{ 'pass' if row.llm_bottleneck else 'fail' }}">
    <strong>5. 병목·해자</strong>
  </div>
</div>

<form action="/leaders/{{ row.symbol }}/edit" method="post" class="narrative">
  <input type="hidden" name="csrf_token" value="{{ session.csrf_token }}">

  <div><h3>TAM / 글로벌 트렌드 (4번-a)</h3>
    <p class="source">{{ '사용자 수정' if row.user_tam_narrative else 'LLM 초안' }}</p>
    <textarea name="tam_narrative">{{ display(row, 'tam_narrative') }}</textarea></div>

  <div><h3>내러티브 확장성 (4번-b)</h3>
    <p class="source">{{ '사용자 수정' if row.user_narrative_expansion else 'LLM 초안' }}</p>
    <textarea name="narrative_expansion">{{ display(row, 'narrative_expansion') }}</textarea></div>

  <div><h3>병목 (5번-a)</h3>
    <p class="source">{{ '사용자 수정' if row.user_bottleneck else 'LLM 초안' }}</p>
    <textarea name="bottleneck">{{ display(row, 'bottleneck') }}</textarea></div>

  <div><h3>해자 (5번-b)</h3>
    <p class="source">{{ '사용자 수정' if row.user_moat else 'LLM 초안' }}</p>
    <textarea name="moat">{{ display(row, 'moat') }}</textarea></div>

  <button type="submit">사용자 메모 저장</button>
</form>

<form action="/leaders/{{ row.symbol }}/refresh" method="post" style="margin-top: 1em;">
  <input type="hidden" name="csrf_token" value="{{ session.csrf_token }}">
  <button type="submit">LLM 재분석</button>
</form>
{% endif %}
</body>
</html>
```

- [ ] **Step 5: Run route tests**

Run: `.venv/bin/python -m pytest tests/test_leaders_routes.py -v`
Expected: 2 passed

- [ ] **Step 6: edit / refresh POST 테스트**

`tests/test_leaders_routes.py` 에 추가:

```python
def test_post_edit_partial_update(app_with_leader, monkeypatch: pytest.MonkeyPatch):
    # CSRF bypass — session csrf_token 을 form 으로
    monkeypatch.setattr("src.web_app._csrf_validate", lambda: None)

    r = app_with_leader.post(
        "/leaders/005930.KS/edit",
        data={"tam_narrative": "사용자 작성 TAM"},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303)

    row = leader_cache.get("005930.KS")
    assert row["user_tam_narrative"] == "사용자 작성 TAM"
    # 다른 필드는 미변경
    assert row["user_moat"] is None


def test_post_refresh_only_updates_llm(app_with_leader, monkeypatch: pytest.MonkeyPatch):
    from src import leader_llm
    monkeypatch.setattr("src.web_app._csrf_validate", lambda: None)
    # user 수정본 먼저 저장
    leader_cache.update_user_fields(
        "005930.KS", {"tam_narrative": "USER"}, "tester"
    )

    fake_result = leader_llm.LLMResult(
        fields={"tam_narrative": "NEW_LLM", "narrative_expansion": "x",
                "bottleneck": "x", "moat": "x"},
        raw="{}", error=None,
    )
    monkeypatch.setattr(leader_llm, "analyze_one", lambda inputs: fake_result)

    r = app_with_leader.post(
        "/leaders/005930.KS/refresh", follow_redirects=False,
    )
    assert r.status_code in (302, 303)

    row = leader_cache.get("005930.KS")
    assert row["llm_tam_narrative"] == "NEW_LLM"
    assert row["user_tam_narrative"] == "USER"   # 보존 확인
```

- [ ] **Step 7: Run all route tests**

Run: `.venv/bin/python -m pytest tests/test_leaders_routes.py -v`
Expected: 4 passed

- [ ] **Step 8: 전체 회귀 확인 (기존 회귀 깨지지 않음)**

Run: `.venv/bin/python -m pytest tests/ --tb=line 2>&1 | tail -5`
Expected: `XXX passed` (XXX = 282 기존 + 신규 ~25)

- [ ] **Step 9: Commit**

```bash
git add src/web_app.py src/templates/leaders.html src/templates/leader_detail.html tests/test_leaders_routes.py
git commit -m "feat(leaders): /leaders 목록 + /leaders/<symbol> 상세 라우트 4개"
```

---

## Task 7: launchd plist + 통합 배포 검증

**Files:**
- Create: `scripts/leaders.plist.template`

- [ ] **Step 1: plist 템플릿 작성**

Create `scripts/leaders.plist.template`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>ai.stock-analyzer.leaders</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/sykim/Projects/stock-analyzer/.venv/bin/python</string>
        <string>main.py</string>
        <string>leaders-refresh</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/sykim/Projects/stock-analyzer</string>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>16</integer>
        <key>Minute</key><integer>30</integer>
    </dict>
    <key>RunAtLoad</key><false/>
    <key>KeepAlive</key><false/>
    <key>StandardOutPath</key>
    <string>/Users/sykim/Projects/stock-analyzer/logs/leaders.out.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/sykim/Projects/stock-analyzer/logs/leaders.err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>MKL_NUM_THREADS</key><string>1</string>
        <key>NUMEXPR_NUM_THREADS</key><string>1</string>
        <key>OMP_NUM_THREADS</key><string>1</string>
        <key>OPENBLAS_NUM_THREADS</key><string>1</string>
        <key>VECLIB_MAXIMUM_THREADS</key><string>1</string>
        <key>PREDICTION_ENGINE_NO_PROCESS_POOL</key><string>1</string>
        <key>TOKENIZERS_PARALLELISM</key><string>false</string>
        <key>KMP_INIT_AT_FORK</key><string>FALSE</string>
        <key>OBJC_DISABLE_INITIALIZE_FORK_SAFETY</key><string>YES</string>
    </dict>
</dict>
</plist>
```

- [ ] **Step 2: Commit + push**

```bash
git add scripts/leaders.plist.template docs/superpowers/plans/2026-05-15-leader-stock-finder.md
git commit -m "feat(leaders): launchd plist 템플릿 + plan 문서"
git push origin main
```

- [ ] **Step 3: 로컬 dev server 로 UI 1회 확인 (선택)**

Run: `.venv/bin/python main.py --web --port 8080`
Then in browser: `http://localhost:8080/leaders`
Expected: 표 표시 (DB 비어있을 수 있음 — 그러면 step 4 부터)

- [ ] **Step 4: 로컬에서 1회 수동 cron 실행**

`.env` 에 `GEMINI_API_KEY` 설정 후:

Run: `.venv/bin/python main.py leaders-refresh 2>&1 | tail -20`
Expected: `leaders-refresh 완료: passed=N llm_calls=M errors=0 dropped=0`

브라우저로 `http://localhost:8080/leaders` 재확인 — 종목 목록 + 상세 페이지 동작 검증.

- [ ] **Step 5: 원격 macmini 배포**

Run:
```bash
ssh sykim-macmini "cd ~/Projects/stock-analyzer && git pull --ff-only && .venv/bin/pip install -r requirements.txt"
ssh sykim-macmini "cp ~/Projects/stock-analyzer/scripts/leaders.plist.template ~/Library/LaunchAgents/ai.stock-analyzer.leaders.plist"
ssh sykim-macmini "launchctl load ~/Library/LaunchAgents/ai.stock-analyzer.leaders.plist"
ssh sykim-macmini "launchctl list | grep leaders"
```
Expected: `-  0  ai.stock-analyzer.leaders` (PID 없음 = 다음 cron 대기)

- [ ] **Step 6: 원격에서 .env 에 GEMINI_API_KEY 설정 안내**

> 사용자가 직접 `~/Projects/stock-analyzer/.env` 에 `GEMINI_API_KEY=...` 추가해야 함. Step 7 의 첫 강제 실행 전에.

- [ ] **Step 7: 강제 1회 실행 검증**

Run: `ssh sykim-macmini "launchctl kickstart -k gui/501/ai.stock-analyzer.leaders" && sleep 30 && ssh sykim-macmini "tail -20 ~/Projects/stock-analyzer/logs/leaders.out.log ~/Projects/stock-analyzer/logs/leaders.err.log"`
Expected: `leaders-refresh 완료: passed=N llm_calls=M errors=E` 로그 + traceback 0건

- [ ] **Step 8: 사용자 URL 으로 직접 확인**

Browser: `https://sykim-macmini.tail8d6ef7.ts.net/leaders`
사용자가 직접 종목 목록 + 상세 + 메모 수정/LLM 재분석 동작 확인.

---

## Out of scope (이 plan 에서 명시적으로 다루지 않음)

- 종목 비교 페이지 (여러 종목 나란히)
- 알림 (텔레그램/메일)
- auto-trader 통합
- 백테스트
- ETF / 미국 종목

## 검증 게이트

- [ ] Task 1 ~ 6 의 모든 회귀 테스트 PASS (~25 신규 + 282 기존)
- [ ] 로컬 `main.py leaders-refresh` 1회 정상 실행
- [ ] 로컬 dev server `/leaders` `/leaders/<symbol>` UI 정상
- [ ] 원격 launchd `kickstart` 강제 실행 정상
- [ ] 외부 URL Funnel 접속 시 5축 스코어카드 + LLM 메모 표시
