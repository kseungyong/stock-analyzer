# Stock Analyzer

한국/미국 주식 기술적 분석 및 ML 기반 가격 예측 시스템

## 주요 기능

- **기술적 분석**: 이동평균(MA5/20/60), RSI, MACD, 볼린저 밴드 기반 매매 신호 생성
- **ML 예측**: Prophet, Random Forest, LightGBM, LSTM 앙상블 예측
- **HTML 리포트**: 차트 포함 종합 분석 리포트 자동 생성
- **웹 대시보드**: Flask 기반 실시간 분석 대시보드
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

### 일일 자동 분석 (스케줄러)
```bash
python main.py --start-scheduler
```
`config/settings.yaml`에 설정된 시간(기본 08:30 KST)에 전체 분석 후 이메일 발송합니다.

## 설정

`config/settings.yaml`에서 종목, 스케줄, 이메일을 설정합니다.

```yaml
email:
  sender: your@gmail.com
  password: app-password
  recipients:
    - recipient@example.com
  smtp_server: smtp.gmail.com
  smtp_port: 587

schedule:
  hour: 8
  minute: 30
  timezone: Asia/Seoul
```

## 기술 스택

| 영역 | 라이브러리 |
|------|-----------|
| 데이터 수집 | yfinance, deep-translator |
| 기술적 분석 | ta (TA-Lib) |
| ML/예측 | Prophet, scikit-learn, LightGBM, TensorFlow (LSTM) |
| 시각화 | matplotlib |
| 웹 | Flask |
| 스케줄링 | APScheduler |

## 프로젝트 구조

```
stock-analyzer/
├── main.py                    # CLI 진입점
├── config/settings.yaml       # 종목/스케줄/이메일 설정
├── requirements.txt
└── src/
    ├── data_fetcher.py        # 주가 데이터 및 뉴스 수집
    ├── technical_analysis.py  # 기술적 지표 및 매매 신호
    ├── ml_predictor.py        # ML 앙상블 예측
    ├── report_generator.py    # HTML 리포트 생성
    ├── email_sender.py        # 이메일 발송
    ├── scheduler.py           # 일일 자동 실행
    └── web_app.py             # Flask 웹 대시보드
```

## 면책 조항

이 프로그램의 분석 결과 및 ML 예측은 참고용이며, 투자 판단의 근거로 사용해서는 안 됩니다.
