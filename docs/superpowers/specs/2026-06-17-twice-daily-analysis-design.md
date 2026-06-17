# 종목 자동분석 하루 2회 스케줄 설계

- **작성일**: 2026-06-17
- **상태**: 설계 승인 (구현 대기)
- **관련**: launchd plist, `main.py auto_analyze_market`(기존), `scripts/*.plist.template`

## 1. 목적

settings.yaml + 외인 발굴 overlay 종목 목록을 **거래일에 하루 2회 자동 분석**해
`analysis_cache` 에 저장한다. 사용자가 웹 접속 시 항상 최신 분석 결과가 나와 있게 한다.

### 스케줄 (KST)
| 시장 | 분석 시각 | 의미 |
|------|----------|------|
| 한국 | 09:00, 15:40 | 09:00 = 전일 마감 기준 아침 제공 / 15:40 = 당일 장마감(15:30) 직후 |
| 미국 | 23:30, 06:00 | 23:30 = 미국 개장 근처 / 06:00 = 미국 마감 근처 (기존 us-analysis 유지) |

### 비목표 (Non-goals)
- 분석 로직 변경 — `auto_analyze_market` 은 그대로 사용.
- 미국 거래일 skip 추가 — 미국 공휴일엔 전일 데이터 재분석(무해), YAGNI.
- foreign-ranking(16:00) 앞당김 — 당일 외인 발굴 종목은 익일 09:00 분석에 첫 반영.

## 2. 현재 상태

- `korea-analysis` plist: 16:30 (1일 1회). `us-analysis` plist: 06:00 (1일 1회).
- `main.load_config()` 가 `apply_overlay()` 호출 → `auto_analyze_market` 의 "목록" =
  settings.yaml `stocks[market]` + foreign_ranking overlay 머지본. 웹 표시 목록과 동일.
- `analysis_cache.put(cache_key=symbol, ...)` 는 symbol 별 UPSERT — 매 분석이 최신으로 덮어씀.
- 한국 휴장일 skip: `auto-analyze korea` 가 `is_kr_market_open_today()` 체크(main.py:474).
  미국은 체크 없음(유지).

## 3. 변경 내용

**코드 변경 없음.** launchd plist 의 `StartCalendarInterval` 만 단일 dict → dict 배열로 변경.

### 3.1 korea-analysis
`StartCalendarInterval` 을 두 시각 배열로:
```xml
<key>StartCalendarInterval</key>
<array>
    <dict><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Hour</key><integer>15</integer><key>Minute</key><integer>40</integer></dict>
</array>
```
기존 16:30 제거. 나머지 키(EnvironmentVariables, ProgramArguments `auto-analyze korea`,
StandardOutPath 등)는 서버 현재 plist 그대로 보존.

### 3.2 us-analysis
```xml
<key>StartCalendarInterval</key>
<array>
    <dict><key>Hour</key><integer>23</integer><key>Minute</key><integer>30</integer></dict>
    <dict><key>Hour</key><integer>6</integer><key>Minute</key><integer>0</integer></dict>
</array>
```
기존 06:00 유지 + 23:30 추가. ProgramArguments `auto-analyze us`.

### 3.3 scripts/ 템플릿 (재현성)
서버에만 있던 korea-analysis/us-analysis plist 를 `scripts/korea-analysis.plist.template`,
`scripts/us-analysis.plist.template` 로 추출(새 스케줄 반영, `__PROJECT_ROOT__` 치환).
기존 EnvironmentVariables(SKIP_ML_PREDICTION, OMP_NUM_THREADS 등)는 서버 현재값 보존.

## 4. 배포

1. scripts/ 템플릿 2개 작성 + 커밋.
2. 서버: 템플릿 치환 → `~/Library/LaunchAgents/` 덮어쓰기.
3. `launchctl bootout` → `launchctl bootstrap` 으로 재로드 (시각 변경 반영).
4. 검증: `launchctl print` 또는 plist 추출로 두 시각 등록 확인. plutil -lint 통과.

## 5. 동작 확인 (배포 후)
- 다음 09:00/15:40(한국), 23:30/06:00(미국) 에 자동 분석 실행 → `logs/korea-analysis.out` 등 로그 확인.
- 웹 `/` 또는 종목 페이지에서 `analysis_cache` 최신 결과(generated_at) 갱신 확인.

## 6. 영향 / 리스크
- 분석 빈도 2배 → 서버 부하/시간 증가. 한국 종목 수십 개 × 2회. 기존 16:30 1회가
  완주하므로 2회도 무리 없음 (각 회 독립).
- 09:00 분석 시 한국 장 개장 직후라 당일 봉 미형성 — 전일 마감 기준 분석(의도된 동작).
- 미국 휴장일 23:30/06:00 분석은 전일 데이터 재분석(무해).
- analysis_cache UPSERT 라 두 시각 결과가 충돌 없이 최신으로 유지.
