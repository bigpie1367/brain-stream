import os
import sys
import types
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

# src/ 를 sys.path에 추가해 `from src.xxx import ...` 가 동작하도록 설정
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ── 선택적 의존성 stub (Docker 전용 패키지) ──────────────────────────────────
# yt_dlp: Docker 컨테이너에서만 설치되는 패키지. 로컬 테스트 시 stub으로 대체한다.
if "yt_dlp" not in sys.modules:
    _yt_dlp_stub = types.ModuleType("yt_dlp")

    class _DownloadError(Exception):
        pass

    _utils_stub = types.ModuleType("yt_dlp.utils")
    _utils_stub.DownloadError = _DownloadError
    _yt_dlp_stub.utils = _utils_stub
    _yt_dlp_stub.YoutubeDL = MagicMock()
    sys.modules["yt_dlp"] = _yt_dlp_stub
    sys.modules["yt_dlp.utils"] = _utils_stub

# schedule: 스케줄러 라이브러리. 로컬에 없으면 stub으로 대체한다.
if "schedule" not in sys.modules:
    _schedule_stub = types.ModuleType("schedule")
    _schedule_stub.every = MagicMock()
    _schedule_stub.run_pending = MagicMock()
    sys.modules["schedule"] = _schedule_stub

# uvicorn: ASGI 서버. 테스트에서는 실행하지 않으므로 stub으로 대체한다.
if "uvicorn" not in sys.modules:
    _uvicorn_stub = types.ModuleType("uvicorn")
    _uvicorn_stub.run = MagicMock()
    sys.modules["uvicorn"] = _uvicorn_stub

from src.state import init_db

# ── 환경변수 픽스처 ─────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def dummy_env_vars(monkeypatch):
    """테스트 전체에 걸쳐 외부 서비스 자격증명을 더미값으로 설정한다."""
    monkeypatch.setenv("LB_USERNAME", "test_user")
    monkeypatch.setenv("LB_TOKEN", "test_token_dummy")
    monkeypatch.setenv("NAVIDROME_USER", "test_admin")
    monkeypatch.setenv("NAVIDROME_PASSWORD", "test_pass")


# ── DB 픽스처 ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def tmp_state_db(tmp_path):
    """tmpdir에 SQLite DB를 초기화하고 경로 문자열을 반환한다."""
    db_path = str(tmp_path / "state.db")
    init_db(db_path)
    return db_path


# ── FastAPI TestClient 픽스처 ──────────────────────────────────────────────────


@pytest.fixture()
def client(tmp_state_db, tmp_path, monkeypatch):
    """
    FastAPI TestClient를 반환한다.
    - api_module._cfg 를 tmp DB를 가리키는 더미 AppConfig로 오버라이드
    - pipeline 실행 스레드는 mock으로 막는다
    """
    import src.api as api_module
    import src.worker as worker_module
    from src.config import (
        AppConfig,
        BeetsConfig,
        DownloadConfig,
        ListenBrainzConfig,
        NavidromeConfig,
        SchedulerConfig,
    )

    staging_dir = str(tmp_path / "staging")
    music_dir = str(tmp_path / "music")
    os.makedirs(staging_dir, exist_ok=True)
    os.makedirs(music_dir, exist_ok=True)

    dummy_cfg = AppConfig(
        listenbrainz=ListenBrainzConfig(username="test_user", token="test_token"),
        download=DownloadConfig(staging_dir=staging_dir),
        beets=BeetsConfig(music_dir=music_dir),
        navidrome=NavidromeConfig(url="http://localhost:4533", username="admin", password="pass"),
        scheduler=SchedulerConfig(interval_hours=6),
        state_db=tmp_state_db,
        log_level="INFO",
        log_file=None,
    )

    # api 모듈에 테스트용 config 주입
    original_cfg = api_module._cfg
    api_module._cfg = dummy_cfg

    # 기존 job queue 오염 방지
    worker_module._job_queues.clear()

    # Rate limit store 초기화 (테스트 간 429 오염 방지)
    api_module._rate_store.clear()

    yield TestClient(api_module.app, raise_server_exceptions=True)

    # 픽스처 해제: 원래 _cfg 복원
    api_module._cfg = original_cfg
    worker_module._job_queues.clear()
