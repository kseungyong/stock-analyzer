# DART 공시 통합 — Phase A 설계서

날짜: 2026-05-23
대상: stock-analyzer
상태: Draft (시니어 검수 대기)

---

## 0. 사전 준비 (구현 시작 전)

1. **DART_API_KEY 이미 보유** ✅ (사용자 확인) — `.env` 에 `DART_API_KEY=...` 추가 (로컬) + macmini `~/Library/LaunchAgents/ai.stock-analyzer.dart-refresh.plist` 의 `EnvironmentVariables` 에도 등록 (cron)
2. **GEMINI_API_KEY** 는 leader_filter 이미 사용 중이라 추가 작업 없음
3. **출처 표기 의무**: DART OpenAPI ToS 에 따라 카드 footer 에 "출처: DART (금감원 전자공시)" 표기

## 1. 목적

한국 종목 분석 카드에 **"공시정보분석"** 섹션 추가. DART (전자공시시스템) 의 주요사항보고서(DS005) + 지분공시(DS004) 를 매일 1회 일괄 fetch → critical event 가 있는 종목만 분석 → 카드 하단에 매매 관점 해석 표시.

**요약 전략 (Hybrid)**:
- 단일 critical event → 규칙 기반 template (LLM skip, 0 호출)
- 2개 이상 critical event → Gemini LLM 종합 해석 (월 ~30 호출)

이 전략으로 LLM 호출 65/일 → 평균 1~3/일 로 95%+ 감소, 환각 위험 + 비용 + Gemini quota 부담 동시 해결.

성공 조건:
- 자기주식 취득 결정/유상증자/대량보유 신고 등 즉시 영향 큰 공시 자동 감지
- 카드에서 "공시정보분석" 섹션으로 2-3문장 요약 + 매매 관점 (매수/매도/관망)
- web 카드 배지에 작은 공시 sentiment 표시 (🟢/🔴/🟡)
- 미국 종목 흐름 영향 0

**Phase 분리**:
- **Phase A (본 spec)**: DS005 주요사항보고서 (6개) + DS004 지분공시 (2개) + DS001 list (overview)
- Phase B (별도): DS001 일반 공시검색 (전체 list)
- Phase C (별도): DS002 배당/자기주식/임원 등 정기보고서

## 2. 아키텍처

```
신규:
  src/dart_client.py    — DART API HTTP wrapper + corp_code 매핑
  src/dart_rules.py     — critical event 분류 + 규칙 기반 template (hybrid 단일 case)
  src/dart_cache.py     — disclosures + corp_codes + dart_summaries 테이블 (DB layer)
  src/dart_llm.py       — Gemini 요약 (hybrid 복수 case 전용)
  src/log_filter.py     — SecretFilter (DART_API_KEY redaction)

수정:
  src/report_generator.py — _render_dart_section() + _render_stock_card 호출
  src/web_app.py        — home/portfolio 카드 배지 (공시 sentiment) + dart_summaries JOIN
  src/templates/report.css — .dart-section 스타일
  main.py               — dart-refresh subcommand + 모듈 로드 시점 init_db

신규 cron:
  ~/Library/LaunchAgents/ai.stock-analyzer.dart-refresh.plist  (macmini, KST 19:30)
  → 18:00 → **19:30 변경** (DS005 주요사항보고서는 KST 18:00~19:00 집중 접수,
     19:30 이면 그날 공시 99% 수집 가능)

DB (data/predictions.db):
  corp_codes              (corp_code, corp_name, stock_code, modify_date)
  disclosures             (id, corp_code, stock_code, disclosure_type, rcept_no, rcept_dt, raw_json, fetched_at)
  dart_summaries          (symbol PRIMARY KEY, summary_json, sentiment, critical_count, generated_at, model)
                          ← analysis_cache 와 분리. race condition 회피.
```

**analysis_cache 미수정** — dart_summaries 별도 테이블로 책임 분리. report_generator/web_app 이 LEFT JOIN (또는 별도 lookup) 으로 합성.

신규 의존성 0개 (`requests`, `google.generativeai` 모두 기존 사용 중).

## 3. DART API 매핑

### 3.1 corp_code 매핑 (DS001)

DART는 `corp_code` (8자리 고유번호)로 조회. yfinance/KRX 종목코드 매핑 필요.

```
GET https://opendart.fss.or.kr/api/corpCode.xml?crtfc_key={KEY}
→ ZIP 파일 (CORPCODE.xml) 반환
→ unzip → XML parse → {corp_code, corp_name, stock_code, modify_date} rows
→ corp_codes 테이블 UPSERT (전체 ~90,000 회사)
```

다운로드 정책:
- **주 1회 (7일 stale)** 자동 갱신 — 신규 상장 종목의 IPO 1주 차 공시 누락 방지
- ZIP 다운로드 실패 시 → warn + 기존 테이블 그대로 사용
- 신규 상장 종목이 corp_codes 에 없으면 → **즉시 corp_codes 재다운로드 트리거** (ZIP ~5MB 라 비용 부담 없음) + 그래도 없으면 skip + warn

### 3.2 Phase A 사용 endpoint (9개)

| Group | endpoint | 의미 | Trading signal |
|---|---|---|---|
| DS001 | `list.json?corp_code=X&bgn_de=YYYYMMDD&end_de=YYYYMMDD&pblntf_detail_ty=PBL` | 최근 N일 공시 목록 | overview |
| DS005 | `piicDecsn.json` | 유상증자 결정 | 매도 (희석) |
| DS005 | `pifricDecsn.json` | 무상증자 결정 | 중립 (분할) |
| DS005 | `crDecsn.json` | 감자 결정 | 매도 강 |
| DS005 | `tsstkAqDecsn.json` | 자기주식 취득 결정 | 매수 |
| DS005 | `tsstkDpDecsn.json` | 자기주식 처분 결정 | 매도 |
| DS005 | `cmpMgDecsn.json` | 회사합병 결정 | 변동성 |
| DS004 | `majorstock.json?corp_code=X` | 대량보유(5%+) 변동 | 단기 수급 |
| DS004 | `elestock.json?corp_code=X` | 임원/주요주주 소유 변동 | 자사매수=매수 강 |

### 3.3 Rate limit

- DART 공식 limit: **1만 호출/일 + 1초당 10회**
- Phase A 부하: 65종목 × 9 API = **585 호출/일** (limit 의 5.85%)
- 호출 간 `time.sleep(0.5)` (jitter 없음) → 안전 마진 확보
- 한 cron 실행 시간 추정: 65 × 9 × 0.5s = **약 5분**

### 3.4 조회 윈도

- DS005/DS004: 최근 **30일** (`bgn_de`/`end_de` 파라미터)
- 30일 이내 critical event 없으면 → LLM skip (cost 절감)

## 4. critical event 판정 + 임계치

`src/dart_rules.py:classify_disclosures(disclosures: dict) -> dict`:

```python
def classify_disclosures(disclosures: dict) -> dict:
    """critical event 분류 + tier 결정.

    Returns:
        {
            "critical_events": [{"type": str, "tier": "high"|"medium", "raw": dict}, ...],
            "count": int,
            "should_call_llm": bool,   # 2개 이상이면 True (hybrid)
        }
    """
```

**tier 1 (high) — 임계치 없이 무조건 critical**:
- `capital_increase` (유상증자), `capital_decrease` (감자)
- `treasury_acquire`, `treasury_dispose`
- `merger`

**tier 2 (medium) — 임계치 적용**:
- `major_holders` (대량보유): **변동 비율 >= 0.5%p AND 보유 비율 >= 5%**
- `exec_holders` (임원/주요주주): **금액 >= 1억원 OR 변동 주식수 >= 1000주**

→ 노이즈 (임원 1주 매수 등) 차단.

**Hybrid 전략**:
- `count == 0` → LLM skip, `dart_summaries.summary_json = {"empty": true}` 저장
- `count == 1` → 규칙 기반 template (dart_rules.render_template), LLM skip
- `count >= 2` → Gemini LLM 호출 (복잡 종합 해석 필요)

**규칙 기반 template 예시** (`dart_rules.render_template`):
- `treasury_acquire(200억원)` → `{"summary": "자기주식 200억원 취득 결정 — 주주환원 시그널", "sentiment": "긍정", "key_events": ["자기주식 취득 200억원"], "trading_view": "매수 — 자사주 매입은 EPS 상승 + 회사 자신감 표명"}`
- `capital_increase(1000억원)` → `{"summary": "유상증자 1000억원 결정 — 신주 발행에 따른 희석", "sentiment": "부정", "key_events": ["유상증자 1000억원"], "trading_view": "매도 — 기존 주주 지분 희석 예상"}`

## 5. LLM 요약 (Gemini, hybrid 복수 case 전용)

`src/dart_llm.py:summarize_disclosures(symbol, name, classified: dict) -> dict | None`:

**호출 조건**: `classified["should_call_llm"] is True` (count >= 2). 단일 critical 은 dart_rules.render_template 사용.

**모델**: `gemini-2.5-flash` (leader_filter 동일).

**프롬프트** — 환각 방지 위해 사실 인용 규칙 추가:
```
당신은 한국 주식 시장의 공시 분석 전문가입니다.
종목: {name} ({symbol})

최근 30일 주요 공시 (분류된 critical events):
{critical_events_json}

다음 규칙을 엄격히 지켜:
1. key_events 의 각 항목은 반드시 입력 disclosures 에 있는 rcept_no (접수번호) 를 인용. rcept_no 없으면 출력 금지.
2. sentiment 는 "긍정" / "부정" / "중립" 중 하나만 (정확한 enum).
3. trading_view 는 "매수" / "매도" / "관망" 중 하나로 시작하고, " — " 다음에 1줄 근거.

응답은 아래 JSON 형식만:
{
  "summary": "2-3문장 종합 해석 (여러 공시의 상호 영향)",
  "sentiment": "긍정" | "부정" | "중립",
  "key_events": ["[rcept_no] 사실 인용 1", "[rcept_no] 사실 인용 2"],
  "trading_view": "매수|매도|관망 — 1줄 근거"
}
```

**generation_config**:
- `temperature: 0.3`
- `max_output_tokens: 1024` (3-4 필드 한국어 충분, 2048 → 1024 축소)
- `response_mime_type: "application/json"`

**반환 형식 + 검증**:
```python
{
    "summary": str,
    "sentiment": "긍정" | "부정" | "중립",   # 다른 값이면 fallback
    "key_events": list[str],
    "trading_view": str,
    "model": "gemini-2.5-flash",
    "generated_at": int,
}
```

호출 후 검증:
- `sentiment in ("긍정", "부정", "중립")` 아니면 → `sentiment = "중립"` + warn
- `trading_view` 가 `매수|매도|관망` 으로 시작 안 하면 → `"관망 — LLM 응답 형식 오류"` fallback

**실패 처리**:
- API timeout/error → `None` 반환 → caller 가 dart_summaries row 미갱신 (이전 값 유지)
- JSON parse 실패 → fallback dict (raw text + neutral sentiment + "관망")

## 6. 캐시 (별도 dart_summaries 테이블)

### 6.1 신규 테이블

```sql
CREATE TABLE IF NOT EXISTS dart_summaries (
    symbol           TEXT PRIMARY KEY,
    summary_json     TEXT NOT NULL,      -- {"summary","sentiment","key_events","trading_view"} 또는 {"empty":true}
    sentiment        TEXT,                -- 긍정/부정/중립/empty (배지용 quick access)
    critical_count   INTEGER NOT NULL,
    generated_at     INTEGER NOT NULL,
    model            TEXT,                -- "gemini-2.5-flash" 또는 "rule_based"
    source           TEXT NOT NULL        -- "llm" / "rule" / "empty"
);
```

`analysis_cache` **무수정** — race condition + 책임 혼합 회피.

### 6.2 dart_cache 인터페이스

`src/dart_cache.py`:
- `init_db()` — 멱등 schema 적용
- `upsert_summary(symbol, summary, sentiment, critical_count, model, source)` — `INSERT ... ON CONFLICT(symbol) DO UPDATE SET ...` atomic
- `get_summary(symbol) -> dict | None`
- `list_summaries() -> dict[symbol, dict]` — web/report 가 한 번에 fetch

### 6.3 dart-refresh 의 atomic batch 흐름

```python
# 1. 모든 종목 fetch + classify (메모리에 누적, DB write 0)
results = []
for stock in settings.stocks.korea:
    corp_code = dart_client.get_corp_code(stock["symbol"])
    if not corp_code:
        continue
    disclosures = dart_client.fetch_disclosures(corp_code, days=30)
    dart_cache.insert_disclosures(stock["symbol"], corp_code, disclosures)  # raw 저장
    classified = dart_rules.classify_disclosures(disclosures)
    results.append((stock, classified))

# 2. summary 생성 (rule or LLM)
final_summaries = []
for stock, classified in results:
    if classified["count"] == 0:
        summary = {"empty": True, "generated_at": int(time.time())}
        source = "empty"
    elif classified["count"] == 1:
        summary = dart_rules.render_template(classified["critical_events"][0])
        source = "rule"
    else:
        summary = dart_llm.summarize_disclosures(stock["symbol"], stock["name"], classified)
        if summary is None:
            continue  # LLM 실패 — 이전 값 유지
        source = "llm"
    final_summaries.append((stock["symbol"], summary, source))

# 3. atomic batch commit
with dart_cache.transaction():
    for symbol, summary, source in final_summaries:
        dart_cache.upsert_summary(
            symbol=symbol,
            summary=json.dumps(summary, ensure_ascii=False),
            sentiment=summary.get("sentiment", "empty"),
            critical_count=...,
            model=summary.get("model"),
            source=source,
        )
```

**Cache invalidation 명문화**: 오늘 cron 에서 critical_count=0 인 종목은 어제 LLM 요약 있더라도 `{"empty": true}` 로 덮어쓴다 (stale 방지).

### 6.4 분석 본체와의 합성

`report_generator._render_stock_card(item)` 에 `item["dart_summary"]` 키 추가:
```python
# main.py auto-analyze 또는 web 카드 렌더 직전:
dart_summaries = dart_cache.list_summaries()  # dict[symbol, dict]
for item in analyses:
    item["dart_summary"] = dart_summaries.get(item["symbol"])
```

`_render_stock_card` 가 `item.get("dart_summary")` 를 `_render_dart_section` 에 전달.

## 7. 카드 표시

### 7.1 report_generator (`_render_stock_card` 내부 호출)

`src/report_generator.py:_render_dart_section(dart_summary: dict | None) -> str`:

- `None` 또는 empty dict → 빈 문자열
- `{"empty": True}` → "최근 30일 critical 공시 없음" 짧은 텍스트
- 정상 summary → 다음 HTML:

```html
<div class="dart-section">
  <h4 class="section-title">📋 공시정보분석</h4>
  <p class="dart-summary">{summary}</p>
  <ul class="dart-events">
    <li>{key_event_1}</li>
    <li>{key_event_2}</li>
  </ul>
  <p class="dart-trading">
    <strong class="trading-view-{sentiment_cls}">{trading_view}</strong>
  </p>
  <p class="dart-asof">분석: {generated_at_kst} | {model}</p>
</div>
```

`_render_stock_card` 에 호출 추가 — `{rel_perf_html}` 와 `{sentiment_html}` 사이에 `{dart_html}` 삽입.

### 7.2 web 카드 배지 (`web_app.py`)

home + portfolio 카드 배지 줄에 작은 공시 배지:
- 긍정 → `🟢 공시+`
- 부정 → `🔴 공시-`
- 중립 → `🟡 공시=`
- 데이터 없음/empty → 배지 미표시

`composite_badge`/`signal_badge`/`bnf_badge` 옆 (pattern_badge 앞).

### 7.3 CSS

`src/templates/report.css` 끝에 추가:

```css
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

## 8. main.py subcommand

```python
parser.add_argument("dart-refresh", help="DART 공시 갱신 + 요약 (cron)")
```

`--no-llm` 플래그 제거 — 환경변수 `DART_SKIP_LLM=1` 로 대체 (SKIP_ML_PREDICTION 패턴 일관성).

핸들러 — §6.3 의 atomic batch 흐름 그대로. 추가로:

```python
if args.command == "dart-refresh":
    from src import dart_client, dart_cache, dart_rules, dart_llm
    if not os.environ.get("DART_API_KEY"):
        logger.error("DART_API_KEY 미설정 — cron 중단")
        sys.exit(1)
    # log filter 등록 (DART_API_KEY redaction)
    from src.log_filter import install_secret_filter
    install_secret_filter([os.environ["DART_API_KEY"]])

    dart_cache.init_db()
    n = dart_client.refresh_corp_codes_if_stale(days=7)
    logger.info("corp_codes 갱신: %d row", n)
    # ... (§6.3 atomic batch 흐름)
    purged = dart_cache.purge_old(days=14)  # raw retention 14일
    logger.info("dart-refresh 완료: ok=%d/%d, llm=%d, rule=%d, empty=%d, purged=%d",
                success, total, llm_count, rule_count, empty_count, purged)
    return
```

**raw disclosures retention**: 90일 → **14일 단축** (재처리 시나리오는 LLM 비용 재발생 부담이라 가치 작음).

## 9. launchd plist (macmini)

`~/Library/LaunchAgents/ai.stock-analyzer.dart-refresh.plist` (git 추적 안 됨, 메모리 reference_sykim_macmini.md 에 추가):

- Label: `ai.stock-analyzer.dart-refresh`
- ProgramArguments: `python main.py dart-refresh`
- StartCalendarInterval: **Hour=19, Minute=30** (KST) — DS005 공시 18~19시 집중 접수, 19:30 이면 그날 99% 수집
- EnvironmentVariables: PATH, **DART_API_KEY**, GEMINI_API_KEY
- ML 환경 변수 불필요
- WakeFromSleep / StartOnMount 등 macmini sleep 정책은 다른 기존 잡 패턴 따름

**main.py lazy import 검증**: dart-refresh 핸들러가 ML 라이브러리 (numpy/lightgbm/torch) 를 import 안 하는지 검증. main.py top-level import 가 ML 모듈 끌어들이면 SIGSEGV 위험 (libomp fork). 필요 시 핸들러 진입 후 lazy import 패턴 적용.

## 10. DB 스키마

```sql
-- corp_code 매핑 (주 1회 / 7일 stale)
CREATE TABLE IF NOT EXISTS corp_codes (
    corp_code   TEXT PRIMARY KEY,
    corp_name   TEXT NOT NULL,
    stock_code  TEXT,                  -- 비상장은 NULL
    modify_date TEXT
);
CREATE INDEX IF NOT EXISTS idx_corp_codes_stock_code ON corp_codes(stock_code);

-- 공시 raw 저장 (디버깅 + 재처리)
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

-- dart_summaries (analysis_cache 와 분리)
CREATE TABLE IF NOT EXISTS dart_summaries (
    symbol           TEXT PRIMARY KEY,
    summary_json     TEXT NOT NULL,
    sentiment        TEXT,
    critical_count   INTEGER NOT NULL,
    generated_at     INTEGER NOT NULL,
    model            TEXT,
    source           TEXT NOT NULL    -- "llm" | "rule" | "empty"
);
```

Retention: `disclosures` 테이블 **14일** 이전 row 자동 삭제 (dart-refresh cron 끝에). dart_summaries 는 retention 없음 (종목당 1 row, 매일 갱신).

## 11. 테스트

`tests/test_dart_client.py` (8):
1. `test_corp_code_xml_parse_extracts_records` — XML fixture → list[dict]
2. `test_get_corp_code_finds_listed_stock` — '005930' → '00126380'
3. `test_get_corp_code_returns_none_for_unknown`
4. `test_fetch_disclosures_aggregates_all_endpoints` — 9 API mock → dict 통합
5. `test_fetch_disclosures_partial_failure_returns_partial`
6. `test_fetch_disclosures_rate_limit_sleep` — 호출 간 sleep 검증
7. `test_fetch_disclosures_empty_results_all_keys_present`
8. `test_refresh_corp_codes_skips_if_recent` — 7일 이내 갱신 시 download skip

`tests/test_dart_rules.py` (6):
9. `test_classify_treasury_acquire_is_tier1_critical`
10. `test_classify_exec_holders_below_threshold_excluded` — 임원 1주 매수 (< 1000주) → 제외
11. `test_classify_exec_holders_above_threshold_included` — 임원 5000주 매수 → critical
12. `test_classify_major_holders_below_threshold_excluded` — 변동 0.1%p (< 0.5%p) → 제외
13. `test_classify_should_call_llm_true_when_count_ge_2`
14. `test_render_template_treasury_acquire_returns_buy_view`

`tests/test_dart_cache.py` (4):

`tests/test_dart_cache.py` (5):
15. `test_corp_codes_upsert_dedup`
16. `test_disclosures_insert_dedup_by_rcept_no` — UNIQUE constraint
17. `test_purge_old_disclosures_14days`
18. `test_dart_summaries_upsert_atomic` — INSERT ... ON CONFLICT 동작
19. `test_list_summaries_returns_dict_keyed_by_symbol`

`tests/test_dart_llm.py` (5):
20. `test_summarize_disclosures_parses_gemini_json`
21. `test_summarize_disclosures_validates_sentiment_enum` — "긍정적" 같은 변형 → "중립" fallback
22. `test_summarize_disclosures_validates_trading_view_prefix` — "강한매수" → "관망 — LLM 응답 형식 오류"
23. `test_summarize_disclosures_handles_parse_failure_falls_back_to_raw`
24. `test_summarize_disclosures_api_error_returns_none`

`tests/test_log_filter.py` (2):
25. `test_secret_filter_redacts_api_key_in_message`
26. `test_secret_filter_passthrough_when_no_secret`

`tests/test_report_generator.py` (4 추가):
27. `test_render_dart_section_with_summary` — 정상 dict → HTML + DART 출처 footnote
28. `test_render_dart_section_empty_marker` — {"empty": True} → "공시 없음" 텍스트
29. `test_render_dart_section_none_returns_empty_string`
30. `test_render_dart_section_escapes_user_content` — LLM 출력에 `<script>` 포함 시 escape 검증 (XSS)

`tests/test_main.py` (2 추가):
31. `test_main_dart_refresh_exits_when_api_key_missing` — sys.exit(1)
32. `test_main_dart_refresh_does_not_import_ml_modules` — ML 모듈 import 없음 (libomp 회피)

총 32 신규 테스트 + 기존 회귀 0.

## 12. 에러 처리 정책

| 경우 | 동작 |
|---|---|
| DART_API_KEY 미설정 | cron 즉시 `sys.exit(1)` + logger.error |
| **로그 출력 시 DART_API_KEY 포함** | `log_filter.SecretFilter` 로 `***` 마스킹. URL query param 등에 키가 들어가도 자동 redacting |
| corpCode.xml ZIP 다운로드 실패 | warn + 기존 corp_codes 그대로 사용 |
| 개별 endpoint 호출 실패 (1개) | warn + 다른 endpoint 계속, 부분 결과 반환 |
| DART status "013" (no data) | 정상 응답으로 처리 (빈 list 반환), warn 안 함 |
| DART status "020" / "021" (API 사용 제한) | warn + 모든 종목 skip (이미 limit 도달) |
| 종목 1개 전체 실패 | warn + 다음 종목 계속 |
| corp_code 매핑 없는 종목 | warn + skip → 즉시 corp_codes 재다운로드 트리거 (이번 cron 만 유효) |
| Gemini LLM 호출 실패 (timeout/429) | warn + summary=None → dart_summaries row 미갱신 (이전 값 유지) |
| Gemini sentiment enum 위반 (예: "긍정적") | sentiment="중립" + warn |
| Gemini trading_view prefix 위반 (예: "강한매수") | "관망 — LLM 응답 형식 오류" fallback |
| Gemini JSON parse 실패 | fallback dict (raw text, sentiment=중립, trading_view="관망 — 응답 형식 오류") |

## 13. 호환성

- 기존 호출자 변경 0 (DART 는 새 cron + 새 컬럼)
- 미국 종목 흐름 영향 0 (corp_code 매핑 0 → skip)
- analysis_cache 신규 컬럼은 nullable → 기존 row 영향 없음
- 신규 dict 키 (`dart_summary_json` in analysis_cache row) 만 추가, 기존 키 영향 없음

## 14. 스코프 외 (이번 작업 제외)

- DS001 일반 공시검색 list (Phase B 별도 spec)
- DS002 정기보고서 (배당/임원/자기주식 — Phase C)
- DS003 재무정보 (별도, leader_filter 강화 용도)
- 실시간 공시 알림 (텔레그램/이메일)
- 공시 검색 UI (특정 종목 검색)
- 공시 본문 (`document.json`) 다운로드 — 본문 PDF/HTML 처리 부담 크고 Phase A 가치 작음

## 15. 알려진 리스크

| 리스크 | 영향 | 완화 |
|---|---|---|
| DART API 점검/장애 | 하루 분석 결과 미갱신 | 다음날 cron 재시도. 분석 본체 영향 0. |
| corp_code 매핑 누락 (신규 상장) | 신규 종목 최대 7일 공시 누락 | 7일 stale 정책 + 매핑 누락 발견 시 즉시 재다운로드 트리거. |
| Gemini rate limit (429) | 일부 종목 요약 누락 | retry 1회 + sleep 후 fallback to None. UI 에서 이전 요약 유지. |
| LLM 환각/오해석 | 잘못된 매매 시그널 표시 | UI 에 "model: gemini-2.5-flash" 명시 + key_events 원본 표시로 사용자 판단 보조. trading_view 는 1줄 근거 포함. |
| 다중 사용자 portfolio 확장 시 호환성 | (현재 단일 user 라 영향 없음) | 추후 spec 별도. |
