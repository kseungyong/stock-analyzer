# Leader Stock Finder — Design Spec

- **작성일**: 2026-05-15
- **작성자**: kseungyong + Claude (`/brainstorming` 세션)
- **상태**: 설계 완료 (사용자 5섹션 승인) → 사용자 spec 파일 검토 대기

## 1. Goal

사용자 선별 universe (KOSPI200 + KOSDAQ150 중 50종 + KR ETF 6, `auto-trader/config/universe.yaml`) 에서 **"주도주 5가지 조건"** 에 부합하는 후보를 발굴하고, 종목별로 각 조건의 부합 상세를 볼 수 있는 stock-analyzer 내 신규 페이지 (`/leaders` + `/leaders/<symbol>`) 를 구축한다.

### 주도주 5조건 (사용자 정의)

| # | 조건 | 자동/정성 |
|---|---|---|
| 1 | **가격** — 신고가 근처 + 시장 대비 +20%p 이상 + 시총 상위 20% (대형주) | 정량 자동 |
| 2 | **이익** — 현재 흑자 또는 컨센서스 성장. 미래 이익에 대한 합리적 상상력 | 정량(현재) + 정성(상상력) |
| 3 | **밸류에이션** — PER 무관 (멀티플의 함정 탈피). 분위만 표시 | 참고용 표시만 |
| 4 | **글로벌 트렌드** — TAM 글로벌 확장 + 내러티브 확장성 (GPU→전력→메모리 식) | 정성 LLM |
| 5 | **병목과 해자** — 산업 밸류체인 필수 구간 + 그 구간 내 진입장벽 | 정성 LLM |

## 2. Scope

### In scope
- stock-analyzer 단독 신규 모듈. auto-trader 와 결합 없음 (auto-trader 의 `config/universe.yaml` 만 데이터 소스 참조)
- universe 범위: `auto-trader/config/universe.yaml` 의 `kospi200:` + `kosdaq150:` 섹션만 합산 (~50종). **`etf:` 섹션은 제외** (개별 종목 주도주 발굴 목적, ETF 가격 추세는 별개 분석 대상)
- 정량 hard filter (1·2번) → 통과 종목만 LLM 분석 (4·5번)
- 3번 (밸류에이션) 은 filter 아닌 참고 표시 — 사용자 정의에 따라 PER 분위만 계산
- Gemini 2.5 Flash 단일 모델로 정성 4필드 (TAM/내러티브/병목/해자) JSON 출력
- 사용자 수정본 (user_*) 과 LLM 초안 (llm_*) 분리 저장, 표시 시 사용자 수정본 우선
- 신규 진입 자동 LLM + 7일 경과 stale 배지 + 사용자 수동 재분석 트리거
- launchd cron `ai.stock-analyzer.leaders` 매일 16:30 KST (KR 분석 16:00 cron 후 22분 buffer)

### Out of scope (의도적 YAGNI)
- 종목 비교 페이지 (여러 종목 나란히 비교)
- 알림 (텔레그램/메일) — leaders 변동 알림
- 자동 매매 통합 — auto-trader 와 명시적 결합 없음
- 백테스트 — 과거 leaders 페이지 시뮬레이션
- 미국 종목 — 한국 시장 (KOSPI/KOSDAQ) 전용
- ETF — universe.yaml 의 ETF 섹션은 파싱 단계에서 제외

## 3. Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  stock-analyzer (Flask + SQLite + launchd)                       │
│                                                                  │
│  ┌─────────────────┐    ┌─────────────────┐                     │
│  │ leader_filter.py│    │ leader_llm.py   │                     │
│  │ (정량 1·2·3)    │    │ (정성 4·5 초안) │                     │
│  └────────┬────────┘    └────────┬────────┘                     │
│           │                       │                              │
│           ▼                       ▼                              │
│  ┌─────────────────────────────────────────┐                    │
│  │ leader_cache.py (SQLite leaders 테이블)  │                    │
│  └───────────────────┬─────────────────────┘                    │
│                      │                                           │
│      ┌───────────────┴───────────────┐                          │
│      ▼                               ▼                          │
│  GET /leaders               GET /leaders/<symbol>                │
│  (목록 페이지)                (상세 페이지 + 수정)                │
│      ▲                               ▲                          │
│      │                               │                          │
│  cron (16:30 KST)              사용자 (브라우저)                 │
│  ai.stock-analyzer.leaders                                       │
└─────────────────────────────────────────────────────────────────┘
```

### 데이터 소스
- universe: `../auto-trader/config/universe.yaml` (~50종 (universe.yaml 의 kospi200+kosdaq150 섹션 합산) + KR ETF 6) — 파싱 read-only
- 가격: yfinance `Ticker.history(period="1y")`
- fundamentals: yfinance `Ticker.info` dict
- 시장 지수: `^KS11` (KOSPI), `^KQ11` (KOSDAQ)
- LLM: Gemini 2.5 Flash (`.env` 의 `GEMINI_API_KEY`)

## 4. Components

### 4.1 `src/leader_filter.py` — 정량 hard filter

**입력**: universe symbol 리스트
**출력**: `list[LeaderCandidate]` dataclass

**Hard filter 규칙** (모두 통과해야 진입):

| 조건 | 규칙 |
|---|---|
| 1번 가격 (a) 신고가 | `last_close / 52w_high ≥ 0.85` (신고가 대비 -15% 이내) |
| 1번 가격 (b) 시장대비 | `return_1y_pct ≥ index_return_1y_pct + 20%p`. 비교 지수: `kospi200:` 섹션 종목 → `^KS11`, `kosdaq150:` 섹션 종목 → `^KQ11` |
| 1번 가격 (c) 대형주 | `market_cap_quintile == 1` (시총 상위 20%). **모집단: universe 전체 ~50종 단일 컷오프**. 시장별 분리 X — universe 가 이미 사용자 선별 대형주 풀이므로 상위 ~10종이 후보 |
| 2번 이익 | `(trailing_eps > 0) OR (forward_eps > trailing_eps)` |
| 3번 밸류 | filter 아님 — `pe_quintile` 만 계산 (universe 전체 분위) |

데이터 fetch: 50종 × yfinance ~200ms = ~10초 (직렬). cron 윈도 매우 여유.

### 4.2 `src/leader_llm.py` — 정성 분석

**Gemini 2.5 Flash 호출**, 종목당 1회.

**System instruction** (고정):
> 당신은 주식 시장의 주도주 분석 전문가다. 입력으로 받은 한국 종목에 대해 4가지 정성 조건을 산출한다. 출력은 반드시 strict JSON, 다른 텍스트 금지. 데이터 부족 시 추정 금지 — "데이터 부족" 명시. 마케팅 어조 금지, 사실 기반 분석만.

**User prompt 템플릿**:
```
종목: {name} ({symbol})
시장: {market}, 섹터: {sector}, 산업: {industry}
시가총액: {market_cap_won}, 1년 수익률: {return_1y_pct}%, 시장지수 대비 +{rel_return_pp}%p
trailing EPS: {trailing_eps}, forward EPS: {forward_eps}, 매출 성장률: {revenue_growth_pct}%
trailing PE: {trailing_pe}

아래 4가지를 분석해 JSON 으로만 응답:

{
  "tam_narrative": "이 회사가 속한 글로벌 산업의 TAM 규모와 성장 동인. 3~5문장.",
  "narrative_expansion": "이 회사 이야기가 인접 섹터로 확장 가능한가 (예: GPU→전력→메모리). 2~3문장.",
  "bottleneck": "산업 밸류체인 내 반드시 거쳐야 하는 구간을 점유하는가. 2~3문장.",
  "moat": "그 구간 내 경쟁자 진입 장벽 (기술/특허/규모/네트워크). 2~3문장."
}
```

**호출 설정**:
- 모델: `gemini-2.5-flash`
- temperature: 0.3
- response_mime_type: `application/json`
- max_output_tokens: 1024
- timeout 30s, retry 1회 (exponential backoff 2s)

**비용 예상**: 종목당 ~1000 in + 500 out tokens. 일일 통과종 평균 5종 가정 (50종 중 hard filter 통과) × 30일 = 150 호출/월 ≈ $0.05/월 (Gemini Flash 가격 기준). 신규 진입 + stale 갱신만이므로 실제는 더 낮음.

### 4.3 `src/leader_cache.py` — SQLite 영속화

`leaders` 테이블 (스키마는 §6). CRUD 인터페이스:
- `init_db()` — `CREATE TABLE IF NOT EXISTS` (모듈 import 시점)
- `list_active() -> list[Row]` — 표시용 (passed=1 AND status='active')
- `get(symbol) -> Row | None` — 상세 페이지용 (탈락 종목 포함)
- `upsert_quantitative(candidates)` — cron 의 정량 갱신
- `upsert_llm(symbol, result)` — LLM 결과 갱신, user_* 미변경
- `update_user_fields(symbol, fields, user)` — 사용자 수정 저장. `user` 는 `_current_username()` 헬퍼 결과 (Session 인증 시 `session['username']`, Basic Auth 시 `request.authorization.username`, 둘 다 없으면 `'anonymous'`)
- `mark_dropped(symbols)` — filter 통과 못한 기존 row → status='dropped'
- `recompute_stale()` — llm_generated_at 기준 7일 경과 → is_stale=1

표시 헬퍼:
- `display_field(row, name) -> str` — `user_<name>` if not NULL else `llm_<name>` else "(분석 대기 중)"

### 4.4 Flask 라우트

| 메서드 | 경로 | 동작 |
|---|---|---|
| GET | `/leaders` | 통과 종목 표 (정렬/필터 사이드바). 컬럼: 종목·시장·last_close·return_1y·near_high·EPS·PER·LLM stale·사용자 수정 여부 |
| GET | `/leaders/<symbol>` | 5축 스코어카드 + 8개 텍스트 필드 (1·2·3 정량 + 4·5 user/llm × 2) + 인라인 수정 UI |
| POST | `/leaders/<symbol>/edit` | 4개 필드 (tam/narrative/bottleneck/moat) 부분 업데이트 → `user_*` 저장 |
| POST | `/leaders/<symbol>/refresh` | LLM 재호출 강제 트리거 → `llm_*` 만 덮어쓰기 |

CSRF: POST 두 라우트는 기존 `_csrf_validate` 적용.

### 4.5 main.py CLI + launchd

- `python main.py leaders-refresh` — cron 진입점, 위 흐름 1~6 실행
- launchd `~/Library/LaunchAgents/ai.stock-analyzer.leaders.plist` — 매일 16:30 KST 단발 cron, KeepAlive=false, 기존 stock-analyzer plist 패턴 동일 (`KMP_INIT_AT_FORK=FALSE` 등 fork 안전 env 포함)

## 5. Data Flow

### Cron 흐름 (매일 KST 16:30)

```
ai.stock-analyzer.leaders launchd
  → main.py leaders-refresh
     1. universe 로드 (../auto-trader/config/universe.yaml 파싱)
     2. leader_filter.run_filter(symbols) → passed N종 (5~30)
     3. leader_cache.diff_with_existing → 신규/유지/stale/탈락 4분류
     4. leader_llm.analyze_batch(신규 + stale) → 순차 Gemini 호출
     5. leader_cache.upsert_all(...)  ※ user_* 보존
     6. 로그: "leaders refresh: passed=N llm_calls=M errors=E"
```

### 사용자 요청 흐름

```
GET /leaders           → list_active() → 표 렌더
GET /leaders/<symbol>  → get(symbol) → 5축 스코어카드
POST /leaders/<sym>/edit  → update_user_fields(...) → 4 필드 user_* 저장
POST /leaders/<sym>/refresh → analyze_one() → llm_* 갱신 (user_* 미변경)
```

### 흐름 핵심 결정

- **cron 시점 16:30** — KR analysis 16:00 cron 종료 (~16:08) 후 buffer 22분. yfinance 데이터는 KR 마감 15:30 이후 finalize.
- **탈락 종목 row 유지** — 사용자 수정본 보존. 다시 진입 시 LLM 메모 재사용 (단 7일 stale 체크).
- **user 수정본 + LLM 분리 저장** — 사용자가 LLM 메모와 자기 분석 비교 가능. 사용자 수정본은 LLM refresh 가 절대 덮어쓰지 않음.
- **순차 LLM 호출** — 신규/stale ~5종 × 2초 = ~10초. 병렬화 불필요.

## 6. Schema

`predictions.db` 에 추가 (기존 `analysis_cache` 와 동일 DB):

```sql
CREATE TABLE IF NOT EXISTS leaders (
    symbol              TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    market              TEXT NOT NULL,                -- 'KOSPI' | 'KOSDAQ'
    sector              TEXT,
    industry            TEXT,

    -- 정량 (1·2·3번)
    last_close          REAL NOT NULL,
    market_cap          INTEGER,
    market_cap_quintile INTEGER,                      -- 1=상위20%, 5=하위20%
    near_high_pct       REAL,                         -- last_close / 52w_high
    return_1y_pct       REAL,
    index_return_1y_pct REAL,
    rel_return_pp       REAL,                         -- return_1y - index_return_1y
    trailing_eps        REAL,
    forward_eps         REAL,
    eps_growth_yoy      REAL,
    trailing_pe         REAL,
    pe_quintile         INTEGER,

    -- 조건별 통과 여부
    cond1_passed        BOOLEAN NOT NULL,
    cond2_passed        BOOLEAN NOT NULL,
    cond3_score         INTEGER,                      -- 1~5 (참고용)
    passed              BOOLEAN NOT NULL,             -- cond1 AND cond2

    -- LLM 초안 (4·5번)
    llm_tam_narrative        TEXT,
    llm_narrative_expansion  TEXT,
    llm_bottleneck           TEXT,
    llm_moat                 TEXT,
    llm_raw_response         TEXT,
    llm_generated_at         INTEGER,                 -- unix epoch
    llm_model                TEXT,
    llm_error                TEXT,                    -- 실패 사유

    -- 사용자 수정본
    user_tam_narrative       TEXT,
    user_narrative_expansion TEXT,
    user_bottleneck          TEXT,
    user_moat                TEXT,
    user_edited_at           INTEGER,
    user_edited_by           TEXT,                    -- BASIC_AUTH username

    -- 메타
    status              TEXT NOT NULL DEFAULT 'active', -- 'active' | 'dropped'
    is_stale            BOOLEAN NOT NULL DEFAULT 0,
    refreshed_at        INTEGER NOT NULL,
    created_at          INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_leaders_passed_status ON leaders(passed, status);
CREATE INDEX IF NOT EXISTS idx_leaders_market ON leaders(market);
```

## 7. Error Handling

| 실패점 | 정책 | 사용자 영향 |
|---|---|---|
| `universe.yaml` 파싱 실패 | cron exit 1 + stderr log. 기존 leaders 테이블 유지 | 그날 cron 만 누락, 페이지는 어제 결과 표시 |
| yfinance fetch 실패 (rate limit / delisted) | 종목별 skip + 카운트 로그. skip 률 >10% 면 cron exit 1 | 실패 종목만 정량 stale, LLM 메모 유지 |
| `Ticker.info` 필수 필드 None | 해당 종목 `cond2_passed=False` (안전 default). `llm_error="data_missing"` | 표에서 제외 |
| Gemini API rate limit / 5xx | 종목당 retry 1회 (2s backoff). 또 실패면 `llm_error` 컬럼. 정량은 정상 저장 | "LLM 분석 실패" 배지 + 다음 cron 재시도 |
| Gemini 응답이 strict JSON 아님 | `llm_raw_response` 에 원본 + `llm_error="parse_failed"`. user_* 있으면 표시 우선 | 사용자가 raw 보고 수동 입력 |
| 동시 cron + 사용자 수정 | user_* 와 llm_* 컬럼이 분리되어 있어 충돌 자체 없음. user 저장은 `update_user_fields` 만 사용 | 없음 |
| Gemini 비용 폭증 | 환경변수 `LEADER_LLM_DAILY_LIMIT=50` cap. 초과 시 skip + log warning | 그날 LLM 미갱신, 다음날 재시도 |

## 8. Testing

```
tests/test_leader_filter.py     (정량 hard filter)
tests/test_leader_llm.py        (LLM wrapping, mock Gemini)
tests/test_leader_cache.py      (SQLite + user 수정본)
tests/test_leaders_routes.py    (Flask 라우트 4개)
tests/test_leaders_e2e.py       (cron 흐름 end-to-end)
```

### 핵심 시나리오

- `filter_passes_when_all_3_conds_met` — happy path
- `filter_fails_cond1_when_below_high` — 신고가 -20% 이하 빠짐
- `filter_fails_cond1_when_smaller_than_market_plus_20pp` — 상대 수익률 미달
- `filter_fails_cond1_when_market_cap_below_top20` — 시총 하위 빠짐
- `filter_passes_cond2_with_forward_growth_only` — trailing 음수여도 forward 성장
- `filter_ignores_cond3_pe` — PER 높아도 통과 (사용자 요구사항)
- `llm_retries_on_5xx_once` — Gemini 5xx → 1회 재시도
- `llm_marks_parse_failed_on_non_json` — JSON 깨지면 error 컬럼 채움
- `llm_respects_daily_limit` — `LEADER_LLM_DAILY_LIMIT` 초과 시 skip
- `cache_user_edit_overrides_llm` — user_* 가 표시 우선
- `cache_drops_status_when_filter_fails` — 탈락 시 status='dropped' (삭제 X)
- `cache_marks_stale_after_7days` — 7일 경과 is_stale=1
- `cache_upsert_llm_preserves_user_fields` — LLM refresh 가 user_* 유지
- `route_get_leaders_lists_passed_active_only` — `status='active' AND passed=1`
- `route_post_refresh_updates_only_llm` — user_* 보존
- `route_post_edit_partial_update` — 1~4 필드 일부만 변경 가능
- `e2e_cron_flow_with_fake_clients` — fake yfinance + fake Gemini

회귀: stock-analyzer 기존 282 테스트 변경 없음. 신규 ~25 테스트.

## 9. Dependencies

- `google-generativeai` (Gemini Python SDK) — 신규 의존성
- `yfinance` — 이미 사용 중
- 기존 `pytest`, `Flask`, `apscheduler` (는 안 씀 — launchd 분리 운영)

`.env` 신규 변수:
- `GEMINI_API_KEY` — Gemini Flash 호출 키
- `LEADER_LLM_DAILY_LIMIT=20` — 비용 폭증 차단 (universe 50종 기준, 일일 통과종 ~5종 가정에 4배 여유)
- `AUTO_TRADER_UNIVERSE_PATH=../auto-trader/config/universe.yaml` — universe 파일 경로 (기본값)

## 10. Migration / Rollout

1. spec 승인 후 `writing-plans` 로 구현 plan 작성
2. 코드 구현 → 회귀 테스트 통과
3. 로컬에서 `python main.py leaders-refresh` 1회 수동 실행 → 결과 검증
4. 로컬 dev server 로 `/leaders` `/leaders/<symbol>` UI 확인
5. 원격 macmini 로 commit/push/pull
6. launchd plist 설치 + load
7. 다음 정식 거래일의 16:30 첫 cron 결과 모니터링 (5/15 금 → 5/18 월). 5/16~17 주말 cron 도 실행되지만 데이터는 5/15 종가 기준 동일

## 11. Open Questions

없음. 모든 핵심 결정은 brainstorming 6 단계 사용자 확정.

## 12. Non-goals (재강조)

- **auto-trader 통합 0**: 종목 push/pull 어느 방향도 없음. universe.yaml 만 read-only 참조
- **자동 매매 영향 0**: leaders 페이지는 정보 제공만, 실 주문에 영향 없음
- **PER 컷오프 없음**: 사용자 요구사항 "PER 무관" 그대로 반영, 분위만 표시
