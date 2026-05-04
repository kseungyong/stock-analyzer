# 종목 추가 자동완성 검색 — 설계서

- **작성일**: 2026-05-04
- **대상 프로젝트**: stock-analyzer
- **변경 범위**: 웹 대시보드(`/`)의 종목 추가 폼

## 1. 배경

현재 대시보드(`src/web_app.py`의 `index()`)의 "종목 추가" 폼은 사용자가 심볼·종목명·시장을 직접 입력해야 한다. 사용자는 정확한 심볼(`AAPL`, `005930` 등)을 미리 알아야 하며, 한국 종목의 경우 `.KS`/`.KQ` 접미사 처리도 신경 써야 한다. 이 설계는 **회사명 또는 심볼 일부**로 검색해 후보를 드롭다운으로 보여주고, 클릭 한 번으로 폼이 채워지도록 한다.

## 2. 요구 사항

| 항목 | 결정 |
|------|------|
| UX | 인라인 자동완성 (타이핑 시 드롭다운) |
| 검색 범위 | 한국 + 미국 통합 (시장 자동 결정) |
| Trigger | 2자 이상 입력 시 300ms debounce |
| 결과 개수 | 최대 10개 |
| 클릭 동작 | 심볼·종목명·시장 자동 채움 (수동 수정 가능) |
| 키보드 | ↑↓/Enter/Esc |

## 3. 아키텍처

```
[index 폼]
    │ 사용자 타이핑 (debounce 300ms)
    ▼
GET /api/stocks/search?q=<query>          (web_app.py 새 라우트)
    │
    ▼
search_stocks(query, limit=10)            (src/stock_search.py 신규)
    ├── 한국: FDR KRX 캐시 (24h TTL) → substring 매칭
    └── 미국: yfinance Search → quotes
    │
    ▼
JSON: [{symbol, name, market}, ...]
    │
    ▼
드롭다운 렌더링 (textContent 기반, XSS 안전)
    │ 클릭 / Enter
    ▼
폼 input(symbol, name) + select(market) 자동 채움
```

## 4. 새 모듈: `src/stock_search.py`

### 4.1 공개 API

```python
def search_stocks(query: str, limit: int = 10) -> list[dict]:
    """회사명 또는 심볼로 한국·미국 종목을 검색한다.

    Returns:
        [{"symbol": str, "name": str, "market": "korea"|"us"}, ...]
        — 한국 종목 우선, 그 다음 미국. 심볼 기준 중복 제거.
    """
```

### 4.2 내부 동작

1. **입력 정규화**: `query.strip()`. 빈 문자열 또는 길이 < 2면 `[]` 즉시 반환.
2. **한국 검색** (`_search_kr`):
   - `_load_krx_cache()` — `FinanceDataReader.StockListing("KRX")` 결과를 모듈 전역 dict(`{symbol_with_suffix: name}`)로 캐싱. TTL 24시간 (마지막 로드 시각 비교). 락으로 동시 로드 방지.
   - 종목명 substring(대소문자 무시) 또는 심볼 prefix 매칭.
   - 심볼 정규화: KOSPI는 `.KS`, KOSDAQ은 `.KQ` 접미사. FDR `Market` 컬럼으로 결정.
   - 캐시 로드 실패 시: `logger.warning` 후 `[]` 반환 (다음 호출에서 재시도).
3. **미국 검색** (`_search_us`):
   - `yf.Search(query, max_results=8).quotes` 호출.
   - 각 quote에서 `symbol`, `shortname`/`longname` 추출.
   - `quoteType in {"EQUITY", "ETF"}` 만 통과.
   - 한국 거래소(`exchange in {"KSC", "KOE"}` 또는 `.KS`/`.KQ` 접미사)는 한국 결과와 중복되므로 미국 결과에서 제외.
   - 호출 실패: `logger.warning` 후 `[]` 반환.
4. **병합**: 한국 결과 먼저, 미국 결과 뒤. 심볼 기준 dedup. `[:limit]`.
5. **타임아웃**: 외부 API 호출에 5초 타임아웃 (yfinance 자체 timeout 옵션 또는 `concurrent.futures` 활용).

### 4.3 캐시 구조

```python
_krx_cache: dict[str, dict] = {"loaded_at": None, "data": {}}
_krx_lock = threading.Lock()
_KRX_TTL = 24 * 3600  # seconds
```

## 5. 백엔드 라우트 (`src/web_app.py`)

### 5.1 새 라우트

```python
@app.route("/api/stocks/search")
def api_stocks_search():
    q = request.args.get("q", "").strip()
    if len(q) > 50 or not _is_valid_search_query(q):
        return jsonify([])
    results = search_stocks(q, limit=10)
    return jsonify(results)
```

- **Sanitize**: `_is_valid_search_query` — 한글/영문/숫자/공백/하이픈/마침표만 허용 (정규식). 길이 50자 이하.
- **CSRF 미적용**: 읽기 전용 GET.
- **타임아웃**: `search_stocks` 내부에서 처리.

### 5.2 폼 변경

`index()` 안 `add_form` 영역의 심볼 필드를 다음 구조로 교체:

```html
<div class="field autocomplete-wrap" style="position:relative;">
  <label>검색</label>
  <input name="symbol" placeholder="종목명 또는 심볼 검색"
         autocomplete="off" required style="width:240px;">
  <div id="autocomplete-list" class="autocomplete-list"></div>
</div>
```

- `name="symbol"`은 그대로 유지 → 폼 제출 시 기존 `/stocks/add` 핸들러와 호환.
- 종목명 input (`name="name"`)·시장 select (`name="market"`)은 그대로 두되, JS가 자동 채움. 사용자는 수정 가능.
- 사용자가 검색 후 결과를 선택하지 않고 폼을 제출하면 입력 텍스트가 그대로 심볼로 전송되어 기존 `validate_stock_symbol` 검증에서 거부 → 에러 배너 표시 (현재 동작과 동일).

### 5.3 CSS 추가

기존 디자인 변수 재사용:

```css
.autocomplete-wrap { position: relative; }
.autocomplete-list {
  position: absolute; top: 100%; left: 0; right: 0;
  background: var(--white); border: 1px solid var(--slate-200);
  border-radius: var(--radius); box-shadow: var(--shadow-md);
  max-height: 280px; overflow-y: auto; z-index: 20;
  display: none;
}
.autocomplete-list.open { display: block; }
.autocomplete-item {
  padding: 8px 12px; cursor: pointer; display: flex;
  align-items: center; justify-content: space-between; gap: 8px;
  font-size: 0.875rem;
}
.autocomplete-item:hover, .autocomplete-item.active { background: var(--blue-50); }
.autocomplete-item .name { font-weight: 600; color: var(--slate-900); }
.autocomplete-item .symbol { font-family: 'Fira Code', monospace; font-size: 0.78rem; color: var(--slate-500); }
.autocomplete-empty { padding: 12px; color: var(--slate-500); font-size: 0.875rem; text-align: center; }
```

### 5.4 JavaScript

페이지 끝에 `<script>` 블록 추가. 의존성 없음 (vanilla JS).

```javascript
(() => {
  const input = document.querySelector('input[name="symbol"]');
  const nameInput = document.querySelector('input[name="name"]');
  const marketSel = document.querySelector('select[name="market"]');
  const list = document.getElementById('autocomplete-list');
  if (!input || !list) return;

  let timer = null;
  let activeIdx = -1;
  let items = [];

  function close() { list.classList.remove('open'); list.innerHTML = ''; activeIdx = -1; items = []; }

  function render(results) {
    list.innerHTML = '';
    if (results.length === 0) {
      const div = document.createElement('div');
      div.className = 'autocomplete-empty';
      div.textContent = '검색 결과 없음';
      list.appendChild(div);
    } else {
      results.forEach((r, i) => {
        const it = document.createElement('div');
        it.className = 'autocomplete-item';
        const left = document.createElement('div');
        const name = document.createElement('span'); name.className = 'name'; name.textContent = r.name;
        const sym = document.createElement('span'); sym.className = 'symbol'; sym.textContent = ' ' + r.symbol;
        left.appendChild(name); left.appendChild(sym);
        const badge = document.createElement('span');
        badge.className = 'badge ' + (r.market === 'korea' ? 'badge-korea' : 'badge-us');
        badge.textContent = r.market === 'korea' ? '한국' : '미국';
        it.appendChild(left); it.appendChild(badge);
        it.addEventListener('mousedown', (e) => { e.preventDefault(); pick(i); });
        list.appendChild(it);
      });
    }
    list.classList.add('open');
    items = results;
  }

  function pick(idx) {
    const r = items[idx]; if (!r) return;
    input.value = r.symbol;
    nameInput.value = r.name;
    marketSel.value = r.market;
    close();
  }

  function highlight(idx) {
    [...list.querySelectorAll('.autocomplete-item')].forEach((el, i) =>
      el.classList.toggle('active', i === idx));
  }

  input.addEventListener('input', () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 2) { close(); return; }
    timer = setTimeout(async () => {
      try {
        const res = await fetch('/api/stocks/search?q=' + encodeURIComponent(q));
        if (!res.ok) { close(); return; }
        render(await res.json());
      } catch { close(); }
    }, 300);
  });

  input.addEventListener('keydown', (e) => {
    if (!list.classList.contains('open') || items.length === 0) return;
    if (e.key === 'ArrowDown') { e.preventDefault(); activeIdx = (activeIdx + 1) % items.length; highlight(activeIdx); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); activeIdx = (activeIdx - 1 + items.length) % items.length; highlight(activeIdx); }
    else if (e.key === 'Enter' && activeIdx >= 0) { e.preventDefault(); pick(activeIdx); }
    else if (e.key === 'Escape') { close(); }
  });

  document.addEventListener('click', (e) => {
    if (!input.contains(e.target) && !list.contains(e.target)) close();
  });
})();
```

## 6. 보안

- **검색 쿼리 sanitize**: 화이트리스트 정규식 (한글/영문/숫자/공백/하이픈/마침표). 위반 시 빈 배열.
- **XSS**: 드롭다운 렌더링은 모두 `textContent` (innerHTML 미사용).
- **응답 형식**: JSON only. HTML 미포함.
- **CSRF**: 읽기 전용 GET이므로 미적용 (기존 정책과 일치).
- **Rate limiting**: 본 설계 범위 외. 외부 API 캐싱(KRX 24h)으로 자체 호출 부담은 작음.

## 7. 에러 처리

| 상황 | 동작 |
|------|------|
| 한쪽 소스 호출 실패 | 다른 소스 결과만 반환. `logger.warning`. |
| 양쪽 모두 실패 | 빈 배열 반환. 드롭다운에 "검색 결과 없음" 표시. |
| KRX 캐시 첫 로드 실패 | 빈 배열 반환. `loaded_at`을 갱신하지 않아 다음 호출에서 재시도. |
| 잘못된 쿼리 (특수문자) | 빈 배열 반환. |
| Fetch 실패 (network) | 클라이언트에서 close. 사용자에 별도 메시지 미표시. |

## 8. 테스트

### 8.1 `tests/test_stock_search.py` (신규)

- `test_short_query_returns_empty`: 길이 0/1 입력 시 빈 배열.
- `test_kr_substring_match`: KRX 캐시 mock으로 "삼성" 검색 시 삼성전자/삼성SDI 등 반환.
- `test_kr_market_suffix`: KOSPI는 `.KS`, KOSDAQ은 `.KQ` 접미사 부여.
- `test_us_search`: `yf.Search` mock으로 "Apple" 검색 시 AAPL 반환.
- `test_us_excludes_korean_exchange`: yfinance가 한국 거래소 quote를 반환해도 미국 결과에서 제외.
- `test_dedup_by_symbol`: 한·미 결과에 동일 심볼 있을 때 한 번만.
- `test_korea_first_ordering`: 한국 결과가 미국 결과보다 앞.
- `test_limit_respected`: limit=5일 때 5개 이하.
- `test_kr_cache_failure`: FDR 호출이 예외 던지면 빈 배열, 캐시 갱신 안 됨.
- `test_us_failure_returns_kr_only`: yfinance 실패해도 KR 결과 반환.

### 8.2 `tests/test_web_app.py` (수정)

- `test_api_stocks_search_basic`: `search_stocks` mock으로 `/api/stocks/search?q=삼성` → 200, JSON 배열.
- `test_api_stocks_search_short_query`: `q=ㅅ` → `[]`.
- `test_api_stocks_search_invalid_chars`: 특수문자 입력 → `[]`.
- `test_api_stocks_search_too_long`: 51자 입력 → `[]`.

## 9. 작업 분할

| 단계 | 파일 | 비고 |
|------|------|------|
| 1 | `src/stock_search.py` 작성 | KRX 캐시 + yf.Search 통합 |
| 2 | `tests/test_stock_search.py` 작성 | 외부 API mock |
| 3 | `src/web_app.py` 라우트 추가 | `/api/stocks/search` |
| 4 | `src/web_app.py` 폼 수정 | CSS + JS + 마크업 |
| 5 | `tests/test_web_app.py` 보강 | API endpoint 테스트 |
| 6 | 수동 검증 | `python main.py --web` 후 브라우저에서 "삼성", "Apple" 등 입력 |

## 10. YAGNI / 비포함 사항

- 검색 결과 정렬 가중치(인기도, 거래량) — 단순 substring 매칭만.
- 검색 히스토리 저장.
- ETF/지수 별도 필터 UI.
- 다국어 종목명 동시 매칭 (한글 ↔ 영문). 향후 필요 시 추가.
- 자동 번역 — 미국 종목은 영문 그대로 표시. 사용자가 폼에서 직접 수정 가능.
