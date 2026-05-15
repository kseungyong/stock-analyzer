# Pattern Badge Modal — Design Spec

날짜: 2026-05-15
프로젝트: stock-analyzer
상태: design (사용자 검토 대기)

## 1. Goal

종목 분석 페이지에서 노출되는 패턴 배지 (잉태형, 더블바텀 등) 를 클릭하면 모달 팝업으로 (1) 해당 패턴의 표준 모양 교과서 일러스트와 (2) 해당 종목에서 검출된 실제 차트 위치를 함께 보여주어 사용자가 즉시 학습/확인할 수 있게 한다.

## 2. Scope

### In scope
- 챠트 패턴 5종 (`src/pattern_chart.py`): 더블바텀(W), 더블탑(M), 헤드앤숄더(H&S), 역헤드앤숄더(역H&S), 삼각형
- 캔들 패턴 60+ 종 (`src/pattern_candle.py`, talib 기반): 도지, 망치, 잉태형, 장악형, 적삼병, 흑삼병, 헤드앤숄더 등 — talib `CDL*` 코드 모두 매핑되어 있음 (`_CANDLE_NAMES` dict 참조)
- **티어드 콘텐츠 전략**: 모든 패턴 이름이 lookup 매핑되지만, 시각 콘텐츠 품질은 2단계
  - **Tier 1 (rich)**: 상위 20종 — 고유 SVG 일러스트 + 3-5줄 상세 설명 (가장 자주 보이는 패턴)
  - **Tier 2 (generic)**: 나머지 ~40종 — signal 방향별 6개 generic SVG 템플릿 중 1개 + 1-2줄 짧은 설명
  - 미정의 패턴 fallback (이론적으로 발생 안 해야 함): "패턴 정보 준비 중" 안내 메시지
- 클릭 가능 영역: 대시보드 종목 목록 배지 (web_app.py:1519) + 종목 상세 페이지 패턴 분석 카드 (web_app.py:1880~)
- 모달: 교과서 탭 (즉시 표시) + 실제 차트 탭 (lazy fetch on tab click)
- 동일 패턴 다중 검출 케이스 처리: `date` 파라미터로 식별

### Out of scope
- 패턴 검출 로직 수정 (기존 `pattern_chart.py` / `pattern_candle.py` 그대로)
- 다국어 지원 (한글만)
- 모바일 전용 최적화 (반응형 max-width 적용은 하되 모바일 UX 별도 디자인 X)
- 디스크 캐싱 (path traversal 위험 회피, 메모리 LRU 만)
- 패턴 시그널/스코어 표시 (이미 배지에 있음, 모달은 학습 콘텐츠에 집중)
- 사용자별 즐겨찾기/메모

## 3. Non-functional requirements

- 모달 오픈 즉시 (<100ms) 교과서 탭 표시. 실제 차트 탭은 클릭 시 fetch (<500ms 응답 목표)
- 동시 차트 생성 요청 처리 (Gunicorn 워커 부하 고려) — matplotlib 호출은 메모리 LRU cache 로 동일 (symbol, pattern, date) 재사용
- 모달 자체는 페이지 reload 없이 동작
- 폰트는 시스템 기본 (별도 폰트 도입 X)

## 4. Architecture

### 4.1 데이터 소스

기존 그대로:
- `pattern_json` 컬럼 — chart_patterns (좌표 풍부), candles (date+name+signal)
- yfinance OHLC history (실제 차트 탭에서 60일치 fetch)

확장:
- `src/data/pattern_metadata.yaml` (신규) — 15개 패턴의 (1) 한글 이름 → 매핑 키, (2) inline SVG 마크업, (3) 짧은 설명 텍스트 (형성 조건 + 신호 의미 + 검출 조건). 빌드 단계 없이 런타임 lookup.

### 4.2 백엔드 라우트 2개

```
GET /api/pattern-popup/textbook?pattern=<name_kor>
GET /api/pattern-popup/actual?symbol=<sym>&pattern=<name_kor>[&date=<YYYY-MM-DD>]
```

`date` 는 optional. 미지정 시 해당 (symbol, pattern) 의 **가장 최근 검출** 사용 (대시보드 배지에서 호출되는 케이스 — 거기서는 날짜 컨텍스트가 없음).

**textbook** — 즉시 응답, 정적 lookup:
```json
{
  "pattern": "더블바텀(W)",
  "svg": "<svg viewBox=\"0 0 200 100\">...</svg>",
  "description_html": "<p>두 저점이 비슷한 가격에서 형성되고 ...</p>",
  "signal_typical": "매수"
}
```
- `pattern` 미매칭: 404
- description_html 은 우리가 작성한 정적 텍스트만 — XSS 위험 없음

**actual** — lazy, matplotlib PNG 생성:
```json
{
  "symbol": "005930.KS",
  "name_kor": "삼성전자",
  "pattern": "더블바텀(W)",
  "date": "2026-05-15",
  "chart_b64": "<base64 PNG>",
  "caption": "11/15 저점1 65,200 → 12/08 저점2 65,500 — 넥라인 68,400 돌파",
  "signal_at_detection": "매수"
}
```
- OHLC 데이터 없거나 fetch 실패: 200 + `chart_b64=null, caption="차트 데이터 없음"` 응답
- `pattern_json` 에서 해당 (pattern, date) 검출 row 없으면: 404
- 동시 동일 요청: 메모리 LRU 가 첫 응답 후 캐싱, 후속 요청은 즉시 응답 (TTL 1시간)

### 4.3 차트 마킹 (실제 차트 탭)

차트는 1-subplot (가격만, RSI/MACD 제외 — 모달 학습 목적에 무관). 60일 OHLC line chart.

**챠트 패턴 마킹** — `pattern_json.chart_patterns[i]` 에서 추출:
- 주요 좌표 (예: 더블바텀의 `low1/low2`, H&S 의 3 peaks) 에 컬러 마커 (○) + 라벨
- `neckline` 가격에 수평 점선 (회색)
- `from_date` ~ `to_date` 범위에 옅은 노란색 음영 박스
- `current` 가격 우상단 텍스트 ("현재 X,XXX")
- 돌파/이탈 여부 (`breakout`/`breakdown`) 에 따라 마커 색상 (녹/빨)

**캔들 패턴 마킹** — `pattern_json.candles[i]` 에서 추출:
- 해당 `date` 위치에 수직 점선
- 봉 위쪽에 화살표 + 패턴 이름 라벨 (예: `↓ 잉태형`)
- 봉 자체에 outline 강조 (signal 에 따라 녹/빨/회)

마킹 데이터 출처 원칙: **저장된 좌표를 그대로 그림 (재검출 X)**. `pattern_json` 에 충분히 들어 있음을 4.4 절에서 확인.

### 4.4 pattern_json 좌표 충분성 검증

`pattern_chart.py` 의 각 detector 가 반환하는 dict (이미 DB 저장):

| 패턴 | 필수 좌표 | 검증 |
|---|---|---|
| 더블바텀(W) | `low1{date,price}`, `low2{date,price}`, `neckline`, `current`, `breakout` | ✓ 전부 있음 (`_detect_double_bottom`) |
| 더블탑(M) | `high1`, `high2`, `neckline`, `current`, `breakdown` | ✓ 전부 있음 |
| 헤드앤숄더 | `left_shoulder`, `head`, `right_shoulder`, `neckline`, `current` | ⚠️ 점검 필요 — 현재 코드 미확인. 누락 시 detector 보강 (out-of-scope 단, 마킹 불가 시 fallback 안내 메시지) |
| 역헤드앤숄더 | 동일 (inverse) | ⚠️ 동일 점검 |
| 삼각형 | `upper_trend_line`, `lower_trend_line`, `current` | ⚠️ 점검 필요 |

캔들 패턴: `name`, `signal`, `date`, `code`, `value` — 마킹에 충분.

**Risk**: H&S/역H&S/삼각형 detector 의 좌표 풍부도 미확인. 구현 시 데이터 부족 발견되면 두 가지 선택:
1. detector 보강 (좌표 추가 반환) — 검출 로직 변경, scope 초과
2. 마킹 일부만 표시 + 나머지는 caption 텍스트로 — 마킹 시각화는 부분 제공, 학습 가치는 교과서 탭으로 보완

기본 방향: **2번 (마킹 부분 제공 + caption 보완)**. detector 보강은 별도 작업 항목.

### 4.5 프론트엔드

**JS (`src/static/pattern-modal.js`, ~150 lines):**
- `document.addEventListener('click', ...)` event delegation — `[data-pattern]` 클릭 시 모달 오픈
- 모달 DOM 동적 생성 (페이지 로드 시점에 hidden div 로 한 번만 생성, 재사용)
- 텍스트북 fetch → 모달 본문 채움 → 표시
- 사용자가 "실제 차트" 탭 클릭 시 actual fetch (lazy)
- 닫기: ESC keydown, 외곽 클릭, X 버튼

**CSS (`src/static/pattern-modal.css`, ~80 lines):**
- 모달 backdrop (rgba(0,0,0,0.6))
- 모달 박스 (max-width: 720px, max-width:100% 모바일 안전)
- 탭 헤더 (2개 탭 active/inactive 스타일)
- SVG/PNG 컨테이너 (`max-width:100%; height:auto;`)

**HTML 통합:**
- `src/web_app.py` 의 페이지 head 에 `<link rel="stylesheet" href="/static/pattern-modal.css">`, body 끝에 `<script src="/static/pattern-modal.js" defer>` 추가
- 패턴 배지 렌더링 부분 수정: 각 패턴 이름을 `<a href="#" data-pattern="잉태형" data-symbol="{sym}" [data-date="{date}"]>잉태형</a>` 로 감쌈
- `data-date` origins:
  - **대시보드 (top_patterns)**: 날짜 컨텍스트 없음 → `data-date` 속성 자체를 생략. API 가 latest detection 으로 fallback.
  - **상세 페이지 챠트 패턴 카드**: 각 검출의 `to_date` 또는 `from_date` 를 `data-date` 로 명시
  - **상세 페이지 캔들 패턴 카드**: 각 검출의 `date` 를 그대로

### 4.6 캐싱

메모리 LRU only — Python `functools.lru_cache(maxsize=128)` 또는 dict + manual TTL.
- Textbook: 캐싱 불필요 (정적 메모리 lookup)
- Actual chart: key `(symbol, pattern, date)`, TTL 1시간, maxsize 128
- 디스크 캐시 X (path traversal 회피)
- 서버 재시작 시 재계산 (1시간 TTL 이라 큰 비용 아님)

## 5. Flow

### 5.1 모달 오픈 (교과서 탭 즉시 표시)

```
[User clicks badge <a data-pattern data-symbol data-date>]
  → JS: open modal with skeleton, show 교과서 tab active
  → JS: fetch /api/pattern-popup/textbook?pattern=잉태형
  → Backend: yaml lookup
  → Response (정적, <100ms)
  → JS: render SVG + description in 교과서 tab
```

### 5.2 실제 차트 탭 클릭 (lazy)

```
[User clicks "실제 차트" tab]
  → JS: show spinner, fetch /api/pattern-popup/actual?symbol=X&pattern=Y&date=Z
  → Backend: pattern_popup.py
       1. pattern_json 에서 (pattern, date) detection row 찾기 → 좌표 추출
       2. yfinance OHLC 60일 fetch (cache hit 가능)
       3. matplotlib: line chart + 마킹 overlay
       4. PNG → base64
       5. LRU cache 저장
  → Response (cache miss <500ms, hit <50ms)
  → JS: render <img src="data:image/png;base64,..."> + caption
```

### 5.3 닫기

ESC keydown / backdrop click / X button → 모달 hide. DOM 은 destroy 안 함 (재사용).

## 6. Files

### 신규
| 파일 | 용도 | 예상 라인 |
|---|---|---|
| `src/data/pattern_metadata.yaml` | 5 챠트 + 60+ 캔들 패턴 매핑 (Tier 1 rich / Tier 2 generic) | ~800 |
| `src/pattern_metadata.py` | YAML 로더 + 매핑 lookup 헬퍼 | ~50 |
| `src/pattern_popup.py` | actual chart 빌더 + LRU cache | ~150 |
| `src/static/pattern-modal.js` | 클릭 핸들러 + 모달 + 탭 + AJAX | ~150 |
| `src/static/pattern-modal.css` | 모달/탭/SVG 스타일 | ~80 |
| `tests/test_pattern_metadata.py` | YAML 로더 단위 | ~50 |
| `tests/test_pattern_popup.py` | actual chart 빌더 단위 | ~80 |
| `tests/test_pattern_routes.py` | `/api/pattern-popup/*` 라우트 통합 | ~100 |

### 수정
- `src/web_app.py` — 라우트 2개 추가, 배지 렌더링 부분 (line 1519, 1880~) 에 `<a data-pattern data-symbol data-date>` 처리, 페이지 head 에 link/script 포함
- `requirements.txt` — `pyyaml>=6.0` 추가 (이미 있으면 skip)

## 7. Security

| 위협 | 대응 |
|---|---|
| XSS — textbook_html | 모두 우리가 작성한 정적 데이터. 외부 입력 아님. innerHTML 사용 OK. 다만 SVG 안에 `<script>` 절대 안 넣기 (yaml 작성 시 self-discipline) |
| Path traversal — cache key | 디스크 캐시 안 만듦. 메모리 dict key 만 사용 |
| Injection — pattern/symbol/date 쿼리 파라미터 | YAML lookup 키로만 사용 (DB 쿼리는 parameterized). 비매칭 시 404 |
| CSRF — GET 라우트만 추가, mutation 없음. CSRF 보호 불필요 | |
| 인증 — 기존 BASIC_AUTH 게이트 적용 (다른 라우트와 동일) | `_session_auth_gate` 자동 적용 |

## 8. Testing

### 단위 테스트

`test_pattern_metadata.py`:
- yaml 로드 시 15개 패턴 키 존재
- 잉태형/더블바텀 등 표본 5개 lookup 정상
- 미정의 패턴 lookup → None 반환
- SVG 내 `<script>` 포함 시 로딩 실패 (security guard)

`test_pattern_popup.py`:
- chart pattern 좌표 → 마커 위치 변환 정확성
- 캔들 패턴 date → 수직선 위치 매핑
- OHLC fetch 실패 → null chart + caption 응답
- 동일 (symbol, pattern, date) 두 번째 호출 → cache hit (matplotlib 호출 안 됨, mock 으로 검증)

### 통합 테스트

`test_pattern_routes.py`:
- GET `/api/pattern-popup/textbook?pattern=잉태형` → 200 + svg/description 응답
- GET `/api/pattern-popup/textbook?pattern=없는패턴` → 404
- GET `/api/pattern-popup/actual?symbol=005930.KS&pattern=더블바텀&date=2026-05-15` → 200 + chart_b64 (mock yfinance + mock pattern_json)
- 인증 미설정 + ENABLE_BASIC_AUTH=1 → 401 (auth gate 작동)
- 비-GET (POST) → 405

### Manual smoke

- 모달 오픈/닫기 (ESC, backdrop, X)
- 탭 전환
- 5개 패턴 표본 (더블바텀, 잉태형, 망치형, H&S, 삼각형) 각각 클릭하여 교과서 + 실제 차트 정상 표시
- 데이터 없는 종목/패턴 → fallback 메시지
- 모바일 width 375px 에서 모달 안 깨짐

## 9. Risks

| 리스크 | 영향 | 완화 |
|---|---|---|
| H&S/삼각형 detector 의 좌표 부족 | 마킹 일부 표시 | caption 텍스트로 보완. 별도 작업으로 detector 보강 검토 |
| matplotlib 동시 호출 시 워커 부하 | 응답 지연 | 메모리 LRU + lazy load + 1-subplot 축소 |
| yfinance fetch 실패 (시장 마감 후 데이터 갱신 시점 등) | actual 탭 표시 불가 | null + caption 응답으로 graceful fallback |
| YAML SVG 의 XSS | 페이지 전체 침해 가능 | YAML 로더에서 `re.search(r'<\s*script', svg, re.IGNORECASE)` 매칭 시 startup 단계에서 `ValueError` raise (배포 차단). 코드리뷰에서 추가로 의심 패턴 검사 |

## 10. Migration / Rollout

- DB schema 변경 없음
- 기존 패턴 검출 로직 변경 없음
- 페이지 head/body 에 static 파일 include — 변경 후 gunicorn 재시작 1회만 필요
- `pyyaml` 신규 의존성 — `pip install -r requirements.txt` 추가 실행

배포 순서:
1. 파일 생성/수정 + 테스트 통과
2. push → Mac mini git pull
3. `pip install -r requirements.txt` (pyyaml)
4. `launchctl kickstart -k gui/$(id -u)/ai.stock-analyzer.web` (gunicorn 재시작)
5. 브라우저 새로고침 후 패턴 배지 클릭 검증

## 11. Open questions

(없음 — LLM 리뷰에서 제기된 5개 이슈 모두 design 에 반영)
