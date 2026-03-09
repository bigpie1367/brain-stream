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


# ── DELETE /api/downloads/{mbid} ─────────────────────────────────────────────

def test_delete_download_returns_404_when_not_found(client):
    """존재하지 않는 mbid 삭제 시 404를 반환해야 한다."""
    resp = client.delete("/api/downloads/nonexistent-mbid")
    assert resp.status_code == 404


def test_delete_download_removes_db_record(client, tmp_state_db):
    """삭제 후 state DB에서 레코드가 제거된다."""
    from src.state import mark_done

    mark_pending(tmp_state_db, "mbid-del", "Track", "Artist")
    mark_done(tmp_state_db, "mbid-del")

    resp = client.delete("/api/downloads/mbid-del")
    assert resp.status_code == 200
    data = resp.json()
    assert data["deleted"] is True

    rows = get_all_downloads(tmp_state_db)
    assert not any(r["mbid"] == "mbid-del" for r in rows)


def test_delete_download_removes_file_when_file_path_set(client, tmp_state_db, tmp_path):
    """file_path가 DB에 저장돼 있으면 실제 파일을 삭제한다."""
    from src.state import mark_done

    # 실제 파일 생성
    dummy_file = tmp_path / "dummy.flac"
    dummy_file.write_bytes(b"fake audio data")

    mark_pending(tmp_state_db, "mbid-file", "Track", "Artist")
    mark_done(tmp_state_db, "mbid-file", file_path=str(dummy_file))

    with patch("src.api.threading.Thread") as mock_thread_cls:
        mock_thread_cls.return_value = MagicMock()
        resp = client.delete("/api/downloads/mbid-file")

    assert resp.status_code == 200
    data = resp.json()
    assert data["files_removed"] == 1
    assert not dummy_file.exists()


def test_delete_download_no_file_path_removes_only_db(client, tmp_state_db):
    """file_path가 None이면 파일 삭제 없이 DB 레코드만 삭제한다."""
    mark_pending(tmp_state_db, "mbid-nofile", "Track", "Artist")

    resp = client.delete("/api/downloads/mbid-nofile")
    assert resp.status_code == 200
    data = resp.json()
    assert data["deleted"] is True
    assert data["files_removed"] == 0


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


# ── GET /api/rematch/search ───────────────────────────────────────────────────

def test_rematch_search_returns_candidates(client):
    """MB 검색이 성공하면 candidates 목록이 반환된다."""
    with patch("src.api.mb_search_recording", return_value=["rec-id-001"]):
        with patch(
            "src.api.mb_album_from_recording_id",
            return_value=("OK Computer", ["album-id-001"]),
        ):
            resp = client.get(
                "/api/rematch/search",
                params={"artist": "Radiohead", "track": "Karma Police"},
            )

    assert resp.status_code == 200
    data = resp.json()
    assert "candidates" in data
    assert len(data["candidates"]) == 1
    c = data["candidates"][0]
    assert c["source"] == "musicbrainz"
    assert c["mb_recording_id"] == "rec-id-001"
    assert c["mb_album_id"] == "album-id-001"
    assert c["album_name"] == "OK Computer"
    assert c["artist_name"] == "Radiohead"
    assert c["cover_url"] == "https://coverartarchive.org/release/album-id-001/front"


def test_rematch_search_empty_when_no_recording(client):
    """MB 검색 결과가 없으면 빈 candidates를 반환한다."""
    with patch("src.api.mb_search_recording", return_value=[]):
        resp = client.get(
            "/api/rematch/search",
            params={"artist": "Unknown", "track": "Nonexistent"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"candidates": []}


def test_rematch_search_empty_when_no_album(client):
    """recording_id는 있지만 앨범 조회 실패 시 빈 candidates를 반환한다."""
    with patch("src.api.mb_search_recording", return_value=["rec-id-002"]):
        with patch("src.api.mb_album_from_recording_id", return_value=("", [])):
            resp = client.get(
                "/api/rematch/search",
                params={"artist": "Artist", "track": "Track"},
            )

    assert resp.status_code == 200
    assert resp.json() == {"candidates": []}


def test_rematch_search_unsupported_source_returns_400(client):
    """source가 musicbrainz가 아니면 400을 반환한다."""
    resp = client.get(
        "/api/rematch/search",
        params={"artist": "Artist", "track": "Track", "source": "itunes"},
    )
    assert resp.status_code == 400


def test_rematch_search_multiple_album_candidates(client):
    """앨범 후보가 여러 개일 때 모두 반환한다."""
    album_ids = ["album-a", "album-b", "album-c"]
    with patch("src.api.mb_search_recording", return_value=["rec-001"]):
        with patch(
            "src.api.mb_album_from_recording_id",
            return_value=("The Bends", album_ids),
        ):
            resp = client.get(
                "/api/rematch/search",
                params={"artist": "Radiohead", "track": "Fake Plastic Trees"},
            )

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["candidates"]) == 3
    returned_ids = [c["mb_album_id"] for c in data["candidates"]]
    assert returned_ids == album_ids


# ── POST /api/rematch/apply ───────────────────────────────────────────────────

def test_rematch_apply_success(client, tmp_path):
    """정상 흐름: 파일 존재 + getSong 성공 + MB release 조회 성공 → 200 반환."""
    dummy_audio = tmp_path / "track.flac"
    dummy_audio.write_bytes(b"fake flac data")

    # Navidrome getSong이 반환하는 path는 music root 기준 상대경로
    # client fixture의 music_dir은 tmp_path/"music" 이지만
    # rematch_apply는 /app/data/music/{path}로 절대경로를 구성하므로
    # 파일을 /app/data/music/... 에 생성하는 대신 os.path.exists를 mock한다.
    song_relative_path = "Artist/Album/track.flac"

    with patch("src.api._navidrome_get_song", return_value={"path": song_relative_path}):
        with patch("src.api.os.path.exists", return_value=True):
            with patch("src.api.requests.get") as mock_get:
                mock_get.return_value = MagicMock(
                    status_code=200,
                    json=MagicMock(return_value={"title": "OK Computer"}),
                    raise_for_status=MagicMock(),
                )
                with patch("src.api.write_album_tag") as mock_write:
                    with patch("src.api.embed_cover_art", return_value=True):
                        with patch("src.api.threading.Thread") as mock_thread_cls:
                            mock_thread_cls.return_value = MagicMock()
                            resp = client.post(
                                "/api/rematch/apply",
                                json={
                                    "song_id": "nav-song-123",
                                    "mb_recording_id": "rec-001",
                                    "mb_album_id": "album-001",
                                },
                            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["album_name"] == "OK Computer"
    mock_write.assert_called_once()


def test_rematch_apply_song_not_found_returns_500(client):
    """getSong 실패 시 500을 반환한다."""
    with patch("src.api._navidrome_get_song", side_effect=RuntimeError("getSong error")):
        resp = client.post(
            "/api/rematch/apply",
            json={
                "song_id": "bad-id",
                "mb_recording_id": "rec-001",
                "mb_album_id": "album-001",
            },
        )
    assert resp.status_code == 500


def test_rematch_apply_file_not_found_returns_404(client):
    """getSong 성공 but 파일이 없으면 404를 반환한다."""
    with patch("src.api._navidrome_get_song", return_value={"path": "Artist/track.flac"}):
        with patch("src.api.os.path.exists", return_value=False):
            resp = client.post(
                "/api/rematch/apply",
                json={
                    "song_id": "nav-song-123",
                    "mb_recording_id": "rec-001",
                    "mb_album_id": "album-001",
                },
            )
    assert resp.status_code == 404


def test_rematch_apply_mb_release_lookup_fails_returns_500(client):
    """MB release 조회 실패 시 500을 반환한다."""
    with patch("src.api._navidrome_get_song", return_value={"path": "Artist/track.flac"}):
        with patch("src.api.os.path.exists", return_value=True):
            with patch("src.api.requests.get", side_effect=Exception("network error")):
                resp = client.post(
                    "/api/rematch/apply",
                    json={
                        "song_id": "nav-song-123",
                        "mb_recording_id": "rec-001",
                        "mb_album_id": "album-001",
                    },
                )
    assert resp.status_code == 500


def test_rematch_apply_missing_song_id_returns_422(client):
    """필수 필드 누락 시 422를 반환한다."""
    resp = client.post(
        "/api/rematch/apply",
        json={"mb_recording_id": "rec-001", "mb_album_id": "album-001"},
    )
    assert resp.status_code == 422
