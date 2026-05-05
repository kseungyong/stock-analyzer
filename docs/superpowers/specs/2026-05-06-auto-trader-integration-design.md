# Auto-Trader 통합 설계

**작성일**: 2026-05-06
**상태**: Phase 0 (vision + Phase 1 spec) — 사용자 사전 승인
**관련 프로젝트**: `~/Projects/stock-analyzer`, `~/Projects/auto-trader`

## 1. 배경

두 프로젝트가 한국 주식 도메인을 다루며 서로 보완적이다:

- **stock-analyzer** — 분석 위주. yfinance 데이터, Tech 시그널 (RSI/MACD/이동평균/볼린저), BNF 시그널 (mean reversion + 시장 패닉), ML 5-모델 앙상블, 90일 예측 히스토리. 정적 종목 리스트 (~16개). KST 16:00/06:00 자동분석 cron.
- **auto-trader** — 실행 위주. KIS API, KOSPI 200 + KOSDAQ 150 universe (350 종목), Donchian breakout + SMA200 + RSI 진입 조건, Telegram 승인 UX, 실 broker 연동 (M6+ 게이트). 자체 cron (15:35 EoD, 08:55 Open).

두 시스템의 신호 산출 의도가 다름 (info vs execution), 데이터 소스가 다름 (yfinance vs KIS), 하지만 **종합 신호 검증** 측면에서 상호 보완 가능.

## 2. 비전

**핵심 시나리오**: stock-analyzer 가 auto-trader 의 **entry gate** 로 작동.

```
[auto-trader 종목 발굴]
  KOSPI 200 + KOSDAQ 150 universe
    ↓
  Donchian 20일 돌파 + SMA200 위 + RSI < 70 + 거래량 1.5배
    ↓
  진입 후보 N개

[stock-analyzer 검증] ─── HTTP polling ───→ GET /api/signal/<symbol>
    ↓
  Tech 시그널 + BNF 시그널 두 검증층
  - Tech 매수 + score ≥ 1
  - BNF 매수/관망 (역추세 매도 신호 없음)
    ↓
  Approval criteria: 양쪽 동의
    ↓
  [auto-trader Telegram 승인 요청 → 주문 제출]
```

**효과**:
- auto-trader 의 trend-following (단일 전략) 에 mean-reversion 검증층 추가 — 추세 끝물 회피
- stock-analyzer 의 BNF "거래량 + 음봉" 신호 — 패닉 매도 후 반등 노림 (양봉 추격은 score 0 으로 가산 안 함)
- ML 앙상블 direction 추가 검증 (advisory, not gating)

## 3. 통합 방식 결정

### 검토한 옵션

| 방식 | 결합도 | 구현 난이도 | 운영 안정성 | ROI |
|---|---|---|---|---|
| **A. Direct Python import** | 매우 높음 (single process, ML 모델 ~800MB share) | 쉬움 | 낮음 (한쪽 깨지면 양쪽 죽음) | 중 |
| **B. Shared SQLite** | 중간 (`predictions.db` 공유 read) | 중간 (TTL 정합) | 중간 (WAL 동시 write 위험) | 중 |
| **C. HTTP JSON API** | 낮음 (HTTP) | 중간 (endpoint 1개) | 높음 (양쪽 독립 운영) | **높음** |
| **D. 공유 모듈 패키지** | 중간 (`shared-indicators`, `shared-data`) | 어려움 (양쪽 리팩터) | 높음 | 장기 (Phase 3) |

### 결정: **C (HTTP JSON API) 우선**

이유:
- **결합도 낮음** — 양 프로젝트 독립 운영 가능. 한쪽 down 되어도 다른 쪽 영향 없음 (auto-trader 가 fallback 가능).
- **운영 환경 일치** — stock-analyzer 가 이미 회사 서버 (sykim-macmini) 에서 Tailscale Funnel + Basic Auth 로 노출 중. 별도 서비스 추가 없음.
- **단일 변경점** — 신규 endpoint 1개 추가 (`GET /api/signal/<symbol>`). 기존 동작 영향 0.
- **점진적 진화** — Phase 1 후 효과 측정 후 Phase 2 (auto-trader 통합), Phase 3 (공유 모듈) 단계적.

옵션 A 는 ML 모델 메모리 공유로 매력적이지만 auto-trader 의 단순한 KIS 운영 환경에 ML 의존성 (lightgbm/torch/transformers 등 ~2GB) 추가가 부담. 옵션 B 는 SQLite 동시성 위험. D 는 장기 비전.

## 4. Phase 1 — Signal JSON API (즉시 구현)

### 신규 endpoint: `GET /api/signal/<symbol>`

**stock-analyzer 의 Flask web_app.py 에 추가**.

#### 응답 형식 (캐시 hit)

```json
{
  "symbol": "005930.KS",
  "name": "삼성전자",
  "market": "korea",
  "generated_at_kst": "2026-05-06 16:02 KST",
  "generated_at_unix": 1746518520,
  "is_fresh": true,
  "tech": {
    "signal": "매수",
    "score": 3
  },
  "bnf": {
    "signal": "관망",
    "score": 1
  }
}
```

#### 응답 형식 (캐시 miss)

HTTP 404:
```json
{
  "error": "no_cache",
  "symbol": "005930.KS",
  "message": "분석 이력 없음. POST /analyze/<symbol> 로 분석 트리거 후 polling."
}
```

#### 응답 형식 (잘못된 심볼)

HTTP 400:
```json
{
  "error": "invalid_symbol",
  "symbol": "<script>"
}
```

### 인증

기존 Basic Auth 게이트 그대로 사용 (`ENABLE_BASIC_AUTH=1`). auto-trader 측은 동일 user/pw 또는 별도 추가 후 환경변수로 호출. CSRF 검증은 `_csrf_validate` 가 POST 만 검증하므로 GET API 영향 없음.

### 응답에 ML 미포함 이유

- `analysis_cache` 에는 result_html (전체 HTML) 만 저장. ML ensemble direction/confidence 별도 컬럼 없음.
- HTML parse 는 fragile. 별도 컬럼 추가는 마이그레이션 + 다른 worker 변경 → 단일 endpoint 가치 대비 큰 변경.
- ML 정보는 follow-up 단계 (`prediction_history.hit_rate_by_model` 활용해 별도 endpoint, 또는 schema 확장) 에서 처리.

Phase 1 의 목적: **빠른 통합**. Tech + BNF 만으로도 entry gate 의미 있음 (auto-trader 의 Donchian breakout 검증 충분).

### 시그니처

```python
@app.route("/api/signal/<path:symbol>")
def api_signal(symbol: str):
    """auto-trader 등 외부 시스템용 — 캐시된 시그널 JSON.

    Returns:
        200 + JSON dict (캐시 hit)
        404 + {"error": "no_cache"} (캐시 miss)
        400 + {"error": "invalid_symbol"} (검증 실패)
    """
```

## 5. 구현 범위

`stock-analyzer` 측 변경:
- `src/web_app.py` — 신규 라우트 `api_signal` + JSON 응답 헬퍼
- `tests/test_web_app.py` — 신규 클래스 `TestApiSignal` (4 케이스)

`auto-trader` 측 변경: **0** (이번 phase). 통합 가이드 문서만 추가.

## 6. 테스트 케이스

`TestApiSignal` (4 케이스):
1. `test_cache_hit_returns_json` — 캐시 row 있는 종목 → 200, JSON dict (symbol/tech/bnf 등 모든 키)
2. `test_cache_miss_returns_404` — 캐시 없는 종목 → 404, `{"error": "no_cache"}`
3. `test_invalid_symbol_returns_400` — `<script>` 등 → 400
4. `test_csrf_not_required` — GET 이라 CSRF 검증 없음 (API 호출에 토큰 불필요)

기존 401 (인증 미통과) 동작은 `_basic_auth_gate` 가 동일하게 처리 → 추가 테스트 불필요.

## 7. 운영

- 배포: stock-analyzer worktree → squash merge → 서버 git pull + web 재시작 (이전 패턴 동일)
- auto-trader 측: 통합 가이드 문서 (docs/integration/stock-analyzer.md) 만 추가. 사용자가 깨어 검토 후 Phase 2 결정.
- 회귀 위험: 0. 신규 endpoint 추가만, 기존 라우트/로직 영향 없음.

## 8. Phase 2 (계획) — auto-trader 측 entry gate

**시기**: Phase 1 검증 + 사용자 승인 후
**범위**: auto-trader 의 `orchestrator/eod_job.py` 가 신호 후보 결정 시점에 stock-analyzer signal API 호출. 응답에 따라 진입 후보 필터링.

```python
# auto-trader 가상 코드 (Phase 2)
def filter_candidates_with_analyzer(candidates):
    approved = []
    for sym in candidates:
        resp = requests.get(f"{ANALYZER_URL}/api/signal/{sym}", auth=BASIC_AUTH, timeout=5)
        if resp.status_code != 200:
            # cache miss / down — fail-open or fail-closed (정책)
            continue
        data = resp.json()
        if data["tech"]["signal"] != "매도" and data["bnf"]["signal"] != "매도":
            approved.append(sym)
    return approved
```

**fail-open vs fail-closed** 결정은 Phase 2 spec 단계에서 합의.

## 9. Phase 3 (장기) — 공유 모듈

- `shared-indicators` — RSI/MACD/이동평균/Donchian 한 곳에 정의 (양 프로젝트 import)
- `shared-telegram` — auto-trader 의 봇 채널을 stock-analyzer 가 일부 사용 (분석 알림)
- `shared-data` (선택) — KIS 클라이언트를 stock-analyzer 도 fallback 으로 사용 (현재 yfinance 만)

장기 — 양 프로젝트 안정화 + 사용 패턴 검증 후.

## 10. 비목표 (Non-goals)

- ML 신호 API 노출 — Phase 1 범위 외 (analysis_cache 스키마 변경 회피)
- auto-trader 코드 직접 수정 — Phase 1 은 stock-analyzer 만
- POST 분석 트리거 (auto-trader 가 cache miss 시 trigger) — 추후. Phase 1 은 read-only
- 두 프로젝트 단일 monorepo 통합 — 운영 독립성 유지
- 실시간 시그널 push (WebSocket / SSE) — polling 으로 충분 (KST 16:00 cron 후 17:00 polling)
- 새 인증 체계 — 기존 Basic Auth 재사용
