# 예측 히스토리 UI 설계 (Prediction History UI)

**작성일**: 2026-05-05
**상태**: 설계 — 사용자 검토 대기
**관련 모듈**: `src/prediction_history.py`, `src/web_app.py`

## 1. 배경 및 목적

`predictions` 테이블에는 매 분석 시 5개 ML 모델 (rf, lgbm, lstm, transformer, ensemble) 의 예측이 저장되고, KST 18:00 cron 의 `backfill_all` 이 다음날 actual_close 를 채워 hit (0/1) 을 계산한다. 즉 **데이터는 이미 다 있다**.

`report_generator` 가 분석 리포트 안에 모델별 hit rate **요약**은 보여주고 있지만, **시간순 row 단위 히스토리** (어느 날, 어떤 방향, 실제 종가, 적중/빗나감) 는 노출되지 않는다.

목적: 사용자가 종목별 결과 페이지에서 한 번에 "이 종목 예측이 그동안 얼마나 맞았는지" 를 볼 수 있게 한다.

## 2. 결정된 정책 (브레인스토밍 합의)

| 항목 | 결정 |
|---|---|
| 진입점 | 종목별 결과 페이지 `/stock/<symbol>` 안에 섹션 추가 |
| 표 형태 | 시간순(target_date 내림차순) 1 row 에 5 모델 함께 표시 |
| 시간 범위 | 최근 **90일** (target_date 기준) |
| 평가 대기 row | 회색 처리 + "평가 대기" 뱃지 (숨기지 않음) |
| 종합 hit 판정 | **ensemble 모델의 hit** 를 종합 결과로 사용 |
| 요약 카드 | 모델 5개의 누적 hit rate (펼친 상태로 항상 노출) |
| 모델 설명 탭바 | 요약 카드 아래, **CSS-only** radio+label 패턴 (JS 없음). 5개 탭 클릭 시 그 모델의 설명 박스만 전환 |
| 히스토리 표 | `<details>` 로 접힌 상태 (사용자가 클릭하면 펼침) |
| 데이터 소스 | `predictions` 테이블, `source = 'live'` row 만 (backtest row 격리) |

## 3. 아키텍처

```
[/stock/<symbol> 라우트]
  └─ stock_view (web_app.py)
      ├─ 캐시 조회 (analysis_cache) — 기존 그대로
      ├─ 메타바 + 결과 HTML 렌더 — 기존 그대로
      └─ _render_prediction_history(symbol)  ── 신규
            ├─ prediction_history.hit_rate_by_model(symbol, source="live")  ── 기존
            ├─ prediction_history.list_history(symbol, days=90)             ── 신규
            ├─ _render_hit_rate_summary(rates)  ── 신규: 모델 5 카드 그리드
            ├─ _render_model_tabs()             ── 신규: 모델 설명 탭바 (CSS-only)
            └─ _render_history_table(rows)      ── 신규: 시간순 표 (<details> 안)

[src/prediction_history.py]
  └─ list_history(symbol, days) ── 신규
       (predictions WHERE symbol AND source='live' AND target_date >= cutoff
        → target_date 기준 pivot → list[dict])
```

## 4. 데이터 모델

스키마 변경 **없음**. 기존 `predictions` 테이블의 SELECT 만 추가.

### `list_history` 시그니처

```python
def list_history(symbol: str, days: int = 90) -> list[dict]:
    """종목의 최근 N일 예측 히스토리 (시간순 내림차순).

    같은 (symbol, target_date) 의 5 모델 row 를 한 dict 에 묶는다.
    ensemble row 가 있으면 base_close/actual_close/hit/ts 의 대표값으로 사용.

    Returns:
        [
          {
            "target_date":   int,            # KST 자정 unix epoch
            "ts":            int | None,     # 분석 실행 시각 (대표 모델의 ts)
            "base_close":    float,          # 분석 시점 기준 종가
            "actual_close":  float | None,   # 다음 영업일 실제 종가
            "ensemble_hit":  int | None,     # 0/1 또는 None (평가 대기)
            "models": {
                "rf":          {"direction": "상승"|"하락", "confidence": 0.0~1.0, "hit": 0|1|None},
                "lgbm":        {...},
                "lstm":        {...},
                "transformer": {...},
                "ensemble":    {...},
            },
          },
          ...
        ]
    """
```

### SQL

```sql
SELECT ts, target_date, model, direction, confidence, base_close, actual_close, hit
FROM predictions
WHERE symbol = ?
  AND source = 'live'
  AND target_date >= ?       -- now - days*86400
ORDER BY target_date DESC, model
```

Python 측 후처리:

```python
from collections import defaultdict
groups = defaultdict(dict)  # target_date → {model: row_dict}
for ts, td, model, direction, confidence, base_close, actual_close, hit in cursor:
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
            m: {"direction": v["direction"], "confidence": v["confidence"], "hit": v["hit"]}
            for m, v in models.items()
        },
    })
return result
```

## 5. 라우트 + UI 통합

### `stock_view` 라우트 (`src/web_app.py`)

기존 흐름에 히스토리 섹션 추가 — try/except 로 격리해 분석 결과는 항상 표시.

```python
# 캐시 hit 분기 끝부분 (현재):
fresh = analysis_cache.is_fresh(row, int(time.time()))
body = _render_meta_bar(row, fresh, name) + f'<div class="card result-frame">{row["result_html"]}</div>'

# ↓ 다음으로 변경:
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

### 신규 헬퍼 (web_app.py)

```python
def _render_prediction_history(symbol: str) -> str:
    """예측 히스토리 섹션 — 요약 카드(펼침) + <details> 표(접힘)."""
    rates = prediction_history.hit_rate_by_model(symbol, source="live")
    rows = prediction_history.list_history(symbol, days=90)
    if not rates and not rows:
        return ""  # 예측 이력 0건 → 섹션 자체 미표시
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


def _render_hit_rate_summary(rates: dict) -> str:
    if not rates:
        return '<div class="alert alert-info">평가된 예측이 아직 없습니다.</div>'
    label = {"rf":"RF","lgbm":"LGBM","lstm":"LSTM","transformer":"Transformer","ensemble":"Ensemble"}
    cards = []
    for m in ("rf","lgbm","lstm","transformer","ensemble"):
        info = rates.get(m)
        if info is None:
            cards.append(_hit_rate_card(label[m], None, 0))
        else:
            cards.append(_hit_rate_card(label[m], info["hit_rate"]*100, info["n"]))
    return f'<div class="hit-rate-grid">{"".join(cards)}</div>'


def _hit_rate_card(name: str, pct: float | None, n: int) -> str:
    if pct is None:
        return (f'<div class="hit-rate-card empty">'
                f'<div class="name">{name}</div>'
                f'<div class="value">—</div>'
                f'<div class="n">평가 없음</div></div>')
    color = "var(--green-600)" if pct >= 60 else ("var(--amber-500)" if pct >= 50 else "var(--red-600)")
    return (f'<div class="hit-rate-card">'
            f'<div class="name">{name}</div>'
            f'<div class="value" style="color:{color};">{pct:.1f}%</div>'
            f'<div class="n">{n}회 평가</div></div>')


def _render_history_table(rows: list[dict]) -> str:
    head = ("<thead><tr>"
            "<th>분석일</th><th>기준 종가</th>"
            "<th>RF</th><th>LGBM</th><th>LSTM</th><th>Transf</th><th>Ensemble</th>"
            "<th>실제 종가</th><th>판정</th>"
            "</tr></thead>")
    body_rows = []
    for r in rows:
        date_str = _format_kst(r["target_date"]).split()[0]  # 'YYYY-MM-DD'
        base = f"{r['base_close']:,.0f}"
        cells = "".join(_pred_cell(r["models"].get(m)) for m in ("rf","lgbm","lstm","transformer","ensemble"))
        if r["actual_close"] is None:
            actual = "—"
            verdict = '<span class="badge-pending">평가 대기</span>'
            row_attr = ' class="row-pending"'
        else:
            actual = f"{r['actual_close']:,.0f}"
            verdict = ('<span class="badge-hit">✅ 적중</span>'
                       if r["ensemble_hit"] == 1
                       else '<span class="badge-miss">❌ 빗나감</span>')
            row_attr = ""
        body_rows.append(
            f"<tr{row_attr}><td>{date_str}</td><td class='num'>{base}</td>"
            f"{cells}<td class='num'>{actual}</td><td>{verdict}</td></tr>"
        )
    return f'<table class="history-table">{head}<tbody>{"".join(body_rows)}</tbody></table>'


def _pred_cell(m: dict | None) -> str:
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


# ── 모델 설명 (정적 텍스트) ──────────────────────────────────────────────────
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
    """CSS-only 모델 설명 탭바. 라디오 버튼 + label 패턴 (JS 없음)."""
    radios = []
    labels = []
    panels = []
    for i, key in enumerate(("rf", "lgbm", "lstm", "transformer", "ensemble")):
        info = _MODEL_INFO[key]
        checked = ' checked' if i == 0 else ''
        radios.append(
            f'<input type="radio" name="model-tab" id="mtab-{key}" class="mtab-radio"{checked}>'
        )
        labels.append(
            f'<label for="mtab-{key}" class="mtab-label mtab-label-{key}">{info["name"].split(" (")[0]}</label>'
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

### 추가 CSS

기존 `_CSS` 끝에 append (~50 줄):

```css
.hit-rate-grid {
  display: grid; grid-template-columns: repeat(5, 1fr);
  gap: 10px; margin-bottom: 16px;
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

/* CSS-only 모델 설명 탭바 */
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

/* radio:checked 시 그 label 활성화 + panel 표시 */
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

/* 키보드 접근성 — focus 시 outline */
.mtab-radio:focus-visible + .mtab-list .mtab-label,
.mtab-radio:focus-visible ~ .mtab-list .mtab-label {
  outline: 2px solid var(--blue-500); outline-offset: 2px;
}
```

모바일 가로 스크롤: `<div style="overflow-x:auto;">` 으로 표 wrap (`_render_prediction_history` 안).

## 6. 에러 / 엣지 케이스

| 시나리오 | 동작 |
|---|---|
| 종목에 예측 row 0건 | `_render_prediction_history` → `""` 반환, 섹션 미표시 |
| 평가된 row 0건, 미평가 row 만 | 요약 카드 = "평가 없음" / 표 모든 row 회색 (평가 대기) |
| `list_history` sqlite 오류 | stock_view 의 try/except → warning log, 섹션 생략. 분석 결과 본문은 정상 |
| 5 모델 중 일부만 저장된 row | 누락 모델 셀 = `—`, 카드 = "평가 없음" |
| backtest 데이터 혼입 | `WHERE source='live'` 격리 — 표시 안 함 |
| 90일 cutoff 가 빈 결과 (오늘 첫 분석) | 표 자리에 안내 메시지 |

## 7. 모듈 책임

```
src/prediction_history.py — list_history(symbol, days=90) 추가 (DB 조회 + pivot)
src/web_app.py            — _render_prediction_history (외부 진입)
                            _render_hit_rate_summary, _hit_rate_card
                            _render_history_table, _pred_cell
                            CSS append
                            stock_view 가 _render_prediction_history 호출
```

`web_app.py` 가 ~1500 라인 넘어가는데, 별도 refactor (예: `src/web_render.py` 분리) 는 이번 작업 외 — follow-up.

## 8. 테스트 전략

`tests/conftest.py` 의 `_DB_PATH` 격리는 그대로. 새 row 는 fixture 안에서 `prediction_history.insert_live` / 직접 SQL 로 setup.

### `tests/test_prediction_history.py` — `TestListHistory` (6 케이스)

1. 빈 DB → `[]`
2. 같은 `target_date` 의 5 모델 row → 1 dict, `models` 5 키
3. 평가된 row + 평가 대기 row 혼합 → `actual_close`/`ensemble_hit` 정확
4. 90일 cutoff — 91일 전 미포함, 89일 포함
5. `source='live'` 만 — `backtest` row 격리
6. 다른 symbol row 격리

### `tests/test_web_app.py` — `TestPredictionHistorySection` (7 케이스)

1. 예측 row 0건 → 섹션 없음 (`<h2>예측 정확도</h2>` 부재)
2. 평가된 row → 요약 카드 마크업 + `<table>` row 마크업 포함
3. 미평가 row 만 → "평가 대기" 텍스트 + `class="row-pending"` 포함
4. `<details>` + summary 텍스트 "최근 90일 예측 히스토리"
5. `prediction_history.list_history` 가 raise → 섹션 누락 + result_html 정상
6. **모델 탭바 마크업** — 섹션이 렌더되면 `id="mtab-rf"`, `id="mtab-ensemble"` 등 5개 radio + label + panel 모두 포함, RF 라디오는 `checked`
7. **모델 설명 텍스트** — 각 panel 본문에 모델 설명 일부 ("Random Forest", "그래디언트 부스팅", "어텐션" 등) 포함

## 9. 마이그레이션 / 배포

- DB 스키마 변경 없음
- 기존 `predictions` row 즉시 활용 가능 (cron 18:00 backfill 누적분)
- 배포: git push → 서버 git pull → `launchctl kickstart -k gui/$(id -u)/ai.stock-analyzer.web`

## 10. 비목표 (Non-goals)

- 종합 history 페이지 (`/history`) — 종목별 진입점만 (follow-up 검토)
- 차트/sparkline — 표만, 시각화는 추후
- **탭바가 데이터 필터링** — 탭은 모델 설명만 전환, 시간순 표는 5 모델 모두 그대로 표시 (가벼운 디자인 — 옵션 A)
- CSV/JSON 내보내기 — 추후
- 백테스트 row 통합 — `source='live'` 만, backtest 결과는 별도 페이지
- 평가 대기 row 자동 갱신 (실시간 폴링) — 정적 SSR
