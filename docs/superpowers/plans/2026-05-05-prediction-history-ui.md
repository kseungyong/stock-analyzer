# 예측 히스토리 UI 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 종목별 결과 페이지 (`/stock/<symbol>`) 안에 "예측 정확도" 섹션 추가 — 모델 5개 hit rate 요약 카드 + 모델 설명 탭바 (CSS-only) + 최근 90일 시간순 예측 히스토리 표 (`<details>`).

**Architecture:** `prediction_history.list_history(symbol, days)` 신규 헬퍼가 `predictions` 테이블에서 90일치 row 를 SELECT 후 `target_date` 기준으로 5 모델을 한 dict 으로 pivot. `web_app.py` 의 `_render_prediction_history` 가 기존 `hit_rate_by_model` + 신규 `list_history` 를 호출하고, 헬퍼 5개 (`_render_hit_rate_summary`, `_hit_rate_card`, `_render_model_tabs`, `_render_history_table`, `_pred_cell`) 와 `_MODEL_INFO` 정적 dict 로 HTML 을 조립. `stock_view` 라우트가 이 결과를 try/except 격리해 분석 본문 뒤에 append.

**Tech Stack:** Python 3.10+, Flask, SQLite (stdlib), pytest, CSS3 (radio+label `:checked` hack — JS 없음)

**Spec:** `docs/superpowers/specs/2026-05-05-prediction-history-ui-design.md`

---

## 파일 구조

| 파일 | 변경 |
|---|---|
| `src/prediction_history.py` | 수정 — `list_history(symbol, days=90)` 함수 append, `defaultdict` import 추가 |
| `src/web_app.py` | 수정 — 신규 헬퍼 5개 (`_render_prediction_history`, `_render_hit_rate_summary`, `_hit_rate_card`, `_render_model_tabs`, `_render_history_table`, `_pred_cell`) + `_MODEL_INFO` 상수 + CSS append + `stock_view` 가 helper 호출 |
| `tests/test_prediction_history.py` | 보강 — `TestListHistory` 클래스 (6 케이스) |
| `tests/test_web_app.py` | 보강 — `TestPredictionHistorySection` 클래스 (7 케이스) |

DB 스키마 변경 없음.

---

## Phase 1 — `prediction_history.list_history`

### Task 1: `list_history` SQL + pivot 로직

**Files:**
- Modify: `src/prediction_history.py` (append 함수)
- Modify: `tests/test_prediction_history.py` (append 클래스)

- [ ] **Step 1.1: 테스트 클래스 작성 (TDD — 6 케이스)**

`tests/test_prediction_history.py` 끝에 추가:

```python
class TestListHistory:
    def _insert_row(self, db_path, *, symbol, ts, target_date, model,
                    direction="상승", confidence=0.7, base_close=100.0,
                    actual_close=None, hit=None, source="live", backtest_id=None):
        """Helper: 직접 SQL insert (insert_live 의 모델 mapping 우회)."""
        import sqlite3
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """INSERT INTO predictions
                   (symbol, ts, target_date, model, direction, confidence,
                    actual_close, base_close, hit, source, backtest_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (symbol, ts, target_date, model, direction, confidence,
                 actual_close, base_close, hit, source, backtest_id),
            )

    def test_empty_db_returns_empty_list(self, tmp_db):
        ph.init_db()
        assert ph.list_history("AAPL", days=90) == []

    def test_pivots_5_models_into_one_row(self, tmp_db):
        ph.init_db()
        td = 1730000000  # 임의 unix epoch
        for model in ("rf", "lgbm", "lstm", "transformer", "ensemble"):
            self._insert_row(tmp_db, symbol="AAPL", ts=1729900000,
                             target_date=td, model=model,
                             direction="상승", confidence=0.7,
                             base_close=100.0, actual_close=105.0, hit=1)
        rows = ph.list_history("AAPL", days=90)
        assert len(rows) == 1
        assert rows[0]["target_date"] == td
        assert rows[0]["base_close"] == 100.0
        assert rows[0]["actual_close"] == 105.0
        assert rows[0]["ensemble_hit"] == 1
        assert set(rows[0]["models"].keys()) == {
            "rf", "lgbm", "lstm", "transformer", "ensemble",
        }
        for m_dict in rows[0]["models"].values():
            assert m_dict["direction"] == "상승"
            assert m_dict["confidence"] == 0.7
            assert m_dict["hit"] == 1

    def test_evaluated_and_pending_rows_mixed(self, tmp_db):
        ph.init_db()
        # 평가된 row (어제)
        self._insert_row(tmp_db, symbol="AAPL", ts=1729900000,
                         target_date=1730000000, model="ensemble",
                         actual_close=105.0, hit=1)
        # 평가 대기 row (오늘 — actual_close NULL)
        self._insert_row(tmp_db, symbol="AAPL", ts=1730000000,
                         target_date=1730086400, model="ensemble",
                         actual_close=None, hit=None)
        rows = ph.list_history("AAPL", days=90)
        assert len(rows) == 2
        # 시간순 내림차순 — 최신이 먼저
        assert rows[0]["target_date"] == 1730086400
        assert rows[0]["actual_close"] is None
        assert rows[0]["ensemble_hit"] is None
        assert rows[1]["actual_close"] == 105.0
        assert rows[1]["ensemble_hit"] == 1

    def test_90_day_cutoff(self, tmp_db, monkeypatch):
        import time
        ph.init_db()
        now = 1730000000
        monkeypatch.setattr(time, "time", lambda: now)
        old_td = now - 91 * 86400  # 91일 전 — 제외
        new_td = now - 89 * 86400  # 89일 전 — 포함
        self._insert_row(tmp_db, symbol="AAPL", ts=now, target_date=old_td,
                         model="ensemble", hit=1, actual_close=105.0)
        self._insert_row(tmp_db, symbol="AAPL", ts=now, target_date=new_td,
                         model="ensemble", hit=0, actual_close=95.0)
        rows = ph.list_history("AAPL", days=90)
        assert len(rows) == 1
        assert rows[0]["target_date"] == new_td

    def test_excludes_backtest_source(self, tmp_db):
        ph.init_db()
        self._insert_row(tmp_db, symbol="AAPL", ts=1729900000,
                         target_date=1730000000, model="ensemble",
                         source="live", hit=1, actual_close=105.0)
        self._insert_row(tmp_db, symbol="AAPL", ts=1729900000,
                         target_date=1730000000, model="ensemble",
                         source="backtest", backtest_id="bt1",
                         hit=0, actual_close=95.0)
        rows = ph.list_history("AAPL", days=90)
        assert len(rows) == 1
        # live row 의 hit=1 가 사용됨 (backtest 격리)
        assert rows[0]["ensemble_hit"] == 1

    def test_isolates_other_symbols(self, tmp_db):
        ph.init_db()
        self._insert_row(tmp_db, symbol="AAPL", ts=1729900000,
                         target_date=1730000000, model="ensemble",
                         hit=1, actual_close=105.0)
        self._insert_row(tmp_db, symbol="TSLA", ts=1729900000,
                         target_date=1730000000, model="ensemble",
                         hit=0, actual_close=200.0)
        rows = ph.list_history("AAPL", days=90)
        assert len(rows) == 1
        assert rows[0]["actual_close"] == 105.0
```

- [ ] **Step 1.2: 테스트 실행 — FAIL 확인**

Run from `/Users/sykim/Projects/stock-analyzer`:
```bash
.venv/bin/python -m pytest tests/test_prediction_history.py::TestListHistory -v
```
Expected: 6 tests fail with `AttributeError: module 'src.prediction_history' has no attribute 'list_history'`

- [ ] **Step 1.3: `defaultdict` import 추가**

`src/prediction_history.py` 상단의 stdlib import 블록에 추가:

```python
from collections import defaultdict
```

기존 import 위치 (1~13행 근처):
```python
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
```
이 영역에 `from collections import defaultdict` 가 없으면 추가. `from collections.abc import Callable` 위 줄에 삽입.

- [ ] **Step 1.4: `list_history` 함수 구현 — append at file end**

`src/prediction_history.py` 끝에 추가:

```python
def list_history(symbol: str, days: int = 90) -> list[dict]:
    """종목의 최근 N일 예측 히스토리 (target_date 내림차순).

    같은 (symbol, target_date) 의 5 모델 row 를 한 dict 에 묶는다.
    ensemble row 가 있으면 base_close/actual_close/ts 의 대표값으로 사용,
    없으면 첫 모델 row 사용.

    Args:
        symbol: 종목 심볼.
        days: cutoff 일수 — target_date >= now - days*86400 만 포함.

    Returns:
        [
          {
            "target_date":   int,            # KST 자정 unix epoch
            "ts":            int,            # 분석 실행 시각 (대표 모델)
            "base_close":    float,
            "actual_close":  float | None,
            "ensemble_hit":  int | None,     # 0/1 또는 None (평가 대기)
            "models": {
                "rf":          {"direction": str, "confidence": float, "hit": int|None},
                ...
            },
          },
          ...
        ]
    """
    cutoff = int(time.time()) - days * 86400
    groups: dict[int, dict[str, dict]] = defaultdict(dict)
    with closing(_connect()) as conn:
        cur = conn.execute(
            """SELECT ts, target_date, model, direction, confidence,
                      base_close, actual_close, hit
               FROM predictions
               WHERE symbol = ?
                 AND source = 'live'
                 AND target_date >= ?
               ORDER BY target_date DESC, model""",
            (symbol, cutoff),
        )
        for ts, td, model, direction, confidence, base_close, actual_close, hit in cur:
            groups[td][model] = {
                "ts": ts,
                "direction": direction,
                "confidence": confidence,
                "base_close": base_close,
                "actual_close": actual_close,
                "hit": hit,
            }

    result = []
    for td in sorted(groups.keys(), reverse=True):
        models = groups[td]
        repr_row = models.get("ensemble") or next(iter(models.values()))
        result.append({
            "target_date": td,
            "ts": repr_row["ts"],
            "base_close": repr_row["base_close"],
            "actual_close": repr_row["actual_close"],
            "ensemble_hit": models.get("ensemble", {}).get("hit"),
            "models": {
                m: {
                    "direction": v["direction"],
                    "confidence": v["confidence"],
                    "hit": v["hit"],
                }
                for m, v in models.items()
            },
        })
    return result
```

- [ ] **Step 1.5: 테스트 실행 — PASS 확인**

```bash
.venv/bin/python -m pytest tests/test_prediction_history.py::TestListHistory -v
```
Expected: 6 passed

- [ ] **Step 1.6: 회귀 테스트**

```bash
.venv/bin/python -m pytest tests/test_prediction_history.py -v
```
Expected: 모든 기존 테스트 + 6 신규 PASS

- [ ] **Step 1.7: 커밋**

```bash
git add src/prediction_history.py tests/test_prediction_history.py
git commit -m "feat(prediction_history): list_history — 종목별 90일 예측 히스토리 pivot"
```

---

## Phase 2 — Web 헬퍼 + UI (TDD)

### Task 2: `_MODEL_INFO` + `_render_model_tabs` (CSS-only 탭바)

**Files:**
- Modify: `src/web_app.py`
- Modify: `tests/test_web_app.py`

- [ ] **Step 2.1: 테스트 작성 (`TestModelTabs`)**

`tests/test_web_app.py` 끝에 추가:

```python
class TestModelTabs:
    def test_renders_5_radio_inputs_with_rf_checked(self, client):
        """탭바 — 라디오 5개, RF 가 기본 활성."""
        from src.web_app import _render_model_tabs
        html = _render_model_tabs()
        # 5개 라디오
        for key in ("rf", "lgbm", "lstm", "transformer", "ensemble"):
            assert f'id="mtab-{key}"' in html
            assert f'for="mtab-{key}"' in html
            assert f'mtab-panel-{key}' in html
        # RF 가 checked
        assert 'id="mtab-rf" class="mtab-radio" checked' in html or 'class="mtab-radio" checked' in html.split('id="mtab-rf"')[1].split(">")[0] or "checked" in html.split('mtab-rf')[1].split("<input")[0]

    def test_panels_contain_model_descriptions(self, client):
        """각 모델 패널에 한국어 설명 키워드 포함."""
        from src.web_app import _render_model_tabs
        html = _render_model_tabs()
        assert "Random Forest" in html
        assert "그래디언트 부스팅" in html  # LGBM
        assert "Long Short-Term Memory" in html
        assert "어텐션" in html              # Transformer
        assert "앙상블" in html              # Ensemble

    def test_panel_has_strengths_weaknesses(self, client):
        """각 패널에 '강점' 과 '약점' 마크업."""
        from src.web_app import _render_model_tabs
        html = _render_model_tabs()
        # 5 모델 × 2 → 최소 5번씩 등장
        assert html.count("<strong>강점</strong>") >= 5
        assert html.count("<strong>약점</strong>") >= 5
```

- [ ] **Step 2.2: 테스트 실행 — FAIL**

```bash
.venv/bin/python -m pytest tests/test_web_app.py::TestModelTabs -v
```
Expected: 3 fail with `ImportError: cannot import name '_render_model_tabs'`

- [ ] **Step 2.3: `_MODEL_INFO` + `_render_model_tabs` 구현**

`src/web_app.py` 의 헬퍼 영역 (예: `_format_kst` 정의 부근, 약 720행 근처) 에 append:

```python
# ── 모델 설명 (정적) ──────────────────────────────────────────────────────
_MODEL_INFO = {
    "rf": {
        "name": "RF (Random Forest)",
        "desc": (
            "여러 결정 트리를 무작위 샘플링으로 학습 시키고 다수결로 결정한다. "
            "비선형 패턴 포착에 강하고 과적합 저항성이 높음. "
            "<strong>강점</strong>: 안정적이고 해석 가능. "
            "<strong>약점</strong>: 시간 의존성을 직접 모델링하지 않음."
        ),
    },
    "lgbm": {
        "name": "LGBM (LightGBM)",
        "desc": (
            "그래디언트 부스팅 트리. 약한 학습기를 순차적으로 쌓아 잔차를 줄인다. "
            "leaf-wise 성장으로 학습 빠르고 메모리 효율적. "
            "<strong>강점</strong>: 정확도 높고 학습 빠름. "
            "<strong>약점</strong>: 작은 데이터에 과적합 가능."
        ),
    },
    "lstm": {
        "name": "LSTM (Long Short-Term Memory)",
        "desc": (
            "순환 신경망 변형. 게이트 구조로 시계열의 장기 의존성을 학습. "
            "긴 추세 포착에 강함. "
            "<strong>강점</strong>: 시계열 패턴 모델링. "
            "<strong>약점</strong>: 학습 느리고 데이터를 많이 요구함."
        ),
    },
    "transformer": {
        "name": "Transformer",
        "desc": (
            "어텐션 메커니즘 기반. 시계열 임의 위치 간 관계를 동시에 가중. "
            "최근 NLP·시계열에서 SOTA. "
            "<strong>강점</strong>: 긴/복잡한 패턴. "
            "<strong>약점</strong>: 작은 데이터에서 과적합 위험, 연산량 큼."
        ),
    },
    "ensemble": {
        "name": "Ensemble (앙상블)",
        "desc": (
            "위 4개 모델 (RF, LGBM, LSTM, Transformer) 의 예측을 가중 평균/투표로 결합. "
            "단일 모델의 약점을 상쇄해 안정성을 높인다. "
            "<strong>강점</strong>: 평균적으로 가장 신뢰할 만한 신호. "
            "<strong>약점</strong>: 개별 모델보다 해석이 어려움."
        ),
    },
}


def _render_model_tabs() -> str:
    """CSS-only 모델 설명 탭바 (radio + label + :checked 셀렉터)."""
    radios = []
    labels = []
    panels = []
    for i, key in enumerate(("rf", "lgbm", "lstm", "transformer", "ensemble")):
        info = _MODEL_INFO[key]
        checked = " checked" if i == 0 else ""
        short = info["name"].split(" (")[0]
        radios.append(
            f'<input type="radio" name="model-tab" id="mtab-{key}" class="mtab-radio"{checked}>'
        )
        labels.append(
            f'<label for="mtab-{key}" class="mtab-label mtab-label-{key}">{short}</label>'
        )
        panels.append(
            f'<section class="mtab-panel mtab-panel-{key}">'
            f'<h3>{info["name"]}</h3><p>{info["desc"]}</p>'
            f'</section>'
        )
    return (
        f'<div class="model-tabs">'
        f'{"".join(radios)}'
        f'<div class="mtab-list">{"".join(labels)}</div>'
        f'<div class="mtab-panels">{"".join(panels)}</div>'
        f'</div>'
    )
```

- [ ] **Step 2.4: 테스트 실행 — PASS**

```bash
.venv/bin/python -m pytest tests/test_web_app.py::TestModelTabs -v
```
Expected: 3 passed

- [ ] **Step 2.5: 커밋**

```bash
git add src/web_app.py tests/test_web_app.py
git commit -m "feat(web): _MODEL_INFO 정적 dict + _render_model_tabs (CSS-only 탭바)"
```

---

### Task 3: `_render_hit_rate_summary` + `_hit_rate_card`

**Files:**
- Modify: `src/web_app.py`
- Modify: `tests/test_web_app.py`

- [ ] **Step 3.1: 테스트 작성**

`tests/test_web_app.py` 끝에 추가:

```python
class TestHitRateSummary:
    def test_empty_rates_shows_pending_alert(self, client):
        from src.web_app import _render_hit_rate_summary
        html = _render_hit_rate_summary({})
        assert "평가된 예측이 아직 없습니다" in html

    def test_renders_5_cards_with_pct(self, client):
        from src.web_app import _render_hit_rate_summary
        rates = {
            "rf":          {"hit_rate": 0.72, "n": 50},
            "lgbm":        {"hit_rate": 0.65, "n": 50},
            "lstm":        {"hit_rate": 0.45, "n": 50},
            "transformer": {"hit_rate": 0.55, "n": 50},
            "ensemble":    {"hit_rate": 0.68, "n": 50},
        }
        html = _render_hit_rate_summary(rates)
        for label in ("RF", "LGBM", "LSTM", "Transformer", "Ensemble"):
            assert label in html
        assert "72.0%" in html
        assert "45.0%" in html
        assert "50회 평가" in html
        # 색상 클래스 — green 60%+, amber 50%+, red <50
        assert "var(--green-600)" in html  # rf=72, ensemble=68
        assert "var(--amber-500)" in html  # transformer=55
        assert "var(--red-600)" in html    # lstm=45

    def test_missing_model_shows_empty_card(self, client):
        from src.web_app import _render_hit_rate_summary
        rates = {"rf": {"hit_rate": 0.7, "n": 10}}
        html = _render_hit_rate_summary(rates)
        # 다른 4 모델은 "평가 없음"
        assert html.count('hit-rate-card empty') == 4
        assert "평가 없음" in html
```

- [ ] **Step 3.2: 테스트 실행 — FAIL**

```bash
.venv/bin/python -m pytest tests/test_web_app.py::TestHitRateSummary -v
```

- [ ] **Step 3.3: 구현**

`src/web_app.py` 에 `_render_model_tabs` 다음에 append:

```python
def _hit_rate_card(name: str, pct: float | None, n: int) -> str:
    if pct is None:
        return (
            f'<div class="hit-rate-card empty">'
            f'<div class="name">{name}</div>'
            f'<div class="value">—</div>'
            f'<div class="n">평가 없음</div></div>'
        )
    color = "var(--green-600)" if pct >= 60 else ("var(--amber-500)" if pct >= 50 else "var(--red-600)")
    return (
        f'<div class="hit-rate-card">'
        f'<div class="name">{name}</div>'
        f'<div class="value" style="color:{color};">{pct:.1f}%</div>'
        f'<div class="n">{n}회 평가</div></div>'
    )


def _render_hit_rate_summary(rates: dict) -> str:
    """모델 5개 hit rate 요약 카드 그리드. 비어있으면 안내 alert."""
    if not rates:
        return '<div class="alert alert-info">평가된 예측이 아직 없습니다.</div>'
    label = {"rf": "RF", "lgbm": "LGBM", "lstm": "LSTM",
             "transformer": "Transformer", "ensemble": "Ensemble"}
    cards = []
    for m in ("rf", "lgbm", "lstm", "transformer", "ensemble"):
        info = rates.get(m)
        if info is None:
            cards.append(_hit_rate_card(label[m], None, 0))
        else:
            cards.append(_hit_rate_card(label[m], info["hit_rate"] * 100, info["n"]))
    return f'<div class="hit-rate-grid">{"".join(cards)}</div>'
```

- [ ] **Step 3.4: 테스트 실행 — PASS**

```bash
.venv/bin/python -m pytest tests/test_web_app.py::TestHitRateSummary -v
```

- [ ] **Step 3.5: 커밋**

```bash
git add src/web_app.py tests/test_web_app.py
git commit -m "feat(web): _render_hit_rate_summary + _hit_rate_card (모델 5 카드)"
```

---

### Task 4: `_render_history_table` + `_pred_cell`

**Files:**
- Modify: `src/web_app.py`
- Modify: `tests/test_web_app.py`

- [ ] **Step 4.1: 테스트 작성**

```python
class TestHistoryTable:
    def _row(self, *, td=1730000000, ensemble_hit=1, actual=105.0,
             models=None):
        if models is None:
            models = {
                m: {"direction": "상승", "confidence": 0.7, "hit": ensemble_hit}
                for m in ("rf", "lgbm", "lstm", "transformer", "ensemble")
            }
        return {
            "target_date": td,
            "ts": td - 86400,
            "base_close": 100.0,
            "actual_close": actual,
            "ensemble_hit": ensemble_hit,
            "models": models,
        }

    def test_renders_thead_with_9_columns(self, client):
        from src.web_app import _render_history_table
        html = _render_history_table([self._row()])
        for col in ("분석일", "기준 종가", "RF", "LGBM", "LSTM",
                    "Transf", "Ensemble", "실제 종가", "판정"):
            assert col in html

    def test_hit_row_shows_green_verdict(self, client):
        from src.web_app import _render_history_table
        html = _render_history_table([self._row(ensemble_hit=1, actual=105.0)])
        assert 'badge-hit' in html
        assert "적중" in html
        assert 'pred-hit' in html  # 모델 셀

    def test_miss_row_shows_red_verdict(self, client):
        from src.web_app import _render_history_table
        miss_models = {
            m: {"direction": "상승", "confidence": 0.7, "hit": 0}
            for m in ("rf", "lgbm", "lstm", "transformer", "ensemble")
        }
        html = _render_history_table([self._row(
            ensemble_hit=0, actual=95.0, models=miss_models,
        )])
        assert 'badge-miss' in html
        assert "빗나감" in html
        assert 'pred-miss' in html

    def test_pending_row_is_grey(self, client):
        from src.web_app import _render_history_table
        pending_models = {
            m: {"direction": "하락", "confidence": 0.6, "hit": None}
            for m in ("rf", "lgbm", "lstm", "transformer", "ensemble")
        }
        html = _render_history_table([self._row(
            ensemble_hit=None, actual=None, models=pending_models,
        )])
        assert 'class="row-pending"' in html
        assert "평가 대기" in html
        assert "—" in html  # actual_close 자리
        assert 'pred-pending' in html

    def test_missing_model_cell_shows_dash(self, client):
        from src.web_app import _render_history_table
        partial = {
            "rf": {"direction": "상승", "confidence": 0.7, "hit": 1},
            # lgbm/lstm/transformer/ensemble 누락
        }
        html = _render_history_table([self._row(
            ensemble_hit=None, actual=105.0, models=partial,
        )])
        # 누락 셀 4개
        assert html.count("<td>—</td>") >= 4

    def test_arrow_direction(self, client):
        from src.web_app import _render_history_table
        up_models = {m: {"direction": "상승", "confidence": 0.7, "hit": 1}
                     for m in ("rf", "lgbm", "lstm", "transformer", "ensemble")}
        down_models = {m: {"direction": "하락", "confidence": 0.6, "hit": 0}
                       for m in ("rf", "lgbm", "lstm", "transformer", "ensemble")}
        html = _render_history_table([
            self._row(td=1730086400, ensemble_hit=1, actual=105.0, models=up_models),
            self._row(td=1730000000, ensemble_hit=0, actual=95.0, models=down_models),
        ])
        assert "🔼" in html  # 상승
        assert "🔽" in html  # 하락
```

- [ ] **Step 4.2: 테스트 실행 — FAIL**

```bash
.venv/bin/python -m pytest tests/test_web_app.py::TestHistoryTable -v
```

- [ ] **Step 4.3: 구현**

`src/web_app.py` 에 `_render_hit_rate_summary` 다음 append:

```python
def _pred_cell(m: dict | None) -> str:
    """시간순 표의 모델 셀 — 방향 + 신뢰도 % + hit/miss/pending 색상."""
    if m is None:
        return '<td>—</td>'
    arrow = "🔼" if m["direction"] == "상승" else "🔽"
    pct = int(m["confidence"] * 100)
    if m.get("hit") is None:
        cls = "pending"
    elif m["hit"] == 1:
        cls = "hit"
    else:
        cls = "miss"
    return f'<td class="pred-cell pred-{cls}">{arrow}{pct}%</td>'


def _render_history_table(rows: list[dict]) -> str:
    """시간순 예측 히스토리 표 — list_history 결과 기반."""
    head = (
        "<thead><tr>"
        "<th>분석일</th><th>기준 종가</th>"
        "<th>RF</th><th>LGBM</th><th>LSTM</th><th>Transf</th><th>Ensemble</th>"
        "<th>실제 종가</th><th>판정</th>"
        "</tr></thead>"
    )
    body_rows = []
    for r in rows:
        date_str = _format_kst(r["target_date"]).split()[0]  # 'YYYY-MM-DD'
        base = f"{r['base_close']:,.0f}"
        cells = "".join(
            _pred_cell(r["models"].get(m))
            for m in ("rf", "lgbm", "lstm", "transformer", "ensemble")
        )
        if r["actual_close"] is None:
            actual = "—"
            verdict = '<span class="badge-pending">평가 대기</span>'
            row_attr = ' class="row-pending"'
        else:
            actual = f"{r['actual_close']:,.0f}"
            verdict = (
                '<span class="badge-hit">✅ 적중</span>'
                if r["ensemble_hit"] == 1
                else '<span class="badge-miss">❌ 빗나감</span>'
            )
            row_attr = ""
        body_rows.append(
            f"<tr{row_attr}><td>{date_str}</td><td class='num'>{base}</td>"
            f"{cells}<td class='num'>{actual}</td><td>{verdict}</td></tr>"
        )
    return (
        f'<table class="history-table">{head}'
        f'<tbody>{"".join(body_rows)}</tbody>'
        f'</table>'
    )
```

- [ ] **Step 4.4: 테스트 실행 — PASS**

```bash
.venv/bin/python -m pytest tests/test_web_app.py::TestHistoryTable -v
```

- [ ] **Step 4.5: 커밋**

```bash
git add src/web_app.py tests/test_web_app.py
git commit -m "feat(web): _render_history_table + _pred_cell (시간순 예측 표)"
```

---

### Task 5: `_render_prediction_history` (조립 + 빈 케이스)

**Files:**
- Modify: `src/web_app.py`
- Modify: `tests/test_web_app.py`

- [ ] **Step 5.1: 테스트 작성**

```python
class TestRenderPredictionHistory:
    def test_empty_when_no_data(self, client, monkeypatch):
        """rates + rows 둘 다 비어있으면 빈 문자열."""
        from src import prediction_history
        from src.web_app import _render_prediction_history
        monkeypatch.setattr(prediction_history, "hit_rate_by_model",
                            lambda *a, **k: {})
        monkeypatch.setattr(prediction_history, "list_history",
                            lambda *a, **k: [])
        assert _render_prediction_history("AAPL") == ""

    def test_shows_summary_only_when_no_rows(self, client, monkeypatch):
        """rates 만 있고 rows 비어있으면 섹션 표시 + details 안내."""
        from src import prediction_history
        from src.web_app import _render_prediction_history
        monkeypatch.setattr(prediction_history, "hit_rate_by_model",
                            lambda *a, **k: {"rf": {"hit_rate": 0.7, "n": 10}})
        monkeypatch.setattr(prediction_history, "list_history",
                            lambda *a, **k: [])
        html = _render_prediction_history("AAPL")
        assert "예측 정확도" in html
        assert "70.0%" in html
        assert "아직 평가된 예측 이력이 없습니다" in html

    def test_full_rendering_with_rates_and_rows(self, client, monkeypatch):
        """rates + rows 둘 다 있으면 헤더 + summary + tabs + details 모두."""
        from src import prediction_history
        from src.web_app import _render_prediction_history
        monkeypatch.setattr(prediction_history, "hit_rate_by_model",
                            lambda *a, **k: {"ensemble": {"hit_rate": 0.65, "n": 20}})
        monkeypatch.setattr(prediction_history, "list_history",
                            lambda *a, **k: [{
                                "target_date": 1730000000, "ts": 1729900000,
                                "base_close": 100.0, "actual_close": 105.0,
                                "ensemble_hit": 1,
                                "models": {
                                    m: {"direction": "상승", "confidence": 0.7, "hit": 1}
                                    for m in ("rf", "lgbm", "lstm", "transformer", "ensemble")
                                },
                            }])
        html = _render_prediction_history("AAPL")
        assert "<h2>예측 정확도</h2>" in html
        # 요약 카드
        assert "65.0%" in html
        # 탭바
        assert 'id="mtab-rf"' in html
        # 표 안 — details 펼친 안내
        assert "최근 90일 예측 히스토리" in html
        assert "1회" in html or "(1회)" in html
        # 시간순 row
        assert "✅ 적중" in html

    def test_section_order(self, client, monkeypatch):
        """헤더 → summary → tabs → details 순서."""
        from src import prediction_history
        from src.web_app import _render_prediction_history
        monkeypatch.setattr(prediction_history, "hit_rate_by_model",
                            lambda *a, **k: {"ensemble": {"hit_rate": 0.65, "n": 20}})
        monkeypatch.setattr(prediction_history, "list_history",
                            lambda *a, **k: [{
                                "target_date": 1730000000, "ts": 1729900000,
                                "base_close": 100.0, "actual_close": 105.0,
                                "ensemble_hit": 1,
                                "models": {"ensemble": {"direction": "상승", "confidence": 0.7, "hit": 1}},
                            }])
        html = _render_prediction_history("AAPL")
        i_header = html.index("예측 정확도")
        i_summary = html.index("hit-rate-grid")
        i_tabs = html.index("model-tabs")
        i_details = html.index("history-details")
        assert i_header < i_summary < i_tabs < i_details
```

- [ ] **Step 5.2: 테스트 실행 — FAIL**

- [ ] **Step 5.3: 구현**

`src/web_app.py` 에 `_render_history_table` 다음 append:

```python
def _render_prediction_history(symbol: str) -> str:
    """예측 정확도 섹션 — 헤더 + 요약 카드 + 모델 탭바 + 시간순 표 (<details>)."""
    rates = prediction_history.hit_rate_by_model(symbol, source="live")
    rows = prediction_history.list_history(symbol, days=90)
    if not rates and not rows:
        return ""

    summary = _render_hit_rate_summary(rates)
    tabs = _render_model_tabs()

    if rows:
        details_inner = _render_history_table(rows)
        details_summary_text = f"최근 90일 예측 히스토리 ({len(rows)}회) — 클릭하여 펼치기"
    else:
        details_inner = "<p>아직 평가된 예측 이력이 없습니다.</p>"
        details_summary_text = "최근 90일 예측 히스토리"

    details = (
        f'<details class="history-details">'
        f'<summary>{details_summary_text}</summary>'
        f'<div style="overflow-x:auto;">{details_inner}</div>'
        f'</details>'
    )
    header = '<div class="page-header" style="margin-top:32px;"><h2>예측 정확도</h2></div>'
    return header + summary + tabs + details
```

- [ ] **Step 5.4: 테스트 실행 — PASS**

```bash
.venv/bin/python -m pytest tests/test_web_app.py::TestRenderPredictionHistory -v
```

- [ ] **Step 5.5: 커밋**

```bash
git add src/web_app.py tests/test_web_app.py
git commit -m "feat(web): _render_prediction_history — 섹션 조립 + 빈 케이스 처리"
```

---

### Task 6: CSS — `_CSS` 끝에 추가

**Files:**
- Modify: `src/web_app.py:_CSS` 상수

- [ ] **Step 6.1: CSS append**

`src/web_app.py` 의 `_CSS = """ ... """` 상수 끝부분에 (closing `"""` 직전) 다음 CSS 를 추가:

```css
/* ── 예측 정확도 섹션 ─────────────────────────────────────────────────── */
.hit-rate-grid {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 10px;
  margin-bottom: 16px;
}
.hit-rate-card {
  background: var(--white); border: 1px solid var(--slate-200);
  border-radius: var(--radius); padding: 14px; text-align: center;
}
.hit-rate-card.empty { opacity: 0.55; }
.hit-rate-card .name  { font-size: 0.78rem; color: var(--slate-500); font-weight: 600; }
.hit-rate-card .value { font-size: 1.6rem; font-weight: 700; margin: 4px 0; }
.hit-rate-card .n     { font-size: 0.72rem; color: var(--slate-500); }

.history-details { margin-top: 12px; }
.history-details summary {
  font-weight: 600; color: var(--blue-800); padding: 6px 0;
  cursor: pointer; list-style: revert;
}
.history-details[open] summary { margin-bottom: 12px; }

.history-table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
.history-table th {
  background: var(--slate-50); padding: 8px 10px; font-size: 0.72rem;
  text-transform: uppercase; color: var(--slate-500); text-align: center;
  white-space: nowrap;
}
.history-table td { padding: 8px 10px; border-bottom: 1px solid var(--slate-100); text-align: center; }
.history-table td.num { font-family: 'Fira Code', monospace; text-align: right; }
.history-table tbody tr:hover td { background: var(--slate-50); }
.history-table tr.row-pending td { color: var(--slate-500); background: var(--slate-50); }

.pred-cell { font-family: 'Fira Code', monospace; font-size: 0.78rem; padding: 4px 8px; }
.pred-cell.pred-hit  { background: var(--green-100); color: var(--green-600); }
.pred-cell.pred-miss { background: var(--red-100);   color: var(--red-600); }
.pred-cell.pred-pending { color: var(--slate-500); }

.badge-hit, .badge-miss, .badge-pending {
  display: inline-block; padding: 3px 9px; border-radius: 20px;
  font-size: 0.78rem; font-weight: 600;
}
.badge-hit { background: var(--green-100); color: var(--green-600); }
.badge-miss { background: var(--red-100); color: var(--red-600); }
.badge-pending { background: var(--slate-100); color: var(--slate-500); }

/* ── CSS-only 모델 설명 탭바 ─────────────────────────────────────────── */
.model-tabs { margin: 16px 0 12px; }
.mtab-radio { position: absolute; opacity: 0; pointer-events: none; }
.mtab-list {
  display: flex; gap: 2px; border-bottom: 2px solid var(--slate-200);
  flex-wrap: wrap;
}
.mtab-label {
  padding: 8px 16px; cursor: pointer; font-weight: 600;
  font-size: 0.85rem; color: var(--slate-500);
  border-bottom: 2px solid transparent; margin-bottom: -2px;
  transition: color var(--transition), border-color var(--transition);
  user-select: none;
}
.mtab-label:hover { color: var(--blue-600); }

.mtab-panel { display: none; padding: 16px 4px; line-height: 1.7; color: var(--slate-700); }
.mtab-panel h3 { font-size: 1rem; color: var(--blue-900); margin-bottom: 8px; }
.mtab-panel strong { color: var(--slate-900); }

#mtab-rf:checked          ~ .mtab-list .mtab-label-rf,
#mtab-lgbm:checked        ~ .mtab-list .mtab-label-lgbm,
#mtab-lstm:checked        ~ .mtab-list .mtab-label-lstm,
#mtab-transformer:checked ~ .mtab-list .mtab-label-transformer,
#mtab-ensemble:checked    ~ .mtab-list .mtab-label-ensemble {
  color: var(--blue-800); border-bottom-color: var(--blue-600);
}
#mtab-rf:checked          ~ .mtab-panels .mtab-panel-rf,
#mtab-lgbm:checked        ~ .mtab-panels .mtab-panel-lgbm,
#mtab-lstm:checked        ~ .mtab-panels .mtab-panel-lstm,
#mtab-transformer:checked ~ .mtab-panels .mtab-panel-transformer,
#mtab-ensemble:checked    ~ .mtab-panels .mtab-panel-ensemble { display: block; }

.mtab-radio:focus-visible ~ .mtab-list .mtab-label {
  outline: 2px solid var(--blue-500); outline-offset: 2px;
}
```

`_CSS` 가 multi-line 트리플쿼트 문자열이므로 끝 `"""` 위에 위 CSS 를 그대로 붙여넣음.

- [ ] **Step 6.2: 회귀 테스트 — CSS 변경이 다른 동작 깨뜨리지 않음 확인**

```bash
.venv/bin/python -m pytest tests/test_web_app.py -q
```
Expected: 모든 기존 테스트 PASS

- [ ] **Step 6.3: 커밋**

```bash
git add src/web_app.py
git commit -m "feat(web): 예측 정확도 + 모델 탭바 CSS"
```

---

## Phase 3 — `stock_view` 통합 + 회귀

### Task 7: `stock_view` 가 `_render_prediction_history` 호출

**Files:**
- Modify: `src/web_app.py` (`stock_view` 라우트, 약 1024–1050행)
- Modify: `tests/test_web_app.py`

- [ ] **Step 7.1: 통합 테스트 작성**

`tests/test_web_app.py` 끝에 추가:

```python
class TestPredictionHistorySection:
    def test_section_absent_when_no_history(self, client, monkeypatch):
        """예측 row 0건 → '예측 정확도' 헤더 미표시."""
        from src import analysis_cache as ac
        from src import prediction_history
        ac.init_db()
        ac.put("AAPL", "us", "<p>cached</p>", "auto_cron")
        monkeypatch.setattr(prediction_history, "hit_rate_by_model",
                            lambda *a, **k: {})
        monkeypatch.setattr(prediction_history, "list_history",
                            lambda *a, **k: [])
        resp = client.get("/stock/AAPL")
        assert resp.status_code == 200
        assert "예측 정확도".encode() not in resp.data

    def test_section_present_when_history_exists(self, client, monkeypatch):
        from src import analysis_cache as ac
        from src import prediction_history
        ac.init_db()
        ac.put("AAPL", "us", "<p>cached</p>", "auto_cron")
        monkeypatch.setattr(prediction_history, "hit_rate_by_model",
                            lambda *a, **k: {"ensemble": {"hit_rate": 0.7, "n": 10}})
        monkeypatch.setattr(prediction_history, "list_history",
                            lambda *a, **k: [{
                                "target_date": 1730000000, "ts": 1729900000,
                                "base_close": 100.0, "actual_close": 105.0,
                                "ensemble_hit": 1,
                                "models": {
                                    m: {"direction": "상승", "confidence": 0.7, "hit": 1}
                                    for m in ("rf", "lgbm", "lstm", "transformer", "ensemble")
                                },
                            }])
        resp = client.get("/stock/AAPL")
        assert resp.status_code == 200
        assert "예측 정확도".encode() in resp.data
        assert b'id="mtab-rf"' in resp.data           # 탭바
        assert b'class="hit-rate-grid"' in resp.data  # 요약
        assert b'class="history-table"' in resp.data  # 표

    def test_pending_row_renders_grey(self, client, monkeypatch):
        from src import analysis_cache as ac
        from src import prediction_history
        ac.init_db()
        ac.put("AAPL", "us", "<p>cached</p>", "auto_cron")
        monkeypatch.setattr(prediction_history, "hit_rate_by_model",
                            lambda *a, **k: {})
        monkeypatch.setattr(prediction_history, "list_history",
                            lambda *a, **k: [{
                                "target_date": 1730086400, "ts": 1730000000,
                                "base_close": 100.0, "actual_close": None,
                                "ensemble_hit": None,
                                "models": {
                                    m: {"direction": "하락", "confidence": 0.6, "hit": None}
                                    for m in ("rf", "lgbm", "lstm", "transformer", "ensemble")
                                },
                            }])
        resp = client.get("/stock/AAPL")
        assert b'class="row-pending"' in resp.data
        assert "평가 대기".encode() in resp.data

    def test_details_summary_text(self, client, monkeypatch):
        from src import analysis_cache as ac
        from src import prediction_history
        ac.init_db()
        ac.put("AAPL", "us", "<p>cached</p>", "auto_cron")
        monkeypatch.setattr(prediction_history, "hit_rate_by_model",
                            lambda *a, **k: {"rf": {"hit_rate": 0.5, "n": 5}})
        monkeypatch.setattr(prediction_history, "list_history",
                            lambda *a, **k: [{
                                "target_date": 1730000000, "ts": 1729900000,
                                "base_close": 100.0, "actual_close": 105.0,
                                "ensemble_hit": 1,
                                "models": {"ensemble": {"direction": "상승", "confidence": 0.7, "hit": 1}},
                            }])
        resp = client.get("/stock/AAPL")
        assert b'<details' in resp.data
        assert "최근 90일 예측 히스토리".encode() in resp.data

    def test_history_error_does_not_break_page(self, client, monkeypatch):
        """list_history 가 raise 해도 페이지 자체는 200 + 캐시 결과 표시."""
        from src import analysis_cache as ac
        from src import prediction_history
        ac.init_db()
        ac.put("AAPL", "us", "<p>cached body unique</p>", "auto_cron")
        def boom(*a, **k):
            raise RuntimeError("db locked")
        monkeypatch.setattr(prediction_history, "list_history", boom)
        resp = client.get("/stock/AAPL")
        assert resp.status_code == 200
        # 캐시 본문은 정상 표시
        assert b"cached body unique" in resp.data
        # 섹션은 누락
        assert "예측 정확도".encode() not in resp.data
```

- [ ] **Step 7.2: 테스트 실행 — FAIL**

```bash
.venv/bin/python -m pytest tests/test_web_app.py::TestPredictionHistorySection -v
```
Expected: 5 fail (stock_view 가 아직 _render_prediction_history 호출 안 함)

- [ ] **Step 7.3: `stock_view` 갱신**

`src/web_app.py` 의 `stock_view` 함수 안 cache hit 분기 (찾는 키워드: `body = _render_meta_bar(row, fresh, name)`) 를 다음과 같이 교체:

기존:
```python
    fresh = analysis_cache.is_fresh(row, int(time.time()))
    body = _render_meta_bar(row, fresh, name) + f'<div class="card result-frame">{row["result_html"]}</div>'
    return _page(f"{name} 분석 결과", body)
```

변경 후:
```python
    fresh = analysis_cache.is_fresh(row, int(time.time()))
    body_parts = [
        _render_meta_bar(row, fresh, name),
        f'<div class="card result-frame">{row["result_html"]}</div>',
    ]
    try:
        body_parts.append(_render_prediction_history(symbol))
    except Exception as e:
        logger.warning("prediction_history 렌더 실패 — %s: %s", symbol, e)
    return _page(f"{name} 분석 결과", "".join(body_parts))
```

- [ ] **Step 7.4: 테스트 실행 — PASS**

```bash
.venv/bin/python -m pytest tests/test_web_app.py::TestPredictionHistorySection -v
```
Expected: 5 passed

- [ ] **Step 7.5: 전체 회귀**

```bash
.venv/bin/python -m pytest tests/ --ignore=tests/test_ml_predictor.py --ignore=tests/test_data_fetcher.py --ignore=tests/test_backtest.py -q
```
Expected: 모든 기존 + 신규 테스트 PASS, 회귀 없음

- [ ] **Step 7.6: 커밋**

```bash
git add src/web_app.py tests/test_web_app.py
git commit -m "feat(web): stock_view 가 예측 정확도 섹션 렌더 (try/except 격리)"
```

---

## Phase 4 — 배포

### Task 8: 서버 배포 + 시각 확인

**Files:** 없음 (서버 운영 명령만)

- [ ] **Step 8.1: push to origin/main**

```bash
git push origin main
```

- [ ] **Step 8.2: 서버 git pull**

```bash
ssh sykim@100.87.151.104 'cd ~/Projects/stock-analyzer && git pull --ff-only origin main'
```
Expected: Fast-forward, 신규 커밋들 적용됨

- [ ] **Step 8.3: web 서비스 재시작**

```bash
ssh sykim@100.87.151.104 'launchctl kickstart -k gui/$(id -u)/ai.stock-analyzer.web'
```

- [ ] **Step 8.4: smoke test**

```bash
ssh sykim@100.87.151.104 'sleep 3; curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/'
```
Expected: 401 (Basic Auth 정상 동작)

- [ ] **Step 8.5: 시각 확인**

브라우저로 https://sykim-macmini.tail8d6ef7.ts.net/ 로그인 후:
- 등록된 종목 카드의 "결과 보기" 클릭
- 분석 결과 페이지에서 **"예측 정확도"** 섹션 확인:
  - 모델 5 카드 그리드
  - 모델 설명 탭바 (RF 가 기본 활성, 클릭 시 다른 탭으로 전환)
  - "최근 90일 예측 히스토리" `<details>` (클릭하면 표 펼침)
  - 평가 대기 row 가 회색
  - hit/miss row 가 초록/빨강

문제 없으면 task 완료. 시각 이슈 발견되면 별도 fix commit.

---

## Self-Review

스펙 (`docs/superpowers/specs/2026-05-05-prediction-history-ui-design.md`) 의 §2 정책 표 각 항목 → plan task 매핑:

| 스펙 항목 | 구현 task |
|---|---|
| `/stock/<symbol>` 안에 섹션 추가 | T7 (stock_view 통합) |
| 시간순 한 행 5 모델 표 | T4 (`_render_history_table`) |
| 시간 범위 90일 | T1 (`list_history` cutoff) |
| 평가 대기 회색 처리 | T4 (row-pending 클래스) |
| ensemble hit 종합 | T1 (`ensemble_hit` 필드) |
| 요약 카드 (펼침) | T3 (`_render_hit_rate_summary`) |
| 모델 설명 탭바 (CSS-only) | T2 (`_render_model_tabs`) + T6 (CSS) |
| 히스토리 표 `<details>` 접힘 | T5 (`_render_prediction_history`) |
| `source='live'` 만 | T1 (SQL WHERE) |

스펙 §6 에러 케이스 → 모두 구현됨:
- 예측 row 0건 → T5 빈 케이스 + T7 통합 테스트
- 평가된/대기 혼합 → T1 + T4
- `list_history` 오류 → T7 try/except (`test_history_error_does_not_break_page`)
- 일부 모델만 row → T4 (`_pred_cell` None 분기)
- backtest 격리 → T1 (`test_excludes_backtest_source`)
- 90일 cutoff 빈 결과 → T5 (rows 빈 케이스 안내)

타입 일관성 체크:
- `list_history` 반환 dict 키 → `_render_history_table` 가 같은 키 사용 (`target_date`, `ts`, `base_close`, `actual_close`, `ensemble_hit`, `models`) ✓
- `_pred_cell` 입력 dict 키 → `models[m]` 의 `direction`/`confidence`/`hit` 와 일치 ✓
- `hit_rate_by_model` 반환 dict 키 (`hit_rate`, `n`) → `_hit_rate_card` 사용 ✓

Placeholder 스캔: TBD/TODO/"add appropriate" 패턴 없음 ✓

스펙 §10 비목표 → plan 에 의도적으로 빠짐 (`/history`, 차트, 데이터 필터 탭, CSV) ✓
