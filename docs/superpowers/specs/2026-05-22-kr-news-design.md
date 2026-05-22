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
KRX_CODE 는 `.KS`/`.KQ` 제거한 6자리 (예: `005930`). 기존 `_to_krx_code()` 재사용.

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
    "summary": str,         # 빈 문자열 (list 페이지엔 없음, YAGNI)
    "summary_en": str,      # 빈 문자열
}
```

### 3.4 HTTP 정책
- `requests.get(url, headers={"User-Agent": "Mozilla/5.0 ...", "Accept-Language": "ko-KR,ko;q=0.9"}, timeout=10)`
  Naver 가 default Python UA 차단 → 일반 브라우저 UA 필수
- 1회 시도, 실패 시 빈 list (yfinance fetch_news 와 동일 패턴)
- 종목 개별 페이지는 fetch 안 함 (부하 + 차단 위험 + YAGNI)

### 3.5 Rate limit
- 캐시 1시간 TTL + 종목당 1회 호출
- 한 시장 분석 (65종목) → 분당 약 13 요청 (직렬 실행)
- 공식 명시된 limit 없음. 일반 브라우저 사용량보다 보수적이라 차단 위험 낮음.

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
- 빈 결과는 **캐시하지 않음** (다음 호출에서 재시도 기회)
- corrupt JSON → 무시하고 fresh fetch
- 디렉토리 없으면 `mkdir(parents=True, exist_ok=True)` 자동 생성
- 동시 write lock 생략 (충돌 시 마지막 writer wins, 수용 가능)

## 5. 번역

`data_fetcher.py` 에 이미 `GoogleTranslator(source="en", target="ko")` + `lru_cache(512)` 캐시 패턴 존재. 역방향 인스턴스 추가:

```python
_translator_ko_en = GoogleTranslator(source="ko", target="en")

@lru_cache(maxsize=512)
def _translate_ko_to_en_cached(text: str) -> str:
    return _translator_ko_en.translate(text)
```

`fetch_news_kr` 가 호출. 실패 (network/API error) 시 한국어 원본 그대로 (`title_en = title`).

## 6. sentiment 통합

`ml_predictor.analyze_sentiment(news_items)` 변경 0. 기존 코드가 이미 `title_en` 우선 사용, 없으면 `title` fallback. 한국 뉴스도 `title_en` 가 채워져 있으므로 FinBERT 가 영어로 처리.

현재 macmini 환경은 `SKIP_SENTIMENT=1` 이라 skip 됨. 추후 환경 변수 해제 시 자동 활성.

## 7. XSS / 인코딩

기존 `_render_news()` 의 `html.escape(title)` / `html.escape(summary)` 그대로 적용. 한국어 문자 escape 영향 없음 (UTF-8 그대로 출력). 별도 처리 불필요.

## 8. 테스트

`tests/test_news_kr.py` (신규):

1. `test_scrape_naver_parses_fixture` — fixture HTML → 5 items 추출
2. `test_scrape_naver_empty_table_returns_empty` — `<table class="type5">` 없으면 `[]`
3. `test_scrape_naver_handles_relative_url` — 상대 URL → 절대 URL
4. `test_cache_hit_returns_cached_within_ttl` — put 직후 get → 동일 list
5. `test_cache_miss_after_ttl_expires` — fetched_at = now - 3700 → None
6. `test_cache_get_nonexistent_returns_none` — 없는 종목 → None
7. `test_fetch_news_kr_uses_cache` — cache 있으면 _scrape mock 호출 안 됨
8. `test_fetch_news_kr_cache_miss_fetches_and_caches` — cache 없으면 mock 1회 + 파일 생성
9. `test_fetch_news_kr_empty_result_not_cached` — mock returns [] → cache 파일 미생성
10. `test_fetch_news_kr_translates_to_english` — title/summary 한국어 → title_en/summary_en 채워짐
11. `test_fetch_news_kr_translation_failure_keeps_original` — translate raises → title_en = title 한국어 원본

`tests/test_data_fetcher.py` (수정):

12. `test_fetch_news_dispatches_to_kr_for_ks_suffix`
13. `test_fetch_news_dispatches_to_kr_for_kq_suffix`
14. `test_fetch_news_dispatches_to_yfinance_for_us`

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

## 13. 알려진 리스크

| 리스크 | 영향 | 완화 |
|---|---|---|
| Naver HTML 구조 변경 | 빈 list 반환, 카드에 뉴스 안 보임 | 분석 본체 영향 0. selector 깨지면 `logger.warning` 으로 감지 후 패치. |
| IP rate limit / 차단 | 한 시장 분석 중단 | 1h 캐시 + 직렬 실행 + 보수적 UA. 차단 발생 시 fallback (Google News) 별도 spec 으로 도입 검토. |
| 번역 API 한도 | sentiment 영어 누락 | 번역 실패 시 한국어 원본 그대로 → FinBERT 가 한국어 입력 시 부정확하지만 crash 없음 |
