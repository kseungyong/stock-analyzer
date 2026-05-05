# Signal JSON API 구현 계획 (auto-trader 통합 Phase 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** stock-analyzer 에 `GET /api/signal/<symbol>` 신규 라우트 추가 — 캐시된 Tech 시그널 + BNF 시그널 JSON 반환. auto-trader 등 외부 시스템이 entry gate 로 polling 가능.

**Architecture:** `analysis_cache.get(symbol)` 결과를 JSON 형식으로 변환. 캐시 hit → 200, miss → 404, 잘못된 심볼 → 400. 기존 Basic Auth 게이트 그대로 적용 (`_basic_auth_gate`). 응답에 `is_fresh` boolean 으로 freshness 명시 — auto-trader 가 stale 데이터 감지 가능.

**Tech Stack:** Flask, sanitize_stock_symbol/validate_stock_symbol (기존), `analysis_cache.get`/`is_fresh` (기존), pytest

**Spec:** `docs/superpowers/specs/2026-05-06-auto-trader-integration-design.md`

---

## 파일 구조

| 파일 | 변경 |
|---|---|
| `src/web_app.py` | 추가 — `api_signal` 라우트 + `_signal_json` 헬퍼 (response shape 빌드) |
| `tests/test_web_app.py` | 보강 — `TestApiSignal` (4 케이스) |

---

## Phase 1 — Signal API

### Task 1: `_signal_json` 헬퍼 + `api_signal` 라우트

**Files:**
- Modify: `src/web_app.py` (라우트 + 헬퍼)
- Modify: `tests/test_web_app.py`

- [ ] **Step 1.1: 테스트 작성**

`tests/test_web_app.py` 끝에 추가:

```python
class TestApiSignal:
    def test_cache_hit_returns_json(self, client):
        from src import analysis_cache as ac
        ac.init_db()
        ac.put("AAPL", "us", "<p/>", "auto_cron",
               signal_value="매수", signal_score=3,
               bnf_signal_value="관망", bnf_signal_score=1)
        resp = client.get("/api/signal/AAPL")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["symbol"] == "AAPL"
        assert data["market"] == "us"
        assert data["tech"] == {"signal": "매수", "score": 3}
        assert data["bnf"] == {"signal": "관망", "score": 1}
        assert isinstance(data["generated_at_unix"], int)
        assert "KST" in data["generated_at_kst"]
        assert isinstance(data["is_fresh"], bool)

    def test_cache_miss_returns_404(self, client):
        """캐시 row 없는 종목 → 404 + {error: 'no_cache'}."""
        from src import analysis_cache as ac
        ac.init_db()
        # 명시적 삭제 (다른 테스트 잔여 가능)
        import sqlite3
        with sqlite3.connect(ac._DB_PATH) as conn:
            conn.execute("DELETE FROM analysis_cache WHERE cache_key='UNKNOWN'")
        resp = client.get("/api/signal/UNKNOWN")
        assert resp.status_code == 404
        data = resp.get_json()
        assert data["error"] == "no_cache"
        assert data["symbol"] == "UNKNOWN"

    def test_invalid_symbol_returns_400(self, client):
        """잘못된 심볼 (특수문자 등) → 400 + {error: 'invalid_symbol'}."""
        resp = client.get("/api/signal/<script>")
        assert resp.status_code == 400

    def test_partial_signal_returns_null_fields(self, client):
        """signal_value 만 있고 bnf_* NULL 인 row → bnf 필드 null 반환."""
        from src import analysis_cache as ac
        ac.init_db()
        ac.put("AAPL", "us", "<p/>", "auto_cron",
               signal_value="매수", signal_score=2)  # bnf 없이
        resp = client.get("/api/signal/AAPL")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["tech"] == {"signal": "매수", "score": 2}
        assert data["bnf"] == {"signal": None, "score": None}
```

- [ ] **Step 1.2: 테스트 실행 — FAIL**

```bash
cd /Users/sykim/Projects/stock-analyzer/.worktrees/signal-api
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_web_app.py::TestApiSignal -v
```
Expected: 4 fail (`/api/signal/<symbol>` 라우트 없음 → 404 with HTML, 또는 `<script>` 검증 미동작)

- [ ] **Step 1.3: `_signal_json` 헬퍼 + `api_signal` 라우트 구현**

`src/web_app.py` 의 다른 `/api/*` 라우트 부근 (`/api/jobs/<id>`, `/api/stocks/search` 가 정의된 영역) 에 append:

```python
def _signal_json(row: dict) -> dict:
    """analysis_cache row 를 외부 API 응답용 JSON dict 로 변환."""
    return {
        "symbol": row["cache_key"],
        "name": row["cache_key"],  # cache 에 종목명 없음 — symbol 그대로
        "market": row["market"],
        "generated_at_kst": _format_kst(row["generated_at"]),
        "generated_at_unix": row["generated_at"],
        "is_fresh": analysis_cache.is_fresh(row, int(time.time())),
        "tech": {
            "signal": row.get("signal_value"),
            "score": row.get("signal_score"),
        },
        "bnf": {
            "signal": row.get("bnf_signal_value"),
            "score": row.get("bnf_signal_score"),
        },
    }


@app.route("/api/signal/<path:symbol>")
def api_signal(symbol: str):
    """외부 시스템용 — 캐시된 Tech + BNF 시그널 JSON.

    Returns:
        200 + signal JSON (cache hit)
        404 + {"error": "no_cache", "symbol": ...} (cache miss)
        400 + {"error": "invalid_symbol", "symbol": ...} (sanitize/validate 실패)

    Spec: docs/superpowers/specs/2026-05-06-auto-trader-integration-design.md
    """
    sym = sanitize_stock_symbol(symbol)
    if not validate_stock_symbol(sym):
        return jsonify({"error": "invalid_symbol", "symbol": symbol}), 400

    row = _safe_cache_get(sym)
    if row is None:
        return jsonify({"error": "no_cache", "symbol": sym,
                        "message": "분석 이력 없음. POST /analyze/<symbol> 로 트리거 후 polling."}), 404

    return jsonify(_signal_json(row))
```

`name` 필드는 분석 시 settings.yaml lookup 으로 채우면 더 정확하지만, Phase 1 단순성 위해 symbol 그대로. follow-up 가능.

- [ ] **Step 1.4: 테스트 실행 — PASS**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_web_app.py::TestApiSignal -v
```
Expected: 4 passed

- [ ] **Step 1.5: 회귀**

```bash
/Users/sykim/Projects/stock-analyzer/.venv/bin/python -m pytest tests/test_web_app.py -q
```
Expected: 모든 기존 테스트 + 4 신규 PASS

- [ ] **Step 1.6: 커밋**

```bash
git add src/web_app.py tests/test_web_app.py
git commit -m "feat(web): GET /api/signal/<symbol> — auto-trader 통합용 JSON API"
```

---

### Task 2: 서버 배포

**Files:** 없음 (서버 운영 명령)

- [ ] **Step 2.1: push to origin/main**

```bash
git push origin main
```

- [ ] **Step 2.2: 서버 git pull + 재시작**

```bash
ssh sykim@100.87.151.104 'cd ~/Projects/stock-analyzer && git pull --ff-only origin main && launchctl kickstart -k gui/$(id -u)/ai.stock-analyzer.web'
```

- [ ] **Step 2.3: smoke test**

```bash
ssh sykim@100.87.151.104 'sleep 3; curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/api/signal/AAPL'
```
Expected: 401 (Basic Auth 미통과) — 인증 추가 후 200/404 확인

```bash
# 인증 포함 smoke (BASIC_AUTH_USERS 의 첫 user/pw 사용)
ssh sykim@100.87.151.104 'USER=$(grep "^BASIC_AUTH_USERS=" ~/Projects/stock-analyzer/.env | head -1 | cut -d= -f2 | cut -d";" -f1 | cut -d: -f1); PASS=$(grep "^BASIC_AUTH_USERS=" ~/Projects/stock-analyzer/.env | head -1 | cut -d= -f2 | cut -d";" -f1 | cut -d: -f2); curl -s -u "$USER:$PASS" http://localhost:8080/api/signal/AAPL | head -c 500'
```
Expected: JSON dict 또는 `{"error":"no_cache"...}` (둘 다 정상 — endpoint 응답)

- [ ] **Step 2.4: 외부 (Funnel) 검증**

```bash
USER=<basic-auth-user>; PASS=<basic-auth-pw>
curl -s -u "$USER:$PASS" https://sykim-macmini.tail8d6ef7.ts.net/api/signal/AAPL
```
Expected: 같은 응답 — 외부에서도 동일 동작.

---

## Self-Review

### Spec coverage

| Spec 항목 (§4 Phase 1) | 구현 task |
|---|---|
| 신규 endpoint `GET /api/signal/<symbol>` | T1 (`api_signal` 라우트) |
| 응답 형식 (cache hit) — symbol/market/generated_at/is_fresh/tech/bnf | T1 (`_signal_json` 헬퍼) |
| 응답 형식 (cache miss) — 404 `{error: "no_cache"}` | T1 (`row is None` 분기) |
| 응답 형식 (invalid symbol) — 400 `{error: "invalid_symbol"}` | T1 (sanitize/validate 분기) |
| Basic Auth 게이트 적용 | T1 (`_basic_auth_gate` 자동) |
| ML 정보 미포함 (의도) | T1 (`_signal_json` 의 키 set) |

### Spec §6 테스트 케이스 → plan 매핑

| Spec 테스트 | Plan 위치 |
|---|---|
| test_cache_hit_returns_json | T1 Step 1.1 |
| test_cache_miss_returns_404 | T1 Step 1.1 |
| test_invalid_symbol_returns_400 | T1 Step 1.1 |
| test_csrf_not_required | 별도 명시 안 함 — Spec 상 자동 (GET 라우트는 CSRF 영향 없음). 신규 어서션 불요 |

대신 더 가치 있는 케이스로 대체:
- `test_partial_signal_returns_null_fields` — signal_value 있고 bnf NULL 인 row → bnf 필드 null

### 타입 일관성

- `_signal_json(row: dict) -> dict` ↔ `analysis_cache.get` 반환 dict 의 키 (cache_key, market, generated_at, signal_value, signal_score, bnf_signal_value, bnf_signal_score) ✓
- `_format_kst(int)` 기존 헬퍼 사용 ✓
- `analysis_cache.is_fresh(row, int)` 기존 시그니처 ✓

### Placeholder

TBD/TODO 없음 ✓

### 회귀 위험

- 신규 endpoint 추가만 — 기존 라우트 영향 0
- Basic Auth 게이트 변경 없음
- 테스트 4 신규 추가 — 기존 280 테스트 그대로
