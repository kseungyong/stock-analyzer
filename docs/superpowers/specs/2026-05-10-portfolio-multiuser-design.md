# Portfolio 다중 사용자 분리 설계

**작성일**: 2026-05-10
**상태**: 사용자 승인 (5 결정 모두 추천안 채택)

## 목표

`/portfolio` 를 사용자별로 격리. 현재 3명 (admin/shnoh/guest) 각자
자신의 보유 종목만 추가/조회/수정/삭제 가능. analysis_cache 는 공유 유지
(분석 결과는 사용자 무관).

## 결정사항

| 항목 | 결정 |
|---|---|
| 사용자간 공유/관전 | ❌ 격리 |
| admin 가 전체 조회 | ❌ admin 도 본인 것만 |
| 인증 OFF 모드 | username = `"default"` |
| 기존 데이터 이전 | 모두 `admin` 으로 할당 |
| UI 사용자 표시 | nav 우측 `👤 admin (N종목)` |

## 1. DB 스키마 변경

### 새 PK
```sql
-- 기존: portfolio(symbol PK)
-- 신규: portfolio(username, symbol) 복합 PK
CREATE TABLE portfolio_new (
    username   TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    avg_price  REAL NOT NULL,
    qty        INTEGER NOT NULL DEFAULT 0,
    added_at   INTEGER NOT NULL,
    notes      TEXT,
    PRIMARY KEY (username, symbol)
);
```

### 마이그레이션
`init_db()` 에서 idempotent 처리:
1. `PRAGMA table_info(portfolio)` 로 `username` 컬럼 존재 확인
2. 없으면:
   - `ALTER TABLE portfolio RENAME TO portfolio_old`
   - 신규 테이블 생성 (위 스키마)
   - `INSERT INTO portfolio SELECT 'admin', symbol, avg_price, qty, added_at, notes FROM portfolio_old`
   - `DROP TABLE portfolio_old`
3. 이미 username 있으면 no-op

기존 라이브 데이터 12종목 → 모두 `admin` 사용자 소유로.

### analysis_cache 변경 없음
- `last_close`, `pattern_json` 등 모두 사용자 무관
- JOIN 시 `c.cache_key = p.symbol` 만 (username 무관)

## 2. `src/portfolio.py` API

모든 함수에 `username: str` 파라미터 추가 (첫 위치):

```python
def init_db() -> None  # 변경 없음 (마이그레이션만 추가)
def add_holding(username, symbol, avg_price, qty, notes=None) -> bool
def remove_holding(username, symbol) -> bool
def update_holding(username, symbol, *, avg_price=None, qty=None, notes=None) -> bool
def list_holdings(username) -> list[dict]
def count_holdings(username) -> int
def get_holding_with_pnl(username, symbol) -> dict | None
def list_holdings_with_pnl(username) -> list[dict]
```

WHERE 절에 `username = ?` 추가, INSERT 시 username 포함.

## 3. `src/web_app.py` 변경

### 사용자 식별 헬퍼

```python
def _current_user() -> str:
    """request.authorization 에서 username 추출. 인증 OFF 면 'default'."""
    if not _basic_auth_on:
        return "default"
    auth = request.authorization
    return auth.username if auth and auth.username else "default"
```

### 라우트 갱신 (5개)
- `/portfolio` (GET) — `_current_user()` 로 list_holdings_with_pnl 호출
- `/portfolio/add` (POST) — add_holding(user, ...)
- `/portfolio/update` (POST) — update_holding(user, ...)
- `/portfolio/delete` (POST) — remove_holding(user, ...)
- `/stock/<sym>` 의 `_render_portfolio_banner` — 현재 사용자 보유만

### nav 표시
`_page()` topbar 우측에 사용자 박지:
```html
<span class="topbar-user">👤 admin · 12종목</span>
```

`portfolio_db.count_holdings(username)` 한 번 호출 추가.

## 4. 보안

- update/delete 시 다른 사용자 종목 침범 시도 → WHERE 절 username 일치 안하므로 자동 noop (rowcount=0 → False 반환)
- 라우트 단에서 추가 검증 불필요

## 5. 테스트 변경

### 기존 11 테스트 (`tests/test_portfolio.py`)
- 모든 호출에 username 인자 추가 (`p.add_holding('admin', 'AAPL', 150.0, 10)` 등)

### 신규 격리 테스트 (4개 추가)
1. `test_user_a_holding_invisible_to_user_b` — A 추가, B list 에서 안 보임
2. `test_user_a_cannot_delete_user_b_holding` — A 가 B 의 종목 remove → False
3. `test_user_a_cannot_update_user_b_holding` — A 가 B 의 종목 update → False
4. `test_same_symbol_different_users_independent` — 둘 다 AAPL 보유, 가격/수량 독립

### 마이그레이션 테스트 (1개)
1. `test_migrate_legacy_assigns_to_admin` — 옛 스키마로 데이터 넣고 init_db() → 모두 admin 소유

## 6. UI 추가

### 통계 헤더에 사용자 표시
```
👤 admin · 12종목 보유
🇰🇷 한국 ...
🇺🇸 미국 ...
```

### `_render_portfolio_banner` 변경 없음 (현재 사용자만 lookup)

## 7. 배포

1. 백업: `predictions.db` → `predictions.db.bak.YYYYMMDD-HHMM`
2. 코드 배포 (init_db 가 자동 마이그레이션)
3. smoke: admin 으로 /portfolio 접속해 12종목 확인

## 8. 비목표 (이번 작업에서 안 함)

- admin/owner 권한 시스템 (모두 동일 권한)
- 사용자 간 공유/관전
- 사용자명 변경 / 사용자 삭제 시 cascade
- 사용자 회원가입 (.env 의 BASIC_AUTH_USERS 가 source of truth)

## 9. 변경 범위

| 파일 | 변경 |
|---|---|
| `src/portfolio.py` | 7 함수 시그니처 + WHERE/VALUES 갱신, 마이그레이션 추가 |
| `src/web_app.py` | `_current_user()` 추가, 5 라우트 + 배너 + nav username 박지 |
| `tests/test_portfolio.py` | 11 기존 테스트 수정 + 격리 테스트 4 + 마이그레이션 테스트 1 |
