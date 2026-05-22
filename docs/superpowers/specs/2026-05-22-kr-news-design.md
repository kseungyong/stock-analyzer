# 한국 종목 뉴스 수집 (Naver Finance) — 설계서

날짜: 2026-05-22
대상: stock-analyzer
상태: Draft (사용자 리뷰 대기)

---

## 1. 목적

현재 `data_fetcher.fetch_news()` 가 yfinance `Ticker.news` 만 사용해서, 한국 종목 (`.KS` / `.KQ`) 의 뉴스가 거의 비어있다. Naver Finance 종목별 뉴스 페이지를 크롤링하여 한국 종목에도 관련 뉴스를 표시하고 FinBERT sentiment 분석에 활용할 수 있게 한다.

성공 조건:
- `005930.KS` 분석 시 최근 매일경제/한경 등 뉴스 5+ 건 카드 하단 "관련 뉴스" 섹션에 표시
- 영어 번역본이 FinBERT 입력으로 들어가 sentiment 라벨 산출 (현재 `SKIP_SENTIMENT=1` 환경에선 skip — 환경 변수 끄면 자동 활성)
- 기존 미국 종목 흐름 (yfinance) 영향 0

## 2. 아키텍처

```
src/data_fetcher.py
  fetch_news(symbol)  ← 수정 (suffix 분기)
    ├─ symbol.endswith(".KS"|".KQ") → news_kr.fetch_news_kr(symbol)
    └─ else → 기존 yfinance 경로

src/news_kr.py  ← 신규
  fetch_news_kr(symbol, max_items=10) -> list[dict]
    1. _cache_get(symbol) → 1h 이내면 반환
    2. _scrape_naver_finance(krx_code) → HTML parse → items
    3. translate ko → en (title_en, summary_en)
    4. _cache_put(symbol, items)
    5. return items[:max_items]

data/news_cache/{symbol}.json  ← 신규 (gitignore 추가)
  {"fetched_at": <unix>, "symbol": str, "items": [...]}
```

신규 의존성 0개 (requests / beautifulsoup4 / deep_translator 모두 기존).

## 3. Naver Finance 크롤링

### 3.1 URL
```
https://finance.naver.com/item/news_news.naver?code={KRX_CODE}&page=1
```
KRX_CODE 는 `.KS`/`.KQ` 제거한 6자리 (예: `005930`). 기존 `_to_krx_code()` 재사용 + **`.zfill(6)` 적용** — 5자리 코드 (앞 0 누락) 같은 엣지 케이스 방어. Naver는 정확히 6자리 요구.

### 3.2 파싱 대상
종목 뉴스 list 페이지 HTML 의 `<table class="type5"> > tbody > tr` 행:
- `td.title > a` — 제목 + href (상대 URL)
- `td.info` — 출판사 (매일경제, 한경 등)
- `td.date` — `2026.05.22 14:23` 형식

### 3.3 추출 dict 형식 (기존과 호환)
```python
{
    "title": str,           # 한국어 원본
    "title_en": str,        # ko→en 번역 (sentiment 입력용)
    "link": str,            # 절대 URL
    "publisher": str,
    "published": str,       # YYYY-MM-DD HH:MM
    "summary": "",          # list 페이지에 없음 — 항상 빈 문자열
    "summary_en": "",       # 동일
}
```

**Summary는 의도적으로 항상 빈 문자열.** Naver list 페이지엔 본문 요약이 없고, 개별 기사 페이지 fetch는 차단 위험을 기하급수적으로 키움. FinBERT sentiment 분석은 **title_en 만으로 수행** (기존 `analyze_sentiment` 도 title fallback 지원).

### 3.4 HTTP 정책
- `requests.get(url, headers={"User-Agent": "Mozilla/5.0 ...", "Accept-Language": "ko-KR,ko;q=0.9"}, timeout=10)`
  Naver 가 default Python UA 차단 → 일반 브라우저 UA 필수
- 1회 시도, 실패 시 빈 list (yfinance fetch_news 와 동일 패턴)
- 종목 개별 페이지는 fetch 안 함 (부하 + 차단 위험 + YAGNI)

### 3.5 Rate limit + 딜레이
- 캐시 1시간 TTL + 종목당 1회 호출
- 한 시장 분석 (65종목) — 캐시 miss 시 fresh fetch 65회
- **호출 간 jitter 딜레이 `time.sleep(random.uniform(0.5, 1.5))`** — WAF 봇 탐지 회피
- 호출 분포: 평균 1초/종목 → 분당 ~60종목 (잠깐 burst 방지)
- 공식 명시된 limit 없음. 위 보수적 정책으로 차단 위험 최소화.

## 4. 캐시

### 4.1 파일 구조
`data/news_cache/{symbol}.json` — symbol 그대로 (suffix 포함)
```json
{
  "fetched_at": 1779452345,
  "symbol": "005930.KS",
  "items": [...]
}
```

### 4.2 TTL
`_NEWS_CACHE_TTL = 3600` (1시간) — 모듈 상수.
- cron (한 시간에 1회) 에선 사실상 매번 miss → fresh fetch
- web "재분석" 연속 클릭 시 차단 보호

### 4.3 정책

**빈 결과 처리 — 구분 필수**:
| 상황 | 캐시 여부 | 이유 |
|---|---|---|
| HTTP/parse **실패** (exception, 빈 HTML 등) | 캐시 안 함 | 일시 장애 후 다음 호출에서 재시도 |
| HTTP/parse **성공** but 뉴스 0건 (소외 종목) | 빈 list로 캐시 | 무한 fresh fetch 방지 |

판별: `_scrape_naver_finance` 가 `None` 반환 = 실패, `[]` 반환 = 성공 (뉴스 0건).

**Atomic write** — race condition 방지:
```python
tmp_path = cache_path.with_suffix(".tmp")
tmp_path.write_text(json.dumps(payload, ensure_ascii=False))
os.replace(tmp_path, cache_path)  # atomic on POSIX
```
write 중 read 시 `JSONDecodeError` 발생 방지. lock 없이도 안전.

**기타**:
- corrupt JSON 발견 시 → 무시하고 fresh fetch (이 경우 atomic write 덕에 거의 없을 것)
- 디렉토리 없으면 `mkdir(parents=True, exist_ok=True)` 자동 생성

## 5. 번역

### 5.1 인스턴스 + 캐시
`data_fetcher.py` 에 이미 `GoogleTranslator(source="en", target="ko")` + `lru_cache(512)` 캐시 패턴 존재. 역방향 인스턴스 추가:

```python
_translator_ko_en = GoogleTranslator(source="ko", target="en")

@lru_cache(maxsize=512)
def _translate_ko_to_en_cached(text: str) -> str:
    return _translator_ko_en.translate(text)
```

`fetch_news_kr` 가 호출. 실패 (network/API error) 시 한국어 원본 그대로 (`title_en = title`).

### 5.2 Rate limit 방어 — Google Translate 호출 제어

Naver보다 **Google Translate 무료 엔드포인트의 429 차단 위험이 더 큼** (65종목 × 10건 = 최대 650 호출).

방어 정책:
- **번역 대상 최소화**: title만 번역 (summary는 항상 빈 문자열이므로 호출 생략) → 호출 수 절반
- **호출 간 짧은 sleep** — `time.sleep(0.3)` (jitter 없이 고정, lru_cache hit 시 skip)
- **429 backoff** — `requests.exceptions` 잡아서 다음 종목까지 `time.sleep(10)` 후 진행. 같은 종목 retry 안 함 (원본 한국어로 fallback)
- lru_cache(512) 효과: 동일 헤드라인 반복 (예: 매일 같은 시장 분석 헤드라인) 시 호출 0

### 5.3 한국 금융 은어 전처리

FinBERT는 영미 금융 텍스트로 학습되어 한국 특유 표현 (`상한가`, `어닝 쇼크`) 직역 시 sentiment 오분류 위험.

번역 직전 간단한 정규식 사전으로 핵심 표현 치환 (`src/news_kr.py:_KR_FINANCE_GLOSSARY` 상수):

```python
_KR_FINANCE_GLOSSARY = {
    "상한가": "Upper limit (+30%)",
    "하한가": "Lower limit (-30%)",
    "어닝 쇼크": "earnings miss",
    "어닝 서프라이즈": "earnings beat",
    "신고가": "new high",
    "신저가": "new low",
    "급등": "surge",
    "급락": "plunge",
    "감자": "capital reduction",
    "증자": "capital increase",
}

def _preprocess_kr(text: str) -> str:
    for kr, en in _KR_FINANCE_GLOSSARY.items():
        text = text.replace(kr, en)
    return text
```

`_translate_ko_to_en_cached(text)` 호출 직전 적용. 10개 + 확장 가능. 한국 특유 표현이 적은 일반 헤드라인은 영향 없음.

## 6. sentiment 통합

`ml_predictor.analyze_sentiment(news_items)` 변경 0. 기존 코드가 이미 `title_en` 우선 사용, 없으면 `title` fallback. 한국 뉴스도 `title_en` 가 채워져 있으므로 FinBERT 가 영어로 처리.

현재 macmini 환경은 `SKIP_SENTIMENT=1` 이라 skip 됨. 추후 환경 변수 해제 시 자동 활성.

## 7. XSS / 인코딩

기존 `_render_news()` 의 `html.escape(title)` / `html.escape(summary)` 그대로 적용. 한국어 문자 escape 영향 없음 (UTF-8 그대로 출력). 별도 처리 불필요.

## 8. 테스트

`tests/test_news_kr.py` (신규):

**스크래핑 (3)**:
1. `test_scrape_naver_parses_fixture` — fixture HTML → 5 items 추출
2. `test_scrape_naver_empty_table_returns_empty` — `<table class="type5">` 있고 `<tr>` 없음 → `[]` (성공, 뉴스 0건)
3. `test_scrape_naver_handles_relative_url` — 상대 URL → 절대 URL

**스크래핑 실패 vs 성공 (2)**:
4. `test_scrape_naver_http_failure_returns_none` — requests raises → None (실패)
5. `test_scrape_naver_no_table_returns_none` — `<table>` 자체 없음 (HTML 구조 변경) → None (실패)

**티커 정규화 (2)**:
6. `test_to_krx_code_pads_to_six_digits` — `5930.KS` → `005930`
7. `test_to_krx_code_strips_kq_suffix` — `247540.KQ` → `247540`

**캐시 (4)**:
8. `test_cache_hit_returns_cached_within_ttl` — put 직후 get → 동일 list
9. `test_cache_miss_after_ttl_expires` — fetched_at = now - 3700 → None
10. `test_cache_get_nonexistent_returns_none` — 없는 종목 → None
11. `test_cache_put_is_atomic_no_partial_read` — write 도중 read 시 corrupt 없음 (tmp + os.replace 검증, 동시성 시뮬레이션)

**빈 결과 캐시 정책 (2)**:
12. `test_fetch_news_kr_empty_success_cached` — _scrape returns [] → 캐시 파일 생성됨
13. `test_fetch_news_kr_failure_not_cached` — _scrape returns None → 캐시 파일 미생성

**fetch_news_kr 흐름 (4)**:
14. `test_fetch_news_kr_uses_cache` — cache 있으면 _scrape mock 호출 안 됨
15. `test_fetch_news_kr_cache_miss_fetches_and_caches` — cache 없으면 mock 1회 + 파일 생성
16. `test_fetch_news_kr_translates_to_english` — title 한국어 → title_en 채워짐
17. `test_fetch_news_kr_translation_failure_keeps_original` — translate raises → title_en = title 한국어 원본

**전처리 사전 (2)**:
18. `test_preprocess_kr_replaces_glossary_terms` — `"삼성전자 상한가"` → `"삼성전자 Upper limit (+30%)"`
19. `test_preprocess_kr_passthrough_no_match` — 일반 헤드라인 변경 없음

`tests/test_data_fetcher.py` (수정):

20. `test_fetch_news_dispatches_to_kr_for_ks_suffix`
21. `test_fetch_news_dispatches_to_kr_for_kq_suffix`
22. `test_fetch_news_dispatches_to_yfinance_for_us`

## 9. 에러 처리 정책

| 경우 | 동작 |
|---|---|
| `requests.get` 실패 (timeout/connection) | `logger.warning` + 빈 list |
| BeautifulSoup parse 실패 (HTML 구조 변경) | `logger.warning` + 빈 list |
| 번역 실패 | 한국어 원본 그대로 (`title_en = title`) |
| 캐시 파일 corrupt | 무시 + fresh fetch |
| 캐시 디렉토리 없음 | 자동 생성 |

## 10. 파일 변경

- 신규: `src/news_kr.py`
- 신규: `tests/test_news_kr.py`
- 수정: `src/data_fetcher.py` (fetch_news suffix 분기 + ko→en translator)
- 수정: `tests/test_data_fetcher.py` (dispatch 3 tests)
- 수정: `.gitignore` (data/news_cache/ 추가)

## 11. 호환성

- 기존 호출자 변경 0 (`fetch_news(symbol)` 시그니처 동일)
- 미국 종목 (yfinance) 흐름 영향 0
- 신규 dict 키 추가 없음 (기존 형식 그대로)
- 분석 본체 영향 0 (실패 시 빈 list 반환, 기존 패턴)

## 12. 스코프 외 (이번 작업 제외)

- 개별 뉴스 페이지 본문 fetch (summary 채우기) — 부하 + 차단 위험
- 한국 FinBERT 모델 (snunlp/KR-FinBert-SC 등) — Google Translate fallback 으로 충분, 모델 다운로드 500MB
- Daum / Google News fallback — Naver 안정성 검증 후 별도 spec
- DART 공시 통합 — 별도 spec (다음 라운드)
- analysis_cache 에 news 별도 컬럼 — 기존 result_html 에 포함됨
- **TTL 동적 가변** (장중 짧게 / 마감 후 길게) — 초기엔 1h 고정 무난. 차단 사례 발생 시 별도 spec.
- **금융 은어 사전 확장** — 초기 10개로 시작. 필요 시 점진 추가 (코드 변경 1줄).

## 13. 알려진 리스크

| 리스크 | 영향 | 완화 |
|---|---|---|
| Naver HTML 구조 변경 | 빈 list 반환, 카드에 뉴스 안 보임 | 분석 본체 영향 0. selector 깨지면 `logger.warning` 으로 감지 후 패치. |
| IP rate limit / 차단 | 한 시장 분석 중단 | 1h 캐시 + 직렬 실행 + 보수적 UA. 차단 발생 시 fallback (Google News) 별도 spec 으로 도입 검토. |
| 번역 API 한도 | sentiment 영어 누락 | 번역 실패 시 한국어 원본 그대로 → FinBERT 가 한국어 입력 시 부정확하지만 crash 없음 |
