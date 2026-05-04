"""Gunicorn 프로덕션 설정."""
import multiprocessing
import os

# macOS: Prophet/ML 라이브러리가 fork() 호출 시 ObjC(libcurl) 충돌 방지
os.environ.setdefault("OBJC_DISABLE_INITIALIZE_FORK_SAFETY", "YES")

# 바인딩
bind = f"0.0.0.0:{os.environ.get('PORT', '8080')}"

# 워커 1개 + 멀티스레드: _jobs가 인메모리 딕셔너리라 멀티 워커 시
# 워커 간 상태 공유가 안 돼 "작업을 찾을 수 없습니다" 오류 발생
workers = 1
worker_class = "gthread"
threads = min(multiprocessing.cpu_count() * 2, 8)

# 타임아웃 — 분석은 백그라운드 스레드에서 실행되므로 요청 자체는 빠름
timeout = 120
keepalive = 5

# 로깅
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info")
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(D)sµs'

# macOS에서 libcurl-impersonate(yfinance)가 fork 이후 ObjC 런타임 초기화 시 크래시
# 발생하므로 preload_app 비활성화 (각 워커가 독립적으로 앱 로드)
preload_app = False
