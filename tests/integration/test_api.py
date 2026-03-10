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


# ── GET /api/downloads/{mbid}/detail ─────────────────────────────────────────

def test_get_download_detail_not_found_returns_404(client):
    """DB에 없는 mbid 조회 시 404를 반환한다."""
    resp = client.get("/api/downloads/nonexistent-mbid/detail")
    assert resp.status_code == 404


def test_get_download_detail_no_file_path_returns_nulls(client, tmp_state_db):
    """file_path가 None이면 album_name, year, cover_art 모두 null을 반환한다."""
    from src.state import mark_pending
    mark_pending(tmp_state_db, "mbid-nofile", "Track", "Artist")

    resp = client.get("/api/downloads/mbid-nofile/detail")
    assert resp.status_code == 200
    data = resp.json()
    assert data["album_name"] is None
    assert data["year"] is None
    assert data["cover_art"] is None


def test_get_download_detail_file_missing_returns_nulls(client, tmp_state_db):
    """file_path가 DB에 있지만 실제 파일이 없으면 nulls를 반환한다."""
    from src.state import mark_pending, mark_done
    mark_pending(tmp_state_db, "mbid-gone", "Track", "Artist")
    mark_done(tmp_state_db, "mbid-gone", file_path="/nonexistent/track.flac")

    resp = client.get("/api/downloads/mbid-gone/detail")
    assert resp.status_code == 200
    data = resp.json()
    assert data["album_name"] is None
    assert data["year"] is None
    assert data["cover_art"] is None


def test_get_download_detail_flac_reads_album_tag(client, tmp_state_db, tmp_path):
    """FLAC 파일의 album 태그를 정상적으로 읽어 반환한다."""
    dummy_file = tmp_path / "track.flac"
    dummy_file.write_bytes(b"fake flac data")

    from src.state import mark_pending, mark_done
    mark_pending(tmp_state_db, "mbid-flac", "Lose Yourself", "Eminem")
    mark_done(tmp_state_db, "mbid-flac", file_path=str(dummy_file))

    mock_audio = MagicMock()
    mock_audio.get.side_effect = lambda key, default=None: (
        ["The Marshall Mathers LP"] if key == "album" else default
    )
    mock_audio.pictures = []

    with patch("src.api.mutagen.flac.FLAC", return_value=mock_audio):
        resp = client.get("/api/downloads/mbid-flac/detail")

    assert resp.status_code == 200
    data = resp.json()
    assert data["album_name"] == "The Marshall Mathers LP"
    assert "file_path" not in data


def test_get_download_detail_opus_reads_album_tag(client, tmp_state_db, tmp_path):
    """OggOpus 파일의 album 태그를 정상적으로 읽어 반환한다."""
    dummy_file = tmp_path / "track.opus"
    dummy_file.write_bytes(b"fake opus data")

    from src.state import mark_pending, mark_done
    mark_pending(tmp_state_db, "mbid-opus", "Lose Yourself", "Eminem")
    mark_done(tmp_state_db, "mbid-opus", file_path=str(dummy_file))

    mock_audio = MagicMock()
    mock_audio.get.side_effect = lambda key, default=None: (
        ["The Marshall Mathers LP"] if key == "album" else default
    )

    with patch("src.api.mutagen.oggopus.OggOpus", return_value=mock_audio):
        resp = client.get("/api/downloads/mbid-opus/detail")

    assert resp.status_code == 200
    data = resp.json()
    assert data["album_name"] == "The Marshall Mathers LP"


def test_get_download_detail_flac_reads_year_tag(client, tmp_state_db, tmp_path):
    """FLAC 파일의 date 태그에서 year를 정상적으로 읽어 반환한다."""
    dummy_file = tmp_path / "track.flac"
    dummy_file.write_bytes(b"fake flac data")

    from src.state import mark_pending, mark_done
    mark_pending(tmp_state_db, "mbid-flac-yr", "Lose Yourself", "Eminem")
    mark_done(tmp_state_db, "mbid-flac-yr", file_path=str(dummy_file))

    mock_audio = MagicMock()
    mock_audio.get.side_effect = lambda key, default=None: (
        ["The Marshall Mathers LP"] if key == "album"
        else ["2000"] if key == "date"
        else default
    )
    mock_audio.pictures = []

    with patch("src.api.mutagen.flac.FLAC", return_value=mock_audio):
        resp = client.get("/api/downloads/mbid-flac-yr/detail")

    assert resp.status_code == 200
    data = resp.json()
    assert data["year"] == "2000"


def test_get_download_detail_flac_reads_cover_art(client, tmp_state_db, tmp_path):
    """FLAC 파일의 커버아트를 base64 data URL로 반환한다."""
    import base64 as b64mod

    dummy_file = tmp_path / "track.flac"
    dummy_file.write_bytes(b"fake flac data")

    from src.state import mark_pending, mark_done
    mark_pending(tmp_state_db, "mbid-flac-art", "Lose Yourself", "Eminem")
    mark_done(tmp_state_db, "mbid-flac-art", file_path=str(dummy_file))

    fake_img_bytes = b"\xff\xd8\xff\xe0fake jpeg"
    mock_pic = MagicMock()
    mock_pic.mime = "image/jpeg"
    mock_pic.data = fake_img_bytes

    mock_audio = MagicMock()
    mock_audio.get.return_value = None
    mock_audio.pictures = [mock_pic]

    with patch("src.api.mutagen.flac.FLAC", return_value=mock_audio):
        resp = client.get("/api/downloads/mbid-flac-art/detail")

    assert resp.status_code == 200
    data = resp.json()
    expected = f"data:image/jpeg;base64,{b64mod.b64encode(fake_img_bytes).decode()}"
    assert data["cover_art"] == expected


def test_get_download_detail_opus_reads_cover_art(client, tmp_state_db, tmp_path):
    """OggOpus 파일의 METADATA_BLOCK_PICTURE 태그로 커버아트를 읽어 반환한다."""
    import base64 as b64mod
    from mutagen.flac import Picture

    dummy_file = tmp_path / "track.opus"
    dummy_file.write_bytes(b"fake opus data")

    from src.state import mark_pending, mark_done
    mark_pending(tmp_state_db, "mbid-opus-art", "Lose Yourself", "Eminem")
    mark_done(tmp_state_db, "mbid-opus-art", file_path=str(dummy_file))

    fake_img_bytes = b"\xff\xd8\xff\xe0fake jpeg"
    pic = Picture()
    pic.mime = "image/jpeg"
    pic.data = fake_img_bytes
    encoded_pic = b64mod.b64encode(pic.write()).decode()

    mock_audio = MagicMock()
    mock_audio.get.side_effect = lambda key, default=None: (
        [encoded_pic] if key == "METADATA_BLOCK_PICTURE" else default
    )

    with patch("src.api.mutagen.oggopus.OggOpus", return_value=mock_audio):
        resp = client.get("/api/downloads/mbid-opus-art/detail")

    assert resp.status_code == 200
    data = resp.json()
    assert data["cover_art"] is not None
    assert data["cover_art"].startswith("data:image/jpeg;base64,")


def test_get_download_detail_no_album_tag_returns_null(client, tmp_state_db, tmp_path):
    """파일은 존재하지만 album 태그가 없으면 album_name이 null이다."""
    dummy_file = tmp_path / "track.flac"
    dummy_file.write_bytes(b"fake flac data")

    from src.state import mark_pending, mark_done
    mark_pending(tmp_state_db, "mbid-notag", "Track", "Artist")
    mark_done(tmp_state_db, "mbid-notag", file_path=str(dummy_file))

    mock_audio = MagicMock()
    mock_audio.get.return_value = None
    mock_audio.pictures = []

    with patch("src.api.mutagen.flac.FLAC", return_value=mock_audio):
        resp = client.get("/api/downloads/mbid-notag/detail")

    assert resp.status_code == 200
    data = resp.json()
    assert data["album_name"] is None
    assert data["year"] is None
    assert data["cover_art"] is None


def test_get_download_detail_mutagen_exception_returns_null_album(client, tmp_state_db, tmp_path):
    """mutagen 파싱 중 예외 발생 시 album_name이 null이고 에러 없이 200을 반환한다."""
    dummy_file = tmp_path / "track.flac"
    dummy_file.write_bytes(b"corrupted data")

    from src.state import mark_pending, mark_done
    mark_pending(tmp_state_db, "mbid-corrupt", "Track", "Artist")
    mark_done(tmp_state_db, "mbid-corrupt", file_path=str(dummy_file))

    with patch("src.api.mutagen.flac.FLAC", side_effect=Exception("corrupted")):
        resp = client.get("/api/downloads/mbid-corrupt/detail")

    assert resp.status_code == 200
    data = resp.json()
    assert data["album_name"] is None
    assert "file_path" not in data


# ── GET /api/rematch/search ───────────────────────────────────────────────────

def _mb_search_response(recordings):
    """requests.get mock 반환값 헬퍼."""
    mock = MagicMock()
    mock.raise_for_status = MagicMock()
    mock.json.return_value = {"recordings": recordings}
    return mock


def _make_recording(rec_id, releases, artist_name="Radiohead"):
    return {
        "id": rec_id,
        "artist-credit": [{"artist": {"name": artist_name}, "joinphrase": ""}],
        "releases": releases,
    }


def _make_release(release_id, title, date="2000-01-01"):
    return {"id": release_id, "title": title, "date": date}


def test_rematch_search_returns_candidates(client):
    """MB stage1 검색이 성공하면 candidates 목록이 반환된다."""
    release = _make_release("album-id-001", "OK Computer")
    rec = _make_recording("rec-id-001", [release])

    with patch("src.api.requests.get", return_value=_mb_search_response([rec])):
        with patch("src.api.time.sleep"):
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
    """두 stage 모두 결과 없으면 빈 candidates를 반환한다."""
    with patch("src.api.requests.get", return_value=_mb_search_response([])):
        with patch("src.api.time.sleep"):
            resp = client.get(
                "/api/rematch/search",
                params={"artist": "Unknown", "track": "Nonexistent"},
            )

    assert resp.status_code == 200
    assert resp.json() == {"candidates": []}


def test_rematch_search_stage2_fallback(client):
    """stage1 결과 없을 때 stage2로 폴백하여 결과를 반환한다."""
    release = _make_release("album-fallback", "Some Album")
    rec = _make_recording("rec-fallback", [release])

    call_count = 0

    def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _mb_search_response([])   # stage1: 빈 결과
        return _mb_search_response([rec])    # stage2: 결과 있음

    with patch("src.api.requests.get", side_effect=side_effect):
        with patch("src.api.time.sleep"):
            resp = client.get(
                "/api/rematch/search",
                params={"artist": "Artist", "track": "Track"},
            )

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["candidates"]) == 1
    assert data["candidates"][0]["mb_album_id"] == "album-fallback"


def test_rematch_search_unsupported_source_returns_400(client):
    """source가 musicbrainz가 아니면 400을 반환한다."""
    resp = client.get(
        "/api/rematch/search",
        params={"artist": "Artist", "track": "Track", "source": "itunes"},
    )
    assert resp.status_code == 400


def test_rematch_search_multiple_album_candidates(client):
    """recording당 여러 release가 있으면 모두 후보로 반환한다."""
    releases = [
        _make_release("album-a", "The Bends", "1995-03-13"),
        _make_release("album-b", "The Bends (Remaster)", "2016-01-01"),
        _make_release("album-c", "The Bends (Japan)", "1995-04-01"),
    ]
    rec = _make_recording("rec-001", releases)

    with patch("src.api.requests.get", return_value=_mb_search_response([rec])):
        with patch("src.api.time.sleep"):
            resp = client.get(
                "/api/rematch/search",
                params={"artist": "Radiohead", "track": "Fake Plastic Trees"},
            )

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["candidates"]) == 3
    returned_ids = [c["mb_album_id"] for c in data["candidates"]]
    assert returned_ids == ["album-a", "album-b", "album-c"]


def test_rematch_search_year_extracted_from_date(client):
    """release date에서 year가 올바르게 추출된다."""
    release = _make_release("album-yr", "OK Computer", "1997-06-16")
    rec = _make_recording("rec-yr", [release])

    with patch("src.api.requests.get", return_value=_mb_search_response([rec])):
        with patch("src.api.time.sleep"):
            resp = client.get(
                "/api/rematch/search",
                params={"artist": "Radiohead", "track": "Paranoid Android"},
            )

    data = resp.json()
    assert data["candidates"][0]["year"] == 1997


def test_rematch_search_deduplicates_album_ids(client):
    """동일 album_id가 여러 recording에 걸쳐 있을 때 중복 제거한다."""
    release = _make_release("shared-album", "Shared Album")
    rec1 = _make_recording("rec-dup-1", [release])
    rec2 = _make_recording("rec-dup-2", [release])

    with patch("src.api.requests.get", return_value=_mb_search_response([rec1, rec2])):
        with patch("src.api.time.sleep"):
            resp = client.get(
                "/api/rematch/search",
                params={"artist": "Artist", "track": "Track"},
            )

    data = resp.json()
    album_ids = [c["mb_album_id"] for c in data["candidates"]]
    assert album_ids.count("shared-album") == 1


def test_rematch_search_mb_request_error_returns_empty(client):
    """MB API 호출 중 예외 발생 시 빈 candidates를 반환한다."""
    with patch("src.api.requests.get", side_effect=Exception("network error")):
        with patch("src.api.time.sleep"):
            resp = client.get(
                "/api/rematch/search",
                params={"artist": "Artist", "track": "Track"},
            )

    assert resp.status_code == 200
    assert resp.json() == {"candidates": []}


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


def test_rematch_apply_missing_song_id_and_mbid_returns_422(client):
    """song_id와 mbid 둘 다 없으면 422를 반환한다."""
    resp = client.post(
        "/api/rematch/apply",
        json={"mb_recording_id": "rec-001", "mb_album_id": "album-001"},
    )
    assert resp.status_code == 422


def test_rematch_apply_via_mbid_success(client, tmp_state_db, tmp_path):
    """mbid 경로: state.db에서 file_path 조회 후 재태깅 성공 → 200 반환."""
    dummy_audio = tmp_path / "track.flac"
    dummy_audio.write_bytes(b"fake flac data")

    from src.state import mark_pending, mark_done
    mark_pending(tmp_state_db, "manual-abc12345", "Lose Yourself", "Eminem")
    mark_done(tmp_state_db, "manual-abc12345", file_path=str(dummy_audio))

    with patch("src.api.requests.get") as mock_get:
        mock_get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={"title": "The Marshall Mathers LP"}),
            raise_for_status=MagicMock(),
        )
        with patch("src.api.write_album_tag") as mock_write:
            with patch("src.api.embed_cover_art", return_value=True):
                with patch("src.api.threading.Thread") as mock_thread_cls:
                    mock_thread_cls.return_value = MagicMock()
                    resp = client.post(
                        "/api/rematch/apply",
                        json={
                            "mbid": "manual-abc12345",
                            "mb_recording_id": "rec-001",
                            "mb_album_id": "album-001",
                        },
                    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["album_name"] == "The Marshall Mathers LP"
    mock_write.assert_called_once()


def test_rematch_apply_via_mbid_not_in_db_returns_404(client):
    """mbid가 state.db에 없으면 404를 반환한다."""
    resp = client.post(
        "/api/rematch/apply",
        json={
            "mbid": "manual-nonexistent",
            "mb_recording_id": "rec-001",
            "mb_album_id": "album-001",
        },
    )
    assert resp.status_code == 404


def test_rematch_apply_via_mbid_file_path_none_returns_500(client, tmp_state_db):
    """mbid는 있지만 file_path가 None이면 500을 반환한다."""
    from src.state import mark_pending
    mark_pending(tmp_state_db, "manual-nofp", "Track", "Artist")
    # file_path를 기록하지 않아 None 상태

    resp = client.post(
        "/api/rematch/apply",
        json={
            "mbid": "manual-nofp",
            "mb_recording_id": "rec-001",
            "mb_album_id": "album-001",
        },
    )
    assert resp.status_code == 500


def test_rematch_apply_via_mbid_file_missing_returns_404(client, tmp_state_db):
    """mbid의 file_path가 존재하지 않는 파일이면 404를 반환한다."""
    from src.state import mark_pending, mark_done
    mark_pending(tmp_state_db, "manual-gone", "Track", "Artist")
    mark_done(tmp_state_db, "manual-gone", file_path="/nonexistent/path/track.flac")

    resp = client.post(
        "/api/rematch/apply",
        json={
            "mbid": "manual-gone",
            "mb_recording_id": "rec-001",
            "mb_album_id": "album-001",
        },
    )
    assert resp.status_code == 404
