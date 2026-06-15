# 시세(일봉) 소스 토스 candles 교체 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 일봉 시세 1차 소스를 yfinance 에서 토스증권 candles API 로 교체하고, FDR 폴백을 유지한다.

**Architecture:** `toss_client.py`(기존 TossClient)에 `fetch_candles`(페이지네이션 I/O) 추가. `data_fetcher.py` 에 `_candles_to_df`(순수 변환)와 `_fetch_with_toss`(조합)를 추가하고, `fetch_stock_data` 를 토스 1차 → FDR 폴백으로 바꾼다(yfinance 제거). 반환 DataFrame 계약(`Open/High/Low/Close/Volume`, tz-naive 오름차순 index)은 불변 — 소비자 무수정.

**Tech Stack:** Python 3, httpx/requests, pandas, FinanceDataReader(폴백), 토스 OAuth2(기존 TossClient).

**Spec:** `docs/superpowers/specs/2026-06-15-toss-price-source-design.md`

---

## File Structure

| 파일 | 역할 | 신규/수정 |
|------|------|-----------|
| `src/toss_client.py` | `_get` 에 params 인자 + `fetch_candles` 페이지네이션 | 수정 |
| `src/data_fetcher.py` | `_candles_to_df`, `_fetch_with_toss`, `fetch_stock_data` 토스 1차화 | 수정 |
| `tests/test_toss_client.py` | fetch_candles 페이지네이션 테스트 | 수정 |
| `tests/test_data_fetcher.py` | 변환/폴백 테스트 | 수정 |

---

## Task 1: toss_client.fetch_candles (페이지네이션)

**Files:**
- Modify: `src/toss_client.py` (`_get` 에 params 인자 추가 + `fetch_candles`)
- Test: `tests/test_toss_client.py`

기존 `TossClient._get(path, extra_headers)` 는 query params 를 안 받는다. candles 는 params 필요 → `_get` 에 `params` 인자를 추가(기존 호출자 fetch_accounts/fetch_holdings 는 params 미사용이라 호환). `fetch_candles` 는 nextBefore 커서로 페이지네이션해 count 개까지 수집.

- [ ] **Step 1: 페이지네이션 테스트 작성**

`tests/test_toss_client.py` 에 추가. `TossClient._get` 을 monkeypatch 해 2페이지 응답을 시뮬레이션:

```python
def test_fetch_candles_paginates(monkeypatch):
    calls = []
    pages = [
        {"candles": [{"t": i} for i in range(200)], "nextBefore": "CURSOR1"},
        {"candles": [{"t": i} for i in range(200, 250)], "nextBefore": None},
    ]
    def fake_get(self, path, params=None, extra_headers=None):
        calls.append(params)
        return pages[len(calls) - 1]
    monkeypatch.setattr(tc.TossClient, "_get", fake_get)
    monkeypatch.setattr(tc.TossClient, "__init__", lambda self: None)  # 자격증명 우회

    client = tc.TossClient()
    result = client.fetch_candles("005930", interval="1d", count=240)
    assert len(result) == 240            # 200 + 50 중 240 개로 트림
    assert calls[0]["before"] is None or "before" not in calls[0]  # 1페이지 커서 없음
    assert calls[1]["before"] == "CURSOR1"  # 2페이지 커서 전달


def test_fetch_candles_stops_on_null_cursor(monkeypatch):
    def fake_get(self, path, params=None, extra_headers=None):
        return {"candles": [{"t": 1}], "nextBefore": None}
    monkeypatch.setattr(tc.TossClient, "_get", fake_get)
    monkeypatch.setattr(tc.TossClient, "__init__", lambda self: None)
    client = tc.TossClient()
    result = client.fetch_candles("005930", count=200)
    assert len(result) == 1   # nextBefore=null → 1페이지서 종료
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_toss_client.py::test_fetch_candles_paginates -v`
Expected: FAIL — `AttributeError: ... 'fetch_candles'`

- [ ] **Step 3: `_get` 에 params 추가 + `fetch_candles` 구현**

`src/toss_client.py` 의 `_get` 시그니처/본문 수정 (params 추가):

```python
    def _get(self, path: str, params: dict | None = None, extra_headers: dict | None = None):
        self._throttle()
        token = self._ensure_token()
        headers = {"authorization": f"Bearer {token}"}
        if extra_headers:
            headers.update(extra_headers)
        resp = _http_get(f"{_BASE_URL}{path}", headers=headers, params=params)
        if resp.status_code == 401:
            # TTL 미만이지만 서버가 거부한 토큰 — unlink 가 _ensure_token 재발급을 강제 (load-bearing)
            logger.warning("토스 401 — 토큰 재발급")
            self._token = None
            try:
                _TOKEN_CACHE.unlink()
            except OSError:
                pass
            headers["authorization"] = f"Bearer {self._ensure_token()}"
            resp = _http_get(f"{_BASE_URL}{path}", headers=headers, params=params)
        resp.raise_for_status()
        return _unwrap(resp.json())
```

`fetch_candles` 추가 (클래스 내부, fetch_holdings 다음):

```python
    def fetch_candles(self, symbol: str, interval: str = "1d", count: int = 200) -> list[dict]:
        """일봉 candles 를 count 개까지 수집 (nextBefore 커서 페이지네이션, 최신순 유지).

        반환: raw candle dict 리스트 (변환 안 함). 토스 응답 candles[] 그대로.
        """
        collected: list[dict] = []
        before: str | None = None
        for _ in range(10):  # 무한루프 가드 (최대 10페이지 = 2000봉)
            params = {"symbol": symbol, "interval": interval, "count": 200}
            if before:
                params["before"] = before
            result = self._get("/api/v1/candles", params=params)
            candles = result.get("candles", []) if isinstance(result, dict) else []
            if not candles:
                break
            collected.extend(candles)
            next_before = result.get("nextBefore")
            if not next_before or next_before == before or len(collected) >= count:
                break
            before = next_before
        return collected[:count]
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_toss_client.py -v`
Expected: PASS (기존 + 신규 2 passed)

- [ ] **Step 5: Commit**

```bash
git add src/toss_client.py tests/test_toss_client.py
git commit -m "feat(toss): fetch_candles 페이지네이션 + _get params 지원"
```

---

## Task 2: data_fetcher._candles_to_df (순수 변환)

**Files:**
- Modify: `src/data_fetcher.py` (`_candles_to_df`)
- Test: `tests/test_data_fetcher.py`

토스 candle 리스트 → yfinance 스타일 DataFrame. 토스 API 없이 테스트 가능한 순수 함수.

- [ ] **Step 1: 변환 테스트 작성**

`tests/test_data_fetcher.py` 에 추가:

```python
import pandas as pd
from src import data_fetcher as df_mod


def test_candles_to_df_converts_and_sorts():
    # 토스는 최신순 — 변환 후 오름차순 정렬돼야 함
    candles = [
        {"timestamp": "2026-06-15T00:00:00.000+09:00", "openPrice": "337500",
         "highPrice": "345000", "lowPrice": "334500", "closePrice": "337500",
         "volume": "27018131", "currency": "KRW"},
        {"timestamp": "2026-06-12T00:00:00.000+09:00", "openPrice": "310000",
         "highPrice": "327500", "lowPrice": "300000", "closePrice": "327000",
         "volume": "52941179", "currency": "KRW"},
    ]
    df = df_mod._candles_to_df(candles)
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert df.index.tz is None                      # tz-naive
    assert list(df.index) == sorted(df.index)       # 오름차순
    assert df["Close"].iloc[-1] == 337500.0         # 최신이 마지막
    assert df["Open"].iloc[0] == 310000.0           # 과거가 처음
    assert df["Close"].dtype == float


def test_candles_to_df_us_decimal_prices():
    candles = [{"timestamp": "2026-06-15T13:00:00.000+09:00", "openPrice": "293",
                "highPrice": "294.34", "lowPrice": "290.45", "closePrice": "292.6",
                "volume": "110158", "currency": "USD"}]
    df = df_mod._candles_to_df(candles)
    assert df["High"].iloc[0] == 294.34             # 소수점 보존


def test_candles_to_df_empty_raises():
    import pytest
    with pytest.raises(ValueError):
        df_mod._candles_to_df([])
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_data_fetcher.py::test_candles_to_df_converts_and_sorts -v`
Expected: FAIL — `AttributeError: ... '_candles_to_df'`

- [ ] **Step 3: `_candles_to_df` 구현**

`src/data_fetcher.py` 에 추가 (`_to_krx_code` 다음):

```python
def _candles_to_df(candles: list[dict]) -> pd.DataFrame:
    """토스 candle 리스트 → yfinance 스타일 DataFrame (Open/High/Low/Close/Volume).

    index: timestamp → tz-naive, 날짜 normalize, 오름차순 정렬.
    빈 입력은 ValueError (fetch_stock_data 가 폴백 트리거).
    """
    if not candles:
        raise ValueError("토스 candles 비어있음")
    idx = pd.to_datetime([c["timestamp"] for c in candles])
    data = {
        "Open": [float(c["openPrice"]) for c in candles],
        "High": [float(c["highPrice"]) for c in candles],
        "Low": [float(c["lowPrice"]) for c in candles],
        "Close": [float(c["closePrice"]) for c in candles],
        "Volume": [float(c["volume"]) for c in candles],
    }
    df = pd.DataFrame(data, index=idx)
    # tz-aware(+09:00) → tz-naive + 날짜 normalize (시각 제거, 일봉이므로 날짜만 유효)
    df.index = df.index.tz_localize(None).normalize()
    return df.sort_index()
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_data_fetcher.py -k candles_to_df -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/data_fetcher.py tests/test_data_fetcher.py
git commit -m "feat(price): 토스 candles → DataFrame 순수 변환"
```

---

## Task 3: data_fetcher._fetch_with_toss

**Files:**
- Modify: `src/data_fetcher.py` (`_required_count`, `_fetch_with_toss`)
- Test: `tests/test_data_fetcher.py`

period_days → count 추정 + TossClient.fetch_candles 호출 + 변환. `.KS/.KQ` 는 `_to_krx_code` 로 제거, 미국은 그대로.

- [ ] **Step 1: 테스트 작성 (TossClient mock)**

`tests/test_data_fetcher.py` 에 추가:

```python
def test_fetch_with_toss_strips_kr_suffix(monkeypatch):
    captured = {}
    class FakeClient:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def fetch_candles(self, symbol, interval="1d", count=200):
            captured["symbol"] = symbol
            captured["count"] = count
            return [{"timestamp": "2026-06-15T00:00:00.000+09:00", "openPrice": "1",
                     "highPrice": "1", "lowPrice": "1", "closePrice": "1", "volume": "1"}]
    monkeypatch.setattr(df_mod, "TossClient", lambda: FakeClient())
    df = df_mod._fetch_with_toss("005930.KS", period_days=365)
    assert captured["symbol"] == "005930"        # .KS 제거
    assert captured["count"] >= 250              # 365일 ≈ 최소 250 거래일분
    assert not df.empty


def test_fetch_with_toss_us_passthrough(monkeypatch):
    captured = {}
    class FakeClient:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def fetch_candles(self, symbol, interval="1d", count=200):
            captured["symbol"] = symbol
            return [{"timestamp": "2026-06-15T13:00:00.000+09:00", "openPrice": "1",
                     "highPrice": "1", "lowPrice": "1", "closePrice": "1", "volume": "1"}]
    monkeypatch.setattr(df_mod, "TossClient", lambda: FakeClient())
    df_mod._fetch_with_toss("AAPL", period_days=365)
    assert captured["symbol"] == "AAPL"          # 변환 없음


def test_fetch_with_toss_empty_raises(monkeypatch):
    import pytest
    class FakeClient:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def fetch_candles(self, symbol, interval="1d", count=200):
            return []
    monkeypatch.setattr(df_mod, "TossClient", lambda: FakeClient())
    with pytest.raises(ValueError):
        df_mod._fetch_with_toss("005930.KS", period_days=365)
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_data_fetcher.py -k fetch_with_toss -v`
Expected: FAIL — `AttributeError: ... '_fetch_with_toss'` 또는 `TossClient`

- [ ] **Step 3: import + 구현**

`src/data_fetcher.py` 상단 import 에 추가:

```python
from src.toss_client import TossClient
```

`src/data_fetcher.py` 에 추가 (`_candles_to_df` 다음):

```python
def _required_count(period_days: int) -> int:
    """period_days(캘린더 일수) → 필요한 거래일 봉 수 추정. 영업일 비율 ~0.69 에 여유."""
    return int(period_days * 0.75) + 10


def _fetch_with_toss(symbol: str, period_days: int) -> pd.DataFrame:
    """토스 candles 로 일봉 수집 → DataFrame. .KS/.KQ 는 6자리 코드로 변환."""
    toss_symbol = _to_krx_code(symbol) if symbol.endswith((".KS", ".KQ")) else symbol
    count = _required_count(period_days)
    with TossClient() as client:
        candles = client.fetch_candles(toss_symbol, interval="1d", count=count)
    return _candles_to_df(candles)   # 빈 candles 면 ValueError
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_data_fetcher.py -k fetch_with_toss -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/data_fetcher.py tests/test_data_fetcher.py
git commit -m "feat(price): _fetch_with_toss (symbol 변환 + count 추정)"
```

---

## Task 4: fetch_stock_data 토스 1차화 + FDR 폴백

**Files:**
- Modify: `src/data_fetcher.py:66-104` (`fetch_stock_data` 본문 교체)
- Test: `tests/test_data_fetcher.py`

yfinance 1차를 토스 1차로 교체. FDR 폴백 유지. 토스 실패/자격증명 미설정 → FDR.

- [ ] **Step 1: 폴백 테스트 작성**

`tests/test_data_fetcher.py` 에 추가:

```python
def test_fetch_stock_data_uses_toss_first(monkeypatch):
    toss_df = pd.DataFrame(
        {"Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [1.0], "Volume": [1.0]},
        index=pd.to_datetime(["2026-06-15"]),
    )
    monkeypatch.setattr(df_mod, "_fetch_with_toss", lambda s, p: toss_df)
    fdr_called = {"n": 0}
    monkeypatch.setattr(df_mod, "_fetch_with_fdr",
                        lambda s, st, e: fdr_called.__setitem__("n", fdr_called["n"] + 1))
    out = df_mod.fetch_stock_data("005930.KS", period_days=365, retries=0)
    assert not out.empty
    assert fdr_called["n"] == 0          # 토스 성공 → FDR 미호출


def test_fetch_stock_data_falls_back_to_fdr(monkeypatch):
    def boom(s, p):
        raise RuntimeError("토스 다운")
    monkeypatch.setattr(df_mod, "_fetch_with_toss", boom)
    fdr_df = pd.DataFrame(
        {"Open": [2.0], "High": [2.0], "Low": [2.0], "Close": [2.0], "Volume": [2.0]},
        index=pd.to_datetime(["2026-06-15"]),
    )
    monkeypatch.setattr(df_mod, "_fetch_with_fdr", lambda s, st, e: fdr_df)
    monkeypatch.setattr(df_mod.time, "sleep", lambda x: None)  # 재시도 sleep 제거
    out = df_mod.fetch_stock_data("005930.KS", period_days=365, retries=1)
    assert out["Close"].iloc[0] == 2.0   # FDR 결과


def test_fetch_stock_data_missing_creds_skips_retries(monkeypatch):
    def no_creds(s, p):
        raise RuntimeError("TOSS_CLIENT_ID/TOSS_CLIENT_SECRET 미설정")
    monkeypatch.setattr(df_mod, "_fetch_with_toss", no_creds)
    fdr_df = pd.DataFrame(
        {"Open": [3.0], "High": [3.0], "Low": [3.0], "Close": [3.0], "Volume": [3.0]},
        index=pd.to_datetime(["2026-06-15"]),
    )
    monkeypatch.setattr(df_mod, "_fetch_with_fdr", lambda s, st, e: fdr_df)
    slept = {"n": 0}
    monkeypatch.setattr(df_mod.time, "sleep", lambda x: slept.__setitem__("n", slept["n"] + 1))
    out = df_mod.fetch_stock_data("005930.KS", period_days=365, retries=2)
    assert out["Close"].iloc[0] == 3.0       # FDR 폴백
    assert slept["n"] == 0                    # 자격증명 미설정 → 재시도 없이 즉시 폴백
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_data_fetcher.py -k fetch_stock_data -v`
Expected: FAIL — 현재 fetch_stock_data 가 yfinance(yf) 를 직접 호출하므로 mock 안 걸려 네트워크 시도/실패

- [ ] **Step 3: fetch_stock_data 본문 교체**

`src/data_fetcher.py:66-104` 의 함수를 통째로 교체:

```python
def fetch_stock_data(symbol: str, period_days: int = 365, retries: int = 2) -> pd.DataFrame:
    """주가 데이터를 토스 candles 로 수집하고, 실패 시 FinanceDataReader 로 폴백한다.

    Args:
        symbol: 종목 코드 (예: '005930.KS', 'AAPL')
        period_days: 수집할 과거 일수
        retries: 토스 실패 시 재시도 횟수

    Returns:
        OHLCV 데이터프레임 (Open/High/Low/Close/Volume, tz-naive 오름차순 index)
    """
    end = datetime.now()
    start = end - timedelta(days=period_days)

    # 1차: 토스 candles 시도
    for attempt in range(retries + 1):
        try:
            df = _fetch_with_toss(symbol, period_days)
            if df.empty:
                raise ValueError(f"토스 데이터 없음 {symbol}")
            logger.info("데이터 수집 완료 [토스]: %s", symbol)
            return df
        except Exception as exc:
            if "TOSS_CLIENT" in str(exc):
                # 자격증명 미설정 — 재시도 무의미, 즉시 FDR 폴백 (CI/키 없는 환경)
                logger.warning("토스 자격증명 미설정 [%s] — FDR 폴백", symbol)
                break
            if attempt < retries:
                time.sleep(1)
            else:
                logger.warning("토스 실패 [%s]: %s — FinanceDataReader로 폴백", symbol, exc)

    # 2차: FinanceDataReader 폴백
    try:
        df = _fetch_with_fdr(symbol, start, end)
        logger.info("데이터 수집 완료 [FinanceDataReader]: %s", symbol)
        return df
    except Exception as fdr_exc:
        raise ValueError(
            f"토스 및 FinanceDataReader 모두 실패 [{symbol}]: {fdr_exc}"
        ) from fdr_exc
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_data_fetcher.py -v`
Expected: PASS (전체 — 기존 + 신규)

- [ ] **Step 5: Commit**

```bash
git add src/data_fetcher.py tests/test_data_fetcher.py
git commit -m "feat(price): fetch_stock_data 토스 1차 + FDR 폴백 (yfinance 제거)"
```

---

## Task 5: yfinance import 정리 + 실거래 검증 + 배포

**Files:**
- Modify: `src/data_fetcher.py` (미사용 yfinance import 처리)

- [ ] **Step 1: yfinance 잔여 사용 확인**

Run: `grep -n "yf\.\|yfinance" src/data_fetcher.py`
- `fetch_news` 등 다른 함수가 `yf` 를 쓰면 import 유지. fetch_stock_data 만 쓰던 거면 제거 가능.
- **판단**: `grep -rn "import yfinance\|yf\." src/` 로 전체 확인. data_fetcher 의 fetch_news 가 yf 를 쓰므로(미국 뉴스) import 유지 가능성 높음. 쓰는 곳이 남아있으면 import 제거하지 말 것. 제거는 선택 — 안전하게 유지.

- [ ] **Step 2: 실제 토스 경로 스모크 (자격증명 있을 때)**

Run:
```bash
.venv/bin/python -c "
from src.data_fetcher import fetch_stock_data
for sym in ['005930.KS', '0193W0.KS', 'AAPL']:
    df = fetch_stock_data(sym, period_days=60)
    print(sym, 'rows=', len(df), 'last_close=', df['Close'].iloc[-1], 'cols=', list(df.columns))
"
```
Expected: 3 종목 모두 rows>0, 특히 `0193W0.KS`(신코드) 성공 — 토스 경로 확인. 컬럼 `['Open','High','Low','Close','Volume']`.

- [ ] **Step 3: 전체 회귀**

Run: `.venv/bin/python -m pytest tests/test_data_fetcher.py tests/test_toss_client.py tests/test_technical_analysis.py -q`
Expected: 전부 PASS (소비자 계약 유지 확인). test_technical_analysis 가 없으면 생략.

- [ ] **Step 4: push + 서버 배포**

```bash
git push origin main
ssh 100.87.151.104 'cd ~/Projects/stock-analyzer && git pull --ff-only origin main && launchctl kickstart -k gui/$(id -u)/ai.stock-analyzer.web'
```

- [ ] **Step 5: 서버 신코드 검증**

```bash
ssh 100.87.151.104 'cd ~/Projects/stock-analyzer && .venv/bin/python -c "
from src.data_fetcher import fetch_stock_data
df = fetch_stock_data(\"0193W0.KS\", period_days=60)
print(\"0193W0 rows=\", len(df), \"last=\", df[\"Close\"].iloc[-1])
"'
```
Expected: 토스 경로로 신코드 시세 수집 성공.

---

## Self-Review (작성자 체크)

- **Spec coverage**: §2 candles API(Task 1) · §3 아키텍처(Task 1-4) · §4 변환(Task 2) ·
  §5 폴백 흐름(Task 4) · §6 페이지네이션(Task 1) · §7 에러처리(Task 4) · §8 테스트(각 Task) ·
  §9 호환성(Task 4 계약 유지 + Task 5 회귀) · §10 배포(Task 5) — 전부 커버.
- **Placeholder**: 없음. yfinance import 제거는 "잔여 사용 확인 후 판단"으로 명시 지시(grep 포함).
- **Type 일관성**: `fetch_candles(symbol, interval, count) -> list[dict]`, `_candles_to_df(candles) -> DataFrame`,
  `_fetch_with_toss(symbol, period_days) -> DataFrame`, `_required_count(period_days) -> int` 시그니처가
  Task 간 일치. `TossClient` 는 toss_client 에서 import (Task 3).
- **자격증명 미설정 폴백**: Task 4 가 `"TOSS_CLIENT" in str(exc)` 분기로 재시도 없이 즉시 FDR
  폴백 (spec §5 "자격증명 미설정 → FDR 직행" 충족). `test_fetch_stock_data_missing_creds_skips_retries`
  가 sleep 미호출(재시도 없음)을 검증.
