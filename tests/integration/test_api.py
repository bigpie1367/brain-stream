"""
tests/integration/test_api.py
FastAPI TestClient로 API 엔드포인트 통합 테스트
- pipeline 실행, download_track, tag_and_import, trigger_scan 등은 mock 처리
"""
import threading
from unittest.mock import MagicMock, patch

import pytest

from src.state import mark_pending, mark_done, get_all_downloads


# ── POST /api/download ────────────────────────────────────────────────────────

def test_post_download_returns_200_and_job_id(client):
    """
    POST /api/download 가 200을 반환하고 job_id를 포함한 응답을 돌려줘야 한다.
    백그라운드 스레드 실행은 mock으로 막는다.
    """
    with patch("src.api.threading.Thread") as mock_thread_cls:
        mock_thread = MagicMock()
        mock_thread_cls.return_value = mock_thread

        resp = client.post("/api/download", json={"artist": "Radiohead", "track": "Creep"})

    assert resp.status_code == 200
    data = resp.json()
    assert "job_id" in data
    assert data["job_id"].startswith("manual-")


def test_post_download_job_id_is_unique(client):
    """POST /api/download를 두 번 호출하면 서로 다른 job_id가 반환되어야 한다."""
    with patch("src.api.threading.Thread") as mock_thread_cls:
        mock_thread_cls.return_value = MagicMock()
        resp1 = client.post("/api/download", json={"artist": "A", "track": "B"})
        resp2 = client.post("/api/download", json={"artist": "A", "track": "B"})

    assert resp1.json()["job_id"] != resp2.json()["job_id"]


def test_post_download_creates_pending_row_in_db(client, tmp_state_db):
    """POST /api/download 후 state DB에 pending 레코드가 생성되어야 한다."""
    with patch("src.api.threading.Thread") as mock_thread_cls:
        mock_thread_cls.return_value = MagicMock()
        resp = client.post("/api/download", json={"artist": "Queen", "track": "Bohemian Rhapsody"})

    job_id = resp.json()["job_id"]
    rows = get_all_downloads(tmp_state_db)
    assert any(r["mbid"] == job_id for r in rows)
    row = next(r for r in rows if r["mbid"] == job_id)
    assert row["status"] == "pending"
    assert row["source"] == "manual"


def test_post_download_missing_artist_returns_422(client):
    """artist 필드 누락 시 422 Unprocessable Entity를 반환해야 한다."""
    resp = client.post("/api/download", json={"track": "Creep"})
    assert resp.status_code == 422


def test_post_download_missing_track_returns_422(client):
    """track 필드 누락 시 422를 반환해야 한다."""
    resp = client.post("/api/download", json={"artist": "Radiohead"})
    assert resp.status_code == 422


# ── GET /api/downloads ────────────────────────────────────────────────────────

def test_get_downloads_returns_200_empty_list(client):
    """DB가 비어있을 때 GET /api/downloads는 200과 빈 리스트를 반환한다."""
    resp = client.get("/api/downloads")
    assert resp.status_code == 200
    assert resp.json() == []


def test_get_downloads_returns_existing_records(client, tmp_state_db):
    """DB에 레코드가 있으면 GET /api/downloads가 그 레코드들을 반환한다."""
    mark_pending(tmp_state_db, "mbid-test-001", "Creep", "Radiohead")
    mark_done(tmp_state_db, "mbid-test-001")
    mark_pending(tmp_state_db, "mbid-test-002", "Karma Police", "Radiohead")

    resp = client.get("/api/downloads")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    mbids = [r["mbid"] for r in data]
    assert "mbid-test-001" in mbids
    assert "mbid-test-002" in mbids


def test_get_downloads_returns_latest_first(client, tmp_state_db):
    """GET /api/downloads는 최신 항목이 앞에 오는 순서로 반환해야 한다."""
    mark_pending(tmp_state_db, "mbid-first", "Track 1", "Artist")
    mark_pending(tmp_state_db, "mbid-second", "Track 2", "Artist")

    resp = client.get("/api/downloads")
    data = resp.json()
    assert data[0]["mbid"] == "mbid-second"
    assert data[1]["mbid"] == "mbid-first"


def test_get_downloads_response_schema(client, tmp_state_db):
    """응답 각 항목에 필수 필드가 모두 있는지 확인한다."""
    mark_pending(tmp_state_db, "mbid-schema", "Track", "Artist")
    resp = client.get("/api/downloads")
    data = resp.json()
    assert len(data) == 1
    row = data[0]
    for field in ("mbid", "track_name", "artist", "status", "source", "attempts"):
        assert field in row


# ── POST /api/pipeline/run ────────────────────────────────────────────────────

def test_post_pipeline_run_returns_200(client):
    """POST /api/pipeline/run이 200을 반환하고 started 상태를 알려야 한다."""
    import src.main as main_module

    with patch.object(main_module, "run_pipeline", MagicMock()):
        with patch("src.api.threading.Thread") as mock_thread_cls:
            mock_thread_cls.return_value = MagicMock()
            resp = client.post("/api/pipeline/run")

    assert resp.status_code == 200
    assert resp.json() == {"status": "started"}


def test_post_pipeline_run_spawns_daemon_thread(client):
    """POST /api/pipeline/run이 daemon=True 스레드를 생성하는지 확인한다."""
    import src.main as main_module

    with patch.object(main_module, "run_pipeline", MagicMock()):
        with patch("src.api.threading.Thread") as mock_thread_cls:
            mock_thread = MagicMock()
            mock_thread_cls.return_value = mock_thread
            client.post("/api/pipeline/run")

    mock_thread_cls.assert_called_once()
    call_kwargs = mock_thread_cls.call_args[1]
    assert call_kwargs.get("daemon") is True
    mock_thread.start.assert_called_once()


# ── GET /api/sse/{job_id} ────────────────────────────────────────────────────

def test_get_sse_unknown_job_returns_404(client):
    """존재하지 않는 job_id로 SSE 요청 시 404를 반환해야 한다."""
    resp = client.get("/api/sse/nonexistent-job-id")
    assert resp.status_code == 404


def test_get_sse_existing_job_returns_200(client):
    """
    존재하는 job_id에 대해 SSE 엔드포인트가 200을 반환하고
    text/event-stream 미디어 타입을 사용한다.
    SSE 스트림은 무한 루프이므로 stream=True로 첫 응답만 확인한다.
    """
    import src.api as api_module
    from queue import Queue

    job_id = "manual-testjob"
    q = Queue()
    # done 이벤트를 미리 큐에 넣어 스트림이 즉시 종료되도록 한다
    q.put({"status": "done", "message": "완료"})
    api_module._job_queues[job_id] = q

    try:
        resp = client.get(f"/api/sse/{job_id}")
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
    finally:
        api_module._job_queues.pop(job_id, None)


# ── GET / (index.html) ────────────────────────────────────────────────────────

def test_get_index_returns_html(client):
    """GET / 가 HTML을 반환해야 한다."""
    with patch("builtins.open", MagicMock(
        return_value=MagicMock(
            __enter__=MagicMock(return_value=MagicMock(read=MagicMock(return_value="<html></html>"))),
            __exit__=MagicMock(return_value=False),
        )
    )):
        resp = client.get("/")
    assert resp.status_code == 200
    assert "html" in resp.headers.get("content-type", "").lower()
