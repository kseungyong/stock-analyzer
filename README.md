# Stock Analyzer

한국/미국 주식 기술적 분석 및 ML 기반 가격 예측 시스템

## 주요 기능

- **기술적 분석**: 이동평균(MA5/20/60), RSI, MACD, 볼린저 밴드, 거래량 분석 기반 매매 신호 생성
- **ML 앙상블 예측**: Prophet, Random Forest, LightGBM, LSTM, Transformer 5종 모델
- **뉴스 감성 분석**: FinBERT(ProsusAI/finbert)로 영문 뉴스 긍정/부정/중립 분류
- **HTML 리포트**: 차트 포함 종합 분석 리포트 자동 생성 및 다운로드
- **웹 대시보드**: Flask 기반 실시간 분석 대시보드 (종목 추가/삭제, 백그라운드 분석)
- **스케줄러**: 매일 지정 시간 자동 분석 및 이메일 발송
- **뉴스**: 관련 뉴스 수집 및 한국어 자동 번역

## 분석 대상 (기본 설정)

| 시장 | 종목 |
|------|------|
| 한국 | 삼성전자, SK하이닉스, 현대자동차 |
| 미국 | Apple, NVIDIA, Tesla, IonQ, Microsoft |

웹 대시보드 또는 `config/settings.yaml`에서 종목을 추가/삭제할 수 있습니다.

## 설치

```bash
git clone https://github.com/kseungyong/stock-analyzer.git
cd stock-analyzer
pip install -r requirements.txt
```

## 사용법

### 특정 종목 분석
```bash
python main.py --symbol AAPL
python main.py --symbol 005930.KS --output samsung.html
```

### 전체 종목 분석
```bash
python main.py --run-now
python main.py --run-now --output report.html
```

### 웹 대시보드
```bash
python main.py --web
python main.py --web --port 3000
```
`http://localhost:8080` 에서 대시보드에 접속하여 종목 추가/삭제 및 실시간 분석을 실행할 수 있습니다.
분석 완료 후 결과 페이지에서 HTML 리포트를 다운로드할 수 있습니다.

### 일일 자동 분석 (스케줄러)
```bash
python main.py --start-scheduler
```

다음 cron 작업이 등록됩니다:

| 시각 (KST) | 작업 | 설명 |
|---|---|---|
| 06:00 | `auto_analyze_us` | 미국 종목 자동분석 → `analysis_cache` UPSERT |
| 08:30 | `daily_email_job` | `analysis_cache` 의 다이제스트 이메일 발송 (분석 재실행 X) |
| 16:00 | `auto_analyze_korea` | 한국 종목 자동분석 → `analysis_cache` UPSERT |
| 18:00 | `backfill_daily` | 예측 이력 actual_close 백필 |

분석 결과는 SQLite `analysis_cache` 테이블에 저장되어 재시작에도 유지됩니다.
캐시 만료 시각: 한국 종목 KST 09:00, 미국 종목 NYSE 09:30 ET (서머타임 자동 처리).
대시보드/결과 페이지의 **재분석** 버튼으로 언제든 수동 갱신 가능.

## 설정

### 이메일 인증정보 (권장: 환경변수 사용)

보안을 위해 이메일 인증정보는 `.env` 파일에 설정하는 것을 권장합니다.

**1. .env 파일 생성:**
```bash
cp .env.example .env
```

**2. .env 파일 편집:**
```bash
EMAIL_SENDER=your-email@gmail.com
EMAIL_PASSWORD=your-app-password
```

> Gmail 앱 비밀번호 생성: https://support.google.com/accounts/answer/185833

**대안: settings.yaml 사용** (권장하지 않음)
```yaml
email:
  sender: your@gmail.com
  password: app-password
  recipients:
    - recipient@example.com
  smtp_server: smtp.gmail.com
  smtp_port: 587
```

### 종목 및 스케줄 설정

`config/settings.yaml`에서 종목과 스케줄을 설정합니다.

```yaml
schedule:
  hour: 8
  minute: 30
  timezone: Asia/Seoul

stocks:
  korea:
    - name: 삼성전자
      symbol: 005930.KS
  us:
    - name: Apple
      symbol: AAPL
```

## 기술 스택

| 영역 | 라이브러리 |
|------|-----------|
| 데이터 수집 | yfinance, deep-translator |
| 기술적 분석 | ta (TA-Lib) |
| ML 예측 | Prophet, scikit-learn, LightGBM, TensorFlow (LSTM), PyTorch (Transformer) |
| 감성 분석 | HuggingFace Transformers (FinBERT) |
| 시각화 | matplotlib |
| 웹 | Flask |
| 스케줄링 | APScheduler |

## 프로젝트 구조

```
stock-analyzer/
├── main.py                    # CLI 진입점
├── config/settings.yaml       # 종목/스케줄/이메일 설정
├── requirements.txt
├── tests/                     # 단위 테스트
└── src/
    ├── data_fetcher.py        # 주가 데이터 및 뉴스 수집 (재시도 포함)
    ├── technical_analysis.py  # 기술적 지표 및 매매 신호 (거래량 포함)
    ├── ml_predictor.py        # ML 앙상블 예측 + FinBERT 감성 분석
    ├── report_generator.py    # HTML 리포트 생성
    ├── validators.py          # 입력 검증 유틸리티
    ├── email_sender.py        # 이메일 발송
    ├── scheduler.py           # 일일 자동 실행
    └── web_app.py             # Flask 웹 대시보드
```

## 면책 조항

이 프로그램의 분석 결과 및 ML 예측은 참고용이며, 투자 판단의 근거로 사용해서는 안 됩니다.
