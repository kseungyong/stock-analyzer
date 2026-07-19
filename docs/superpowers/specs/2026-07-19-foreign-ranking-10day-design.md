# 외인 ranking 10일 누적 추가 + 영업일 기준 수정 — 설계

날짜: 2026-07-19

## 배경

- `/foreign-ranking` 웹 뷰는 투자자(외인/기관/연기금) × 방향(순매수/순매도) × 기간(일별/5일)을 표시.
- 기존 "5일 누적"은 `snap - timedelta(4)` 캘린더 날짜 범위 SUM이라 실제로는 3~4영업일치만
  합산되는 버그가 있음 (스냅샷은 영업일에만 존재). legend 의 "최근 5영업일 합산" 문구와 불일치.

## 결정 사항

1. **10일 누적은 웹 표시에만 추가.** universe push(`compute_union_top`)는 기존 1일+5일 union 유지.
2. **기간 계산을 영업일(스냅샷 존재일) 기준으로 수정.** `period_days=N` = 최근 N개 snap_date 합산.

## 변경 내용

### src/foreign_ranking.py

`top_n_by_investor` 의 날짜 범위 `BETWEEN start AND end` 를 서브쿼리로 교체:

```sql
WHERE snap_date IN (
  SELECT DISTINCT snap_date FROM foreign_ranking_history
  WHERE snap_date <= ? ORDER BY snap_date DESC LIMIT ?
)
```

`period_days` 의미: "최근 N영업일(스냅샷 존재일)". docstring 갱신.

부수효과: `compute_union_top` 의 5일 union 도 진짜 5영업일치가 됨 → push 종목이 다소
달라질 수 있으나 이것이 의도된 정확한 동작.

### src/web_app.py — foreign_ranking_view

기간 dict 에 `biweekly`(period_days=10) 추가: `daily(1)` / `weekly(5)` / `biweekly(10)`.

### src/templates/foreign_ranking.html

- 기간 loop 를 `['daily', 'weekly', 'biweekly']` 3개로 확장, 라벨 "10일 누적".
- 카드 수 방향별 6→9. auto-fit grid 이므로 레이아웃 변경 불필요.
- legend 문구를 영업일 기준으로 정정 + 10일 설명 추가.

### tests/test_foreign_ranking.py

- 주말 갭 포함 스냅샷으로 `period_days=5` 가 최근 5개 스냅샷을 합산하는지 검증.
- `period_days=10` 케이스 추가.

## 데이터 현황

DB 누적 시작이 2026-07-08 이라 당분간 10일 누적은 실질 8~9영업일 합산으로 표시됨
(빈 표 아님, 시간이 지나면 자연 해소).
