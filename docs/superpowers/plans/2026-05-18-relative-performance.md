# Relative Performance Display Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 각 종목 리포트 카드에 "금일 등락률 | 시장 지수 등락률 | 알파(pp)" 한 줄을 추가한다. KOSPI/KOSDAQ/S&P 500을 심볼 suffix로 자동 매핑.

**Architecture:** `src/technical_analysis.py`에 인덱스 매핑·계산 함수 추가, `main.py`의 `analyze_stock()`에서 호출하여 결과 dict에 `rel_perf` 키로 포함, `src/report_generator.py`의 `_render_stock_card()`에서 한 줄로 렌더링.

**Tech Stack:** Python 3, pandas, pytest, yfinance(기존 활용)

**Spec:** `docs/superpowers/specs/2026-05-18-relative-performance-design.md`

---

## File Structure

**Modify:**
- `src/technical_analysis.py` — `_MARKET_INDEX`에 `kosdaq` 추가, `resolve_index_market()`, `compute_relative_performance()` 추가
- `main.py` — `analyze_stock()` 결과 dict에 `rel_perf` 포함
- `src/report_generator.py` — `_render_rel_perf()` 추가, `_render_stock_card()`에서 호출
- `src/templates/report.css` — `.rel-perf`, `.rel-perf .up/.down/.flat`, `.rel-perf-asof` 추가

**Modify (tests):**
- `tests/test_technical_analysis.py` — `TestResolveIndexMarket`, `TestComputeRelativePerformance` 클래스 추가
- `tests/test_report_generator.py` — `TestRenderRelPerf` 클래스 추가

**No new files.** 신규 의존성 없음.

---

## Task 1: KOSDAQ 인덱스 추가 + resolve_index_market

**Files:**
- Modify: `src/technical_analysis.py:10-13` (`_MARKET_INDEX` dict)
- Modify: `src/technical_analysis.py` (function 추가, `fetch_market_df` 위)
- Test: `tests/test_technical_analysis.py` (신규 클래스 추가)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_technical_analysis.py`:

```python
class TestResolveIndexMarket:
    def test_kospi_suffix_ks(self):
        from src.technical_analysis import resolve_index_market
        assert resolve_index_market("005930.KS") == ("KOSPI", "korea")

    def test_kosdaq_suffix_kq(self):
        from src.technical_analysis import resolve_index_market
        assert resolve_index_market("247540.KQ") == ("KOSDAQ", "kosdaq")

    def test_us_no_suffix(self):
        from src.technical_analysis import resolve_index_market
        assert resolve_index_market("AAPL") == ("S&P 500", "us")

    def test_us_with_dot_but_not_kr(self):
        from src.technical_analysis import resolve_index_market
        # BRK.B 같은 미국 심볼 — KR suffix 아니면 US
        assert resolve_index_market("BRK.B") == ("S&P 500", "us")

    def test_kosdaq_market_key_resolves_via_fetch_market_df(self):
        """_MARKET_INDEX['kosdaq']가 ^KQ11로 매핑되는지 확인."""
        from src.technical_analysis import _MARKET_INDEX
        assert _MARKET_INDEX.get("kosdaq") == "^KQ11"
```

- [ ] **Step 2: Run tests to verify failure**

```bash
cd ~/Projects/stock-analyzer
.venv/bin/python -m pytest tests/test_technical_analysis.py::TestResolveIndexMarket -v
```

Expected: 5 FAIL — `ImportError: cannot import name 'resolve_index_market'` 또는 `KeyError: 'kosdaq'`

- [ ] **Step 3: Implement**

In `src/technical_analysis.py`, replace lines 10-13:

```python
_MARKET_INDEX = {
    "korea":  "^KS11",   # KOSPI
    "kosdaq": "^KQ11",   # KOSDAQ
    "us":     "^GSPC",   # S&P 500
}
```

Then add this function right above `fetch_market_df` (around line 19):

```python
def resolve_index_market(symbol: str) -> tuple[str, str]:
    """심볼 suffix로 (지수 표시명, market_key) 반환.

    market_key는 _MARKET_INDEX의 키 — fetch_market_df()에 그대로 전달된다.

    예:
        '005930.KS' -> ('KOSPI', 'korea')
        '247540.KQ' -> ('KOSDAQ', 'kosdaq')
        'AAPL'      -> ('S&P 500', 'us')
    """
    if symbol.endswith(".KS"):
        return ("KOSPI", "korea")
    if symbol.endswith(".KQ"):
        return ("KOSDAQ", "kosdaq")
    return ("S&P 500", "us")
```

- [ ] **Step 4: Run tests to verify pass**

```bash
.venv/bin/python -m pytest tests/test_technical_analysis.py::TestResolveIndexMarket -v
```

Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add src/technical_analysis.py tests/test_technical_analysis.py
git commit -m "feat(rel-perf): KOSDAQ 인덱스 추가 + resolve_index_market 헬퍼"
```

---

## Task 2: compute_relative_performance 함수

**Files:**
- Modify: `src/technical_analysis.py` (function 추가, `compute_indicators` 위)
- Test: `tests/test_technical_analysis.py` (신규 클래스 추가)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_technical_analysis.py`:

```python
class TestComputeRelativePerformance:
    """compute_relative_performance — 종목 vs 시장 인덱스 등락률 계산."""

    def _stock_df(self, prev: float, last: float):
        """Close 2개만 있는 최소 fixture."""
        idx = pd.date_range("2026-05-15", periods=2, freq="B")
        return pd.DataFrame({
            "Close": [prev, last],
            "Open":  [prev, last],
            "High":  [prev, last],
            "Low":   [prev, last],
            "Volume": [1, 1],
        }, index=idx)

    def test_basic_positive_alpha(self, monkeypatch):
        """종목 +2%, 인덱스 +1% -> 알파 +1pp"""
        from src import technical_analysis as ta_mod
        stock = self._stock_df(100.0, 102.0)   # +2%
        index = self._stock_df(1000.0, 1010.0)  # +1%
        monkeypatch.setattr(ta_mod, "fetch_market_df", lambda mk: index)

        result = ta_mod.compute_relative_performance(stock, "005930.KS")

        assert result is not None
        assert result["index_name"] == "KOSPI"
        assert result["stock_pct"] == pytest.approx(2.0, abs=1e-6)
        assert result["index_pct"] == pytest.approx(1.0, abs=1e-6)
        assert result["alpha_pp"]  == pytest.approx(1.0, abs=1e-6)
        assert "as_of" in result
        assert "stage" in result

    def test_negative_alpha_us(self, monkeypatch):
        """종목 -1%, S&P +0.5% -> 알파 -1.5pp, US 매핑"""
        from src import technical_analysis as ta_mod
        stock = self._stock_df(200.0, 198.0)    # -1%
        index = self._stock_df(5000.0, 5025.0)  # +0.5%
        monkeypatch.setattr(ta_mod, "fetch_market_df", lambda mk: index)

        result = ta_mod.compute_relative_performance(stock, "AAPL")

        assert result["index_name"] == "S&P 500"
        assert result["stock_pct"] == pytest.approx(-1.0, abs=1e-6)
        assert result["index_pct"] == pytest.approx(0.5, abs=1e-6)
        assert result["alpha_pp"]  == pytest.approx(-1.5, abs=1e-6)

    def test_short_df_returns_none(self, monkeypatch):
        """len(df) < 2 -> None (신규상장 등)"""
        from src import technical_analysis as ta_mod
        idx = pd.date_range("2026-05-18", periods=1, freq="B")
        stock = pd.DataFrame({"Close": [100.0]}, index=idx)
        monkeypatch.setattr(ta_mod, "fetch_market_df", lambda mk: self._stock_df(1000, 1010))

        assert ta_mod.compute_relative_performance(stock, "005930.KS") is None

    def test_index_fetch_fail_returns_none(self, monkeypatch):
        """fetch_market_df가 None 반환 -> None"""
        from src import technical_analysis as ta_mod
        stock = self._stock_df(100.0, 102.0)
        monkeypatch.setattr(ta_mod, "fetch_market_df", lambda mk: None)

        assert ta_mod.compute_relative_performance(stock, "005930.KS") is None

    def test_short_index_df_returns_none(self, monkeypatch):
        """인덱스 df도 len < 2면 None"""
        from src import technical_analysis as ta_mod
        stock = self._stock_df(100.0, 102.0)
        short_idx = pd.DataFrame({"Close": [1000.0]},
                                  index=pd.date_range("2026-05-18", periods=1, freq="B"))
        monkeypatch.setattr(ta_mod, "fetch_market_df", lambda mk: short_idx)

        assert ta_mod.compute_relative_performance(stock, "005930.KS") is None

    def test_zero_prev_close_returns_none(self, monkeypatch):
        """전일 종가 0 -> div-by-zero 방어, None"""
        from src import technical_analysis as ta_mod
        stock = self._stock_df(0.0, 1.0)
        index = self._stock_df(1000.0, 1010.0)
        monkeypatch.setattr(ta_mod, "fetch_market_df", lambda mk: index)

        assert ta_mod.compute_relative_performance(stock, "005930.KS") is None

    def test_zero_prev_index_returns_none(self, monkeypatch):
        """인덱스 전일 종가 0 -> None"""
        from src import technical_analysis as ta_mod
        stock = self._stock_df(100.0, 102.0)
        index = self._stock_df(0.0, 1.0)
        monkeypatch.setattr(ta_mod, "fetch_market_df", lambda mk: index)

        assert ta_mod.compute_relative_performance(stock, "005930.KS") is None
```

- [ ] **Step 2: Run tests to verify failure**

```bash
.venv/bin/python -m pytest tests/test_technical_analysis.py::TestComputeRelativePerformance -v
```

Expected: 7 FAIL — `AttributeError` / `ImportError` (function not yet defined)

- [ ] **Step 3: Implement**

In `src/technical_analysis.py`, add this function immediately after `resolve_index_market` (above `compute_indicators`):

```python
def _stage_label(now=None, market_key: str = "korea") -> str:
    """분석 시점과 시장 운영시간으로 라벨 결정. KST 기준."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = now or datetime.now(ZoneInfo("Asia/Seoul"))
    weekday = now.weekday()  # 0=Mon ... 6=Sun
    if weekday >= 5:
        return "weekend"
    hour_min = now.hour * 60 + now.minute
    if market_key in ("korea", "kosdaq"):
        # 09:00-15:30 KST
        if 9 * 60 <= hour_min <= 15 * 60 + 30:
            return "market_open"
        if hour_min > 15 * 60 + 30:
            return "after_close"
        return "before_open"
    if market_key == "us":
        # KST 22:30 ~ 익일 06:00 (DST 단순화: 22:30-06:00 범위)
        if hour_min >= 22 * 60 + 30 or hour_min < 6 * 60:
            return "market_open"
        if 6 * 60 <= hour_min < 9 * 60:
            return "after_close"
        return "before_open"
    return "after_close"  # 알 수 없으면 보수적


def compute_relative_performance(
    stock_df: "pd.DataFrame", symbol: str
) -> dict | None:
    """종목의 일간 등락률을 시장 지수와 비교.

    공식: pct = (Close[-1] - Close[-2]) / Close[-2] * 100
    alpha_pp = stock_pct - index_pct

    Returns:
        {
            "index_name": "KOSPI",
            "stock_pct": 1.52,
            "index_pct": 0.81,
            "alpha_pp": 0.71,
            "as_of": "2026-05-18 14:32",
            "stage": "market_open",
        }
        다음의 경우 None:
        - len(stock_df) < 2 또는 len(index_df) < 2
        - 인덱스 fetch 실패 (fetch_market_df가 None 반환)
        - prev_close == 0 (div-by-zero 방어)
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    if len(stock_df) < 2:
        return None

    index_name, market_key = resolve_index_market(symbol)
    index_df = fetch_market_df(market_key)
    if index_df is None or len(index_df) < 2:
        return None

    s_prev = float(stock_df["Close"].iloc[-2])
    s_last = float(stock_df["Close"].iloc[-1])
    i_prev = float(index_df["Close"].iloc[-2])
    i_last = float(index_df["Close"].iloc[-1])

    if s_prev == 0 or i_prev == 0:
        return None

    stock_pct = (s_last - s_prev) / s_prev * 100.0
    index_pct = (i_last - i_prev) / i_prev * 100.0
    alpha_pp = stock_pct - index_pct

    now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
    return {
        "index_name": index_name,
        "stock_pct": stock_pct,
        "index_pct": index_pct,
        "alpha_pp": alpha_pp,
        "as_of": now_kst.strftime("%Y-%m-%d %H:%M"),
        "stage": _stage_label(now_kst, market_key),
    }
```

- [ ] **Step 4: Run tests to verify pass**

```bash
.venv/bin/python -m pytest tests/test_technical_analysis.py::TestComputeRelativePerformance -v
```

Expected: 7 PASS

- [ ] **Step 5: Run full technical_analysis test suite for regression**

```bash
.venv/bin/python -m pytest tests/test_technical_analysis.py -v
```

Expected: all PASS (기존 테스트 + 신규 12개)

- [ ] **Step 6: Commit**

```bash
git add src/technical_analysis.py tests/test_technical_analysis.py
git commit -m "feat(rel-perf): compute_relative_performance + _stage_label"
```

---

## Task 3: analyze_stock에서 rel_perf 호출

**Files:**
- Modify: `main.py:91-150` (`analyze_stock` 함수 본문)
- Test: `tests/test_main.py` (확인만, 기존 mock 패턴 따라 추가)

- [ ] **Step 1: Check existing test_main.py for analyze_stock mocking pattern**

```bash
grep -n "analyze_stock\|rel_perf" tests/test_main.py | head -20
```

이미 `analyze_stock`을 모킹하는 패턴이 있다면 그에 맞춰 한 줄 추가. 없으면 통합 검증은 Task 6의 end-to-end에 위임하고 이 Task는 코드 변경만 진행.

- [ ] **Step 2: Modify analyze_stock**

In `main.py`, in the `analyze_stock` function, locate the block (around line 105-109):

```python
        # BNF 시그널 — 실패해도 분석 본체에 영향 없음
        bnf_signal = None
        try:
            market_df = fetch_market_df(market) if market else None
            bnf_signal = generate_bnf_signal(df, market_df=market_df)
        except Exception as e:
            logger.warning("generate_bnf_signal 실패 (분석은 계속): %s", e)
```

Add the following block immediately after the BNF block (still inside the `try:` in `analyze_stock`):

```python
        # 시장 지수 대비 등락률 (알파) — 실패해도 분석 본체 무관
        rel_perf = None
        try:
            from src.technical_analysis import compute_relative_performance
            rel_perf = compute_relative_performance(df, symbol)
        except Exception as e:
            logger.warning("compute_relative_performance 실패 (분석은 계속): %s", e)
```

Then in the return dict (around line 139-150), add `"rel_perf": rel_perf,`:

```python
        return {
            "name": name,
            "symbol": symbol,
            "df": df,
            "last_close": float(df["Close"].iloc[-1]),
            "signal": signal,
            "bnf_signal": bnf_signal,
            "rel_perf": rel_perf,
            "prediction": prediction,
            "news": news,
            "sentiment": sentiment,
            "patterns": patterns,
        }
```

- [ ] **Step 3: Lint check**

```bash
.venv/bin/python -m py_compile main.py
```

Expected: no output (success)

- [ ] **Step 4: Smoke test — single stock**

```bash
cd ~/Projects/stock-analyzer
.venv/bin/python -c "
from main import analyze_stock
r = analyze_stock('AAPL', 'Apple', market='us')
print('rel_perf:', r.get('rel_perf') if r else 'no result')
"
```

Expected: `rel_perf: {'index_name': 'S&P 500', 'stock_pct': <num>, 'index_pct': <num>, 'alpha_pp': <num>, 'as_of': ..., 'stage': ...}` 또는 시장 데이터 fetch 실패 시 None (로그 경고 동반).

주의: 네트워크/시장이 닫혀있으면 fetch 실패할 수 있음. 그 경우 None이면 정상 — 분석 본체가 영향받지 않는지만 확인 (`r is not None`).

- [ ] **Step 5: Commit**

```bash
git add main.py
git commit -m "feat(rel-perf): analyze_stock 결과에 rel_perf 포함"
```

---

## Task 4: _render_rel_perf 렌더 함수

**Files:**
- Modify: `src/report_generator.py` (function 추가 + `_render_stock_card` 한 줄 삽입)
- Test: `tests/test_report_generator.py` (신규 클래스 추가)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_report_generator.py`:

```python
class TestRenderRelPerf:
    """_render_rel_perf — 종목 vs 인덱스 등락률 한 줄 렌더."""

    def test_none_returns_empty(self):
        from src.report_generator import _render_rel_perf
        assert _render_rel_perf(None) == ""

    def test_positive_stock_index_alpha(self):
        from src.report_generator import _render_rel_perf
        html = _render_rel_perf({
            "index_name": "S&P 500",
            "stock_pct": 1.52,
            "index_pct": 0.81,
            "alpha_pp": 0.71,
            "as_of": "2026-05-18 14:32",
            "stage": "market_open",
        })
        assert "rel-perf" in html
        assert "+1.52%" in html
        assert "+0.81%" in html
        assert "+0.71%pp" in html
        assert "S&amp;P 500" in html or "S&P 500" in html
        assert "장중" in html
        # 양수는 'up' class
        assert html.count('class="up"') >= 3

    def test_negative_alpha(self):
        from src.report_generator import _render_rel_perf
        html = _render_rel_perf({
            "index_name": "KOSPI",
            "stock_pct": -2.10,
            "index_pct": 0.50,
            "alpha_pp": -2.60,
            "as_of": "2026-05-18 16:05",
            "stage": "after_close",
        })
        assert "-2.10%" in html
        assert "+0.50%" in html
        assert "-2.60%pp" in html
        assert "마감 후" in html
        assert 'class="down"' in html
        assert 'class="up"' in html  # index_pct 양수

    def test_zero_uses_flat_class(self):
        from src.report_generator import _render_rel_perf
        html = _render_rel_perf({
            "index_name": "KOSDAQ",
            "stock_pct": 0.0,
            "index_pct": 0.0,
            "alpha_pp": 0.0,
            "as_of": "2026-05-18 09:00",
            "stage": "market_open",
        })
        assert 'class="flat"' in html
        assert "+0.00%" in html or "0.00%" in html

    def test_stage_before_open_label(self):
        from src.report_generator import _render_rel_perf
        html = _render_rel_perf({
            "index_name": "KOSPI", "stock_pct": 1.0, "index_pct": 0.5,
            "alpha_pp": 0.5, "as_of": "2026-05-18 08:00", "stage": "before_open",
        })
        assert "장 시작 전" in html

    def test_stage_weekend_label(self):
        from src.report_generator import _render_rel_perf
        html = _render_rel_perf({
            "index_name": "KOSPI", "stock_pct": 1.0, "index_pct": 0.5,
            "alpha_pp": 0.5, "as_of": "2026-05-17 14:00", "stage": "weekend",
        })
        assert "주말" in html
```

- [ ] **Step 2: Run tests to verify failure**

```bash
.venv/bin/python -m pytest tests/test_report_generator.py::TestRenderRelPerf -v
```

Expected: 6 FAIL — `ImportError: cannot import name '_render_rel_perf'`

- [ ] **Step 3: Implement render function**

In `src/report_generator.py`, add this function after `_render_sentiment` (around line 245, before `_render_stock_card`):

```python
_STAGE_LABEL = {
    "market_open": "장중",
    "after_close": "마감 후",
    "before_open": "장 시작 전",
    "weekend": "주말",
}


def _rel_perf_class(value: float) -> str:
    if value > 0:
        return "up"
    if value < 0:
        return "down"
    return "flat"


def _render_rel_perf(rel_perf: dict | None) -> str:
    """종목 vs 시장 인덱스 등락률 한 줄 렌더. None이면 빈 문자열."""
    if not rel_perf:
        return ""
    stock_pct = rel_perf["stock_pct"]
    index_pct = rel_perf["index_pct"]
    alpha_pp = rel_perf["alpha_pp"]
    index_name = html.escape(rel_perf["index_name"])
    as_of = html.escape(rel_perf.get("as_of", ""))
    stage = _STAGE_LABEL.get(rel_perf.get("stage", ""), "")
    asof_suffix = f"{as_of}, {stage}" if stage else as_of

    return (
        '<p class="rel-perf">'
        f'금일: <span class="{_rel_perf_class(stock_pct)}">{stock_pct:+.2f}%</span>'
        f' │ {index_name}: <span class="{_rel_perf_class(index_pct)}">{index_pct:+.2f}%</span>'
        f' │ 알파: <span class="{_rel_perf_class(alpha_pp)}">{alpha_pp:+.2f}%pp</span>'
        f' <span class="rel-perf-asof">({asof_suffix})</span>'
        '</p>'
    )
```

- [ ] **Step 4: Wire into _render_stock_card**

In `src/report_generator.py`, in `_render_stock_card` (line 247-276), modify the function body:

Find this block:

```python
    sentiment_html = _render_sentiment(item.get("sentiment", {}))

    return f"""
    <div class="stock-card">
        <h3>{name} ({symbol_esc})</h3>
        <p class="stock-summary">현재가: <b>{sig['close']:,.2f}</b> | RSI: {sig['rsi']}
           | <span class="{signal_cls}">{sig['signal']}</span> (점수: {sig['score']})</p>
        <p class="stock-reasons">{', '.join(sig['reasons']) if sig['reasons'] else '특이사항 없음'}</p>
        {sentiment_html}
```

Replace with (add `rel_perf_html` computation + insertion line between reasons and sentiment):

```python
    sentiment_html = _render_sentiment(item.get("sentiment", {}))
    rel_perf_html = _render_rel_perf(item.get("rel_perf"))

    return f"""
    <div class="stock-card">
        <h3>{name} ({symbol_esc})</h3>
        <p class="stock-summary">현재가: <b>{sig['close']:,.2f}</b> | RSI: {sig['rsi']}
           | <span class="{signal_cls}">{sig['signal']}</span> (점수: {sig['score']})</p>
        <p class="stock-reasons">{', '.join(sig['reasons']) if sig['reasons'] else '특이사항 없음'}</p>
        {rel_perf_html}
        {sentiment_html}
```

- [ ] **Step 5: Run tests to verify pass**

```bash
.venv/bin/python -m pytest tests/test_report_generator.py::TestRenderRelPerf -v
```

Expected: 6 PASS

- [ ] **Step 6: Run full report_generator test suite for regression**

```bash
.venv/bin/python -m pytest tests/test_report_generator.py -v
```

Expected: all PASS

- [ ] **Step 7: Commit**

```bash
git add src/report_generator.py tests/test_report_generator.py
git commit -m "feat(rel-perf): _render_rel_perf + stock-card 한 줄 삽입"
```

---

## Task 5: CSS 추가

**Files:**
- Modify: `src/templates/report.css` (파일 끝에 append)

- [ ] **Step 1: Append CSS rules**

In `src/templates/report.css`, append at the end of the file:

```css

/* 시장 지수 대비 등락률 */
.rel-perf {
  margin: 4px 0;
  font-size: 0.9em;
  color: #555;
}

.rel-perf .up   { color: #28a745; font-weight: 600; }
.rel-perf .down { color: #dc3545; font-weight: 600; }
.rel-perf .flat { color: #6c757d; }

.rel-perf-asof {
  color: #999;
  font-size: 0.85em;
  margin-left: 6px;
}
```

- [ ] **Step 2: Verify CSS loads in generated report**

```bash
.venv/bin/python -c "
from src.report_generator import _load_css
css = _load_css()
assert '.rel-perf' in css
assert '.rel-perf .up' in css
print('CSS rules loaded:', css.count('.rel-perf'))
"
```

Expected: `CSS rules loaded: 5` (또는 그 이상)

- [ ] **Step 3: Commit**

```bash
git add src/templates/report.css
git commit -m "style(rel-perf): .rel-perf 색상/타이포 규칙 추가"
```

---

## Task 6: End-to-end 검증 (시각 확인)

**Files:** 없음 (수동 검증)

- [ ] **Step 1: 단일 종목 리포트 생성**

```bash
cd ~/Projects/stock-analyzer
.venv/bin/python main.py --symbol AAPL --output /tmp/rel_perf_test.html
```

Expected: `리포트 저장: /tmp/rel_perf_test.html` 로그.

- [ ] **Step 2: 생성된 HTML 확인 — rel-perf 줄 존재**

```bash
grep -c "rel-perf" /tmp/rel_perf_test.html
grep "금일:" /tmp/rel_perf_test.html | head -1
grep "알파:" /tmp/rel_perf_test.html | head -1
```

Expected:
- `grep -c "rel-perf"`: 5 이상 (CSS 정의 + 인스턴스)
- `grep "금일:"`: 한 줄에 `금일: +X.XX% │ S&P 500: +X.XX% │ 알파: +X.XX%pp (...)` 형태

만약 시장이 닫혀있고 인덱스 fetch가 실패해서 `rel_perf=None`이면 `grep "금일:"`이 비어 있을 수 있음. 그 경우 로그에서 `compute_relative_performance 실패` 경고 확인 — 정상 동작.

- [ ] **Step 3: 브라우저 시각 확인 (선택)**

```bash
open /tmp/rel_perf_test.html
```

Apple 카드에서 종목명 아래에 새 줄이 보이는지 확인:
- 양수면 녹색, 음수면 적색
- 알파 색깔이 알파 부호 기준
- 우측에 작은 회색 텍스트로 시각/stage 표시

- [ ] **Step 4: KR 종목으로 한 번 더**

```bash
.venv/bin/python main.py --symbol 005930.KS --output /tmp/rel_perf_test_kr.html
grep "금일:" /tmp/rel_perf_test_kr.html | head -1
```

Expected: `KOSPI` 표시가 인덱스명으로 들어가 있음.

- [ ] **Step 5: 전체 테스트 스위트 회귀 확인**

```bash
.venv/bin/python -m pytest tests/test_technical_analysis.py tests/test_report_generator.py tests/test_main.py -v
```

Expected: 모두 PASS.

- [ ] **Step 6: cleanup (선택)**

```bash
rm -f /tmp/rel_perf_test.html /tmp/rel_perf_test_kr.html
```

- [ ] **Step 7: Final commit (없으면 skip)**

이 Task는 검증만이므로 commit 없음. 모든 변경은 Task 1-5에서 이미 commit 됨.

---

## 완료 체크

- [ ] `_MARKET_INDEX`에 `kosdaq: '^KQ11'` 존재
- [ ] `resolve_index_market(symbol)`이 3개 분기를 정확히 처리
- [ ] `compute_relative_performance`가 7개 엣지 케이스 통과
- [ ] `analyze_stock` 결과 dict에 `rel_perf` 키 존재
- [ ] `_render_rel_perf`가 None/양수/음수/0 모두 정확히 렌더
- [ ] `report.css`에 `.rel-perf`/`.up`/`.down`/`.flat`/`.rel-perf-asof` 정의
- [ ] 단일 종목 생성 리포트에서 새 줄 시각 확인
- [ ] 기존 테스트 회귀 없음 (technical_analysis, report_generator, main)
