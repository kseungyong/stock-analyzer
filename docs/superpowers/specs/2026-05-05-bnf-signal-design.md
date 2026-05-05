# BNF 스타일 시그널 추가 설계

**작성일**: 2026-05-05
**상태**: 설계 — 사용자 검토 대기
**관련 모듈**: `src/technical_analysis.py`, `src/analysis_cache.py`, `src/web_app.py`, `main.py`

## 1. 배경 및 목적

기존 `generate_signal` (RSI/MACD/이동평균/볼린저/거래량) 은 추세 추종 + 모멘텀 위주로 매수/매도/관망을 판단한다. **BNF (Takashi Kotegawa) 기법** 은 이와 다른 철학:

- **이격율 기반 평균회귀** — MA20 에서 -10% 이상 이격되면 반발 매수
- **시장 패닉 매수** — 시장 인덱스 + 종목 동시 폭락 시 매수 강도 강화
- **거래량 + 음봉 결합** — 패닉 매도 후 반발 노림 (양봉 추격 매수 안 함)

목적: 두 기법을 동시에 보여 사용자가 합의/분기를 한 눈에 인식. 둘 다 매수 → 강한 신호, 의견 분기 → 신중.

## 2. 결정된 정책 (브레인스토밍 합의)

| 항목 | 결정 |
|---|---|
| 통합 vs 분리 | **별도 `generate_bnf_signal` 함수** + 카드에 두 시그널 동시 표시 |
| 시장 센티먼트 | **통합** — KOSPI(한국) / S&P500(미국) 인덱스 fetch |
| 시장 데이터 캐싱 | **메모리 TTL 15분** (모듈 변수, 외부 의존성 없음) |
| 임계값 | 기존과 일치 (`score >= 2` 매수, `<= -2` 매도) — 두 시그널 비교 용이 |
| 카드 표시 | header 영역 세로: `[Tech] [BNF] [시장]` 3 뱃지 |
| 결과 페이지 | BNF 시그널 추가 표시 안 함 (카드만) — 기존 result_html 변경 회피 |
| DB | `analysis_cache` 에 `bnf_signal_value` / `bnf_signal_score` 컬럼 추가 |
| Graceful degradation | 시장 fetch 실패 시 종목 단독 BNF 시그널 산출 (`market_df=None`) |

## 3. 아키텍처

```
[fetch_stock_data(symbol)] → df
[fetch_market_df(market)]  → market_df  (TTL 15분 캐시)
            ↓
[analyze_stock(symbol, name, market)]
  ├─ generate_signal(df)                        → result["signal"]      (기존)
  └─ generate_bnf_signal(df, market_df)         → result["bnf_signal"]  (신규)
            ↓
[analysis_cache.put(... signal_*/bnf_signal_*)]
            ↓
[대시보드 카드] — Tech 뱃지 + BNF 뱃지 + 시장 뱃지 (세로)
```

## 4. BNF 시그널 점수 로직

### `generate_bnf_signal(df, market_df=None) -> dict`

`src/technical_analysis.py` 에 신규 함수 추가.

#### 점수 항목 (총 가능 범위 약 -4 ~ +5)

| 지표 | 조건 | 점수 |
|---|---|---|
| **MA20 이격율** | ≤ -10% | +2 |
| | ≤ -5% | +1 |
| | ≥ +7% | -1 |
| | ≥ +10% | -2 |
| **RSI** | ≤ 30 | +1 |
| | ≥ 70 | -1 |
| **거래량 + 음봉** | Volume_Ratio ≥ 2.0 AND close < open | +1 |
| **거래량 + 양봉** | Volume_Ratio ≥ 2.0 AND close > open | 0 (BNF 는 추격 매수 안 함) |
| **시장 이격율** (market_df 있을 때) | 시장 ≤ -3% AND 종목 ≤ -10% | +1 |
| | 시장 ≥ +5% AND 종목 ≥ +7% | -1 |

이격율 계산: `disparity = (close - MA20) / MA20 * 100`

#### 임계값 (기존 generate_signal 과 일치)

- `score >= 2` → **매수**
- `score <= -2` → **매도**
- 그 외 → **관망**

#### 반환 구조 (`generate_signal` 과 동일 형태)

```python
{
    "signal": "매수" | "매도" | "관망",
    "score": int,
    "reasons": ["MA20 -12% 이격", "거래량 2.5배 음봉", ...],
    "indicators": [
        {"name": "MA20 이격율", "value": "-12.3%", "comment": "강한 과매도 — 반발 가능성"},
        {"name": "시장 이격율", "value": "-4.5%", "comment": "시장 패닉 구간"},
        ...
    ],
    "disparity": -12.3,        # 종목 MA20 이격율 % (round 1)
    "market_disparity": -4.5,  # 시장 이격율 % (market_df=None 이면 None)
}
```

## 5. 시장 데이터 fetch + 캐싱

### `fetch_market_df(market: str) -> pd.DataFrame | None`

`src/technical_analysis.py` 에 신규 함수.

```python
_MARKET_INDEX = {
    "korea": "^KS11",   # KOSPI
    "us":    "^GSPC",   # S&P 500
}

_market_cache: dict[str, tuple] = {}  # {index: (df, cached_at_unix)}
_MARKET_CACHE_TTL = 15 * 60  # 15분


def fetch_market_df(market: str) -> pd.DataFrame | None:
    """시장 인덱스 데이터 fetch + 15분 TTL 메모리 캐시.

    market: "korea" 또는 "us". 그 외/None/fetch 실패 시 None.
    """
    index = _MARKET_INDEX.get(market)
    if not index:
        return None
    cached = _market_cache.get(index)
    if cached and (time.time() - cached[1] < _MARKET_CACHE_TTL):
        return cached[0]
    try:
        from src.data_fetcher import fetch_stock_data
        df = fetch_stock_data(index)
        df = compute_indicators(df)
        _market_cache[index] = (df, time.time())
        return df
    except Exception as e:
        logger.warning("시장 데이터 fetch 실패 (%s): %s", index, e)
        return None
```

캐시는 같은 시장에 대해 16종목 일괄 분석 시 fetch 1회로 압축. TTL 15분이라 cron 사이에 자연스럽게 갱신.

## 6. `analyze_stock` 통합

### 시그니처 변경

```python
def analyze_stock(symbol: str, name: str, market: str | None = None) -> dict | None:
```

`market` 기본값 `None` — backwards-compat. 호출자가 명시 전달 권장.

### 결과 dict 에 `bnf_signal` 추가

```python
result = {
    "name": name, "symbol": symbol, "df": df,
    "signal": signal,                    # 기존
    "bnf_signal": bnf_signal_or_none,   # 신규 — generate_bnf_signal 결과 dict 또는 None
    "prediction": prediction,
    "news": news, "sentiment": sentiment,
}
```

### 호출자 (`market` 전달)

- `_run_analysis_bg` (web_app.py): `_market_of(symbol)` 로 lookup → `analyze_stock(symbol, name, market=...)`
- `collect_analyses` (main.py): config 에서 종목별 market 매핑 → 전달
- `auto_analyze_market("korea")` (main.py): 이미 market 변수 있음 → `analyze_stock(s["symbol"], s["name"], market=market)`

## 7. `analysis_cache` 스키마 확장

### 컬럼 추가

```sql
ALTER TABLE analysis_cache ADD COLUMN bnf_signal_value TEXT;
ALTER TABLE analysis_cache ADD COLUMN bnf_signal_score INTEGER;
```

`_migrate` 가 자동 처리 — 이전 작업과 같은 패턴 (PRAGMA table_info 체크 후 조건부 ALTER).

### `put` 시그니처

```python
def put(
    cache_key: str, market: str, result_html: str, source: str,
    *,
    signal_value: str | None = None, signal_score: int | None = None,
    bnf_signal_value: str | None = None, bnf_signal_score: int | None = None,
) -> None:
```

INSERT/UPDATE SQL 도 두 컬럼 추가 (signal_value 와 같은 패턴).

### `get` / `list_symbols` 반환 dict

기존 7 키 + 2 키:

```python
{
    "cache_key": str, "market": str, "result_html": str,
    "generated_at": int, "source": str,
    "signal_value": str | None, "signal_score": int | None,
    "bnf_signal_value": str | None,    # 신규
    "bnf_signal_score": int | None,    # 신규
}
```

## 8. 카드 UI

### `_render_signal_badge` 시그니처 확장 — `prefix` 매개변수

```python
def _render_signal_badge(
    value: str | None,
    score: int | None,
    prefix: str = "",
) -> str:
    """prefix 가 있으면 'BNF 매수 +3' 형태."""
    if not value:
        return ""
    cls = _SIGNAL_CLASS.get(value, "signal-hold")
    if score is None:
        score_part = ""
    elif score > 0:
        score_part = f" +{score}"
    elif score < 0:
        score_part = f" {score}"
    else:
        score_part = " 0"
    label = f"{prefix}{value}" if prefix else value
    return f'<span class="signal-badge {cls}">{label}{score_part}</span>'
```

### `index` 카드 마크업

```python
signal_badge_html = _render_signal_badge(
    cache_row.get("signal_value") if cache_row else None,
    cache_row.get("signal_score") if cache_row else None,
)
bnf_badge_html = _render_signal_badge(
    cache_row.get("bnf_signal_value") if cache_row else None,
    cache_row.get("bnf_signal_score") if cache_row else None,
    prefix="BNF ",
)
# stock-card-badges 컨테이너 안:
#   {signal_badge_html}{bnf_badge_html}<span class="badge ...">{market_label}</span>
```

`stock-card-badges` 컨테이너는 이미 세로 정렬 (`flex-direction: column`) — CSS 추가 변경 없음.

### 카드 결과 시각

**합의 매수**:
```
┌────────────────────────────────────┐
│ Apple                  [매수 +3]   │
│ AAPL                [BNF 매수 +2]  │
│                          [미국]    │
└────────────────────────────────────┘
```

**의견 분기** (Tech 매도 / BNF 매수 — 과매도 반발 노림):
```
┌────────────────────────────────────┐
│ Tesla                  [매도 -3]   │
│ TSLA                [BNF 매수 +2]  │
│                          [미국]    │
└────────────────────────────────────┘
```

## 9. 에러 / 엣지 케이스

| 시나리오 | 동작 |
|---|---|
| 시장 fetch 실패 | `fetch_market_df` → None. `generate_bnf_signal(df, None)` → 시장 점수 항목 0, 종목 단독 시그널 |
| TTL 캐시 stale 후 fetch 실패 | 이전 stale 데이터 삭제 + None 반환 |
| `analyze_stock(market=None)` | bnf_signal=None 반환 — cache.put 시 NULL |
| 기존 row (bnf 없음) | 카드에 BNF 뱃지 미표시. 다음 분석 시 채워짐 |
| `generate_bnf_signal` 예외 | `analyze_stock` 의 try/except 가 catch — bnf_signal=None, 분석 정상 |
| MA20 NaN (60일 미만 데이터) | 이격율 점수 항목 0. 다른 점수만 합산 |
| 시장 외 종목 (미정의 market) | `_MARKET_INDEX.get(market)` → None → market_df=None |
| 두 시그널 의견 분기 | 카드에 색상 다른 두 뱃지 — 시각적 분기 명시 |

## 10. 모듈 책임

```
src/technical_analysis.py  — generate_bnf_signal, _MARKET_INDEX, _market_cache,
                             fetch_market_df (+ TTL 캐시 로직)
src/analysis_cache.py      — _SCHEMA / _migrate 두 컬럼 추가, put/get/list_symbols 시그니처 확장
main.py                    — analyze_stock 시그니처 (market 인자) + bnf_signal 결과 통합,
                             collect_analyses / auto_analyze_market 가 market 전달
src/web_app.py             — _render_signal_badge prefix 매개변수, index 카드 BNF 뱃지 추가,
                             _run_analysis_bg / _run_full_analysis_bg 가 BNF signal cache.put
```

## 11. 테스트 전략

### `tests/test_technical_analysis.py` (신규/보강)

**TestGenerateBnfSignal** (8 케이스)
1. test_strong_oversold_buy — MA20 -12%, RSI 25 → 매수
2. test_strong_overbought_sell — MA20 +12%, RSI 75 → 매도
3. test_neutral_hold — 모든 지표 중립
4. test_panic_volume_buy_signal — 거래량 2.5배 + 음봉 + 이격 -8% → 매수 가산
5. test_volume_surge_no_buy_on_green — 거래량 2.5배 + 양봉 → 0점
6. test_market_panic_amplifies_buy — 시장 -4%, 종목 -11% → 시장 +1
7. test_market_overheat_amplifies_sell — 시장 +6%, 종목 +8% → 시장 -1
8. test_market_df_none_uses_stock_only — market_df=None → 시장 점수 0

**TestFetchMarketDf** (4 케이스)
1. korea → ^KS11 fetch
2. us → ^GSPC fetch
3. fetch 실패 (stub raise) → None + warning
4. TTL 캐시 — 첫 fetch 후 두 번째 호출 시 fetch_stock_data call_count == 1

> **테스트 격리 주의**: `_market_cache` 가 모듈 레벨 dict 라 테스트 간 격리 필요. 각 케이스 시작 시 `technical_analysis._market_cache.clear()` 또는 `autouse` fixture 로 자동 비우기.

### `tests/test_analysis_cache.py` 보강

**TestMigrateAddsBnfColumns** (2 케이스)
1. 새 DB → bnf_signal_value, bnf_signal_score 컬럼 존재
2. 기존 row 마이그레이션 후 NULL 보존

**TestPutGetBnfSignal** (3 케이스)
1. put with bnf kwargs → get bnf 필드 정확
2. put 기본 → bnf 필드 None
3. UPSERT bnf NULL 로 덮어쓰기

### `tests/test_main.py` 보강

**TestAnalyzeStockBnfSignal** (2 케이스)
1. analyze_stock(market="us") 호출 → result["bnf_signal"] 존재
2. analyze_stock(market=None) → bnf_signal=None

### `tests/test_web_app.py` 보강

**TestRenderSignalBadgeBnfPrefix** (2 케이스)
1. prefix="BNF " → "BNF 매수 +3"
2. prefix 기본 → 기존 동작

**TestIndexCardBnfBadge** (3 케이스)
1. cache_row bnf_signal_value="매수" → "BNF 매수" 표시
2. bnf_signal_value=NULL → BNF 뱃지 미표시
3. signal + bnf 둘 다 → 두 뱃지 표시

**TestWorkerBnfSignal** (1 케이스)
`_run_analysis_bg` cache.put 호출 시 bnf_signal_value/score 전달

총 신규 테스트 25건.

## 12. 마이그레이션 / 배포

1. `analysis_cache` 두 컬럼 자동 추가 (`init_db` 모듈 로드 시)
2. 기존 row bnf NULL → 다음 분석 후 채워짐
3. 배포: git push → 서버 git pull → web/scheduler 재시작
4. 첫 자동 cron (KST 16:00 / 06:00) 또는 수동 분석 후 카드에 BNF 뱃지 표시

## 13. 비목표 (Non-goals)

- BNF 시그널의 백테스트 — 별도 follow-up
- 결과 페이지 (`/stock/<symbol>`) 의 result_html 안에 BNF 섹션 추가 — `report_generator` 변경 회피, 카드만
- 환경변수로 BNF on/off 토글 — 항상 on
- 시그널 알림 (이메일/푸시) — 추후
- `report_generator` 의 이메일 다이제스트에 BNF 시그널 표시 — follow-up
- 일중 BNF 시그널 (intraday) — 일봉 기반만
- 시장 인덱스 캐시 SQLite 영속화 — 메모리 TTL 충분
- 종목 외 자산 (암호화폐, FX) — `_MARKET_INDEX` 에 정의된 market만
