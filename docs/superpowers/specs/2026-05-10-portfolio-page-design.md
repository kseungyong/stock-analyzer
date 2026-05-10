# Portfolio 페이지 설계

**작성일**: 2026-05-10
**상태**: 사용자 승인 (a Full feature)

## 목표

`/portfolio` 새 페이지 — 본인 보유 종목만 입력 (symbol, 평균가, 수량) → 손익 + 분석 시그널 + 패턴 1:1 카드 표시.

## 1. DB

신규 테이블 `portfolio`:
```sql
CREATE TABLE portfolio (
  symbol TEXT PRIMARY KEY,
  avg_price REAL NOT NULL,
  qty INTEGER NOT NULL DEFAULT 0,
  added_at INTEGER NOT NULL,
  notes TEXT
);
```

`analysis_cache` 컬럼 추가:
```sql
ALTER TABLE analysis_cache ADD COLUMN last_close REAL;
```

`predictions.db` 같은 파일에 둘 다 (analysis_cache 와 동일 DB).

## 2. 새 모듈 `src/portfolio.py`

```python
def init_db() -> None
def add_holding(symbol, avg_price, qty, notes=None) -> bool
def remove_holding(symbol) -> bool
def update_holding(symbol, *, avg_price=None, qty=None, notes=None) -> bool
def list_holdings() -> list[dict]
def get_holding_with_pnl(symbol) -> dict | None
def list_holdings_with_pnl() -> list[dict]
```

`list_holdings_with_pnl()` — JOIN `analysis_cache` 로 last_close + signals + pattern 한 번에.

## 3. UI

### 카드 (각 보유 종목)
```
┌─ 삼성전자 (005930.KS) ─────────────────────┐
│ [📊 +12.5] [매수: 골든크로스 · 더블바텀]      │
│                                            │
│ 평균가: 12,000원  →  현재가: 12,600원        │
│ 수익률: +5.00% (+60,000원) · 10주            │
│ ─────────                                  │
│ 📈 매수 — 골든크로스 (80%)                   │
│ 📊 더블바텀(W) [4/15~5/8, 23일, 넥라인 돌파] │
│ 🎯 지지 11,800원 (-6.3%)                    │
│ ⚠️ W바닥 65% → 신속히 매수                   │
│                                            │
│ [✏️ 수정] [🗑 삭제] [상세 분석 →]            │
└────────────────────────────────────────────┘
```

미국주식 → `$145.30`, 한국주식 → `12,000원`.

### 입력 폼 (페이지 상단)
```
심볼 [_____ autocomplete]  평균가 [_____]  수량 [___]  메모 [____]  [+ 추가]
```

### 통계 헤더
```
보유 5종목  ·  평가액 45,000,000원  ·  평균 수익률 +3.2%  ·  총 손익 +1,440,000원
```

### 정렬 토글 (URL `?sort=`)
- `pnl_pct` (기본) — 수익률 %
- `pnl_abs` — 절대 손익
- `composite` — 매수 추천 강도
- `symbol` — 종목명

### Nav
대시보드 ↔ 포트폴리오 두 탭. 카드 수 표시 (예: "포트폴리오 (5)").

## 4. API 라우트

| 경로 | 메서드 | 동작 |
|---|---|---|
| `/portfolio` | GET | 카드 페이지 렌더 |
| `/portfolio/add` | POST | 보유 추가 (CSRF) |
| `/portfolio/update` | POST | 평균가/수량/메모 수정 |
| `/portfolio/delete` | POST | 삭제 |

기존 `/api/stocks/search` autocomplete 재사용.

## 5. 손익 계산

```
last_close: analysis_cache 의 새 컬럼 (df.Close.iloc[-1])
pnl_pct = (last_close - avg_price) / avg_price × 100
pnl_abs = (last_close - avg_price) × qty
```

last_close NULL (분석 안 된 경우) → 손익 표시 "—" 또는 "분석 필요" 안내.

## 6. 변경 범위

| 파일 | 변경 |
|---|---|
| `src/analysis_cache.py` | `last_close` 컬럼 + migration + `put()` kwarg |
| `src/portfolio.py` 신규 | DB 모듈 |
| `src/web_app.py` | `/portfolio*` 라우트 + nav + 카드 빌드 |
| `main.py` 의 analyze_stock | last_close 추출 후 cache.put 전달 |
| `_run_analysis_bg` / `_run_full_analysis_bg` | 같이 |
| `tests/test_portfolio.py` 신규 | ~10 케이스 |
| `tests/test_analysis_cache.py` | last_close 컬럼 검증 |

## 7. 비목표

- CSV 업로드 — follow-up
- 거래 history (BUY/SELL transactions) — 추후
- 자동 손절선 알림 — 추후
- 종목 그룹화 (산업/시장별 분류) — 추후
- 차트 시각화 (라인 차트) — 텍스트 카드만
