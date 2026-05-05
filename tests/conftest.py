"""세션 전체 fixture — 테스트 시 실제 predictions.db 오염 방지."""
import tempfile
from pathlib import Path


def pytest_configure(config):
    """pytest collection 단계 (`import main`)이 일어나기 전에 _DB_PATH를
    임시 경로로 redirect한다. 그렇지 않으면 main.py 모듈 로드 시 실행되는
    prediction_history.init_db()가 실제 data/predictions.db를 생성/갱신한다.
    """
    from src import prediction_history as ph

    tmp_dir = Path(tempfile.mkdtemp(prefix="pytest_predictions_"))
    config._predictions_tmp_dir = tmp_dir  # cleanup 시 참조용
    ph._DB_PATH = tmp_dir / "predictions.db"


def pytest_unconfigure(config):
    """세션 종료 시 임시 디렉토리 정리."""
    import shutil
    tmp_dir = getattr(config, "_predictions_tmp_dir", None)
    if tmp_dir and tmp_dir.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)
