# 예측 이력 DB + 백테스트 리포트 — 설계서

- **작성일**: 2026-05-04
- **대상 프로젝트**: stock-analyzer
- **변경 범위**: ML 예측 결과의 영속화·평가, 종목별 walk-forward 백테스트
- **외부 LLM 검토**: gemini-2.5-pro 1회 (반영 사항 본문 표기)

## 1. 배경

현재 `analyze_stock` 결과는 분석 시점 1회용이다. 모델 5개(RF/LGBM/LSTM/Transformer/Ensemble)가 다음 영업일 상승/하락을 예측하지만, **다음날 실제로 맞췄는지 추적되지 않는다**. 사용자는 "이 모델이 얼마나 신뢰할 만한가"를 알 수 없고, 모델 비교도 불가능하다.

이 설계는 두 가지를 도입한다:

1. **예측 이력 DB**: 매 분석 시 모든 모델 예측을 SQLite에 영속화하고, 다음 영업일 종가가 들어오면 자동 평가한다. 누적 hit rate를 분석 리포트에 표시한다.
2. **백테스트 리포트**: 종목별로 RF+LGBM 두 모델을 과거 6개월 walk-forward 시뮬레이션해 모델 정확도를 검증한다.

## 2. 결정 사항

| 항목 | 결정 | 근거 |
|------|------|------|
| 트래킹 모델 | RF, LGBM, LSTM, Transformer, Ensemble (5개) | 모델 비교가 핵심 가치 |
| 백테스트 모델 | RF + LGBM + (둘의 voting) | LSTM/Transformer는 CPU 환경에서 walk-forward 비용 과다 |
| 저장 | SQLite (`data/predictions.db`), WAL 모드, `_writer_lock`으로 프로세스 내 직렬화 | 표준 라이브러리, 단일 파일, 집계 쿼리 빠름 |
| 백필 트리거 | 인라인(분석 시 보조) + 일일 cron 18:00 KST(주된 완전성) | Gemini MID 반영 — cron이 메인 |
| 타임스탬프 | INTEGER (UTC unix epoch) | Gemini LOW 반영 — TZ 혼란 방지, 비교 빠름 |
| UI | 분석 리포트 ML 섹션에 hit rate 인라인 + "백테스트 실행" 버튼 | 별도 페이지 불필요 |
| 백테스트 동시 실행 | 글로벌 lock으로 1개 제한 | DoS 방어 (Gemini MID 반영 — Celery 대신 경량 lock) |

## 3. 아키텍처

```
[analyze_stock]
    ├─→ run_prediction (ML)
    │       └─→ prediction_history.insert_live(symbol, predictions, base_close, target_date)
    │           (RF, LGBM, LSTM, Transformer, Ensemble 5개 행 일괄 삽입)
    │
    ├─→ prediction_history.backfill_inline(symbol, df)
    │   (받은 365일 df로 미평가 행의 actual_close 채우고 hit 계산)
    │
    └─→ generate_report
            └─→ prediction_history.hit_rate_by_model(symbol, source='live')
                → 리포트 ML 섹션에 모델별 hit rate 표시

[scheduler 매일 18:00 KST]
    └─→ prediction_history.backfill_all(fetch_fn=fetch_stock_data)
        (주된 완전성 메커니즘 — 미평가 + target_date < now 일괄 평가)

[/jobs/<id> 리포트 페이지]
    └─→ POST /backtest/<symbol> (CSRF 검증, _backtest_lock acquire)
            └─→ _run_backtest_bg(job_id, symbol, backtest_id) 스레드
                ├─→ fetch_stock_data + compute_indicators
                ├─→ backtest.walk_forward(symbol, df, days=126)
                │   (RF + LGBM + 둘의 voting, 6개월 walk-forward)
                └─→ prediction_history.insert_backtest(rows, backtest_id)
            → /jobs/<job_id>/result에 백테스트 미니 리포트 표시
```

## 4. SQLite 스키마

데이터 파일: `data/predictions.db`

```sql
CREATE TABLE IF NOT EXISTS predictions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT NOT NULL,
    ts            INTEGER NOT NULL,           -- UTC unix epoch (예측 시점)
    target_date   INTEGER NOT NULL,           -- UTC unix epoch (다음 영업일 KST 자정 = 15:00 UTC 전날)
    model         TEXT NOT NULL,              -- 'rf'|'lgbm'|'lstm'|'transformer'|'ensemble'
    direction     TEXT NOT NULL,              -- '상승'|'하락'
    confidence    REAL NOT NULL,
    actual_close  REAL,                       -- 평가 후 채워짐 (NULL = 미평가)
    base_close    REAL NOT NULL,              -- 예측 시점 종가 (hit 계산용)
    hit           INTEGER,                    -- 1|0|NULL
    evaluated_at  INTEGER,                    -- UTC unix epoch (백필 시점)
    source        TEXT NOT NULL DEFAULT 'live',  -- 'live'|'backtest'
    backtest_id   TEXT,                       -- backtest run uuid (live면 NULL)
    UNIQUE(symbol, target_date, model, source, backtest_id)
);

CREATE INDEX idx_pred_symbol_model ON predictions(symbol, model, source);
CREATE INDEX idx_pred_unevaluated ON predictions(symbol, target_date) WHERE actual_close IS NULL;
```

**PRAGMA**:
- `journal_mode=WAL` (동시 읽기 + 쓰기 안전)
- `synchronous=NORMAL` (WAL과 함께 권장 — 충돌 안전, 약간 빠름)

**hit 계산 규칙**:
- `direction='상승' AND actual_close > base_close` → `hit=1`
- `direction='하락' AND actual_close < base_close` → `hit=1`
- 그 외 → `hit=0`
- **변동 없음(`actual_close == base_close`) 처리**: 보수적으로 `hit=0`. 근거 — 모델이 명확한 방향을 예측한 만큼, "변동 없음"은 예측 실패로 간주. 매우 드물게(소수점 일치) 발생.

**target_date 정규화**: 다음 영업일의 KST 자정에 해당하는 UTC timestamp. KST 자정 = UTC 전날 15:00. 정수 비교로 정렬·필터링.

## 5. `src/prediction_history.py` API

단일 모듈, 함수 기반.

```python
"""예측 이력 SQLite 영속화 + 백필/집계."""
from __future__ import annotations
import sqlite3
import threading
import time
import uuid
from pathlib import Path
import pandas as pd

_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "predictions.db"
_writer_lock = threading.Lock()  # 동일 프로세스 내 쓰기 직렬화 (Gemini HIGH 반영)

# 트래킹 모델 식별자. 구현 시 run_prediction 반환 dict 구조를 확인해
# 각 모델의 direction/confidence를 추출하는 어댑터를 작성한다.
_TRACKED_MODELS = ('rf', 'lgbm', 'lstm', 'transformer', 'ensemble')


def init_db() -> None:
    """첫 호출 시 DB 파일과 부모 디렉토리 생성, 스키마 적용, WAL 활성화. 멱등."""

def insert_live(
    symbol: str,
    predictions: dict,    # run_prediction 결과 dict 그대로
    base_close: float,
    target_date: int,     # UTC unix timestamp
) -> None:
    """live 예측 5개 모델을 일괄 저장. UNIQUE 충돌 시 INSERT OR IGNORE."""

def insert_backtest(rows: list[dict], backtest_id: str) -> None:
    """백테스트 walk-forward 결과 일괄 저장 (단일 트랜잭션)."""

def backfill_inline(symbol: str, df: pd.DataFrame) -> int:
    """인라인 백필: df 인덱스에 있는 날짜의 미평가 예측 actual_close 채움.
    Returns: 평가된 행 수.
    """

def backfill_all(fetch_fn) -> dict:
    """cron용 전체 백필 (주된 완전성 메커니즘).
    
    fetch_fn(symbol) -> pd.DataFrame 콜러블 의존성 주입 (테스트 용이).
    심볼별로 그룹화 후 한 번씩 fetch → 일괄 평가.
    
    Returns: {'evaluated': N, 'failed_symbols': [...]}
    """

def hit_rate_by_model(
    symbol: str,
    source: str = 'live',
    backtest_id: str | None = None,
) -> dict:
    """심볼의 모델별 hit rate.
    
    Args:
        symbol: 종목 심볼
        source: 'live' 또는 'backtest'
        backtest_id: source='backtest' 일 때 특정 회차로 필터 (None이면 모든 백테스트 합산 — 라이브에서는 미사용)
    
    Returns: {'rf': {'hit_rate': 0.62, 'n': 42}, 'lgbm': {...}, ...}
        — 평가된(hit IS NOT NULL) 행 기준. n=0인 모델은 결과에서 누락.
    """

def get_backtest_results(backtest_id: str) -> dict:
    """백테스트 1회분 결과: 모델별 hit rate + walk-forward 행들."""

def list_pending_backfills() -> list[dict]:
    """미평가 예측 통계 (대시보드 표시용 옵션)."""
```

**경계**:
- 외부 API 호출 없음 — `backfill_all`은 `fetch_fn` 인자로 의존성 주입
- pandas DataFrame은 인터페이스로만 (내부에서 row dict로 즉시 변환)
- 모든 쓰기는 `with _writer_lock:` 안에서 실행
- SQL은 parameterized queries만 (f-string SQL 금지)

## 6. 통합 지점

### 6.1 `main.py` — `analyze_stock`

```python
def analyze_stock(symbol, name):
    df = fetch_stock_data(symbol)
    df = compute_indicators(df)
    
    # NEW: 인라인 백필 (즉시성 보조)
    prediction_history.backfill_inline(symbol, df)
    
    signal = generate_signal(df)
    prediction = _engine.run(df, symbol)
    
    # NEW: live 예측 저장
    last_close = float(df["Close"].iloc[-1])
    target_date = _next_business_day_unix(df.index[-1])
    prediction_history.insert_live(symbol, prediction, last_close, target_date)
    
    # ... news, sentiment, return ...
```

`_next_business_day_unix(date)`: pandas `BDay()` offset 사용해 KST 자정의 UTC unix timestamp 반환.

### 6.2 `src/report_generator.py`

ML 예측 섹션 끝에 hit rate 블록 추가:

```python
hit_rates = prediction_history.hit_rate_by_model(symbol, source='live')
# 표 렌더링: 모델 / Hit Rate / 평가 횟수
# n < 10이면 "데이터 부족 (n=N)" 표시
# hit_rates가 빈 dict면 섹션 자체 생략
```

### 6.3 `src/backtest.py` (신규)

```python
def walk_forward(symbol: str, df: pd.DataFrame, days: int = 126) -> dict:
    """RF + LGBM + 둘의 voting ensemble을 과거 N영업일 walk-forward.
    
    각 일자 t (df의 마지막 days 영업일):
        - df[:t] 데이터로 RF, LGBM 학습
        - df[t]에서 다음날 방향 예측
        - df[t+1] 실제 close로 hit/miss 평가
    
    Args:
        symbol: 종목 심볼 (저장용)
        df: indicators 계산된 DataFrame (analyze_stock에서 받은 df 재사용)
        days: walk-forward 일수 (기본 126 ≈ 6개월)
    
    Returns:
        {
            'backtest_id': '<uuid 8자>',
            'rows': [...],  # insert_backtest에 전달할 행 리스트
            'summary': {
                'rf': {'hit_rate': 0.61, 'n': 124},
                'lgbm': {'hit_rate': 0.58, 'n': 124},
                'ensemble': {'hit_rate': 0.62, 'n': 124},
            },
        }
    
    윈도우 부족(len(df) < 30 + days) 시:
        {'backtest_id': None, 'rows': [], 'summary': {}, 'error': '데이터 부족'}
    """
```

학습 윈도우는 매 t마다 처음부터(expanding) — `_prepare_clf_data` 재사용. 메모리/시간은 RF+LGBM 두 모델만이라 6개월×1종목 ≈ 20초.

**Voting ensemble 규칙** (RF + LGBM):
- 두 모델이 같은 방향이면 그 방향 채택, confidence는 둘의 평균.
- 두 모델이 다른 방향이면 confidence가 더 높은 쪽 채택.

**df 인덱스와 target_date 매칭**: `predictions.target_date`는 UTC unix epoch (KST 자정 기준), `df.index`는 pandas DatetimeIndex (yfinance/FDR이 반환한 거래일). `backfill_inline`은 df 인덱스를 동일 기준으로 정규화한 뒤 매칭 — 구체적으로 `df.index.normalize().tz_localize('Asia/Seoul', nonexistent='shift_forward').tz_convert('UTC')` 후 unix epoch 비교.

### 6.4 `src/scheduler.py`

기존 `daily_job` (08:30 KST 분석) 그대로 두고 추가:

```python
scheduler.add_job(
    func=lambda: prediction_history.backfill_all(fetch_fn=fetch_stock_data),
    trigger=CronTrigger(hour=18, minute=0, timezone='Asia/Seoul'),
    id='backfill_daily',
)
```

### 6.5 `src/web_app.py`

```python
_backtest_lock = threading.Lock()  # 글로벌 1개 제한 (DoS 방어)


@app.route("/backtest/<path:symbol>", methods=["POST"])
def start_backtest(symbol):
    _csrf_validate()
    symbol = sanitize_stock_symbol(symbol)
    if not validate_stock_symbol(symbol):
        abort(400)
    
    if not _backtest_lock.acquire(blocking=False):
        return redirect(url_for('index', error="다른 백테스트가 실행 중입니다."), 303)
    
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
    return redirect(f"/jobs/{job_id}", 303)


def _run_backtest_bg(job_id, symbol, backtest_id):
    try:
        from main import _engine  # not used, just guard for fetch
        from src.data_fetcher import fetch_stock_data
        from src.technical_analysis import compute_indicators
        from src import backtest as bt
        
        df = fetch_stock_data(symbol)
        df = compute_indicators(df)
        result = bt.walk_forward(symbol, df, days=126)
        if result.get('error'):
            _jobs_set(job_id, status='error', error=result['error'])
            return
        prediction_history.insert_backtest(result['rows'], backtest_id)
        html = _render_backtest_report(symbol, result)
        _jobs_set(job_id, status='done', result_html=html)
    except Exception as e:
        logger.exception("백테스트 실패: %s", e)
        _jobs_set(job_id, status='error', error=str(e))
    finally:
        _backtest_lock.release()
        _trim_jobs()
```

`/jobs/<id>` 페이지 (분석 리포트) 하단에 백테스트 폼 추가:

```html
<form method="post" action="/backtest/{symbol}" style="margin-top:24px;">
  {csrf_input}
  <button type="submit" class="btn btn-amber">백테스트 실행 (RF+LGBM, 6개월 walk-forward)</button>
</form>
```

## 7. 에러 처리 / 보안 / 운영

| 영역 | 처리 |
|------|------|
| DB 파일 없음 | `init_db()` 첫 호출 시 `data/` 디렉토리 생성 + 스키마 적용. 멱등. |
| DB 동시성 | WAL + `_writer_lock` (단일 프로세스 직렬화). gunicorn은 workers=1 권장 (config 주석) |
| `insert_live` 실패 | log warning + 분석 결과 반환은 정상 (DB 장애 → UX 차단 안 함) |
| `backfill_inline` 실패 | log warning + 무시 (다음 cron이 처리) |
| `backfill_all` 부분 실패 | symbol별 try/except, 실패 목록 로그 + 다음 회차 재시도 |
| 백테스트 도중 종료 | `finally:` 에서 lock release. job status=error 표기 |
| 백테스트 동시 실행 | 글로벌 lock 1개 제한, 두 번째 요청은 redirect with error |
| `/backtest/<symbol>` 보안 | CSRF 검증, `validate_stock_symbol` |
| SQL injection | sqlite3 parameterized queries만 사용 |
| 데이터 보존 | 자동 만료 없음. 운영자 수동 관리 (수년치도 수만 행 수준) |

## 8. 테스트

### 8.1 `tests/test_prediction_history.py` (신규)

- `init_db` 멱등성 + 스키마 생성
- `insert_live` 5개 모델 일괄 저장
- `insert_live` UNIQUE 충돌 시 INSERT OR IGNORE
- `backfill_inline` actual_close 채움 + hit 계산 (상승/하락 양방향)
- `backfill_inline` 평가 가능한 행만 (target_date가 df 인덱스에 존재)
- `backfill_all` mock fetch_fn으로 다중 심볼 일괄 평가
- `backfill_all` 일부 fetch 실패 시 다른 심볼 정상 처리
- `hit_rate_by_model` n=0인 모델 누락
- `_writer_lock` 동시 호출 직렬화 (스레드 2개로 동시 insert)

### 8.2 `tests/test_backtest.py` (신규)

- `walk_forward` 합성 df로 RF+LGBM 결과 dict 반환
- 윈도우 부족(`len(df) < 30 + days`) 시 error 응답
- 결과 행이 `prediction_history`로 저장되는 통합 테스트 (in-memory SQLite)

### 8.3 `tests/test_web_app.py` (수정)

- `POST /backtest/<symbol>` CSRF 누락 → 403
- 잘못된 심볼 → 400
- 백테스트 동시 실행 → 두 번째는 redirect with error
- 정상 실행 → `/jobs/<id>` redirect, status=running

## 9. 작업 분할

| 단계 | 파일 | 비고 |
|------|------|------|
| 1 | `src/prediction_history.py` + `tests/test_prediction_history.py` | DB 모듈, 단위 테스트 |
| 2 | `main.py`의 `analyze_stock` 통합 (insert_live + backfill_inline) | 회귀 테스트 |
| 3 | `src/report_generator.py` hit rate 섹션 추가 | 시각 확인 |
| 4 | `src/backtest.py` + `tests/test_backtest.py` | walk_forward 단위 |
| 5 | `src/web_app.py` `/backtest` 라우트 + lock + 폼 + `tests/test_web_app.py` 보강 | CSRF·동시성 테스트 |
| 6 | `src/scheduler.py` 18:00 KST cron 추가 | manual run 검증 |
| 7 | 수동 검증 — 분석 → DB 행 확인 → 다음날 분석 → hit 채워짐 → 백테스트 실행 → 결과 표시 | E2E |

## 10. YAGNI / 비포함

- Prophet 가격 예측 트래킹 (별도 평가 지표 MAE/RMSE 필요)
- LSTM/Transformer 백테스트 (CPU 환경에서 비실용적)
- 종목 간 비교 페이지 (단일 종목만)
- 예측 이력 export (CSV 다운로드)
- 알림 (hit rate 급락 시 슬랙 등)
- DB 마이그레이션 시스템 (스키마 변경은 수동)
- 사용자별 격리 (단일 사용자 가정)
- 다중 프로세스/PostgreSQL (단일 사용자 + dev/single-worker 환경 가정. Gemini HIGH 부분 반영)
- 작업 큐 시스템 Celery/Redis (글로벌 lock으로 1개 제한 충분. Gemini MID 부분 반영)
