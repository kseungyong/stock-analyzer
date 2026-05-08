"""세션 전체 fixture — 테스트 시 실제 predictions.db 오염 방지."""
import os
import tempfile
from pathlib import Path

# web_app.py 의 module-level load_dotenv() 가 .env 의 ENABLE_BASIC_AUTH=1 을
# os.environ 에 재주입 → 모든 테스트 요청이 401 UNAUTHORIZED.
# 단순 pop 만으로는 부족 — load_dotenv 자체를 no-op 으로 monkeypatch (test only).
import sys
import types
_noop_dotenv = types.ModuleType("dotenv")
_noop_dotenv.load_dotenv = lambda *args, **kwargs: False  # type: ignore[attr-defined]
sys.modules.setdefault("dotenv", _noop_dotenv)
# 이미 import 된 경우도 보호 — pop env (재주입 후라도 web_app._basic_auth_on 평가 전이면 효과 있음)
os.environ.pop("ENABLE_BASIC_AUTH", None)
os.environ.pop("BASIC_AUTH_USERS", None)
os.environ.pop("BASIC_AUTH_USERNAME", None)
os.environ.pop("BASIC_AUTH_PASSWORD", None)


def pytest_configure(config):
    """pytest collection 단계 (`import main`)이 일어나기 전에
    prediction_history와 analysis_cache 의 _DB_PATH 를
    임시 경로로 redirect 한다.
    """
    from src import prediction_history as ph
    from src import analysis_cache as ac

    tmp_dir = Path(tempfile.mkdtemp(prefix="pytest_predictions_"))
    config._predictions_tmp_dir = tmp_dir
    db_path = tmp_dir / "predictions.db"
    ph._DB_PATH = db_path
    ac._DB_PATH = db_path  # 동일 파일 공유
    # 스키마를 미리 초기화 — index 라우트가 analysis_cache.get 을 호출하므로
    # 모든 테스트가 시작되기 전 테이블이 존재해야 한다.
    ac.init_db()


def pytest_unconfigure(config):
    import shutil
    tmp_dir = getattr(config, "_predictions_tmp_dir", None)
    if tmp_dir and tmp_dir.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)
