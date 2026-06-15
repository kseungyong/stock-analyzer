# 토스증권 보유주식 → 포트폴리오 자동 동기화 설계

- **작성일**: 2026-06-15
- **상태**: 설계 승인 완료 + 실제 API probe 검증 완료 (구현 대기)
- **관련 모듈**: `src/portfolio.py`, `src/stock_search.py`, `src/kis_client.py`(참조 패턴)

> **2026-06-15 probe 검증**: 실제 토스 계좌로 token→accounts→holdings 호출 성공.
> 응답 포맷·식별자·symbol 포맷·소수점 수량 이슈를 실측으로 확정 (아래 반영).

## 1. 목적

토스증권 Open API의 보유주식(holdings) 조회를 이용해, 사용자의 실제 증권계좌
보유 종목을 stock-analyzer 의 `portfolio` 테이블에 자동으로 미러링한다. 수동으로
종목·수량·평단을 입력하던 작업을 제거하고, 실계좌와 100% 일치시킨다.

### 비목표 (Non-goals)

- 주문/매매 기능 (토스 Order API 미사용 — 읽기 전용)
- 시세/캔들 데이터 소스 교체 (별도 작업으로 분리)
- 양방향 sync (stock-analyzer → 토스 푸시 안 함)
- 거래내역(transaction) 자동 생성
- 미국 종목 환율 변환 (토스가 주는 USD 평단 그대로 저장)

## 2. 데이터 소스 — 토스 Open API

문서: https://developers.tossinvest.com/docs
OpenAPI: https://openapi.tossinvest.com/openapi-docs/latest/openapi.json

### 사용 엔드포인트 (3개)

| 메서드 | 경로 | 용도 | 헤더 |
|--------|------|------|------|
| POST | `/oauth2/token` | 토큰 발급 | (없음) |
| GET | `/api/v1/accounts` | 계좌 조회 | `Authorization: Bearer` |
| GET | `/api/v1/holdings` | 보유주식 조회 | `Authorization: Bearer` + `X-Tossinvest-Account: {accountSeq}` |

- Base URL: `https://openapi.tossinvest.com`
- **모든 응답은 `{"result": ...}` envelope 로 감싸짐** (probe 확인). 에러 시
  `{"result": {"error": {"requestId", "code", "message"}}}` (HTTP 4xx).
- **`X-Tossinvest-Account` 헤더에는 `accountNo`(11자리 문자열)가 아니라
  `accountSeq`(정수)를 넣어야 함.** accountNo 사용 시 400 `account-not-found`.
  (probe 로 확정 — OpenAPI 문서 추정과 다름)

### 인증

OAuth2 Client Credentials Grant.
- 요청(form-urlencoded): `grant_type=client_credentials`, `client_id`, `client_secret`
- 응답: `access_token`(JWT), `token_type=Bearer`, `expires_in`(초, ~86400)
- KIS 와 동일한 패턴 → `src/kis_client.py` 토큰 캐시/rate-limit 구조 복제

### accounts 응답 (probe 실측)

`{"result": [{"accountNo": "<11자리 문자열>", "accountSeq": <정수>, "accountType": "BROKERAGE"}]}`

- `result` 는 **list** (계좌 복수 가능). 첫 계좌 또는 `TOSS_SYNC_ACCOUNT_SEQ` 지정분 사용.
- holdings 호출에 쓰는 식별자는 **`accountSeq`**.

### holdings 응답 (probe 실측)

`{"result": {"totalPurchaseAmount", "marketValue", "profitLoss", "dailyProfitLoss", "items": [...]}}`

`items[]` 각 종목 키 (실측):
`symbol, name, marketCountry, currency, quantity, lastPrice, averagePurchasePrice,
marketValue{purchaseAmount,amount,amountAfterCost}, profitLoss{amount,amountAfterCost,rate,rateAfterCost},
dailyProfitLoss{amount,rate}, cost{commission,tax}`

### holdings 핵심 필드 (`items[]`)

| 토스 필드 | 타입 | 매핑 |
|-----------|------|------|
| `symbol` | string | → stock-analyzer symbol (변환 필요) |
| `name` | string | 저장 안 함 (portfolio 스키마에 name 컬럼 없음; notes 는 사용자 수동 메모용이라 덮어쓰지 않음) |
| `marketCountry` | string (KR/US) | 거래소 판정 분기 |
| `currency` | string (KRW/USD) | 저장 안 함 (참고용) |
| `quantity` | string | → `qty` (int 변환) |
| `averagePurchasePrice` | string | → `avg_price` (float 변환) |
| `lastPrice` | string | 미사용 (현재가는 stock-analyzer 가 별도 fetch) |

## 3. 자격증명 & 설정

`.env` (gitignored):
```
TOSS_CLIENT_ID=...
TOSS_CLIENT_SECRET=...
TOSS_SYNC_USERNAME=admin       # 자동 스케줄이 sync 할 대상 username
TOSS_SYNC_ACCOUNT_SEQ=         # (선택) 계좌 복수 시 지정. 미지정이면 첫 계좌
```
토큰 캐시: `~/.cache/stock-analyzer/toss_token.json` (TTL 24h)

자격증명 로드 우선순위: 환경변수 → `stock-analyzer/.env` (KIS 와 동일 fallback 패턴)

## 4. 아키텍처

```
src/toss_client.py            # 외부 I/O (OAuth2 + holdings)
  ├ _load_credentials()        env → .env
  ├ _load_cached_token() / _save_token()
  ├ TossClient.fetch_accounts() -> list[dict]   # [{accountNo, accountSeq, accountType}]
  └ TossClient.fetch_holdings(account_seq) -> list[dict]   # X-Tossinvest-Account=accountSeq

src/toss_sync.py             # 비즈니스 로직 (토스 API 비의존, 단위 테스트 가능)
  ├ _to_sa_symbol(toss_symbol, market_country) -> str
  ├ mirror_to_portfolio(username, holdings) -> dict   # {added, updated, removed, skipped}
  └ run_sync(username, *, dry_run=False) -> dict

main.py                      # CLI 서브커맨드 'toss-sync'
src/web_app.py               # POST /portfolio/sync + 버튼
scripts/toss-sync.plist.template   # launchd
```

**경계 원칙**: `toss_client`(I/O)와 `toss_sync`(로직)를 분리. sync 로직은 토스 API
없이 임시 DB + stub 으로 단위 테스트한다.

## 5. symbol 변환 (`_to_sa_symbol`)

토스 `symbol` + `marketCountry` → stock-analyzer symbol:

- `marketCountry == "US"` → ticker 그대로 (예: `AAPL`)
- `marketCountry == "KR"` → 6자리 코드에 거래소 suffix 부착:
  - `stock_search.py` 의 `fdr.StockListing("KRX")` 결과로 code→market(KOSPI/KOSDAQ) 룩업
  - KOSDAQ → `.KQ`, 그 외 → `.KS` (stock_search.py:50 과 동일 규칙)
  - 룩업 실패 시 `.KS` 기본값 + 경고 로그

> **probe 확정**: 토스 한국 종목 `symbol` 은 **6자리 순수 숫자**(`isdigit=True`, 예 `005930`),
> 미국은 알파벳 ticker(`isalnum`, 예 `INTQ`). 별도 정규화 불필요 — 한국은 6자리 코드에
> suffix 부착, 미국은 그대로. KRX listing 룩업 테이블은 프로세스 1회 캐시.
> 룩업 실패/변환 불가 종목은 skip + 로그 (sync 중단 안 함).

## 5b. 소수점 수량 — `portfolio.qty` REAL 마이그레이션

**probe 확정**: 토스 미국 보유분에 **소수점 수량(fractional shares)** 존재
(`any fractional quantity: True`). 현 `portfolio.qty INTEGER` 는 `int()` 절삭으로
1.5주 → 1주가 되어 평가액이 틀어진다. → **`portfolio.qty` 를 REAL 로 마이그레이션.**

영향 범위 (구현 시 일괄 점검):
- 스키마: `portfolio.qty INTEGER` → `REAL`. SQLite 는 ALTER 로 타입 변경 불가 →
  `_migrate_to_multiuser` 와 동일한 rename→recreate→copy→drop 패턴으로 멱등 마이그레이션.
- `portfolio.py`: `add_holding`, `update_holding`, `list_holdings`,
  `get_holding_with_pnl`, `_validate` 의 `int(qty)` → `float(qty)`.
- 표시 포맷: 정수면 정수로, 소수면 소수 자릿수 표시 (web_app 보유표/평가손익).
- **범위 한정**: `portfolio_transactions.qty` 와 `record_buy/sell/adjust` 는 이번 sync
  경로 밖(수동 거래 기록 전용)이므로 INTEGER 유지. 단 record_* 가 portfolio.qty 를
  갱신할 때 `int(existing_qty)` → `float(existing_qty)` 로 읽기 일관성만 맞춘다.
- 수동 입력 폼(web_app)은 한국 주식 기준 정수 입력 유지 — 변경 불필요.

## 6. 미러링 로직 (`mirror_to_portfolio`)

```
target = { _to_sa_symbol(h): (float(avg_price), float(qty)) for h in holdings if float(qty) > 0 }
current = { row.symbol: row for row in portfolio.list_holdings(username) }

for sym, (avg, qty) in target.items():
    # add_holding 반환값(신규=True/갱신=False)으로 added vs updated 카운트.
    # notes 인자는 생략 → 기존 사용자 메모 보존.
    portfolio.add_holding(username, sym, avg, qty)   # upsert (추가/갱신)

for sym in current.keys() - target.keys():
    portfolio.remove_holding(username, sym)          # 토스에 없음 → 제거

# transaction 테이블 미변경 (상태 동기화이므로 매매 시점/체결가 없음)
```

반환: `{added, updated, removed, skipped, target_count}`

## 7. 안전장치

전체 미러링은 토스 API 가 일시적으로 빈 배열/에러를 주면 포트폴리오를 통째로
비울 위험이 있다. 다음 가드로 방지한다:

| 조건 | 동작 |
|------|------|
| `fetch_accounts()` 빈 배열 | abort — 포트폴리오 무변경 |
| holdings fetch 예외 / non-200 | abort — 기존 포트폴리오 보존 |
| 현재 보유 **3종목 이상** & 삭제 대상이 50% 초과 | abort + 경고 로그. `TOSS_SYNC_FORCE=1` 로만 강제 |
| `avg_price <= 0` 또는 `qty < 0` | 해당 종목 skip + 로그 |
| symbol 변환 실패 | 해당 종목 skip + 로그 (sync 계속) |

> 빈 계좌가 진짜 정상(전량 청산)일 수 있으므로 50% 가드는 hard-fail 이 아니라
> 강제 플래그로 우회 가능하게 둔다. cleanup.py 의 safety-limit 과 동일 철학.
>
> **3종목 이상 조건**: 1~2종목 소규모 포트폴리오는 1종목 정상 매도도 50% 초과가
> 되어 false-positive 가 잦다. 가드 목적은 *대량* 삭제 방지이므로 보유 3종목
> 이상일 때만 활성화한다 (구현 시 plan 의 50%-only 공식이 자체 테스트와 모순돼 보정).

## 8. 트리거

### CLI
```
python main.py toss-sync                # TOSS_SYNC_USERNAME(admin) 에 sync
python main.py toss-sync --dry-run      # diff 출력, DB 무변경
python main.py toss-sync --user <name>  # 특정 사용자
```
한국 휴장일에도 실행 (미국 보유분 평일 반영). KR 전용 데이터가 아니므로
`is_kr_market_open_today` 로 skip 하지 않는다.

### 웹 UI (`/portfolio`)
- 상단 **"📥 토스 동기화"** 버튼 → `POST /portfolio/sync` (CSRF 보호)
- sync 대상: `_current_user()` (버튼 누른 로그인 사용자)
- 결과 flash: "추가 N · 갱신 M · 제거 K" 또는 abort 사유
- 버튼 옆 마지막 sync 시각 표시

### launchd
- `scripts/toss-sync.plist.template` → `ai.stock-analyzer.toss-sync`
- 매일 **15:50 KST** (장 마감 15:30 직후, foreign-ranking 16:00 보다 앞)
- 서버 배포: plist 치환 + `launchctl bootstrap` (기존 절차 동일)

## 9. 에러 처리

- 토큰 401 → 캐시 삭제 후 1회 재발급 재시도 (kis_client 와 동일)
- sync 전체 실패 → 로그 + `exit 1` (포트폴리오 무변경), 웹은 flash 에러
- 부분 실패(일부 종목 변환/검증 실패) → 해당 종목 skip + 로그, 나머지 진행

## 10. 테스트 (`tests/test_toss_sync.py`)

- `_to_sa_symbol`: KOSPI/KOSDAQ/US 변환 (KRX listing monkeypatch stub)
- `mirror_to_portfolio`: add/update/remove/skip diff (임시 DB)
- 50% 삭제 가드 abort + `TOSS_SYNC_FORCE` 우회
- `qty == 0` skip, `avg_price <= 0` skip
- **소수점 qty(미국 fractional) 저장/조회 정확성** — REAL 마이그레이션 후 1.5주 보존 검증
- envelope/식별자: `accountSeq` 로 holdings 조회, `accountNo` 는 실패하는 회귀 테스트
- client(`toss_client.py`)는 외부 I/O → httpx mock 또는 통합테스트로 분리,
  단위 테스트 범위에서 제외

## 11. 배포 체크리스트

1. `.env` 에 TOSS_CLIENT_ID/SECRET 설정 (로컬 + 서버)
2. `pytest tests/test_toss_sync.py` 통과
3. 서버 `git pull` + 웹 재시작
4. plist 치환 + launchd 등록
5. `python main.py toss-sync --dry-run` 으로 변환/diff 검증
6. 실제 1회 sync 후 `/portfolio` 확인
