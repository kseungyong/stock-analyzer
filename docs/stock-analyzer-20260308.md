# stock-analyzer 병렬 ML 예측 엔진 구축 설계서

> 작성일: 2026-03-08
> 작성자: Claude (Claude Code /plan)
> 버전: 1.1 (감리 반영)

---

## 1. 프로젝트 개요 및 목표

### 1.1 배경

현재 `ml_predictor.py`의 `run_prediction()`은 Prophet, RandomForest, LightGBM, LSTM, Transformer 5개 모델을 `ThreadPoolExecutor`로 실행한다.
그러나 아래 문제로 인해 단일 종목 분석에 **20~40초**가 소요된다.

- **Python GIL**: CPU 집약적 모델(LSTM, Transformer)의 실질적 병렬화 불가
- **매 요청마다 재학습**: 학습 결과를 메모리 캐시에만 저장 → 서버 재시작 시 초기화
- **LSTM 병목**: 20 epoch 학습이 전체 대기 시간을 결정

### 1.2 목표

- 단일 종목 분석 시간: **20~40s → 5~10s** (캐시 히트 시 0.5s 이내)
- 모델별 진정한 병렬 실행: `ProcessPoolExecutor` 도입 (GIL 우회)
- 학습 결과 디스크 캐시: 서버 재시작 후에도 캐시 유효

### 1.3 범위

**포함:**
- `src/ml_predictor.py` 리팩토링 (top-level 함수 정리, TSTransformer 클래스 이동)
- `src/prediction_engine.py` 신규 작성 (병렬 엔진 + 디스크 캐시)
- `main.py` 연동 (`run_prediction` → `PredictionEngine` 교체)
- 기존 메모리 캐시(`_prediction_cache`) 제거

**제외:**
- 모델 정확도 개선 및 구조 변경
- FinBERT 감성 분석 병렬화

---

## 2. 현재 vs 개선 아키텍처

### 현재 구조 (문제)

```
run_prediction(df)
  └── ThreadPoolExecutor(max_workers=5)
        ├── prophet()         ~3s  ┐
        ├── random_forest()   ~1s  │ GIL로 인해 CPU 작업은
        ├── lightgbm()        ~1s  │ 실질적으로 순차 실행
        ├── lstm()           ~20s  │
        └── transformer()    ~10s  ┘
  결과: LSTM 종료까지 대기 ≈ 20~30s
```

### 개선 구조

```
PredictionEngine.run(df, symbol)
  ├── 디스크 캐시 확인 → HIT 시 즉시 반환 (<0.5s)
  └── MISS 시 _pool (사전 생성 ProcessPoolExecutor) 재사용
        ├── [Process 1] predict_prophet(df)        ~3s ┐
        ├── [Process 2] predict_direction(df)      ~1s │ 진정한
        ├── [Process 3] predict_direction_lgbm(df) ~1s │ 병렬 실행
        ├── [Process 4] predict_direction_lstm(df) ~?s │ (GIL 우회)
        └── [Process 5] predict_direction_transf.. ~?s ┘
      as_completed() → 빠른 결과부터 수집
      결과 캐시 저장 (디스크, 키: symbol+날짜, TTL 1시간)
  기대치: max(각 모델 실제 소요시간), Pool 재사용으로 오버헤드 최소화
```

**성능 기대치 (수정):**
- ProcessPoolExecutor는 `max(모든 모델 시간)`이 병목 (코어 수로 나누는 게 아님)
- Pool 사전 생성으로 프로세스 초기화 오버헤드(3~8s)를 최초 1회로 한정
- 캐시 히트 시 < 0.5s

---

## 3. 작업 단계별 상세 계획

| 단계 | 작업 내용 | 비고 |
|------|-----------|------|
| 1단계 | `ml_predictor.py` 리팩토링 | TSTransformer top-level 이동, lambda 제거 |
| 2단계 | `src/prediction_engine.py` 작성 | Pool 사전 생성, 디스크 캐시, 보안 검증 |
| 3단계 | `main.py` 연동 | PredictionEngine 싱글톤 교체 |

### 단계별 상세 설명

#### 1단계: `ml_predictor.py` 리팩토링

- **목적**: `ProcessPoolExecutor`는 pickle 가능한 top-level 함수/클래스만 지원
- **작업 내용**:
  - `TSTransformer(nn.Module)` 클래스를 함수 내부에서 **모듈 최상위로 이동**
  - `_run_prophet()` 내부 함수를 top-level `predict_prophet(df)` 함수로 분리
  - `run_prediction()` 내 `lambda` 제거
  - 기존 메모리 캐시(`_prediction_cache`, `_PREDICTION_CACHE_TTL`) 제거
- **완료 기준**: 모든 모델 함수와 클래스가 모듈 최상위에 정의됨

#### 2단계: `prediction_engine.py` 작성

- **핵심 설계 결정**:
  - `mp.get_context('spawn')` 사용 (Flask 환경에서 `set_start_method` 호출 불가 대응)
  - Pool을 모듈 수준에서 **사전 생성**하여 요청마다 프로세스 생성 오버헤드 제거
  - `_worker_init()` initializer로 자식 프로세스 환경 설정 (TF 로그 억제 등)
  - 캐시 키: `{symbol}_{YYYY-MM-DD}` (날짜 포함으로 최신 데이터 보장)
  - Path traversal 방어: 캐시 키 sanitize + `resolve()` 검증
  - 원자적 캐시 쓰기: tmp 파일 → rename

- **구현 스켈레톤**:
  ```python
  _SPAWN_CTX = mp.get_context('spawn')
  _pool = ProcessPoolExecutor(max_workers=5,
                              mp_context=_SPAWN_CTX,
                              initializer=_worker_init)
  atexit.register(lambda: _pool.shutdown(wait=False))

  class PredictionEngine:
      CACHE_DIR = Path(".cache/predictions")
      TTL = 3600

      def run(self, df, cache_key):
          keyed = f"{cache_key}_{df.index[-1].date()}"
          cached = self._load_cache(keyed)
          if cached: return cached
          results = self._run_parallel(df)
          self._save_cache(keyed, results)
          return results

      def _run_parallel(self, df):
          tasks = {
              _pool.submit(predict_prophet, df): "prophet",
              _pool.submit(predict_direction, df): "random_forest",
              ...
          }
          results = {}
          for f in as_completed(tasks):
              results[tasks[f]] = f.result()
          return results

      def _safe_cache_path(self, key):
          safe_key = re.sub(r'[^\w.\-]', '_', key)
          path = (self.CACHE_DIR / f"{safe_key}.pkl").resolve()
          if not str(path).startswith(str(self.CACHE_DIR.resolve())):
              raise ValueError(f"Invalid cache key: {key}")
          return path

      def _save_cache(self, key, data):
          path = self._safe_cache_path(key)
          path.parent.mkdir(parents=True, exist_ok=True)
          tmp = path.with_suffix('.tmp')
          tmp.write_bytes(pickle.dumps(data))
          tmp.replace(path)  # 원자적 rename
  ```

#### 3단계: `main.py` 연동

- `run_prediction` import 제거
- `PredictionEngine` 싱글톤 생성
- `analyze_stock()` 내 호출 교체

---

## 4. 예상 파일 / 폴더 구조

```
stock-analyzer/
├── src/
│   ├── ml_predictor.py        # 리팩토링: top-level 함수/클래스, 메모리 캐시 제거
│   ├── prediction_engine.py   # 신규: 병렬 엔진 + 디스크 캐시
│   └── ...
├── main.py                    # PredictionEngine 연동
├── .cache/
│   └── predictions/           # 디스크 캐시 (.gitignore 추가)
│       └── AAPL_2026-03-08.pkl
└── docs/
    └── stock-analyzer-20260308.md
```

---

## 5. 주요 기술 스택 및 의존성

| 항목 | 기술/도구 | 비고 |
|------|----------|------|
| 병렬 실행 | `ProcessPoolExecutor` + `mp.get_context('spawn')` | GIL 우회, Flask 호환 |
| 디스크 캐시 | `pickle` + 원자적 rename | DataFrame 제외, 예측 결과만 저장 |
| Pool 수명 | 모듈 수준 싱글톤 + `atexit` 정리 | 프로세스 초기화 오버헤드 1회 한정 |
| 캐시 키 | `{symbol}_{YYYY-MM-DD}` | 날짜 포함으로 최신 데이터 보장 |

---

## 6. 위험 요소 및 대응 방안

| 위험 요소 | 심각도 | 대응 방안 |
|----------|--------|----------|
| TF/PyTorch spawn 초기화 오버헤드 | 🔴 | Pool 사전 생성으로 최초 1회 한정 |
| TSTransformer pickle 불가 | 🔴 | 모듈 top-level로 클래스 이동 |
| 캐시 키에 날짜 미포함 | 🔴 | `{symbol}_{date}` 형태 사용 |
| Flask 동시 요청 시 Process 폭발 | 🔴 | Pool 재사용 (요청마다 생성 금지) |
| Path traversal 공격 | 🟡 | 캐시 키 sanitize + resolve() 검증 |
| pickle 역직렬화 코드 실행 | 🟡 | 내부 전용 도구, `.cache/` 디렉토리 권한 관리 |
| 자식 프로세스 환경 변수 미상속 | 🟡 | `initializer=_worker_init`으로 재설정 |
| Flask debug=True + spawn 충돌 | 🟡 | `run_web(debug=False)` 기본값 유지 |
| 멀티프로세스 로깅 충돌 | 🟡 | `_worker_init`에서 로거 재설정 |

---

## 7. 변경 이력

| 날짜 | 버전 | 변경 내용 |
|------|------|----------|
| 2026-03-08 | 1.0 | 최초 작성 |
| 2026-03-08 | 1.1 | 감리 반영: spawn 컨텍스트, Pool 사전 생성, 캐시 키 날짜 포함, TSTransformer top-level, path traversal 방어, 성능 계산식 수정 |
