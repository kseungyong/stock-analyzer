# 분석 결과 캐시 설계 (Cached Analysis)

**작성일**: 2026-05-05
**상태**: 설계 — 사용자 검토 대기
**관련 모듈**: `src/web_app.py`, `src/email_sender.py`, `main.py`, 신규 `src/analysis_cache.py`

## 1. 배경 및 목적

현재 `/analyze/<symbol>` 와 `/analyze-all` 은 클릭마다 새 분석을 실행한다. 분석은 ML 예측 + 뉴스 fetch 포함이라 ~30초 이상 걸리고 같은 종목을 여러 번 보면 중복 비용이 발생한다. 또한 결과는 `_jobs` 메모리 dict에만 있어 서버 재시작 시 사라진다.

목적:
- 자동분석 결과를 캐시해 사용자가 결과를 즉시 볼 수 있게 한다.
- 분석 시각을 사용자에게 표시해 신선도를 판단할 수 있게 한다.
- 사용자가 원하면 그 자리에서 재분석할 수 있다.
- 기존 일일 이메일은 캐시 결과를 재사용한다.

## 2. 결정된 정책 (브레인스토밍 합의)

| 항목 | 결정 |
|---|---|
| 자동분석 시각 | 한국 종목 KST **16:00**, 미국 종목 KST **06:00** |
| 캐시 만료 | 각 시장 **장 시작 직전**까지 (한국 09:00, 미국 NYSE 09:30 ET → KST 환산) |
| 만료 후 동작 | 만료된 캐시 + 노란 "오래됨" 뱃지 표시, 사용자 재분석 권장 |
| 저장소 | **SQLite** — 새 테이블 `analysis_cache`, 기존 `prediction_history` 와 동일 DB 파일 |
| 캐시 단위 | 종목별(`symbol`) + 전체 분석(`"ALL"`) — 둘 다 |
| 기존 KST 08:30 작업 | 분석 재실행 안 함, **캐시에서 HTML 모아 이메일 발송**만 |
| 라우팅 | `GET /stock/<symbol>` (조회) + `POST /analyze/<symbol>` (재분석) 분리 |
| 결과 페이지 재분석 | **인라인 갱신** — `?job=<id>` 쿼리로 같은 페이지에 머무르며 폴링 → 완료 시 PRG redirect |

## 3. 아키텍처

```
[APScheduler]
  ├─ daily_email_job          (KST 08:30) ─→ analysis_cache 조회 → 이메일 발송
  ├─ auto_analyze_korea       (KST 16:00) ─→ 한국 종목 N개 → analysis_cache UPSERT
  ├─ auto_analyze_us          (KST 06:00) ─→ 미국 종목 N개 → analysis_cache UPSERT
  └─ backfill_daily           (KST 18:00) — 기존 그대로

[Flask]
  ├─ GET  /                    대시보드 (카드에 신선도 표시)
  ├─ GET  /stock/<symbol>      캐시 조회·표시 (인라인 갱신 진입점)
  ├─ GET  /stock/all           "ALL" 캐시 조회·표시
  ├─ POST /analyze/<symbol>    강제 재분석 (return_to=jobs|stock)
  ├─ POST /analyze-all         강제 전체 재분석
  ├─ GET  /jobs                기존 작업 내역 (변경 없음)
  ├─ GET  /jobs/<id>           기존 작업 상세 (변경 없음)
  └─ GET  /api/jobs/<id>       기존 폴링 API (변경 없음)

[src/analysis_cache.py]  ── 신규 모듈
  ├─ init_db()                          # 테이블 멱등 생성
  ├─ get(cache_key) -> dict|None
  ├─ put(cache_key, market, result_html, source)
  ├─ is_fresh(row, now) -> bool         # 시장별 만료 판단
  └─ list_symbols() -> list[dict]       # 종목별 row만 반환 (market != "all")
                                        # 이메일 다이제스트용
```

## 4. 데이터 모델

### `analysis_cache` 테이블

```sql
CREATE TABLE IF NOT EXISTS analysis_cache (
  cache_key      TEXT PRIMARY KEY,    -- symbol or "ALL"
  market         TEXT NOT NULL,       -- "korea" | "us" | "all"
  result_html    TEXT NOT NULL,       -- 분석 리포트 HTML
  generated_at   INTEGER NOT NULL,    -- unix epoch (UTC)
  source         TEXT NOT NULL        -- "auto_cron" | "manual"
);
CREATE INDEX IF NOT EXISTS idx_analysis_cache_market ON analysis_cache(market);
```

설계 결정:
- **PK = `cache_key`** — 종목별 row + `"ALL"` row 1개. UPSERT(`INSERT ... ON CONFLICT(cache_key) DO UPDATE`)로 항상 최신 1개만 유지(이력 누적 안 함).
- **`generated_at` UTC unix epoch** — 기존 `prediction_history` 컬럼 패턴 일치.
- **HTML 직접 TEXT 저장** — 평균 ~150KB, sqlite TEXT 충분. 외부 파일 분리는 YAGNI.
- **`source`** — 디버깅용 메타. 제품 동작에 영향 없음.

### `is_fresh` 신선도 판단

```
generated_at_kst = generated_at(UTC) → KST 변환

market == "korea":
  fresh until next KST 09:00 after generated_at
market == "us":
  fresh until next KST equivalent of 09:30 America/New_York after generated_at
  (서머타임 정확히 처리)
market == "all":
  fresh iff (모든 한국·미국 종목 row가 fresh)
```

서머타임 처리: `zoneinfo.ZoneInfo("America/New_York")` 의 09:30을 KST로 변환. 추가 의존성 없음(Python 3.9+ stdlib).

## 5. 캐시 라이프사이클

### 종목별 흐름

```
[사용자 클릭 "결과 보기"]
  GET /stock/<symbol>
    │
    ├─ analysis_cache.get(symbol) hit
    │     → 결과 HTML + 메타바 표시
    │       - 분석 시각 (KST 변환)
    │       - is_fresh → 🟢 / 🟡 뱃지
    │       - "재분석" 버튼 (POST /analyze/<symbol>, return_to=stock)
    │
    └─ miss (캐시 없음)
          → 빈 안내 페이지 + "분석 시작" 버튼

[사용자 클릭 "재분석"]
  POST /analyze/<symbol>  (CSRF 검증)
    │
    ├─ return_to=stock → redirect /stock/<symbol>?job=<id> (인라인)
    └─ return_to=jobs   → redirect /jobs/<id>            (대시보드 카드 클릭)
```

### 전체 분석 흐름

```
GET /stock/all
  ├─ analysis_cache.get("ALL") hit → 결과 HTML + 메타바 + "재분석" (return_to=stock-all)
  └─ miss → 빈 안내 페이지 + "전체 분석 시작" (POST /analyze-all)

POST /analyze-all (기존)
  → _run_full_analysis_bg → 종료 시 analysis_cache.put("ALL", market="all", ...)
  종목별 row 는 갱신하지 않음 (자동 cron 영역으로 분리)
```

자동 cron 은 "ALL" 을 갱신하지 않으므로 사용자가 명시적으로 `/analyze-all` 을 한 번이라도 트리거해야 `/stock/all` 에 결과가 표시된다. 자주 미사용되면 영구 miss 상태일 수 있으나, 이는 의도된 동작 — 종목별 카드가 1차 진입점이고 "전체"는 보조 뷰.

### 캐시 갱신 시점

| 트리거 | 갱신되는 row |
|---|---|
| `auto_analyze_korea` (KST 16:00) | 한국 각 종목 (source=auto_cron) |
| `auto_analyze_us` (KST 06:00) | 미국 각 종목 (source=auto_cron) |
| `POST /analyze/<symbol>` | 해당 종목 1개 (source=manual) |
| `POST /analyze-all` | "ALL" row 1개 (source=manual) |
| `daily_email_job` (KST 08:30) | **읽기만** — 캐시 안 만짐 |

### 동시성 / Race

- 자동 cron 진행 중 사용자 수동 재분석: 둘 다 진행, 종료 시 last-write-wins (UPSERT). 별도 락 없음.
- 자동분석은 종목 1개씩 직렬. 단일 cron 실행 내 동시성 없음.
- `analyze_stock` 호출은 thread-safe(ML 모델 인스턴스 1개를 ThreadPoolExecutor로 병렬 사용 — 기존 코드 그대로).

## 6. 라우팅 변경

| 라우트 | 메소드 | 변경 | 동작 |
|---|---|---|---|
| `/` | GET | 변경 | 카드의 "분석" 링크가 `GET /stock/<symbol>` 로 변경 |
| `/stock/<symbol>` | GET | **신규** | 캐시 조회·표시 (인라인 갱신 진입점) |
| `/stock/all` | GET | **신규** | "ALL" 캐시 조회·표시 |
| `/analyze/<symbol>` | POST | 메소드 변경 | 기존 GET → POST(CSRF 필수). `return_to` 분기 |
| `/analyze-all` | POST | 유지 | "ALL" 캐시 UPSERT 추가 |
| `/jobs/<id>` | GET | 유지 | 작업 진행/결과 페이지 |
| `/api/jobs/<id>` | GET | 유지 | 폴링 API |

기존 `GET /analyze/<symbol>` 은 단일 사용자(로컬/EC2 1인) 환경이라 호환 라우트 없이 즉시 마이그레이션. 기존 `GET /jobs/<id>` 는 PRG 패턴이라 영향 없음.

## 7. 자동분석 스케줄러 + 이메일

### `main.py` 변경

```python
def auto_analyze_market(market: str) -> None:
    """시장의 모든 종목을 차례로 분석하고 analysis_cache에 UPSERT."""
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


def daily_email_job() -> None:
    """캐시에서 결과 모아 이메일 발송. 분석 재실행하지 않음."""
    config = load_config()
    rows = analysis_cache.list_symbols()
    if not rows:
        logger.warning("이메일 발송 스킵 — 캐시가 비어있음")
        return
    html = render_email_digest(rows)
    send_report(html, config["email"])


extra_jobs = {
    "auto_analyze_korea": {
        "func": lambda: auto_analyze_market("korea"),
        "trigger": CronTrigger(hour=16, minute=0, timezone="Asia/Seoul"),
        "name": "Korea Auto Analysis",
    },
    "auto_analyze_us": {
        "func": lambda: auto_analyze_market("us"),
        "trigger": CronTrigger(hour=6, minute=0, timezone="Asia/Seoul"),
        "name": "US Auto Analysis (post-close)",
    },
    "backfill_daily": {  # 기존 유지
        "func": lambda: prediction_history.backfill_all(fetch_fn=fetch_stock_data),
        "trigger": CronTrigger(hour=18, minute=0, timezone="Asia/Seoul"),
        "name": "Daily Prediction Backfill",
    },
}

start_scheduler(daily_email_job, config["schedule"], extra_jobs=extra_jobs)
```

`config/settings.yaml` 의 `schedule.hour=8, minute=30` 은 그대로 두고 `daily_job` → `daily_email_job` 으로 함수만 교체.

### 시각 선택 근거

- **한국 16:00** — KOSPI 정규장 마감 15:30 + 30분 마진 (yfinance 데이터 동기화 시간 확보).
- **미국 06:00 KST** — NYSE 마감 04:00 KST (서머타임) / 06:00 KST (표준시) 직후~2시간 후. 단순화를 위해 고정 시각 사용.
- 휴장일 영업일 보정 안 함. yfinance가 직전 거래일 종가를 반환하므로 결과 유의미 (last-write-wins 로 무해).

### `render_email_digest`

`src/email_sender.py` 에 추가:

```python
def render_email_digest(rows: list[dict]) -> str:
    """analysis_cache row 리스트를 받아 이메일용 HTML 합성."""
    parts = ["<h1>일일 시장 분석 다이제스트</h1>"]
    for row in rows:
        gen_kst = utc_to_kst(row["generated_at"]).strftime("%Y-%m-%d %H:%M")
        fresh = "🟢 최근" if analysis_cache.is_fresh(row, now()) else "🟡 오래됨"
        parts.append(
            f'<section><h2>{escape(row["cache_key"])} '
            f'<small>{fresh} · 분석 {gen_kst} KST</small></h2>'
            f'{row["result_html"]}</section>'
        )
    return "<html><body>" + "".join(parts) + "</body></html>"
```

별도 템플릿 파일 분리는 YAGNI.

## 8. UI 변경

### 대시보드 카드 (`/`)

기존:
```
┌─────────────────┐
│ Apple    [미국] │
│ AAPL            │
│ [▶ 분석] [🗑 삭제] │
└─────────────────┘
```

변경 후:
```
┌──────────────────────────────┐
│ Apple                  [미국] │
│ AAPL                          │
│ 🟢 16:02 (3시간 전)            │
│ [▶ 결과 보기] [🔄] [🗑]        │
└──────────────────────────────┘
```

신선도 줄 규칙:
- 캐시 없음: `⚪ 분석 이력 없음` + 버튼 `▶ 분석 시작` (POST /analyze, return_to=jobs)
- fresh: `🟢 16:02 (Nh 전)` 초록
- 만료: `🟡 16:02 (어제)` 노랑

색상은 기존 CSS 토큰 `--green-100/600`, `--amber-100/500` 재사용.

### 결과 페이지 (`/stock/<symbol>`)

상단 메타바 카드 + 기존 리포트 HTML:
```
┌────────────────────────────────────────────────────────┐
│ Apple (AAPL)                                          │
│ 🟢 분석 시각: 2026-05-05 16:02 KST · 자동분석          │
│ [🔄 재분석]                                            │
└────────────────────────────────────────────────────────┘
[기존 result_html — 차트·ML 예측·뉴스 등]
```

만료된 경우 노란 배경:
```
┌────────────────────────────────────────────────────────┐
│ Apple (AAPL)                                          │
│ 🟡 분석 시각: 2026-05-04 16:02 KST · 자동분석          │
│ ⚠️ 마지막 분석 후 시장이 다시 마감되었습니다. 재분석 권장 │
│ [🔄 재분석]                                            │
└────────────────────────────────────────────────────────┘
```

### 인라인 갱신

진행 중 오버레이:
```
┌────────────────────────────────────────────────────┐
│ Apple (AAPL)                                       │
│ 🟢 분석 시각: 2026-05-05 16:02 KST · 자동분석       │
│ [🔄 재분석 중... 16:34 시작]                         │
├────────────────────────────────────────────────────┤
│  ╔════════════════════════════════════════╗        │
│  ║ ⏳ 새 분석 진행 중 (예상 30~60초)        ║        │
│  ╚════════════════════════════════════════╝        │
│  [기존 캐시 결과 — opacity 0.5 흐리게]              │
└────────────────────────────────────────────────────┘
```

## 9. 인라인 갱신 메커니즘

### 폼 → 서버 → 결과 페이지

```
[결과 페이지의 재분석 폼]
  POST /analyze/<symbol>  body: csrf_token, return_to=stock
       ↓
  서버: job 시작 후
       redirect /stock/<symbol>?job=<id> (303)
       ↓
  결과 페이지: ?job=<id> 감지
    │
    ├─ _jobs[<id>].status == "running"
    │     → 오버레이 + 폴링 JS 삽입
    │
    └─ status != running (또는 _jobs 에 없음)
          → redirect /stock/<symbol> (쿼리 제거 PRG)
```

### 폴링 JS

```javascript
(() => {
  const jobId = "{{job_id}}";
  if (!jobId) return;
  const tick = async () => {
    try {
      const res = await fetch(`/api/jobs/${jobId}`);
      if (res.status === 404) {
        window.location.replace(window.location.pathname);
        return;
      }
      const data = await res.json();
      if (data.status === "done" || data.status === "error") {
        window.location.replace(window.location.pathname);
        return;
      }
    } catch (_) { /* 일시 네트워크 오류 — 다음 틱 재시도 */ }
    setTimeout(tick, 2000);
  };
  setTimeout(tick, 2000);
})();
```

`_run_analysis_bg` 가 종료 시 `analysis_cache.put` 을 호출하므로 reload 시점에 새 캐시가 이미 들어가 있음.

### 엣지 케이스

| 케이스 | 동작 |
|---|---|
| 폴링 중 페이지 새로고침 | `?job=<id>` 살아있으면 오버레이 재진입. status 변경 시 PRG redirect |
| 다른 탭에서 또 재분석 클릭 | 새 job_id. 두 job 모두 종료 시 last-write-wins. 첫 탭은 첫 job 끝나면 reload → 최종 결과 표시 (자연스러움) |
| `_jobs` 50개 limit 로 job 제거된 후 폴링 | `/api/jobs/<id>` 404 → 즉시 reload (캐시는 들어있을 가능성 높음) |
| `analysis_cache.put` 실패 | warning log, job 결과는 정상. reload 후 캐시 miss 안내 페이지 |

## 10. 에러 처리

| 시나리오 | 처리 |
|---|---|
| `analysis_cache.get` sqlite 락/오류 | try/except → None 반환 + warning log → 캐시 miss 안내 |
| 자동 cron 중 사용자 수동 재분석 | 둘 다 진행, last-write-wins. 별도 락 없음 |
| `_run_analysis_bg` → `analysis_cache.put` 실패 | warning log, job 정상 표시 (`prediction_history.insert_live` 패턴 동일) |
| `is_fresh` 시스템 시계 이상 | 표시만 어긋나고 데이터 손상 없음 |
| sqlite 파일 권한 오류 | `init_db` 에서 raise (기존 패턴) |
| 자동 cron 일부 종목 fetch 실패 | 그 종목 skip + 나머지 진행. 이전 캐시 row 그대로 유지 |
| `daily_email_job` 시점 캐시 비어있음 | 이메일 skip + warning log |
| 종목을 settings.yaml 에서 다른 시장으로 옮김 | 다음 자동 cron 이 새 market 으로 UPSERT. 만료 시각 그 사이 1회 어긋날 수 있음 (수용) |
| settings.yaml 에서 종목 삭제 후에도 캐시 row 존재 | `analysis_cache.list_symbols` 가 그 row 도 반환 — 이메일에 노출. 별도 정리 cron 안 만듦 (운영 시 수동 삭제하거나 다음 단계에서 처리) |

## 11. 모듈 책임

```
src/analysis_cache.py     ── DB 액세스 (init_db, get, put, is_fresh, list_symbols,
                              _next_market_open_kst — 만료 시각 계산)
src/web_app.py            ── 라우트 + 메타바 렌더 헬퍼
src/email_sender.py       ── render_email_digest 추가
main.py                   ── auto_analyze_market, daily_email_job + extra_jobs 등록
```

`_next_market_open_kst(market, generated_at)` 은 별도 함수로 분리해 단위 테스트 용이성 확보.

## 12. 테스트 전략

기존 `tests/conftest.py` 의 `_DB_PATH` 격리(`pytest_configure`) 활용 — 새 테이블은 같은 DB 파일에 추가되므로 격리도 자동 적용.

### `tests/test_analysis_cache.py` (신규)

1. `init_db()` 멱등성 — 두 번 호출해도 오류 없음
2. `put` → `get` 라운드트립
3. `put` UPSERT — 같은 cache_key 두 번 insert → row 1개, source/generated_at 갱신
4. `is_fresh` 한국 — generated_at KST 16:00 → 다음날 08:59 True / 09:01 False
5. `is_fresh` 미국 표준시 — generated_at KST 06:00 → 같은 날 22:00 True / 22:31 False
6. `is_fresh` 미국 서머타임 — generated_at KST 06:00 → 같은 날 22:00 False / 21:30 True (서머타임은 미국 마감 23:30)
7. `is_fresh` "ALL" — 모든 종목 fresh → True / 한 종목 만료 → False
8. `list_symbols` — "ALL" row 제외, market·cache_key 정렬
9. `_next_market_open_kst("korea", t)` 단위 테스트 — 다양한 시각

### `tests/test_web_app.py` (신규/보강)

1. `GET /stock/<symbol>` 캐시 hit → result_html 포함, 메타바 포함
2. `GET /stock/<symbol>` 캐시 miss → "분석 시작" 안내 페이지
3. `GET /stock/all` 캐시 hit / miss
4. `POST /analyze/<symbol>` + return_to=stock → redirect `/stock/<symbol>?job=<id>`
5. `POST /analyze/<symbol>` + return_to=jobs → redirect `/jobs/<id>`
6. `POST /analyze/<symbol>` CSRF 누락 → 403
7. `GET /stock/<symbol>?job=<id>` running → 페이지에 폴링 스크립트 + 오버레이 마크업 포함
8. `GET /stock/<symbol>?job=<id>` job done/없음 → redirect `/stock/<symbol>` (쿼리 제거)

### `tests/test_main_scheduler.py` (신규)

1. `auto_analyze_market("korea")` — 한국 종목만 처리, 캐시 row 갱신 확인
2. `daily_email_job` 캐시 비어있음 → `send_report` 호출 안 됨
3. `daily_email_job` 캐시 있음 → `send_report` 호출, body 에 종목명 포함

`analyze_stock` 자체 테스트는 기존 그대로 (ML 모델 mocking 안 함).

## 13. 마이그레이션 / 배포

1. `analysis_cache.py` 작성 + 테스트 통과
2. `init_db` 호출 추가 (`prediction_history.init_db()` 옆)
3. 라우트 변경 (`/stock`, `/analyze` POST 화) + 카드/결과 페이지 UI
4. main.py 자동분석 cron + `daily_job` → `daily_email_job` 교체
5. 로컬에서 `--start-scheduler` 실행 + 다음 16:00 / 06:00 동작 로그 확인
6. EC2 배포 시 기존 sqlite 파일 그대로 (새 테이블만 추가)

## 14. 비목표 (Non-goals)

- 캐시 이력 누적 (시간별 결과 비교) — `prediction_history` 가 별도 담당
- 영업일 판정 / 휴장일 스킵 — yfinance 응답 신뢰
- 캐시 결과 외부 공유 / 영구 URL — 단일 사용자 환경
- "전체 분석" 도 종목별 row 자동 갱신 — `/analyze-all` 은 "ALL" row만 갱신
- WebSocket / SSE 실시간 push — 폴링 충분
- 캐시 압축 / 별도 BLOB 분리 — TEXT 충분 (~150KB)
