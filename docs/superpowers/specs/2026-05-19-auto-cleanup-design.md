# 자동 종목 정리 (auto-cleanup) — 설계서

날짜: 2026-05-19
대상: stock-analyzer
상태: Draft (사용자 리뷰 대기)

---

## 1. 목적

대시보드(`settings.yaml` 기반 watchlist)에서 **관심 가치가 떨어진 종목**을 자동으로 정리한다.
사용자가 N+1, N+2, ... 시점에 관심도가 낮아진 종목을 수동으로 추적·제거하는 부담을 제거.

핵심 원칙:
- **지속적인 약세**만 제거 대상 (일시 변동 보호)
- 명시적으로 보호된 종목(ETF, 보유, pinned, note 가진 종목) 자동 제외
- 모든 변경은 git history에 commit → revert로 복원 가능

## 2. 삭제 조건 (모두 AND)

1. **composite < -5** (Tech + BNF + Pattern×0.5 합)
2. **최근 7일 (영업일 5일+)** 모든 record가 조건 1 만족
3. 종목이 **ETF/인덱스가 아님** (화이트리스트 기반 식별)
4. **portfolio 테이블에 없음** (보유 종목 자동 보호)
5. `settings.yaml`에 **`pinned: true` 또는 `note` 필드 없음**

### 추가 안전장치
- **Grace period**: composite_history row 수 < 5 → skip (신규 추가 종목 보호)
- **Safety limit**: 한 번에 최대 10종목 삭제. 초과 시 abort + warn (시장 폭락 사고 방지)

## 3. 아키텍처

```
신규 모듈:
  src/composite_history.py  — DB wrapper (init/insert/recent/purge_old)
  src/cleanup.py            — 조건 판정 + apply

수정 모듈:
  main.py
    - import composite_history
    - composite_history.init_db()
    - auto_analyze_market 내 analysis_cache.put() 직후 composite_history.insert() 1줄
    - main()에 `cleanup` subcommand 추가 (--dry-run / --apply)

신규 launchd:
  ~/Library/LaunchAgents/ai.stock-analyzer.cleanup.plist  (macmini, git 추적 안 됨)
  메모리 reference_sykim_macmini.md 에 등록 절차 기록.
```

## 4. composite_history 스키마

```sql
CREATE TABLE IF NOT EXISTS composite_history (
    symbol       TEXT NOT NULL,
    recorded_at  INTEGER NOT NULL,   -- unix epoch
    composite    REAL NOT NULL,
    PRIMARY KEY (symbol, recorded_at)
);
CREATE INDEX IF NOT EXISTS idx_ch_symbol_date
    ON composite_history(symbol, recorded_at DESC);
```

DB 파일: 기존 `data/predictions.db` 재사용 (이미 analysis_cache, portfolio 등 사용 중).

### Retention
- cleanup 실행 시 `recorded_at < now - 90 days` row 일괄 삭제.
- 90일 = 90 × 86400 초.

## 5. 핵심 함수 인터페이스

### `src/composite_history.py`

```python
def init_db() -> None:
    """스키마 적용 (멱등)."""

def insert(symbol: str, composite: float, recorded_at: int | None = None) -> None:
    """recorded_at 생략 시 현재 시각."""

def recent(symbol: str, days: int = 7) -> list[tuple[int, float]]:
    """[(recorded_at, composite), ...] 최신순. 비어있을 수 있음."""

def purge_old(days: int = 90) -> int:
    """N일 이전 row 삭제. 삭제 row 수 반환."""
```

### `src/cleanup.py`

```python
_ETF_PREFIXES = ("KODEX", "TIGER", "ARIRANG", "KBSTAR", "HANARO")
_ETF_SYMBOLS = {
    "SPY", "QQQ", "VTI", "VOO", "IWM",
    "KORU", "QLD", "TECL", "USD", "SOXL", "TQQQ",
}
_COMPOSITE_THRESHOLD = -5.0
_PERSISTENCE_DAYS = 7
_MIN_HISTORY_ROWS = 5
_SAFETY_LIMIT = 10

def is_etf(symbol: str, name: str) -> bool: ...

def should_remove(
    symbol: str, name: str,
    history_rows: list[tuple[int, float]],
    is_held: bool,
    is_pinned_or_noted: bool,
) -> bool:
    """5개 조건 AND. 단위 테스트의 핵심 함수."""

def find_candidates(config: dict, held_symbols: set[str]) -> list[dict]:
    """settings.yaml 의 모든 종목 검사. 후보 list (각 {symbol,name,market,composite,days}) 반환."""

def apply(candidates: list[dict], dry_run: bool = False) -> dict:
    """
    settings.yaml 수정 + logs/auto_remove.log 1줄/종목 + git commit + git push.
    Returns: {"removed": N, "skipped": [...], "limited": bool}
    Safety limit 초과 시 abort, removed=0.
    """
```

### `main.py` cleanup subcommand

```python
parser.add_argument("cleanup", help="자동 종목 정리 (composite < -5, 7일 연속)")
cleanup_parser.add_argument("--dry-run", action="store_true",
                            help="후보만 출력, 변경 없음")
cleanup_parser.add_argument("--apply", action="store_true",
                            help="실제 settings.yaml 수정 + git commit")
```

## 6. 데이터 흐름

```
[매일 KST 16:00, 22:00]  auto_analyze_market(market)
  → for each stock:
      analyze_stock(...)
      analysis_cache.put(...)
      composite = _composite_score(cache_row)
      composite_history.insert(symbol, composite)         (신규)

[매일 KST 23:30]  cleanup --apply
  → composite_history.purge_old(90)
  → config = load settings.yaml
  → held = portfolio.list_holdings_with_pnl(user).map(symbol)
  → for each stock:
      rows = composite_history.recent(symbol, 7)
      if should_remove(symbol, name, rows, symbol in held, has pinned/note):
          candidates.append(...)
  → apply(candidates):
      if len(candidates) > 10: abort
      remove from settings.yaml
      append logs/auto_remove.log
      git add config/settings.yaml logs/auto_remove.log
      git commit + git push
```

## 7. 보호 종목 (자동 제외)

| 종류 | 식별 방법 | 예시 |
|---|---|---|
| 한국 ETF | name이 `_ETF_PREFIXES` 로 시작 | "KODEX 200", "TIGER 미국나스닥100" |
| 미국 ETF | symbol이 `_ETF_SYMBOLS` 에 있음 | SPY, QQQ, VTI, KORU |
| 보유 중 | portfolio 테이블 존재 | 사용자 보유 종목 |
| Pinned | settings.yaml에 `pinned: true` | 사용자 명시 보호 |
| Noted | settings.yaml에 `note: "..."` | 메모 = 의도적 추적 |

settings.yaml 확장 (backward compatible):
```yaml
stocks:
  korea:
    - name: 삼성전자
      symbol: 005930.KS              # 기존 종목 영향 없음
    - name: 어떤 종목
      symbol: XXX.KS
      pinned: true                    # NEW: 자동 삭제 보호
      note: "장기 보유 의도"          # NEW: 있으면 보호
```

## 8. 삭제 로그 (`logs/auto_remove.log`)

매 삭제마다 1줄 append:
```
2026-05-19T23:30:01+09:00  005930.KS  삼성전자       composite_avg=-6.21 days=7
2026-05-19T23:30:01+09:00  XXX.KQ     어떤 잡주     composite_avg=-7.50 days=7
```

**Dry-run**: stdout에만 후보 list 출력. `auto_remove.log` 파일 작성 안 함, git commit 없음.

## 9. Git Commit 형식

```
chore(cleanup): 자동 제거 N종목 (composite < -5, 7일 연속)

- 005930.KS 삼성전자 (composite_avg=-6.21)
- XXX.KQ   어떤 잡주 (composite_avg=-7.50)
- ...

Triggered by: ai.stock-analyzer.cleanup launchd cron
Restore: git revert <this-sha>
```

push 실패 시 → warn만 출력, 로컬 commit 유지 (다음날 cron 또는 사용자 수동 push).

## 10. 테스트 (`tests/test_cleanup.py`)

1. `test_is_etf_kodex` — `("069500.KS", "KODEX 200")` → True
2. `test_is_etf_us_symbol` — `("SPY", "SPDR S&P 500")` → True
3. `test_is_etf_regular_stock` — `("005930.KS", "삼성전자")` → False
4. `test_composite_history_insert_recent` — insert 후 recent로 조회 round-trip
5. `test_should_remove_all_seven_days_below` — 7 rows 모두 -6 → True
6. `test_should_remove_one_recovery_protects` — 6 rows -6 + 1 row +2 → False
7. `test_should_remove_insufficient_history` — 3 rows만 → False (grace period)
8. `test_should_remove_skip_etf` — KODEX 200 -7 7일 → False
9. `test_should_remove_skip_portfolio` — held=True → False
10. `test_should_remove_skip_pinned` — pinned=True → False
11. `test_apply_dry_run_no_changes` — dry-run → settings.yaml & git status unchanged
12. `test_apply_safety_limit_aborts` — 11 candidates → abort, removed=0
13. `test_apply_writes_log_line` — 정상 시 auto_remove.log에 N줄 추가
14. `test_purge_old_history` — 91일 이전 row 1개 → purge 후 0개

테스트는 임시 SQLite DB (`tmp_path` fixture)와 임시 settings.yaml로 격리. git 호출 mock.

## 11. launchd plist (macmini, git 추적 안 됨)

`~/Library/LaunchAgents/ai.stock-analyzer.cleanup.plist`:
- Label: `ai.stock-analyzer.cleanup`
- ProgramArguments: `python main.py cleanup --apply`
- WorkingDirectory: `/Users/sykim/Projects/stock-analyzer`
- StartCalendarInterval: KST 23:30 매일
- EnvironmentVariables: PATH (git 호출용). ML 환경변수 불필요.
- StandardOut/Error: `logs/cleanup.out.log` / `logs/cleanup.err.log`

설치 절차:
```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.stock-analyzer.cleanup.plist
launchctl start ai.stock-analyzer.cleanup    # 즉시 1회 트리거 (검증용)
```

설치 절차를 메모리 `reference_sykim_macmini.md` 에 추가.

## 12. 호환성 / 마이그레이션

- 신규 테이블 `composite_history` — `init_db()` 멱등.
- `settings.yaml` `pinned`/`note` 필드는 옵셔널 → 기존 종목 영향 없음.
- 첫 7일은 history 부족으로 자동 발동 안 함 (자연스러운 grace period).
- 기존 종목 자동 backfill 안 함 (의도 — 새 기준으로만 평가).

## 13. 스코프 외 (이번 작업에서 제외)

- Web UI에서 cleanup history 보기 페이지
- Web UI에서 종목별 `pinned: true` toggle 버튼
- 다중 사용자 portfolio 지원 (현재 단일 user 시스템)
- `--restore <date>` 명령으로 N일 이전 settings.yaml 복원 (git revert로 충분)
- Slack/Telegram 알림 (로그 파일 + git history로 충분)
