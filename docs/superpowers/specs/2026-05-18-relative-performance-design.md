# 종목별 지수 대비 상승률/하락률 표시 — 설계서

날짜: 2026-05-18
대상: stock-analyzer
상태: Draft (사용자 리뷰 대기)

---

## 1. 목적

각 종목 리포트 카드에 해당 종목이 속한 시장 지수 대비 등락률(알파)을 표시한다. 사용자가 한눈에 "이 종목이 시장보다 잘하고 있는가?"를 판단할 수 있게 한다.

- KOSPI 종목 → KOSPI(^KS11) 비교
- KOSDAQ 종목 → KOSDAQ(^KQ11) 비교
- 미국 종목 → S&P 500(^GSPC) 비교

## 2. 표시 형식

리포트 카드 상단(`stock-summary` 다음 줄)에 한 줄 추가:

```
금일: +1.52%  │  S&P 500: +0.81%  │  알파: +0.71%pp   (2026-05-18 14:32, 장중)
```

- 양수: 녹색(#28a745), 음수: 적색(#dc3545), 0.00%: 회색(#6c757d)
- 알파의 색은 알파 부호 기준 (stock/index 색과 독립)
- stage 라벨: `장중` / `마감 후` / `장 시작 전` / `주말`
- rel_perf 계산 실패 시 해당 줄 생략

## 3. 계산 정의

### 3.1 단일 공식 (시점 무관)

```
stock_pct = (df.Close[-1] - df.Close[-2]) / df.Close[-2] * 100
index_pct = (idx.Close[-1] - idx.Close[-2]) / idx.Close[-2] * 100
alpha_pp  = stock_pct - index_pct
```

yfinance 일봉 데이터는 시점에 따라 마지막 row의 의미가 달라지지만, 계산식은 동일하다:

| 분석 시점 (KST) | df 마지막 row | 의미 |
|---|---|---|
| 장 시작 전 | 어제 날짜, 어제 종가 | "어제의 일간 등락률" |
| 장중 | 오늘 날짜, 실시간 진행중 가격 | "오늘 지금까지의 등락률" |
| 장 마감 후 | 오늘 날짜, 오늘 종가 | "오늘 일간 등락률" |

### 3.2 stage 분류

분석 호출 시점(`datetime.now(KST)`)과 시장별 운영 시간으로 라벨링:

- KOSPI/KOSDAQ: 09:00–15:30 KST
- S&P (NYSE/NASDAQ): 22:30–05:00 KST (서머타임 시 23:30–06:00) — 단순화: 22:30–06:00 범위면 장중으로 간주
- 주말 → `weekend`

라벨은 **표시 보조용**. 계산 자체에는 영향 없음.

### 3.3 시장(인덱스) 분류

심볼 suffix 기반. `resolve_index_market(symbol)`는 `(display_name, market_key)`를 반환하고, 인덱스 데이터는 기존 `fetch_market_df(market_key)`로 조회한다.

- `*.KS` → `("KOSPI", "korea")` → `^KS11`
- `*.KQ` → `("KOSDAQ", "kosdaq")` → `^KQ11`  (신규 키)
- 그 외 (영문 심볼) → `("S&P 500", "us")` → `^GSPC`

`market_key`는 `_MARKET_INDEX` dict의 키로 직접 사용된다 — 기존 `korea/us` API와 호환되며 `kosdaq` 한 키만 추가하면 된다.

## 4. 아키텍처

### 4.1 변경 파일

```
src/technical_analysis.py    + _MARKET_INDEX['kosdaq'] = '^KQ11'
                             + resolve_index_market(symbol) -> (name, index_symbol)
                             + compute_relative_performance(df, symbol) -> dict|None

main.py                      + analyze_stock() 결과에 'rel_perf' 포함
                               (try/except로 실패 시 None — 기존 BNF/pattern 패턴과 동일)

src/report_generator.py      + _render_rel_perf(rel_perf: dict|None) -> str
                             + _render_stock_card()에 호출 삽입

src/templates/report.css     + .rel-perf, .rel-perf .up/.down/.flat, .rel-perf-asof
```

신규 파일 없음. 신규 의존성 없음.

### 4.2 데이터 흐름

```
analyze_stock(symbol, name, market)
  → df = fetch_stock_data(symbol)                       (기존)
  → rel_perf = compute_relative_performance(df, symbol)  (신규)
      ├─ index_name, market_key = resolve_index_market(symbol)
      ├─ idx_df = fetch_market_df(market_key)            (기존, 15분 캐시 활용)
      └─ {index_name, stock_pct, index_pct, alpha_pp, as_of, stage}
  → return {..., 'rel_perf': rel_perf}

generate_report(analyses)
  → _render_stock_card(item)
    → _render_rel_perf(item['rel_perf'])
```

### 4.3 인터페이스

```python
# src/technical_analysis.py

def resolve_index_market(symbol: str) -> tuple[str, str]:
    """심볼 suffix로 (지수 표시명, market_key) 반환.
    market_key는 _MARKET_INDEX의 키 — fetch_market_df()에 그대로 전달된다.
    예: '005930.KS' → ('KOSPI', 'korea'), '247540.KQ' → ('KOSDAQ', 'kosdaq')
    """

def compute_relative_performance(
    stock_df: pd.DataFrame, symbol: str
) -> dict | None:
    """
    Returns:
        {
            "index_name": "KOSPI",
            "stock_pct": 1.52,
            "index_pct": 0.81,
            "alpha_pp": 0.71,
            "as_of": "2026-05-18 14:32",
            "stage": "market_open",
        }
        실패 시 None.
    """
```

## 5. 에러 처리

기존 코드의 BNF/pattern 처리 패턴과 동일:

```python
# main.py analyze_stock()
rel_perf = None
try:
    rel_perf = compute_relative_performance(df, symbol)
except Exception as e:
    logger.warning("compute_relative_performance 실패 (분석은 계속): %s", e)
```

`None` 반환 케이스:
- `len(df) < 2` (신규상장 등)
- 인덱스 fetch 실패 (`fetch_market_df` 캐시 실패)
- `prev_close == 0` (div-by-zero 방어)

## 6. 테스트

### 신규: `tests/test_relative_performance.py`

1. `test_resolve_index_market_kospi` — `005930.KS` → `("KOSPI", "korea")`
2. `test_resolve_index_market_kosdaq` — `247540.KQ` → `("KOSDAQ", "kosdaq")`
3. `test_resolve_index_market_us` — `AAPL` → `("S&P 500", "us")`
4. `test_compute_relative_performance_basic` — fixture df 주입 → 알파 계산 검증
5. `test_compute_relative_performance_short_df` — `len(df) < 2` → `None`
6. `test_compute_relative_performance_index_fetch_fail` — mocked `None` → `None`
7. `test_compute_relative_performance_zero_prev_close` — div-by-zero 방어 → `None`

### 추가: `tests/test_report_generator.py`

8. `test_render_rel_perf_none_returns_empty` — `None` 입력 → `""`
9. `test_render_rel_perf_positive_alpha` — `up` class + `+` 부호
10. `test_render_rel_perf_negative_alpha` — `down` class + `-` 부호

## 7. 스코프 외 (이번 작업에서 제외)

- NASDAQ/Dow 등 추가 미국 지수
- 섹터 인덱스(XLK 등) 비교
- `analysis_cache` 테이블에 `rel_perf` 영구 저장 (매 분석마다 재계산이 저렴함)
- 이메일 다이제스트(`render_email_digest`)에 별도 추가 — 동일 카드 렌더 함수를 쓰면 자동 반영, 별도 작업 불필요할 가능성 높음. 구현 시 확인.

## 8. 호환성 / 마이그레이션

- 신규 필드는 모두 옵셔널. 기존 호출자/소비자는 영향 없음.
- DB 스키마 변경 없음.
- config(`settings.yaml`) 변경 없음 (suffix로 자동 분류).
- `.KQ` 종목을 새로 추가하면 자동으로 KOSDAQ 비교가 적용된다.
