# DART 공시 통합 — Phase A 설계서

날짜: 2026-05-23
대상: stock-analyzer
상태: Draft (시니어 검수 대기)

---

## 1. 목적

한국 종목 분석 카드에 **"공시정보분석"** 섹션 추가. DART (전자공시시스템) 의 주요사항보고서(DS005) + 지분공시(DS004) 를 매일 1회 일괄 fetch → critical event 가 있는 종목만 Gemini LLM 으로 요약 → 카드 하단에 매매 관점 해석 표시.

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
  src/dart_cache.py     — disclosures + corp_codes 테이블 (DB layer)
  src/dart_llm.py       — Gemini 요약 (leader_llm 패턴 재사용)

수정:
  src/analysis_cache.py — dart_summary_json TEXT 컬럼 마이그레이션 + get/put/list 반환에 포함
  src/report_generator.py — _render_dart_section() + _render_stock_card 호출
  src/web_app.py        — home/portfolio 카드 배지 (공시 sentiment)
  src/templates/report.css — .dart-section 스타일
  main.py               — dart-refresh subcommand + 모듈 로드 시점 init_db

신규 cron:
  ~/Library/LaunchAgents/ai.stock-analyzer.dart-refresh.plist  (macmini, KST 18:00)

DB (data/predictions.db):
  corp_codes              (corp_code, corp_name, stock_code, modify_date)
  disclosures             (id, corp_code, stock_code, disclosure_type, rcept_no, rcept_dt, raw_json, fetched_at)
  analysis_cache.dart_summary_json (TEXT, nullable)
```

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
- **월 1회** 자동 갱신 (마지막 modify_date 가 30일 이전이면 재다운로드)
- ZIP 다운로드 실패 시 → warn + 기존 테이블 그대로 사용
- 신규 상장 종목이 corp_codes 에 없으면 → skip + warn (다음 월에 갱신)

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

## 4. critical event 판정

`src/dart_client.py:has_critical_events(disclosures: dict) -> bool`:

```python
def has_critical_events(disclosures: dict) -> bool:
    critical_keys = (
        "capital_increase", "capital_decrease",
        "treasury_acquire", "treasury_dispose",
        "merger", "major_holders", "exec_holders",
    )
    return any(disclosures.get(k) for k in critical_keys)
```

DS001 list 만 있고 다른 critical 카테고리 0건 → LLM skip + dart_summary_json = `{"empty": true}` (UI에서 "최근 30일 critical 공시 없음" 표시).

## 5. LLM 요약 (Gemini)

`src/dart_llm.py:summarize_disclosures(symbol, name, disclosures) -> dict | None`:

**모델**: `gemini-2.5-flash` (leader_llm 동일).

**프롬프트**:
```
당신은 한국 주식 시장의 공시 분석 전문가입니다.
종목: {name} ({symbol})

최근 30일 주요 공시:
{disclosures_json}

다음 JSON 형식으로만 응답:
{
  "summary": "2-3문장 핵심 요약",
  "sentiment": "긍정" | "부정" | "중립",
  "key_events": ["가장 중요한 이벤트 1-3개"],
  "trading_view": "매수/매도/관망 + 1줄 근거"
}
```

**generation_config**:
- `temperature: 0.3`
- `max_output_tokens: 2048` (4 필드 한국어 충분, kr-news leader_llm 의 4096 보다 작음)
- `response_mime_type: "application/json"`

**반환 형식**:
```python
{
    "summary": str,
    "sentiment": "긍정" | "부정" | "중립",
    "key_events": list[str],
    "trading_view": str,
    "model": "gemini-2.5-flash",
    "generated_at": int,        # unix timestamp
}
```

**실패 처리**:
- API timeout/error → `None` 반환 → caller 가 dart_summary_json = NULL 저장
- JSON parse 실패 → `{"summary": <raw_text>, "sentiment": "중립", "key_events": [], "trading_view": "관망 (LLM 응답 형식 오류)"}` fallback

## 6. 캐시 (analysis_cache)

### 6.1 컬럼 추가

```sql
ALTER TABLE analysis_cache ADD COLUMN dart_summary_json TEXT
```

멱등 마이그레이션 (`PRAGMA table_info` 후 조건부 ALTER).

### 6.2 put/get/list 통합

`src/analysis_cache.py`:
- `put(..., dart_summary_json: str | None = None)` — 새 인자
- `get(symbol)` SELECT 에 `dart_summary_json` 추가, dict 반환에 포함
- `list_symbols()` SELECT 에 추가, dict 반환에 포함

### 6.3 dart-refresh 의 UPDATE 흐름

```python
for stock in settings.stocks.korea:
    corp_code = dart_client.get_corp_code(stock["symbol"])
    if not corp_code:
        continue
    disclosures = dart_client.fetch_disclosures(corp_code, days=30)
    dart_cache.insert_raw(disclosures)
    if dart_client.has_critical_events(disclosures):
        summary = dart_llm.summarize_disclosures(stock["symbol"], stock["name"], disclosures)
    else:
        summary = {"empty": True, "generated_at": int(time.time())}
    if summary is not None:
        # analysis_cache row UPSERT — 기존 row 의 다른 필드는 보존
        _update_dart_summary_only(stock["symbol"], json.dumps(summary, ensure_ascii=False))
```

신규 helper `analysis_cache.update_dart_summary(cache_key, dart_summary_json)`:
- `UPDATE analysis_cache SET dart_summary_json = ? WHERE cache_key = ?`
- 다른 필드 영향 없음 (auto-analyze cron 과 충돌 회피)
- row 없으면 (한 번도 분석 안 된 종목) → INSERT (market/result_html 은 빈값)

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
parser.add_argument("dart-refresh", help="DART 공시 갱신 + LLM 요약 (cron)")
dart_parser.add_argument("--no-llm", action="store_true",
                          help="LLM 호출 skip (디버깅용, raw fetch 만)")
```

핸들러:
```python
if args.command == "dart-refresh":
    from src import dart_client, dart_cache, dart_llm
    if not os.environ.get("DART_API_KEY"):
        logger.error("DART_API_KEY 미설정 — cron 중단")
        sys.exit(1)
    dart_cache.init_db()
    n = dart_client.refresh_corp_codes_if_stale(days=30)
    logger.info("corp_codes 갱신: %d row", n)
    config = load_config()
    success = 0
    for stock in config["stocks"].get("korea", []):
        try:
            corp_code = dart_client.get_corp_code(stock["symbol"])
            if not corp_code:
                logger.warning("corp_code 없음 — skip: %s", stock["symbol"])
                continue
            disclosures = dart_client.fetch_disclosures(corp_code, days=30)
            dart_cache.insert_disclosures(stock["symbol"], corp_code, disclosures)
            if dart_client.has_critical_events(disclosures):
                if args.no_llm:
                    continue
                summary = dart_llm.summarize_disclosures(
                    stock["symbol"], stock["name"], disclosures,
                )
            else:
                summary = {"empty": True, "generated_at": int(time.time())}
            if summary is not None:
                analysis_cache.update_dart_summary(
                    stock["symbol"], json.dumps(summary, ensure_ascii=False),
                )
            success += 1
        except Exception as e:
            logger.exception("dart-refresh 오류 — %s: %s", stock["symbol"], e)
    purged = dart_cache.purge_old(days=90)
    logger.info("dart-refresh 완료: ok=%d/%d, purged=%d", success, len(config["stocks"]["korea"]), purged)
    return
```

## 9. launchd plist (macmini)

`~/Library/LaunchAgents/ai.stock-analyzer.dart-refresh.plist` (git 추적 안 됨, 메모리 reference_sykim_macmini.md 에 추가):

- Label: `ai.stock-analyzer.dart-refresh`
- ProgramArguments: `python main.py dart-refresh`
- StartCalendarInterval: Hour=18, Minute=0 (KST)
- EnvironmentVariables: PATH, DART_API_KEY, GEMINI_API_KEY
- ML 환경 변수 불필요 (DART 는 ML 안 거침)

## 10. DB 스키마

```sql
-- corp_code 매핑 (월 1회 다운로드)
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

-- analysis_cache 마이그레이션
ALTER TABLE analysis_cache ADD COLUMN dart_summary_json TEXT;
```

Retention: `disclosures` 테이블 90일 이전 row 자동 삭제 (dart-refresh cron 끝에).

## 11. 테스트

`tests/test_dart_client.py` (8):
1. `test_corp_code_xml_parse_extracts_records` — XML fixture → list[dict]
2. `test_get_corp_code_finds_listed_stock` — '005930' → '00126380'
3. `test_get_corp_code_returns_none_for_unknown`
4. `test_fetch_disclosures_aggregates_all_endpoints` — 9 API mock → dict 통합
5. `test_fetch_disclosures_partial_failure_returns_partial` — 1 API 실패해도 나머지 반환
6. `test_fetch_disclosures_rate_limit_sleep` — 호출 간 sleep 검증
7. `test_fetch_disclosures_empty_results_all_keys_present`
8. `test_refresh_corp_codes_skips_if_recent` — 30일 이내 갱신 시 download skip
9. `test_has_critical_events_true_for_treasury_acquire`
10. `test_has_critical_events_false_for_empty_dict`
11. `test_has_critical_events_false_for_list_only` — DS001 list 만 있고 critical 0건

`tests/test_dart_cache.py` (4):
12. `test_corp_codes_upsert_dedup`
13. `test_disclosures_insert_dedup_by_rcept_no` — UNIQUE constraint
14. `test_purge_old_disclosures` — 90일 이전 삭제
15. `test_analysis_cache_update_dart_summary_preserves_other_fields`

`tests/test_dart_llm.py` (4):
16. `test_summarize_disclosures_parses_gemini_json` — Gemini mock → dict
17. `test_summarize_disclosures_handles_parse_failure_falls_back_to_raw`
18. `test_summarize_disclosures_api_error_returns_none`
19. `test_summarize_disclosures_required_fields_present` (summary, sentiment, key_events, trading_view, model, generated_at)

`tests/test_report_generator.py` (4 추가):
20. `test_render_dart_section_with_summary` — 정상 dict → HTML 포함
21. `test_render_dart_section_empty_marker` — {"empty": True} → "공시 없음" 텍스트
22. `test_render_dart_section_none_returns_empty_string`
23. `test_render_stock_card_includes_dart_section_when_present`

`tests/test_analysis_cache.py` (2 추가):
24. `test_update_dart_summary_preserves_signal_value` — 기존 signal_value 영향 없음
25. `test_get_returns_dart_summary_json_field`

총 25 신규 테스트 + 기존 회귀 0.

## 12. 에러 처리 정책

| 경우 | 동작 |
|---|---|
| DART_API_KEY 미설정 | cron 즉시 `sys.exit(1)` + logger.error |
| corpCode.xml ZIP 다운로드 실패 | warn + 기존 corp_codes 그대로 사용 |
| 개별 endpoint 호출 실패 (1개) | warn + 다른 endpoint 계속, 부분 결과 반환 |
| 종목 1개 전체 실패 | warn + 다음 종목 계속 |
| corp_code 매핑 없는 종목 | warn + skip |
| Gemini LLM 호출 실패 (timeout/429) | warn + summary=None → dart_summary_json 미갱신 (UI 이전 값 유지) |
| Gemini JSON parse 실패 | fallback dict (raw text, sentiment=중립, trading_view="LLM 응답 형식 오류") |
| analysis_cache row 없음 | INSERT 자동 생성 (market/result_html 빈값) |

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
| corp_code 매핑 누락 (신규 상장) | 신규 종목 1개월간 공시 미수집 | 월 1회 corp_codes 갱신 + warn 로그. |
| Gemini rate limit (429) | 일부 종목 요약 누락 | retry 1회 + sleep 후 fallback to None. UI 에서 이전 요약 유지. |
| LLM 환각/오해석 | 잘못된 매매 시그널 표시 | UI 에 "model: gemini-2.5-flash" 명시 + key_events 원본 표시로 사용자 판단 보조. trading_view 는 1줄 근거 포함. |
| 다중 사용자 portfolio 확장 시 호환성 | (현재 단일 user 라 영향 없음) | 추후 spec 별도. |
