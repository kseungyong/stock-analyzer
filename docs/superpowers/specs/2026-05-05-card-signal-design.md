# 종목 카드 매수/매도/관망 시그널 표시 설계

**작성일**: 2026-05-05
**상태**: 설계 — 사용자 검토 대기
**관련 모듈**: `src/analysis_cache.py`, `src/web_app.py`, `main.py`, `src/technical_analysis.py` (변경 없음, 데이터 소스)

## 1. 배경 및 목적

`technical_analysis.generate_signal(df)` 가 이미 매수/매도/관망 3분류 시그널 + score 정수를 산출한다 (RSI/MACD/이동평균/볼린저/거래량 기여도 합산, `score >= 2` 매수, `<= -2` 매도, 그 외 관망). `analyze_stock` 결과 dict 의 `result["signal"]` 에 이 정보가 들어가지만 **DB 에는 저장되지 않아** 대시보드 카드에서는 보이지 않는다.

목적: 사용자가 대시보드 카드만 보고도 종목별 매수/매도/관망 신호를 한 눈에 볼 수 있게 한다.

## 2. 결정된 정책 (브레인스토밍 합의)

| 항목 | 결정 |
|---|---|
| 시그널 출처 | `generate_signal` (기술 분석) — 이미 매수/매도/관망 3분류 + score |
| 강도 표시 | **라벨 + score 숫자** — `매수 +3`, `매도 -2`, `관망 +1` |
| 카드 위치 | 시장 뱃지 옆 — 같은 영역에 세로 정렬 (`stock-card-badges`) |
| 데이터 저장 | `analysis_cache` 테이블에 `signal_value TEXT` + `signal_score INTEGER` 컬럼 추가 |
| 마이그레이션 | `init_db` 안에서 `PRAGMA table_info` 확인 후 조건부 `ALTER TABLE ADD COLUMN` (멱등) |
| 시그널 갱신 | 분석 worker (수동 단일/수동 전체/자동 cron) 가 매번 signal 함께 cache.put |
| 기존 row 호환 | signal_value=NULL 허용 — 카드에 뱃지 미표시. 다음 분석 시 채워짐 |
| score=0 표시 | sign 없이 "관망 0" / "매수 0" |

## 3. 아키텍처

```
[분석 worker — 3개]
  ├─ _run_analysis_bg (web_app.py)            ── 수동 단일 종목
  ├─ _run_full_analysis_bg (web_app.py)       ── 수동 전체
  └─ auto_analyze_market (main.py)            ── KST 16:00/06:00 cron
       └→ analyze_stock(...) → result["signal"] = {"signal", "score", ...}
       └→ analysis_cache.put(..., signal_value=, signal_score=)

[analysis_cache.py]
  ├─ _SCHEMA — signal_value/signal_score 추가
  ├─ _migrate(conn) — 기존 DB 멱등 컬럼 추가
  ├─ init_db()      — _SCHEMA 실행 + _migrate
  ├─ put(...)       — keyword-only signal_value/signal_score 매개변수
  └─ get(cache_key) — 반환 dict 에 두 필드 추가

[web_app.py — 카드 렌더]
  ├─ _SIGNAL_CLASS 상수 (매수→signal-buy, 매도→signal-sell, 관망→signal-hold)
  ├─ _render_signal_badge(value, score) — HTML 뱃지 또는 빈 문자열
  └─ index() 카드 루프 — header 영역 stock-card-badges 컨테이너에
                          [signal_badge] [market_badge] 세로 정렬
```

## 4. 데이터 모델

### 컬럼 추가

```sql
-- 새 DB 스키마
CREATE TABLE IF NOT EXISTS analysis_cache (
    cache_key      TEXT PRIMARY KEY,
    market         TEXT NOT NULL,
    result_html    TEXT NOT NULL,
    generated_at   INTEGER NOT NULL,
    source         TEXT NOT NULL,
    signal_value   TEXT,           -- "매수" | "매도" | "관망" | NULL
    signal_score   INTEGER         -- e.g. 3, -2, 0, NULL
);

-- 기존 DB 마이그레이션 (idempotent via PRAGMA check)
ALTER TABLE analysis_cache ADD COLUMN signal_value TEXT;
ALTER TABLE analysis_cache ADD COLUMN signal_score INTEGER;
```

### 마이그레이션 헬퍼

```python
def _migrate(conn: sqlite3.Connection) -> None:
    """기존 DB 에 누락된 컬럼을 추가하는 멱등 마이그레이션."""
    cur = conn.execute("PRAGMA table_info(analysis_cache)")
    cols = {row[1] for row in cur.fetchall()}
    if "signal_value" not in cols:
        conn.execute("ALTER TABLE analysis_cache ADD COLUMN signal_value TEXT")
    if "signal_score" not in cols:
        conn.execute("ALTER TABLE analysis_cache ADD COLUMN signal_score INTEGER")
```

`init_db()` 안에서 `_SCHEMA` 실행 후 `_migrate(conn)` 호출.

### `put` 시그니처 확장 (keyword-only 추가)

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
    덮어쓰는 효과 (의도된 동작 — 호출자가 명시적으로 전달해야 보존).
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

### `get` 반환 dict 확장

```python
{
    "cache_key": str,
    "market": str,
    "result_html": str,
    "generated_at": int,
    "source": str,
    "signal_value": str | None,    # 신규
    "signal_score": int | None,    # 신규
}
```

`list_symbols` 도 같은 컬럼 SELECT 에 추가.

## 5. Worker — 시그널 저장

### `_run_analysis_bg` (수동 단일, web_app.py)

```python
def _run_analysis_bg(job_id: str, symbol: str, name: str) -> None:
    ...
    if result is None:
        ...
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
            logger.warning("analysis_cache.put 실패: %s", e)
```

### `_run_full_analysis_bg` (수동 전체, web_app.py)

종목별 UPSERT 부분에 signal 추가:

```python
for r in analyses:
    sym = r["symbol"]
    try:
        ind_html = generate_report([r])
        sig = r.get("signal") or {}
        analysis_cache.put(
            sym, symbol_to_market.get(sym, "us"),
            ind_html, source="manual",
            signal_value=sig.get("signal"),
            signal_score=sig.get("score"),
        )
        cached += 1
    except Exception as e:
        ...

# ALL row — signal 없이 (다이제스트 합본이라 단일 시그널 부적절)
analysis_cache.put("ALL", "all", full_html, source="manual")
```

### `auto_analyze_market` (cron, main.py)

```python
for s in stocks:
    try:
        result = analyze_stock(s["symbol"], s["name"])
        if result is None:
            continue
        html = generate_report([result])
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

## 6. 카드 렌더링

### 신규 헬퍼 (`src/web_app.py`)

```python
_SIGNAL_CLASS = {
    "매수": "signal-buy",
    "매도": "signal-sell",
    "관망": "signal-hold",
}


def _render_signal_badge(value: str | None, score: int | None) -> str:
    """시그널 뱃지 HTML — value 가 None 이면 빈 문자열.

    score 양수는 '+N', 음수는 자동 '-N', 0 은 sign 없이 '0'.
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

### `index` 카드 마크업 변경

```python
# 신선도 줄 + actions 위쪽, 카드 header 변경
signal_badge = _render_signal_badge(
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
      {signal_badge}
      <span class="badge {badge_cls}">{market_label}</span>
    </div>
  </div>
  {freshness_line}
  <div class="stock-card-actions">
    {primary_btn}
    {reanalyze_btn}
    <form method="post" action="/stocks/delete" ...>
      ...
    </form>
  </div>
</div>""")
```

### CSS 추가

`_CSS` 끝에 append:

```css
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

### 결과 시각

```
┌──────────────────────────────┐
│ Apple              [매수 +3] │
│ AAPL                  [미국] │
│ 🟢 16:02 (3시간 전)            │
│ [▶ 결과 보기] [🔄] [🗑]       │
└──────────────────────────────┘
```

매도면 빨강, 관망이면 회색. 시그널 없는 종목 (분석 안 됨) 은 시장 뱃지만 표시.

## 7. 에러 / 엣지 케이스

| 시나리오 | 동작 |
|---|---|
| 기존 row (signal_value=NULL) | 카드에 시그널 뱃지 미표시. 다음 분석 시 채워짐 |
| `result["signal"]` 가 dict 아님 (None 등) | `result.get("signal") or {}` 로 빈 dict, NULL 저장 |
| `signal_value` 가 정의된 3개 외 문자열 | `_SIGNAL_CLASS.get(value, "signal-hold")` → 회색 fallback |
| `signal_score = 0` | "관망 0" / "매수 0" — sign 없이 |
| `analysis_cache.put` 시그널 저장 실패 | 기존 try/except 가 잡음, 분석 결과 본문 정상 |
| 마이그레이션 실패 (권한 등) | `init_db` 가 raise — fail-fast (기존 패턴) |
| 새 컬럼 추가 후 server roll-back | 새 코드가 기존 row read 시 `signal_value: None` — 정상 처리 |
| ALL row 의 시그널 | 항상 NULL — 다이제스트 합본이라 단일 시그널 부적절. `/stock/all` 도 시그널 표시 안 함 |

## 8. 모듈 책임

```
src/analysis_cache.py     — _SCHEMA 갱신, _migrate 추가, put/get/list_symbols 시그니처/SELECT 확장
src/web_app.py            — _SIGNAL_CLASS, _render_signal_badge, index 카드 마크업, CSS append,
                            _run_analysis_bg / _run_full_analysis_bg signal 전달
main.py                   — auto_analyze_market signal 전달
src/technical_analysis.py — 변경 없음 (generate_signal 그대로)
```

## 9. 테스트 전략

기존 conftest 의 `_DB_PATH` 격리 그대로.

### `tests/test_analysis_cache.py`

#### `TestMigrateAddsSignalColumns` (3 케이스)
1. 새 DB → init_db 후 `signal_value` / `signal_score` 컬럼 존재 (`PRAGMA table_info` 확인)
2. 멱등성 — init_db 두 번 호출해도 `ALTER` 중복 안 됨 (오류 없음)
3. 기존 DB 시뮬레이션 — `_SCHEMA` 만 실행 후 (signal 컬럼 없는 상태) `_migrate` 호출 → 컬럼 추가됨

#### `TestPutGetSignal` (4 케이스)
1. `put(..., signal_value="매수", signal_score=3)` 후 `get(...)` → 두 필드 정확
2. `put(...)` 기본 (signal 매개변수 없이) → get 결과 두 필드 None
3. `put` keyword-only — `put("AAPL", "us", "<p/>", "manual", "매수", 3)` 호출 시 `TypeError`
4. signal 있던 row 를 signal 없이 UPSERT → 두 필드 NULL 로 덮어쓰기

### `tests/test_web_app.py`

#### `TestRenderSignalBadge` (5 케이스)
1. value None → `""` 빈 문자열
2. value "매수" + score 3 → "매수 +3" 텍스트, `signal-buy` 클래스
3. value "매도" + score -2 → "매도 -2", `signal-sell`
4. value "관망" + score 1 → "관망 +1", `signal-hold`
5. score=0 → "관망 0" (sign 없이)

#### `TestIndexCardSignal` (3 케이스)
1. 캐시 row 에 signal_value="매수", signal_score=3 → 카드 HTML 에 `signal-badge` + "매수 +3"
2. 캐시 row signal_value=None (기존 row) → 카드에 `signal-badge` 마크업 없음
3. 캐시 row 자체 None (분석 한 번도 안 됨) → 시그널 뱃지 없음

#### `TestAnalyzeBgSavesSignal` (1 케이스)
`_run_analysis_bg` 호출 후 `analysis_cache.put` 가 받은 인자에 `signal_value="매수"`, `signal_score=3` 포함 (monkeypatch).

#### `TestFullAnalysisSavesSignal` (1 케이스)
`_run_full_analysis_bg` 안의 종목별 put 호출이 signal 전달, "ALL" put 은 signal 없이.

### `tests/test_main.py`

#### `TestAutoAnalyzeMarketSavesSignal` (1 케이스)
`auto_analyze_market("us")` 호출 시 `analysis_cache.put` 받은 kwargs 에 `signal_value`, `signal_score` 포함.

## 10. 마이그레이션 / 배포

1. `analysis_cache.py` 변경 — `_SCHEMA` 갱신 + `_migrate` 함수 추가 + put/get/list_symbols 확장
2. worker 3개 (web_app + main) signal 전달
3. 카드 렌더 + CSS 추가
4. 배포: git push → 서버 git pull → `launchctl kickstart -k gui/$(id -u)/ai.stock-analyzer.web` 그리고 `... ai.stock-analyzer.scheduler`
5. `init_db` (모듈 로드 시 자동 호출) → `_migrate` → 기존 DB 에 컬럼 자동 추가

기존 row 의 signal 은 NULL → 카드에 뱃지 안 보임. **다음 분석부터 채워짐** — 자동 cron 또는 수동 재분석 후 표시.

## 11. 비목표 (Non-goals)

- ML ensemble 기반 시그널 합성 — 기술 분석만 (별도 추적: 결과 페이지의 모델 hit rate)
- 5단계 라벨 (`강한 매수` 등) — 3분류 + score 숫자로 강도 표현
- 시그널 변경 이력 — 매 분석마다 덮어쓰기 (UPSERT). 시간순 변화는 결과 페이지의 예측 히스토리 표 참고
- 카드에서 클릭으로 시그널 상세 보기 — `결과 보기` 클릭 시 결과 페이지의 기존 시그널 섹션에서 상세 (RSI 점수 등)
- ALL row 시그널 — 다이제스트라 단일 시그널 부적절
- 시그널 알림 (이메일/푸시) — 추후
