# Pattern Indicators 설계 — 보조지표 5 카테고리 + UI

**작성일**: 2026-05-10
**상태**: 사용자 승인 (D scope + UX 옵션 1 — 카드 + 분석 페이지 둘 다)
**근거**: 사용자 제공 차트 분석 노트 7장 (이동평균 / 차트 패턴 / 수평선 / 매도매수분류 / 캔들 패턴 / 확률 경고 / 폭발완만형)

## 1. 배경 + 목표

기존 stock-analyzer 시그널 (Tech + BNF + ML) 외에 **차트 분석가가 실제로 보는 패턴들** 을 자동 감지하고 카드 + 분석 페이지에 명시적으로 표시. **explainable** — 사용자가 "왜 매수/매도인지" 패턴 이름과 함께 즉시 이해.

## 2. 결정 사항

| 항목 | 결정 |
|---|---|
| **카테고리** | 5개 (이동평균 / 캔들 / 차트 / 지지저항 / 확률경고) |
| **시그널 통합** | 별개 카테고리. 기존 Tech/BNF/ML 와 score 합산 안 함. summary 만 가중 다수결로 |
| **UI** | 카드 (요약 배지) + 분석 페이지 (5 섹션 상세) 둘 다 |
| **라이브러리** | `pandas-ta` (캔들 60+ 자동) + `scipy.signal` (차트 피크/골) + 자체 구현 (지지저항 피벗) |
| **데이터 흐름** | analysis_cache 확장 — 새 컬럼 `pattern_json` 추가 (full payload) |

## 3. 카테고리 설계

### 3.1 Phase A — 이동평균 4상태 (이미지 1)

**입력**: OHLCV DataFrame
**4 상태**:
- `매수`: 골든크로스 (단기 5/10이 중기 50을 위로 돌파) + 장기선 (50/200) 우상향
- `사지마`: 골든크로스이지만 장기선 하향 (False breakout 위험)
- `매도`: 데드크로스 (단기가 중기 아래) + 장기선 하향
- `팔지마`: 데드크로스이지만 장기선 상향 (단기 조정)

**구현**: pandas SMA 차분 + 최근 N일 cross 감지.

**출력**:
```python
{
  "signal": "매수",
  "label": "골든크로스 (5일이 50일을 5/8에 돌파, 장기선 우상향)",
  "confidence": 0.8,  # 장기선 기울기 강도 + cross 후 일수
  "ma": {"sma5": 12500, "sma50": 12000, "sma200": 11500},
}
```

### 3.2 Phase B — 캔들 패턴 (이미지 5, 7)

**라이브러리**: `pandas-ta` (60+ 패턴 자동 — `cdl_pattern("all")`).

**매수/매도/관망 분류**: pandas-ta 의 결과값 (>0 강세, <0 약세, 0 중립) 을 매핑.

**한국어 이름 매핑**: pandas-ta 영문 이름 → 한국어 (이미지 기반 dict).
- `CDLDOJI` → "도지" (관망)
- `CDLHAMMER` → "망치" (매수)
- `CDL3WHITESOLDIERS` → "적삼병" (매수+폭익)
- `CDL3BLACKCROWS` → "흑삼병" (매도+폭익)
- ~60종

**출력** (최근 5일 기준):
```python
[
  {"name": "적삼병", "signal": "매수", "magnitude": "폭익", "date": "2026-05-08"},
  {"name": "도지", "signal": "관망", "date": "2026-05-09"},
]
```

### 3.3 Phase C — 차트 패턴 (이미지 2, 4)

**자체 구현** (~15 패턴 — 가장 큰 작업):

**필수 알고리즘**: `scipy.signal.find_peaks` 로 피크/골 감지 → 패턴 매칭.

**감지 패턴**:
- **반전 (매수)**: 더블바텀(W), 역헤드앤숄더, 하락쐐기 (반등), 상승삼각형, 상승플래그
- **반전 (매도)**: 더블탑(M), 헤드앤숄더, 상승쐐기 (반락), 하락삼각형, 하락플래그
- **횡보 (대기)**: 박스권, 삼각형 수렴, 확산삼각형
- **추세 (지속)**: 상승채널, 하락채널, 상승깃발, 하락깃발

**출력**:
```python
[
  {"name": "더블바텀(W)", "signal": "매수", "confidence": 0.65, "low1_date": "...", "low2_date": "..."},
  {"name": "삼각형 수렴", "signal": "관망", "confidence": 0.55},
]
```

### 3.4 Phase D — 지지/저항 (이미지 3)

**자체 구현 — 피벗 포인트 cluster**:
1. `find_peaks` 로 high/low 피벗 추출 (최근 N=120일)
2. 가격 cluster (±0.5% 범위 내 그룹화)
3. cluster 의 touch 수 ≥2 → 의미 있는 수평선
4. 현재 가격 대비 위 = 저항, 아래 = 지지

**출력** (최근 가격 ±20% 범위):
```python
[
  {"price": 12000, "type": "지지", "touches": 3, "last_touch": "2026-04-15"},
  {"price": 14500, "type": "저항", "touches": 2, "last_touch": "2026-03-20"},
]
```

### 3.5 Phase E — 확률 경고 (이미지 6)

**위 4 카테고리 통합 후 신뢰도 % 와 함께 매수/매도 경고**.

**이미지 6 의 7 패턴** (왼쪽 → 오른쪽: 매도 강 → 매수 강):

| 패턴 | 신호 | 신뢰도 | 액션 |
|---|---|---|---|
| M형 (이중 천장) | 매도 | 100% | 폭락 대비 |
| 하락 깃발형 | 매도 | 80% | 신속히 매도 |
| 다이아몬드 천장 | 매도 | 65% | 완만 하락 |
| 박스권 정리 | 관망 | 50% | 위험 접근 금지 |
| W바닥 (이중 바닥) | 매수 | 65% | 신속히 매수 |
| 상승 깃발형 | 매수 | 80% | 신속히 매수 |
| 상승 쐐기형 | 매수 | 100% | 폭등 맞이 |

**구현**: Phase C 결과에서 위 7 패턴이 발견되면 신뢰도 + 액션 라벨 추가.

**출력**:
```python
{
  "warning_signal": "매수",
  "confidence": 0.65,
  "label": "W바닥 → 신속히 매수",
  "pattern": "W바닥(이중 바닥)",
}
```

### 3.6 Summary (전체 통합)

```python
{
  "signal": "매수",     # 5 카테고리 가중 다수결
  "score": 4,            # +값=매수쪽, -값=매도쪽 (max ±10)
  "top_patterns": ["골든크로스", "더블바텀", "적삼병"],
  "weights": {"ma": 2, "candle": 1, "chart": 2, "sr": 1, "warn": 1},
}
```

## 4. UI 설계

### 4.1 카드 (대시보드)

기존 카드에 **추가 배지 1줄**:

```
┌─ 삼성전자 (005930.KS) ─────────────────┐
│  [매수: 골든크로스+W바닥]              │
│  Tech: 매수  /  BNF: 관망  /  ML: 매수 │
│  생성 14:00 KST                        │
└────────────────────────────────────────┘
```

배지 텍스트: `summary.signal` + `top_patterns[:2]` join.

### 4.2 분석 페이지 (`/stock/<symbol>`)

기존 분석 결과 아래 **새 섹션 5개** 추가:

```
[기존 Tech 분석]
[기존 BNF 분석]
[기존 ML 예측]

══ 패턴 분석 ══════════════════════════════════════

📈 이동평균 (4상태)
   매수 — 골든크로스 (5일이 50일을 5/8에 돌파)
   장기선 우상향 → 신뢰도 80%
   [SMA5: 12,500] [SMA50: 12,000] [SMA200: 11,500]

🕯  캔들 패턴 (최근 5일)
   ✅ 적삼병 (매수, 폭익)  — 5/8
   ⚪ 도지 (관망)         — 5/9

📊 차트 패턴
   ✅ 더블바텀 (W형) — 매수, 신뢰도 65%
       low1: 4/15  low2: 4/29
   ⚪ 삼각형 수렴   — 관망, 55%

🎯 지지/저항 수평선
   12,000원 — 지지 (3회 반응, 마지막 4/15)
   14,500원 — 저항 (2회 반응, 마지막 3/20)

⚠️  확률 경고
   W바닥 65% → 신속히 매수

══ 종합 ════════════════════════════════════════════

매수 시그널 (score +4)
주요 근거: 골든크로스, 더블바텀, 적삼병
```

각 섹션 아이콘 + 색상 (매수 초록 / 매도 빨강 / 관망 회색).

## 5. 데이터 저장

### 5.1 analysis_cache 스키마 확장

신규 컬럼:
- `pattern_json TEXT` — 위 5 카테고리 + summary 의 full payload (JSON serialized)
- `pattern_signal TEXT` — `summary.signal` (인덱스 가능)
- `pattern_score INTEGER` — `summary.score`

`_MIGRATIONS` registry 활용:
```sql
ALTER TABLE analysis_cache ADD COLUMN pattern_json TEXT;
ALTER TABLE analysis_cache ADD COLUMN pattern_signal TEXT;
ALTER TABLE analysis_cache ADD COLUMN pattern_score INTEGER;
```

PRAGMA table_info 체크 후 idempotent ALTER.

### 5.2 GET /api/signal 응답 확장

```json
{
  "symbol": "005930.KS",
  "tech": {"signal": "매수", "score": 3},
  "bnf": {"signal": "관망", "score": 0},
  "patterns": {
    "summary": {"signal": "매수", "score": 4, "top_patterns": [...]},
    "ma_state": {...},
    "candles": [...],
    "chart_patterns": [...],
    "sr_levels": [...],
    "warning": {...}
  }
}
```

기존 응답 호환 (auto-trader analyzer_gate 영향 0 — `tech` / `bnf` 만 사용).

## 6. 단계 분리 (5 phase, phase 별 commit)

| Phase | 내용 | 예상 라인 | 의존 |
|---|---|---|---|
| A | 이동평균 4상태 + UI (카드+분석페이지) + DB migration + 테스트 | ~250 | — |
| B | 캔들 패턴 (`pandas-ta`) + UI 섹션 + 테스트 | ~250 | A |
| C | 차트 패턴 자체 구현 + UI 섹션 + 테스트 | ~500 | A |
| D | 지지/저항 자체 구현 + UI 섹션 + 테스트 | ~250 | A |
| E | 확률 경고 통합 + summary 가중 다수결 + UI 종합 | ~200 | A~D |

각 phase 별 별도 commit, 한 spec/plan 안.

## 7. 비목표

- 차트 시각화 overlay (canvas / D3) — 텍스트 라벨만
- 백테스트 (이 패턴들로 매매 전략 검증) — 별도 spec
- 실시간 가격 fetch — 일별 close 만 (기존 동일)
- 사용자 커스텀 패턴 — pre-defined 만
- TA-Lib 의존 — pandas-ta pure Python 만

## 8. 운영 고려

- 분석 시간 ↑: 종목당 ~10초 추가 (5 카테고리 × 2초). 현재 30초 → ~40초. 65 종목 분석 ~30분 → ~45분.
- analysis_cache 크기: pattern_json ~5KB/row × 65 종목 = ~325KB. 무시 가능.
- 호환성: 기존 시그널 컬럼 그대로. 새 컬럼만 추가.

## 9. 성공 기준

- 카드에 "매수: 골든크로스+W바닥" 같은 배지 표시
- 분석 페이지 5 섹션 (이동평균 / 캔들 / 차트 / 지지저항 / 경고)
- pattern_json 이 analysis_cache 에 저장됨
- 65 종목 분석 정상 완료 (시간 < 1시간)
- 기존 Tech/BNF/ML 동작 영향 0
