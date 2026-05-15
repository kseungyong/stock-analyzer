# Pattern Indicators 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 차트 분석가가 보는 5 카테고리 (이동평균/캔들/차트/지지저항/경고) 자동 감지 + 카드 배지 + 분석 페이지 5 섹션.

**Architecture:** `src/pattern_indicators.py` 통합 entry + 5 sub-module. analysis_cache 컬럼 확장. UI 카드 배지 + 분석 페이지 섹션.

**Tech Stack:** pandas-ta (캔들), scipy.signal (피크/골), pandas (이동평균), Flask UI

**Spec:** `docs/superpowers/specs/2026-05-10-pattern-indicators-design.md`

---

## Phase A — 이동평균 4상태

**Files:**
- Create: `src/pattern_ma.py` (이동평균 4상태 감지)
- Create: `src/pattern_indicators.py` (entry, Phase A 만 우선)
- Modify: `src/analysis_cache.py` (pattern_json/signal/score 컬럼 + migration)
- Modify: `src/web_app.py` (카드 배지 + 분석 페이지 새 섹션)
- Modify: `main.py` 또는 `analyze_stock` (분석 흐름에 pattern 추가)
- Create: `tests/test_pattern_ma.py`

### Task A1 — `pattern_ma.detect()` 구현

- [ ] **Step A1.1**: 테스트 작성 (`tests/test_pattern_ma.py`)
  ```python
  import pandas as pd
  from src.pattern_ma import detect_ma_state

  def _df_uptrend():
      """우상향 추세 — 골든크로스 + 장기선 상승."""
      import numpy as np
      n = 250
      base = np.linspace(10000, 13000, n)
      noise = np.random.default_rng(42).normal(0, 100, n)
      close = base + noise
      return pd.DataFrame({"close": close, "high": close*1.01, "low": close*0.99, "volume": [100000]*n})

  def test_uptrend_returns_buy():
      df = _df_uptrend()
      result = detect_ma_state(df)
      assert result["signal"] == "매수"
      assert "골든크로스" in result["label"]
      assert result["confidence"] >= 0.5

  def test_downtrend_returns_sell():
      df = _df_uptrend().iloc[::-1].reset_index(drop=True)  # reverse
      df["close"] = df["close"]
      result = detect_ma_state(df)
      assert result["signal"] in ("매도", "팔지마")  # 추세에 따라

  def test_flat_returns_hold():
      n = 250
      df = pd.DataFrame({"close": [10000]*n, "high": [10100]*n, "low": [9900]*n, "volume": [100000]*n})
      result = detect_ma_state(df)
      assert result["signal"] in ("관망", "사지마", "팔지마")
  ```

- [ ] **Step A1.2**: 테스트 FAIL 확인 (모듈 없음)

- [ ] **Step A1.3**: 구현 — `src/pattern_ma.py`
  ```python
  """이동평균 4상태 — 사 / 팔아 / 사지마 / 팔지마.

  Spec: docs/superpowers/specs/2026-05-10-pattern-indicators-design.md §3.1
  """
  from __future__ import annotations
  import pandas as pd

  def detect_ma_state(df: pd.DataFrame) -> dict:
      """OHLCV → 이동평균 4상태.

      반환:
        {"signal": "매수"|"매도"|"사지마"|"팔지마"|"관망",
         "label": str (한국어 설명),
         "confidence": float (0.0 ~ 1.0),
         "ma": {"sma5": ..., "sma50": ..., "sma200": ...}}
      """
      if df is None or len(df) < 200:
          return {"signal": "관망", "label": "데이터 부족 (200일 미만)", "confidence": 0.0, "ma": {}}

      close = df["close"]
      sma5 = close.rolling(5).mean()
      sma50 = close.rolling(50).mean()
      sma200 = close.rolling(200).mean()

      # 최근 값
      last_sma5 = float(sma5.iloc[-1])
      last_sma50 = float(sma50.iloc[-1])
      last_sma200 = float(sma200.iloc[-1])

      # 단기-중기 cross (5 vs 50)
      diff_5_50 = sma5 - sma50
      cross_up = (diff_5_50.iloc[-2] < 0) and (diff_5_50.iloc[-1] >= 0)
      cross_down = (diff_5_50.iloc[-2] > 0) and (diff_5_50.iloc[-1] <= 0)

      # 장기선 (200) 기울기 — 최근 20일 변화율
      slope = (sma200.iloc[-1] - sma200.iloc[-20]) / sma200.iloc[-20]
      uptrend_long = slope > 0.001  # 0.1% 이상 상승
      downtrend_long = slope < -0.001

      # 4 상태 매핑
      if cross_up and uptrend_long:
          return {
              "signal": "매수",
              "label": f"골든크로스 + 장기선 우상향 (200일 +{slope*100:.2f}%)",
              "confidence": min(1.0, 0.5 + abs(slope) * 50),
              "ma": {"sma5": last_sma5, "sma50": last_sma50, "sma200": last_sma200},
          }
      if cross_up and downtrend_long:
          return {
              "signal": "사지마",
              "label": f"골든크로스이지만 장기선 하향 ({slope*100:.2f}%) — false breakout 위험",
              "confidence": 0.4,
              "ma": {"sma5": last_sma5, "sma50": last_sma50, "sma200": last_sma200},
          }
      if cross_down and downtrend_long:
          return {
              "signal": "매도",
              "label": f"데드크로스 + 장기선 하향 ({slope*100:.2f}%)",
              "confidence": min(1.0, 0.5 + abs(slope) * 50),
              "ma": {"sma5": last_sma5, "sma50": last_sma50, "sma200": last_sma200},
          }
      if cross_down and uptrend_long:
          return {
              "signal": "팔지마",
              "label": f"데드크로스이지만 장기선 우상향 ({slope*100:.2f}%) — 단기 조정 가능",
              "confidence": 0.4,
              "ma": {"sma5": last_sma5, "sma50": last_sma50, "sma200": last_sma200},
          }

      # cross 없으면 — 추세에 따라 매수/매도 약 또는 관망
      if uptrend_long and last_sma5 > last_sma50:
          return {"signal": "매수", "label": f"단기 > 중기, 장기 우상향", "confidence": 0.55,
                  "ma": {"sma5": last_sma5, "sma50": last_sma50, "sma200": last_sma200}}
      if downtrend_long and last_sma5 < last_sma50:
          return {"signal": "매도", "label": f"단기 < 중기, 장기 하향", "confidence": 0.55,
                  "ma": {"sma5": last_sma5, "sma50": last_sma50, "sma200": last_sma200}}
      return {
          "signal": "관망",
          "label": "추세 미확정 (이동평균선 혼재)",
          "confidence": 0.3,
          "ma": {"sma5": last_sma5, "sma50": last_sma50, "sma200": last_sma200},
      }
  ```

- [ ] **Step A1.4**: pytest PASS

- [ ] **Step A1.5**: 커밋

### Task A2 — DB schema migration

- [ ] **Step A2.1**: `src/analysis_cache.py` 의 schema SQL 에 컬럼 추가
  ```sql
  CREATE TABLE IF NOT EXISTS analysis_cache (
      ...,  -- 기존
      pattern_json TEXT,
      pattern_signal TEXT,
      pattern_score INTEGER
  );
  ```
  + idempotent migration: `init_db` 안에서 PRAGMA table_info 체크 후 ALTER:
  ```python
  cols = {r[1] for r in conn.execute("PRAGMA table_info(analysis_cache)")}
  if "pattern_json" not in cols:
      conn.execute("ALTER TABLE analysis_cache ADD COLUMN pattern_json TEXT")
      conn.execute("ALTER TABLE analysis_cache ADD COLUMN pattern_signal TEXT")
      conn.execute("ALTER TABLE analysis_cache ADD COLUMN pattern_score INTEGER")
  ```

- [ ] **Step A2.2**: `analysis_cache.put()` 시그니처에 `pattern_json/signal/score` kwarg 추가 (선택, default None) + INSERT/UPDATE 에 포함

- [ ] **Step A2.3**: 회귀 — `tests/test_analysis_cache.py` 통과 (기존 + 새 컬럼 nullable)

- [ ] **Step A2.4**: 커밋

### Task A3 — 분석 흐름 통합

- [ ] **Step A3.1**: `src/pattern_indicators.py` 신규
  ```python
  """5 카테고리 통합 entry. Phase 별 진행."""
  from src.pattern_ma import detect_ma_state

  def detect_all_patterns(df, market: str) -> dict:
      """Phase A: ma_state 만. Phase B/C/D/E 는 별도 commit."""
      ma = detect_ma_state(df)
      summary = {
          "signal": ma["signal"],
          "score": _ma_to_score(ma),
          "top_patterns": [ma["label"].split(" ")[0]] if ma.get("label") else [],
      }
      return {"ma_state": ma, "summary": summary}

  def _ma_to_score(ma: dict) -> int:
      sig = ma.get("signal", "관망")
      conf = ma.get("confidence", 0.0)
      base = {"매수": 2, "사지마": 0, "매도": -2, "팔지마": 0, "관망": 0}.get(sig, 0)
      return int(base * conf * 2)  # max ±4 for Phase A
  ```

- [ ] **Step A3.2**: `main.py` 의 `analyze_stock` 또는 `report_generator` 가 `detect_all_patterns` 호출 + cache 에 저장
  ```python
  patterns = detect_all_patterns(df, market)
  analysis_cache.put(
      ..., 
      pattern_json=json.dumps(patterns, ensure_ascii=False),
      pattern_signal=patterns["summary"]["signal"],
      pattern_score=patterns["summary"]["score"],
  )
  ```

- [ ] **Step A3.3**: 회귀

- [ ] **Step A3.4**: 커밋

### Task A4 — UI 카드 배지

- [ ] **Step A4.1**: `web_app.py` 의 카드 빌드 부분에 pattern 배지 추가
  ```python
  pattern_badge_html = ""
  if cache_row and cache_row.get("pattern_signal"):
      pj = json.loads(cache_row.get("pattern_json") or "{}")
      tops = pj.get("summary", {}).get("top_patterns", [])
      sig = cache_row["pattern_signal"]
      color = {"매수": "#16A34A", "매도": "#DC2626"}.get(sig, "#64748B")
      tops_text = "+".join(tops[:2]) if tops else "패턴 없음"
      pattern_badge_html = (
          f'<div style="font-size:0.75rem;color:{color};margin-top:0.25rem;">'
          f'📈 {sig}: {tops_text}</div>'
      )
  ```
  + 카드 HTML 안에 삽입

- [ ] **Step A4.2**: 분석 페이지 (`/stock/<symbol>`) 에 새 섹션 — Phase A 는 이동평균 만
  ```python
  ma = pattern_payload.get("ma_state", {})
  pattern_section = f"""
  <h2>📈 패턴 분석</h2>
  <h3>이동평균 (4상태)</h3>
  <p><strong>{ma.get("signal")}</strong> — {ma.get("label")}</p>
  <p>신뢰도: {ma.get("confidence", 0)*100:.0f}%</p>
  <p>SMA5: {ma.get("ma",{}).get("sma5", "?")} / SMA50: {sma50} / SMA200: {sma200}</p>
  """
  ```

- [ ] **Step A4.3**: 회귀 + UI smoke (curl 또는 dashboard)

- [ ] **Step A4.4**: 커밋

### Phase A 완료 검증

- [ ] **Step A5.1**: 회귀 — 116 + 신규 (~5) PASS
- [ ] **Step A5.2**: push + 서버 갱신
- [ ] **Step A5.3**: 분석 진행 중 종목 1-2개 결과 확인 (카드 배지 + 분석 페이지)
- [ ] **Step A5.4**: 사용자 검토 → Phase B 진행 여부 결정

---

## Phase B — 캔들 패턴 (pandas-ta)

(Phase A 완료 후 진행)

### Task B1 — pandas-ta 추가 + 매핑 dict

- [ ] requirements.txt 에 `pandas-ta>=0.3` 추가
- [ ] `src/pattern_candle.py` 신규
  ```python
  import pandas as pd
  import pandas_ta as ta

  _NAME_MAP = {
      "CDL_DOJI": ("도지", "관망", "소폭"),
      "CDL_HAMMER": ("망치", "매수", "소폭"),
      "CDL_3WHITESOLDIERS": ("적삼병", "매수", "폭익"),
      "CDL_3BLACKCROWS": ("흑삼병", "매도", "폭익"),
      # ... ~60 매핑 (이미지 5, 7 의 한국어 이름)
  }

  def detect_candles(df: pd.DataFrame, days: int = 5) -> list[dict]:
      """최근 N일 의 캔들 패턴 list 반환."""
      patterns = ta.cdl_pattern(df["open"], df["high"], df["low"], df["close"], name="all")
      results = []
      for col in patterns.columns:
          recent = patterns[col].tail(days)
          for date, val in recent.items():
              if val == 0:
                  continue
              name, signal, magnitude = _NAME_MAP.get(col, (col, "관망", "소폭"))
              if val < 0:
                  signal = "매도" if signal == "매수" else signal
              results.append({"name": name, "signal": signal, "magnitude": magnitude, "date": str(date)})
      return results
  ```
- [ ] 테스트 작성 + 구현 + 회귀 + 커밋

### Task B2 — UI 섹션

- [ ] 카드 배지 — `top_patterns` 에 캔들 포함
- [ ] 분석 페이지에 "🕯 캔들 패턴" 섹션 (최근 5일 list)

---

## Phase C — 차트 패턴 (자체 구현)

(Phase B 완료 후 진행. 가장 큰 작업.)

### 핵심 알고리즘
- `scipy.signal.find_peaks` 로 high/low 피벗 추출
- 패턴 매칭 (더블바텀, 헤드앤숄더 등 ~15 패턴)
- 각 패턴 → 점수 + 신뢰도

### Tasks (요약)
- pattern_chart.py 신규 (~500 라인)
- 각 패턴 별 detector 함수 (`detect_double_bottom`, `detect_head_shoulders`, etc.)
- 통합 `detect_chart_patterns(df) -> list[dict]`
- 테스트 — 합성 데이터로 각 패턴 시뮬레이션

---

## Phase D — 지지/저항 (자체 구현)

### 알고리즘
- 피벗 포인트 (find_peaks high/low)
- 가격 cluster (±0.5%)
- touch 수 ≥2 → 의미 있는 line
- 현재가 비교 → 지지/저항 분류

### Tasks
- pattern_sr.py 신규 (~250 라인)
- `detect_support_resistance(df) -> list[dict]`
- 테스트 + 통합

---

## Phase E — 확률 경고 + 종합 summary

### 알고리즘
- Phase C 의 차트 패턴 결과에서 이미지 6 의 7 패턴 (M형, 하락 깃발 등) 추출
- 신뢰도 % + 액션 라벨 ("폭락 대비", "신속히 매수" 등)
- summary 가중 다수결 (ma:2, candle:1, chart:2, sr:1, warn:1)

### Tasks
- pattern_warn.py 신규 (~150 라인)
- 통합 `pattern_indicators.detect_all_patterns()` 의 summary 보강
- 분석 페이지 ⚠️ 섹션 + 종합 결과

---

## Self-Review

### Spec coverage

| Spec § | Plan task |
|---|---|
| §3.1 ma_state | Phase A (Task A1) |
| §3.2 candles | Phase B (Task B1) |
| §3.3 chart | Phase C |
| §3.4 sr | Phase D |
| §3.5 warning | Phase E |
| §3.6 summary | Phase E |
| §4.1 카드 배지 | Phase A (Task A4) |
| §4.2 분석 페이지 | Phase A~E (각 phase 별 섹션 추가) |
| §5 DB | Phase A (Task A2 — migration) |

### 진행 단위

각 Phase 별 commit. Phase A 가 가장 빠른 가치 (즉각 카드/페이지 UI). 사용자 검토 후 Phase B 진행 여부 결정.
