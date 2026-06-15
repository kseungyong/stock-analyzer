# 시세(일봉) 소스 토스증권 교체 설계

- **작성일**: 2026-06-15
- **상태**: 설계 승인 + 실제 candles probe 검증 완료 (구현 대기)
- **관련 모듈**: `src/data_fetcher.py`, `src/toss_client.py`(기존 재사용)
- **선행**: 2026-06-15-toss-portfolio-sync (TossClient 인증/토큰 인프라)

> **2026-06-15 probe 검증**: 실제 토스 candles API 호출로 일반/신코드/미국 종목 +
> 페이지네이션을 실측 확정 (아래 반영).

## 1. 목적

일봉 시세 수집의 1차 소스를 yfinance 에서 토스증권 Open API candles 로 교체한다.
주요 동기:
- **한국 신코드 정확성**: 단일종목 레버리지 ETF(예 `0193W0`) 등 KRX 영문 신코드를
  yfinance 가 미지원, FDR 폴백도 불안정. 토스는 정확히 제공 (probe 확인).
- **데이터 소스 일관성**: 포트폴리오(토스) 와 시세를 한 소스로 통일.
- **해외의존 탈피**: yfinance 의 간헐적 빈 데이터/rate-limit/지연 제거.

### 비목표 (Non-goals)
- 분봉(1m)/실시간 시세 — 현재 분석 파이프라인이 일봉 기반이므로 사용처 부재. 별도 사이클.
- 시세 캐싱 — 기존 fetch_stock_data 도 무캐시. rate-limit 문제 발생 시 후속.
- 주봉/월봉 — 토스 미지원, 현재 미사용.

## 2. 데이터 소스 — 토스 candles (probe 실측)

`GET /api/v1/candles` (인증: 기존 TossClient OAuth2)

### 파라미터
| 파라미터 | 필수 | 값 |
|----------|------|-----|
| `symbol` | ✓ | 한국 6자리(`005930`, `0193W0`) / 미국 ticker(`AAPL`) |
| `interval` | ✓ | `1d` (일봉) — 이번 작업은 1d 만 |
| `count` | - | 기본 100, **최대 200** |
| `before` | - | ISO8601 커서 (exclusive), 페이지네이션 |
| `adjusted` | - | 수정주가, 기본 true (유지) |

### 응답 (probe 실측)
```json
{"result": {
  "candles": [
    {"timestamp": "2026-06-15T00:00:00.000+09:00", "openPrice": "337500",
     "highPrice": "345000", "lowPrice": "334500", "closePrice": "337500",
     "volume": "27018131", "currency": "KRW"}, ...
  ],
  "nextBefore": "2026-06-08T00:00:00.000+09:00"
}}
```
- `{"result": ...}` envelope (toss_client._unwrap 재사용).
- candles 는 **최신순**(latest first). 모든 값은 **문자열**.
- `timestamp`: ISO8601 (한국 +09:00 / 미국도 +09:00 으로 변환된 장 시각).
- `nextBefore`: null 이면 마지막 페이지. count=200 기준 1페이지 200봉 + 커서 확인.
- 미국 가격은 소수점(`294.34`), 한국은 정수 문자열.

### probe 확정 사실
- `0193W0`(단일종목 레버리지 신코드) → 200 OK, OHLCV 정상 (핵심 동기 해결).
- `005930`/`AAPL` 정상. count=200 → 200봉 반환, nextBefore 커서로 2페이지 200봉 추가 확인.

## 3. 아키텍처

```
src/toss_client.py (기존 — TossClient 에 메서드 1개 추가)
  └ TossClient.fetch_candles(symbol, interval="1d", count=200) -> list[dict]
       내부 페이지네이션(200/요청, nextBefore 커서)으로 count 개까지 모아 반환.
       count 는 호출자(_fetch_with_toss)가 period_days 기반으로 계산해 전달.
       반환: 최신순 raw candle dict 리스트 (변환 안 함 — I/O 책임만).

src/data_fetcher.py (수정)
  ├ _fetch_with_toss(symbol, period_days) -> pd.DataFrame
  │     _to_krx_code 로 .KS/.KQ 제거 → fetch_candles → DataFrame 변환
  ├ _candles_to_df(candles) -> pd.DataFrame   # 순수 변환 (단위 테스트 용이)
  └ fetch_stock_data(symbol, period_days, retries) 수정:
        토스 1차 (retries) → 실패 시 _fetch_with_fdr 폴백. yfinance 제거.
```

**경계**: toss_client 는 I/O(인증+HTTP+페이지네이션), data_fetcher 는 변환+폴백 오케스트레이션.
`_candles_to_df` 는 토스 API 없이 단위 테스트 가능한 순수 함수.

## 4. DataFrame 변환 (`_candles_to_df`)

토스 candle 리스트 → 기존 yfinance 스타일 DataFrame (소비자 호환):
- 컬럼: `Open, High, Low, Close, Volume` (대문자 — 기존 fetch_stock_data 계약)
- index: `timestamp` → `pd.to_datetime` → **tz 제거**(`tz_localize(None)`), 날짜만
  (기존 `df.index = pd.to_datetime(df.index).tz_localize(None)` 패턴과 동일)
- 값: `openPrice` 등 문자열 → `float`. `volume` → float(또는 int).
- 정렬: 최신순 입력 → **시간 오름차순**으로 정렬 (기술지표/ML 이 오름차순 가정).
- 빈 candles → `ValueError` (fetch_stock_data 가 폴백 트리거).

## 5. fetch_stock_data 폴백 흐름 (수정)

```
def fetch_stock_data(symbol, period_days=365, retries=2):
    # 1차: 토스 (retries 회)
    for attempt in range(retries + 1):
        try:
            df = _fetch_with_toss(symbol, period_days)
            if df.empty: raise ValueError(...)
            return df   # index tz-naive, 오름차순
        except Exception as exc:
            if attempt < retries: time.sleep(1)
            else: logger.warning("토스 실패 [%s]: %s — FDR 폴백", symbol, exc)
    # 2차: FDR 폴백 (기존 _fetch_with_fdr 그대로)
    df = _fetch_with_fdr(symbol, start, end)
    return df
```
- 토스 401 → TossClient 내부에서 토큰 재발급 1회 재시도 (기존 _get 로직).
- 토스 자격증명 미설정(RuntimeError) → 즉시 FDR 폴백 (로컬/CI 에서 토스 키 없을 때).
- period_days → 필요한 거래일 수 추정해 count 결정 (예: `int(period_days * 0.75) + 10`,
  대략 영업일 비율 0.69 에 여유. 200 초과면 페이지네이션).

## 6. 페이지네이션 (`fetch_candles` 내부)

```
collected = []
before = None
while len(collected) < count:
    page = GET candles(symbol, interval, count=200, before=before)
    candles = page["candles"]
    if not candles: break
    collected.extend(candles)
    before = page["nextBefore"]
    if before is None: break
return collected[:count]   # 최신순 유지, 필요 개수만
```
- throttle: TossClient 기존 `_REQUEST_INTERVAL`(0.1s) 재사용 (페이지 간).
- 안전장치: 무한루프 방지 — `before` 가 직전과 같으면 중단, 최대 페이지 수 제한(예 10).

## 7. 에러 처리

- 토스 예외/빈 candles/non-200 → `_fetch_with_toss` 가 예외 → fetch_stock_data 가 FDR 폴백.
- 자격증명 미설정 → FDR 직행 (로그 1회).
- FDR 까지 실패 → 기존처럼 `ValueError("토스 및 FDR 모두 실패")`.
- 미국 종목 소수점 가격 → float 변환으로 보존.

## 8. 테스트 (`tests/test_data_fetcher.py` 확장 + `tests/test_toss_client.py`)

- `_candles_to_df`: 변환 정확성 — 컬럼명/float/tz-naive index/오름차순 정렬/빈 입력 ValueError.
- `fetch_candles` 페이지네이션: httpx mock 으로 2페이지(nextBefore 커서) → count 개 수집,
  nextBefore=null 종료, 무한루프 가드.
- `fetch_stock_data` 폴백: 토스 실패(mock 예외) → FDR 호출됨(mock) 검증. 토스 성공 시 FDR 미호출.
- 자격증명 미설정 → FDR 직행.
- 회귀: 기존 fetch_stock_data 소비자(web/technical_analysis/main) 가 동일 컬럼·index 계약 유지.

## 9. 영향 범위 / 호환성

- `fetch_stock_data` 반환 계약(컬럼 `Open/High/Low/Close/Volume`, tz-naive 오름차순 index)
  **불변** — 소비자(technical_analysis, ml_predictor, web_app, prediction_history) 무수정.
- yfinance import 제거 (requirements 정리는 선택 — 다른 곳에서 yf 안 쓰면 제거).
- `_to_krx_code`(기존) 재사용 — `.KS/.KQ` → 6자리. 미국은 그대로 통과 확인 필요
  (현재 `_to_krx_code` 는 `.split(".")[0]` 라 'AAPL' → 'AAPL' OK).

## 10. 배포

- 서버 `.env` 에 TOSS_CLIENT_ID/SECRET 이미 존재 (포트폴리오 sync 에서 설정됨) → 추가 설정 없음.
- 토스 장애 시 FDR 자동 폴백이므로 무중단.
- 배포 후 검증: 신코드(0193W0.KS) 분석이 토스 경로로 성공하는지 로그 확인.
