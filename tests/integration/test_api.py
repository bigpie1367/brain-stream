"""
tests/integration/test_api.py
FastAPI TestClient로 API 엔드포인트 통합 테스트
- pipeline 실행, download_track, tag_and_import, trigger_scan 등은 mock 처리
"""

from unittest.mock import AsyncMock, MagicMock, patch

from src.state import get_all_downloads, mark_done, mark_pending


# ── httpx 응답 헬퍼 ──────────────────────────────────────────────────────────


def _httpx_response(json_data, status_code=200):
    """httpx.AsyncClient.get mock 반환값 헬퍼."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    return resp


def _mock_http_client(client, return_value=None, side_effect=None):
    """app.state.http_client.get을 AsyncMock으로 패치하는 컨텍스트 매니저를 반환한다."""
    mock_get = AsyncMock(return_value=return_value, side_effect=side_effect)
    return patch.object(
        client.app.state, "http_client", MagicMock(get=mock_get)
    ), mock_get


# ── POST /api/download ────────────────────────────────────────────────────────


def test_post_download_returns_200_and_job_id(client):
    """
    POST /api/download 가 200을 반환하고 job_id를 포함한 응답을 돌려줘야 한다.
    백그라운드 스레드 실행은 mock으로 막는다.
    """
    with patch("src.api.threading.Thread") as mock_thread_cls:
        mock_thread = MagicMock()
        mock_thread_cls.return_value = mock_thread

        resp = client.post(
            "/api/download", json={"artist": "Radiohead", "track": "Creep"}
        )

    assert resp.status_code == 200
    data = resp.json()
    assert "job_id" in data
    assert data["job_id"].startswith("manual-")


def test_post_download_job_id_is_unique(client):
    """POST /api/download를 같은 artist+track으로 두 번 호출하면 두 번째는 409 중복."""
    with patch("src.api.threading.Thread") as mock_thread_cls:
        mock_thread_cls.return_value = MagicMock()
        resp1 = client.post("/api/download", json={"artist": "A", "track": "B"})
        resp2 = client.post("/api/download", json={"artist": "A", "track": "B"})

    assert resp1.status_code == 200
    assert resp2.status_code == 409


def test_post_download_different_tracks_both_succeed(client):
    """POST /api/download를 다른 track으로 호출하면 서로 다른 job_id가 반환된다."""
    with patch("src.api.threading.Thread") as mock_thread_cls:
        mock_thread_cls.return_value = MagicMock()
        resp1 = client.post("/api/download", json={"artist": "A", "track": "B"})
        resp2 = client.post("/api/download", json={"artist": "A", "track": "C"})

    assert resp1.json()["job_id"] != resp2.json()["job_id"]


def test_post_download_creates_pending_row_in_db(client, tmp_state_db):
    """POST /api/download 후 state DB에 pending 레코드가 생성되어야 한다."""
    with patch("src.api.threading.Thread") as mock_thread_cls:
        mock_thread_cls.return_value = MagicMock()
        resp = client.post(
            "/api/download", json={"artist": "Queen", "track": "Bohemian Rhapsody"}
        )

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
    """DB가 비어있을 때 GET /api/downloads는 200과 빈 items를 반환한다."""
    resp = client.get("/api/downloads")
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["total"] == 0


def test_get_downloads_returns_existing_records(client, tmp_state_db):
    """DB에 레코드가 있으면 GET /api/downloads가 그 레코드들을 반환한다."""
    mark_pending(tmp_state_db, "mbid-test-001", "Creep", "Radiohead")
    mark_done(tmp_state_db, "mbid-test-001")
    mark_pending(tmp_state_db, "mbid-test-002", "Karma Police", "Radiohead")

    resp = client.get("/api/downloads")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    mbids = [r["mbid"] for r in data["items"]]
    assert "mbid-test-001" in mbids
    assert "mbid-test-002" in mbids


def test_get_downloads_returns_latest_first(client, tmp_state_db):
    """GET /api/downloads는 최신 항목이 앞에 오는 순서로 반환해야 한다."""
    mark_pending(tmp_state_db, "mbid-first", "Track 1", "Artist")
    mark_pending(tmp_state_db, "mbid-second", "Track 2", "Artist")

    resp = client.get("/api/downloads")
    data = resp.json()
    assert data["items"][0]["mbid"] == "mbid-second"
    assert data["items"][1]["mbid"] == "mbid-first"


def test_get_downloads_response_schema(client, tmp_state_db):
    """응답에 pagination 필드와 각 항목에 필수 필드가 모두 있는지 확인한다."""
    mark_pending(tmp_state_db, "mbid-schema", "Track", "Artist")
    resp = client.get("/api/downloads")
    data = resp.json()
    assert "items" in data
    assert "total" in data
    assert "limit" in data
    assert "offset" in data
    assert data["total"] == 1
    row = data["items"][0]
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
    from queue import Queue

    import src.worker as worker_module

    job_id = "manual-testjob"
    q = Queue()
    # done 이벤트를 미리 큐에 넣어 스트림이 즉시 종료되도록 한다
    q.put({"status": "done", "message": "완료"})
    import time

    worker_module._job_queues[job_id] = (q, time.time())

    try:
        resp = client.get(f"/api/sse/{job_id}")
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
    finally:
        worker_module._job_queues.pop(job_id, None)


# ── DELETE /api/downloads/{mbid} ─────────────────────────────────────────────


def test_delete_download_returns_404_when_not_found(client):
    """존재하지 않는 mbid 삭제 시 404를 반환해야 한다."""
    resp = client.delete("/api/downloads/nonexistent-mbid")
    assert resp.status_code == 404


def test_delete_download_marks_record_as_ignored(client, tmp_state_db):
    """삭제 후 state DB 레코드가 ignored 상태로 전환된다 (재다운로드 방지)."""
    from src.state import get_download_by_mbid, mark_done

    mark_pending(tmp_state_db, "mbid-del", "Track", "Artist")
    mark_done(tmp_state_db, "mbid-del")

    resp = client.delete("/api/downloads/mbid-del")
    assert resp.status_code == 200
    data = resp.json()
    assert data["deleted"] is True

    row = get_download_by_mbid(tmp_state_db, "mbid-del")
    assert row is not None
    assert row["status"] == "ignored"


def test_delete_download_removes_file_when_file_path_set(
    client, tmp_state_db, tmp_path
):
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


def test_delete_download_no_file_path_marks_ignored(client, tmp_state_db):
    """file_path가 None이면 파일 삭제 없이 DB 레코드를 ignored 상태로 전환한다."""
    from src.state import get_download_by_mbid

    mark_pending(tmp_state_db, "mbid-nofile", "Track", "Artist")

    resp = client.delete("/api/downloads/mbid-nofile")
    assert resp.status_code == 200
    data = resp.json()
    assert data["deleted"] is True
    assert data["files_removed"] == 0

    row = get_download_by_mbid(tmp_state_db, "mbid-nofile")
    assert row is not None
    assert row["status"] == "ignored"


# ── GET / (index.html) ────────────────────────────────────────────────────────


def test_get_index_returns_html(client):
    """GET / 가 HTML을 반환해야 한다."""
    with patch(
        "builtins.open",
        MagicMock(
            return_value=MagicMock(
                __enter__=MagicMock(
                    return_value=MagicMock(read=MagicMock(return_value="<html></html>"))
                ),
                __exit__=MagicMock(return_value=False),
            )
        ),
    ):
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
    from src.state import mark_done, mark_pending

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

    from src.state import mark_done, mark_pending

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

    from src.state import mark_done, mark_pending

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

    from src.state import mark_done, mark_pending

    mark_pending(tmp_state_db, "mbid-flac-yr", "Lose Yourself", "Eminem")
    mark_done(tmp_state_db, "mbid-flac-yr", file_path=str(dummy_file))

    mock_audio = MagicMock()
    mock_audio.get.side_effect = lambda key, default=None: (
        ["The Marshall Mathers LP"]
        if key == "album"
        else ["2000"]
        if key == "date"
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

    from src.state import mark_done, mark_pending

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

    from src.state import mark_done, mark_pending

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

    from src.state import mark_done, mark_pending

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


def test_get_download_detail_mutagen_exception_returns_null_album(
    client, tmp_state_db, tmp_path
):
    """mutagen 파싱 중 예외 발생 시 album_name이 null이고 에러 없이 200을 반환한다."""
    dummy_file = tmp_path / "track.flac"
    dummy_file.write_bytes(b"corrupted data")

    from src.state import mark_done, mark_pending

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
    """httpx AsyncClient.get mock 반환값 헬퍼."""
    return _httpx_response({"recordings": recordings})


def _make_recording(rec_id, releases, artist_name="Radiohead"):
    return {
        "id": rec_id,
        "artist-credit": [{"artist": {"name": artist_name}, "joinphrase": ""}],
        "releases": releases,
    }


def _make_release(release_id, title, date="2000-01-01"):
    return {"id": release_id, "title": title, "date": date}


def _patch_http_client_get(client, return_value=None, side_effect=None):
    """app.state.http_client.get을 AsyncMock으로 교체한다."""
    mock_get = AsyncMock(return_value=return_value, side_effect=side_effect)
    mock_client = MagicMock()
    mock_client.get = mock_get
    return patch.object(client.app.state, "http_client", mock_client), mock_get


def test_rematch_search_returns_candidates(client):
    """MB stage1 검색이 성공하면 candidates 목록이 반환된다."""
    release = _make_release("album-id-001", "OK Computer")
    rec = _make_recording("rec-id-001", [release])

    patcher, _ = _patch_http_client_get(client, return_value=_mb_search_response([rec]))
    with patcher:
        with patch("src.api.asyncio.sleep", new_callable=AsyncMock):
            with patch("src.api.itunes_search", return_value={}):
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
    """두 stage 모두 결과 없고 iTunes도 결과 없으면 빈 candidates를 반환한다."""
    patcher, _ = _patch_http_client_get(client, return_value=_mb_search_response([]))
    with patcher:
        with patch("src.api.asyncio.sleep", new_callable=AsyncMock):
            with patch("src.api.itunes_search", return_value={}):
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

    async def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _mb_search_response([])  # stage1: 빈 결과
        return _mb_search_response([rec])  # stage2: 결과 있음

    patcher, _ = _patch_http_client_get(client, side_effect=side_effect)
    with patcher:
        with patch("src.api.asyncio.sleep", new_callable=AsyncMock):
            with patch("src.api.itunes_search", return_value={}):
                resp = client.get(
                    "/api/rematch/search",
                    params={"artist": "Artist", "track": "Track"},
                )

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["candidates"]) == 1
    assert data["candidates"][0]["mb_album_id"] == "album-fallback"


def test_rematch_search_multiple_album_candidates(client):
    """recording당 여러 release가 있으면 모두 후보로 반환한다."""
    releases = [
        _make_release("album-a", "The Bends", "1995-03-13"),
        _make_release("album-b", "The Bends (Remaster)", "2016-01-01"),
        _make_release("album-c", "The Bends (Japan)", "1995-04-01"),
    ]
    rec = _make_recording("rec-001", releases)

    patcher, _ = _patch_http_client_get(client, return_value=_mb_search_response([rec]))
    with patcher:
        with patch("src.api.asyncio.sleep", new_callable=AsyncMock):
            with patch("src.api.itunes_search", return_value={}):
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

    patcher, _ = _patch_http_client_get(client, return_value=_mb_search_response([rec]))
    with patcher:
        with patch("src.api.asyncio.sleep", new_callable=AsyncMock):
            with patch("src.api.itunes_search", return_value={}):
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

    patcher, _ = _patch_http_client_get(
        client, return_value=_mb_search_response([rec1, rec2])
    )
    with patcher:
        with patch("src.api.asyncio.sleep", new_callable=AsyncMock):
            with patch("src.api.itunes_search", return_value={}):
                resp = client.get(
                    "/api/rematch/search",
                    params={"artist": "Artist", "track": "Track"},
                )

    data = resp.json()
    album_ids = [c["mb_album_id"] for c in data["candidates"]]
    assert album_ids.count("shared-album") == 1


def test_rematch_search_mb_request_error_returns_empty(client):
    """MB API 호출 중 예외 발생 시 빈 candidates를 반환한다."""
    patcher, _ = _patch_http_client_get(client, side_effect=Exception("network error"))
    with patcher:
        with patch("src.api.asyncio.sleep", new_callable=AsyncMock):
            resp = client.get(
                "/api/rematch/search",
                params={"artist": "Artist", "track": "Track"},
            )

    assert resp.status_code == 200
    assert resp.json() == {"candidates": []}


def test_rematch_search_returns_combined_sources(client):
    """MB 결과 뒤에 iTunes 후보가 source='itunes'로 추가된다."""
    release = _make_release("album-mb-001", "OK Computer")
    rec = _make_recording("rec-mb-001", [release])
    itunes_result = {
        "album": "OK Computer",
        "artwork_url": "https://example.com/art.jpg",
    }

    patcher, _ = _patch_http_client_get(client, return_value=_mb_search_response([rec]))
    with patcher:
        with patch("src.api.asyncio.sleep", new_callable=AsyncMock):
            with patch("src.api.itunes_search", return_value=itunes_result):
                resp = client.get(
                    "/api/rematch/search",
                    params={"artist": "Radiohead", "track": "Karma Police"},
                )

    assert resp.status_code == 200
    data = resp.json()
    candidates = data["candidates"]
    assert len(candidates) == 2

    mb_c = candidates[0]
    assert mb_c["source"] == "musicbrainz"
    assert mb_c["mb_recording_id"] == "rec-mb-001"
    assert mb_c["mb_album_id"] == "album-mb-001"

    it_c = candidates[1]
    assert it_c["source"] == "itunes"
    assert it_c["mb_recording_id"] == ""
    assert it_c["mb_album_id"] == ""
    assert it_c["album_name"] == "OK Computer"
    assert it_c["artist_name"] == "Radiohead"
    assert it_c["year"] == ""
    assert it_c["cover_url"] == "https://example.com/art.jpg"


# ── POST /api/rematch/apply ───────────────────────────────────────────────────


def test_rematch_apply_success(client, tmp_path):
    """정상 흐름: 파일 존재 + getSong 성공 + MB release 조회 성공 -> 200 반환."""
    dummy_audio = tmp_path / "track.flac"
    dummy_audio.write_bytes(b"fake flac data")

    # Navidrome getSong이 반환하는 path는 music root 기준 상대경로
    # client fixture의 music_dir은 tmp_path/"music" 이지만
    # rematch_apply는 /app/data/music/{path}로 절대경로를 구성하므로
    # 파일을 /app/data/music/... 에 생성하는 대신 os.path.exists를 mock한다.
    song_relative_path = "Artist/Album/track.flac"

    with patch(
        "src.api._navidrome_get_song",
        new_callable=AsyncMock,
        return_value={"path": song_relative_path},
    ):
        with patch("src.api.os.path.exists", return_value=True):
            patcher, _ = _patch_http_client_get(
                client,
                return_value=_httpx_response({"title": "OK Computer"}),
            )
            with patcher:
                with patch("src.api.asyncio.sleep", new_callable=AsyncMock):
                    with patch("src.api.write_album_tag") as mock_write:
                        with patch("src.api.embed_cover_art", return_value=True):
                            with patch(
                                "src.api.move_to_music_dir",
                                return_value="/app/data/music/Artist/Album/track.flac",
                            ):
                                with patch(
                                    "src.api.threading.Thread"
                                ) as mock_thread_cls:
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
    with patch(
        "src.api._navidrome_get_song",
        new_callable=AsyncMock,
        side_effect=RuntimeError("getSong error"),
    ):
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
    with patch(
        "src.api._navidrome_get_song",
        new_callable=AsyncMock,
        return_value={"path": "Artist/track.flac"},
    ):
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
    with patch(
        "src.api._navidrome_get_song",
        new_callable=AsyncMock,
        return_value={"path": "Artist/track.flac"},
    ):
        with patch("src.api.os.path.exists", return_value=True):
            patcher, _ = _patch_http_client_get(
                client, side_effect=Exception("network error")
            )
            with patcher:
                with patch("src.api.asyncio.sleep", new_callable=AsyncMock):
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
    """mbid 경로: state.db에서 file_path 조회 후 재태깅 성공 -> 200 반환."""
    dummy_audio = tmp_path / "track.flac"
    dummy_audio.write_bytes(b"fake flac data")

    from src.state import mark_done, mark_pending

    mark_pending(tmp_state_db, "manual-abc12345", "Lose Yourself", "Eminem")
    mark_done(tmp_state_db, "manual-abc12345", file_path=str(dummy_audio))

    patcher, _ = _patch_http_client_get(
        client,
        return_value=_httpx_response({"title": "The Marshall Mathers LP"}),
    )
    with patcher:
        with patch("src.api.asyncio.sleep", new_callable=AsyncMock):
            with patch("src.api.write_album_tag") as mock_write:
                with patch("src.api.embed_cover_art", return_value=True):
                    with patch(
                        "src.api.move_to_music_dir", return_value=str(dummy_audio)
                    ):
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


def test_rematch_apply_moves_file_when_album_changes(client, tmp_state_db, tmp_path):
    """앨범명이 바뀌면 파일이 새 앨범 폴더로 이동되고 state.db file_path가 업데이트된다."""
    artist_dir = tmp_path / "Artist"
    old_album_dir = artist_dir / "OldAlbum"
    old_album_dir.mkdir(parents=True)
    dummy_audio = old_album_dir / "track.flac"
    dummy_audio.write_bytes(b"fake flac data")

    from src.state import get_download_by_mbid, mark_done, mark_pending

    mark_pending(tmp_state_db, "manual-moveme", "Track", "Artist")
    mark_done(tmp_state_db, "manual-moveme", file_path=str(dummy_audio))

    new_album_name = "NewAlbum"

    patcher, _ = _patch_http_client_get(
        client,
        return_value=_httpx_response({"title": new_album_name}),
    )
    with patcher:
        with patch("src.api.asyncio.sleep", new_callable=AsyncMock):
            with patch("src.api.write_album_tag"):
                with patch("src.api.embed_cover_art", return_value=True):
                    with patch("src.api.threading.Thread") as mock_thread_cls:
                        mock_thread_cls.return_value = MagicMock()
                        resp = client.post(
                            "/api/rematch/apply",
                            json={
                                "mbid": "manual-moveme",
                                "mb_recording_id": "rec-001",
                                "mb_album_id": "album-001",
                            },
                        )

    assert resp.status_code == 200
    new_file_path = str(artist_dir / new_album_name / "track.flac")
    assert not dummy_audio.exists(), "원본 파일이 이동되어 있어야 한다"
    assert (artist_dir / new_album_name / "track.flac").exists(), (
        "새 경로에 파일이 있어야 한다"
    )

    row = get_download_by_mbid(tmp_state_db, "manual-moveme")
    assert row["file_path"] == new_file_path


def test_rematch_apply_no_move_when_album_unchanged(client, tmp_state_db, tmp_path):
    """앨범명이 같으면 파일 이동이 발생하지 않는다."""
    artist_dir = tmp_path / "Artist"
    same_album_dir = artist_dir / "SameAlbum"
    same_album_dir.mkdir(parents=True)
    dummy_audio = same_album_dir / "track.flac"
    dummy_audio.write_bytes(b"fake flac data")

    from src.state import mark_done, mark_pending

    mark_pending(tmp_state_db, "manual-nomove", "Track", "Artist")
    mark_done(tmp_state_db, "manual-nomove", file_path=str(dummy_audio))

    patcher, _ = _patch_http_client_get(
        client,
        return_value=_httpx_response({"title": "SameAlbum"}),
    )
    with patcher:
        with patch("src.api.asyncio.sleep", new_callable=AsyncMock):
            with patch("src.api.write_album_tag"):
                with patch("src.api.embed_cover_art", return_value=True):
                    with patch("src.api.threading.Thread") as mock_thread_cls:
                        mock_thread_cls.return_value = MagicMock()
                        resp = client.post(
                            "/api/rematch/apply",
                            json={
                                "mbid": "manual-nomove",
                                "mb_recording_id": "rec-001",
                                "mb_album_id": "album-001",
                            },
                        )

    assert resp.status_code == 200
    assert dummy_audio.exists(), "앨범명이 같으면 파일이 그대로 있어야 한다"


def test_rematch_apply_move_fails_returns_500(client, tmp_state_db, tmp_path):
    """move_to_music_dir 실패 시 500을 반환한다."""
    artist_dir = tmp_path / "Artist"
    old_album_dir = artist_dir / "OldAlbum"
    old_album_dir.mkdir(parents=True)
    dummy_audio = old_album_dir / "track.flac"
    dummy_audio.write_bytes(b"fake flac data")

    from src.state import mark_done, mark_pending

    mark_pending(tmp_state_db, "manual-movefail", "Track", "Artist")
    mark_done(tmp_state_db, "manual-movefail", file_path=str(dummy_audio))

    patcher, _ = _patch_http_client_get(
        client,
        return_value=_httpx_response({"title": "DifferentAlbum"}),
    )
    with patcher:
        with patch("src.api.asyncio.sleep", new_callable=AsyncMock):
            with patch("src.api.write_album_tag"):
                with patch(
                    "src.api.move_to_music_dir",
                    side_effect=OSError("permission denied"),
                ):
                    resp = client.post(
                        "/api/rematch/apply",
                        json={
                            "mbid": "manual-movefail",
                            "mb_recording_id": "rec-001",
                            "mb_album_id": "album-001",
                        },
                    )

    assert resp.status_code == 500
    assert "file move failed" in resp.json()["detail"]


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
    from src.state import mark_done, mark_pending

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


def test_rematch_apply_song_id_absolute_path_used_directly(client):
    """getSong이 /app/data/music/... 형태의 절대경로를 반환할 때 prefix를 이중으로 붙이지 않는다."""
    absolute_path = "/app/data/music/Artist/Album/track.flac"

    with patch(
        "src.api._navidrome_get_song",
        new_callable=AsyncMock,
        return_value={"path": absolute_path},
    ):
        with patch("src.api.os.path.exists", return_value=True):
            patcher, _ = _patch_http_client_get(
                client,
                return_value=_httpx_response({"title": "OK Computer"}),
            )
            with patcher:
                with patch("src.api.asyncio.sleep", new_callable=AsyncMock):
                    with patch("src.api.write_album_tag") as mock_write:
                        with patch("src.api.embed_cover_art", return_value=True):
                            with patch(
                                "src.api.move_to_music_dir", return_value=absolute_path
                            ):
                                with patch(
                                    "src.api.threading.Thread"
                                ) as mock_thread_cls:
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
    # write_album_tag의 첫 번째 인자(file_path)가 절대경로 그대로여야 한다
    called_path = mock_write.call_args[0][0]
    assert called_path == absolute_path
    # /app/data/music가 이중으로 붙으면 안 된다
    assert "/app/data/music/app/data/music" not in called_path


# ── POST /api/edit/{song_id} ──────────────────────────────────────────────────


def _setup_done_record(db_path, mbid, artist, track_name, album, file_path):
    """state.db에 done 레코드를 삽입하는 헬퍼."""
    from src.state import mark_done, mark_pending, update_track_info

    mark_pending(db_path, mbid, track_name, artist)
    mark_done(db_path, mbid, file_path=file_path, album=album)
    update_track_info(db_path, mbid, artist=artist, track_name=track_name, album=album)


def test_edit_song_not_found_returns_404(client):
    """존재하지 않는 song_id -> 404."""
    resp = client.post("/api/edit/nonexistent-id", json={"artist": "New Artist"})
    assert resp.status_code == 404


def test_edit_file_path_null_returns_404(client, tmp_state_db):
    """file_path가 None인 레코드 -> 404."""
    from src.state import mark_pending

    mark_pending(tmp_state_db, "manual-nofp2", "Track", "Artist")
    resp = client.post("/api/edit/manual-nofp2", json={"artist": "New Artist"})
    assert resp.status_code == 404


def test_edit_file_missing_returns_404(client, tmp_state_db):
    """file_path가 기록되어 있지만 파일이 실제로 없으면 -> 404."""
    _setup_done_record(
        tmp_state_db,
        "manual-gone2",
        "Artist",
        "Track",
        "Album",
        "/nonexistent/path/track.flac",
    )
    resp = client.post("/api/edit/manual-gone2", json={"artist": "New Artist"})
    assert resp.status_code == 404


def test_edit_no_change_returns_200_immediately(client, tmp_state_db, tmp_path):
    """artist / album / track_name이 모두 기존값과 같으면 즉시 200 반환."""
    dummy = tmp_path / "track.flac"
    dummy.write_bytes(b"fake")
    _setup_done_record(
        tmp_state_db, "manual-noop", "Artist", "Track", "Album", str(dummy)
    )
    with patch("src.api.write_artist_tag") as mock_artist:
        with patch("src.api.write_album_tag") as mock_album:
            with patch("src.api.write_title_tag") as mock_title:
                resp = client.post(
                    "/api/edit/manual-noop",
                    json={"artist": "Artist", "album": "Album", "track_name": "Track"},
                )

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    mock_artist.assert_not_called()
    mock_album.assert_not_called()
    mock_title.assert_not_called()


def test_edit_artist_only_updates_tags_and_moves_file(client, tmp_state_db, tmp_path):
    """artist만 바꾸면 태그 쓰기 + 파일 이동 + state.db 업데이트 발생."""
    music_dir = tmp_path / "music"
    old_artist_dir = music_dir / "OldArtist"
    album_dir = old_artist_dir / "Album"
    album_dir.mkdir(parents=True)
    dummy = album_dir / "Track.flac"
    dummy.write_bytes(b"fake")

    _setup_done_record(
        tmp_state_db, "manual-edt1", "OldArtist", "Track", "Album", str(dummy)
    )

    import src.api as api_module

    api_module._cfg.beets.music_dir = str(music_dir)

    with patch("src.api.write_artist_tag") as mock_artist:
        with patch("src.api.write_album_tag") as mock_album:
            with patch("src.api.write_title_tag") as mock_title:
                with patch("src.api.threading.Thread") as mock_thread_cls:
                    mock_thread_cls.return_value = MagicMock()
                    resp = client.post(
                        "/api/edit/manual-edt1",
                        json={"artist": "NewArtist"},
                    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert "NewArtist" in data["file_path"]
    mock_artist.assert_called_once()
    mock_album.assert_called_once()
    mock_title.assert_called_once()

    from src.state import get_download_by_mbid

    row = get_download_by_mbid(tmp_state_db, "manual-edt1")
    assert row["artist"] == "NewArtist"
    assert "NewArtist" in row["file_path"]


def test_edit_track_name_only(client, tmp_state_db, tmp_path):
    """track_name만 바꾸면 새 파일명으로 이동한다."""
    music_dir = tmp_path / "music"
    artist_dir = music_dir / "Artist"
    album_dir = artist_dir / "Album"
    album_dir.mkdir(parents=True)
    dummy = album_dir / "OldTitle.opus"
    dummy.write_bytes(b"fake")

    _setup_done_record(
        tmp_state_db, "manual-edt2", "Artist", "OldTitle", "Album", str(dummy)
    )

    import src.api as api_module

    api_module._cfg.beets.music_dir = str(music_dir)

    with patch("src.api.write_artist_tag"):
        with patch("src.api.write_album_tag"):
            with patch("src.api.write_title_tag"):
                with patch("src.api.threading.Thread") as mock_thread_cls:
                    mock_thread_cls.return_value = MagicMock()
                    resp = client.post(
                        "/api/edit/manual-edt2",
                        json={"track_name": "NewTitle"},
                    )

    assert resp.status_code == 200
    data = resp.json()
    assert "NewTitle" in data["file_path"]
    assert data["file_path"].endswith(".opus")


def test_edit_conflict_returns_409(client, tmp_state_db, tmp_path):
    """새 경로에 파일이 이미 존재하면 409 반환."""
    music_dir = tmp_path / "music"
    artist_dir = music_dir / "Artist"
    old_album_dir = artist_dir / "OldAlbum"
    new_album_dir = artist_dir / "NewAlbum"
    old_album_dir.mkdir(parents=True)
    new_album_dir.mkdir(parents=True)

    dummy = old_album_dir / "Track.flac"
    dummy.write_bytes(b"fake")
    conflict = new_album_dir / "Track.flac"
    conflict.write_bytes(b"existing")

    _setup_done_record(
        tmp_state_db, "manual-conflict", "Artist", "Track", "OldAlbum", str(dummy)
    )

    import src.api as api_module

    api_module._cfg.beets.music_dir = str(music_dir)

    with patch("src.api.write_artist_tag"):
        with patch("src.api.write_album_tag"):
            with patch("src.api.write_title_tag"):
                resp = client.post(
                    "/api/edit/manual-conflict",
                    json={"album": "NewAlbum"},
                )

    assert resp.status_code == 409


def test_edit_tag_write_failure_returns_500(client, tmp_state_db, tmp_path):
    """mutagen 태그 쓰기 실패 시 500 반환."""
    music_dir = tmp_path / "music"
    artist_dir = music_dir / "Artist"
    album_dir = artist_dir / "Album"
    album_dir.mkdir(parents=True)
    dummy = album_dir / "Track.flac"
    dummy.write_bytes(b"fake")

    _setup_done_record(
        tmp_state_db, "manual-tagfail", "Artist", "Track", "Album", str(dummy)
    )

    import src.api as api_module

    api_module._cfg.beets.music_dir = str(music_dir)

    with patch("src.api.write_artist_tag", side_effect=Exception("mutagen error")):
        resp = client.post(
            "/api/edit/manual-tagfail",
            json={"artist": "NewArtist"},
        )

    assert resp.status_code == 500
    assert "tag write failed" in resp.json()["detail"]


def test_rematch_apply_song_id_relative_path_gets_prefix(client):
    """getSong이 상대경로를 반환할 때 /app/data/music/ prefix를 붙인다."""
    relative_path = "Artist/Album/track.flac"
    expected_path = f"/app/data/music/{relative_path}"

    with patch(
        "src.api._navidrome_get_song",
        new_callable=AsyncMock,
        return_value={"path": relative_path},
    ):
        with patch("src.api.os.path.exists", return_value=True):
            patcher, _ = _patch_http_client_get(
                client,
                return_value=_httpx_response({"title": "OK Computer"}),
            )
            with patcher:
                with patch("src.api.asyncio.sleep", new_callable=AsyncMock):
                    with patch("src.api.write_album_tag") as mock_write:
                        with patch("src.api.embed_cover_art", return_value=True):
                            with patch(
                                "src.api.move_to_music_dir", return_value=expected_path
                            ):
                                with patch(
                                    "src.api.threading.Thread"
                                ) as mock_thread_cls:
                                    mock_thread_cls.return_value = MagicMock()
                                    resp = client.post(
                                        "/api/rematch/apply",
                                        json={
                                            "song_id": "nav-song-456",
                                            "mb_recording_id": "rec-002",
                                            "mb_album_id": "album-002",
                                        },
                                    )

    assert resp.status_code == 200
    called_path = mock_write.call_args[0][0]
    assert called_path == expected_path


# ── iTunes KR 스토어 + rematch apply (mb_album_id 없음) ──────────────────────


def test_rematch_search_itunes_kr_added_when_different_album(client):
    """US와 KR iTunes 스토어가 다른 앨범을 반환하면 두 후보가 모두 추가된다."""
    release = _make_release("album-mb", "OK Computer")
    rec = _make_recording("rec-mb", [release])

    call_count = 0

    def itunes_side_effect(artist, track, country=None):
        nonlocal call_count
        call_count += 1
        if country is None:
            return {"album": "OK Computer", "artwork_url": "https://example.com/us.jpg"}
        # KR store returns different album title
        return {
            "album": "OK Computer (Korean Edition)",
            "artwork_url": "https://example.com/kr.jpg",
        }

    patcher, _ = _patch_http_client_get(client, return_value=_mb_search_response([rec]))
    with patcher:
        with patch("src.api.asyncio.sleep", new_callable=AsyncMock):
            with patch("src.api.itunes_search", side_effect=itunes_side_effect):
                resp = client.get(
                    "/api/rematch/search",
                    params={"artist": "Radiohead", "track": "Karma Police"},
                )

    assert resp.status_code == 200
    data = resp.json()
    candidates = data["candidates"]
    # MB 1개 + iTunes US 1개 + iTunes KR 1개
    assert len(candidates) == 3
    sources = [c["source"] for c in candidates]
    assert "musicbrainz" in sources
    assert "itunes" in sources
    assert "itunes-kr" in sources

    kr_c = next(c for c in candidates if c["source"] == "itunes-kr")
    assert kr_c["album_name"] == "OK Computer (Korean Edition)"
    assert kr_c["mb_recording_id"] == ""
    assert kr_c["mb_album_id"] == ""


def test_rematch_search_itunes_kr_deduplicated_when_same_album(client):
    """US와 KR iTunes 스토어가 같은 앨범을 반환하면 하나만 추가된다."""
    release = _make_release("album-mb", "OK Computer")
    rec = _make_recording("rec-mb", [release])

    def itunes_same(artist, track, country=None):
        return {"album": "OK Computer", "artwork_url": "https://example.com/art.jpg"}

    patcher, _ = _patch_http_client_get(client, return_value=_mb_search_response([rec]))
    with patcher:
        with patch("src.api.asyncio.sleep", new_callable=AsyncMock):
            with patch("src.api.itunes_search", side_effect=itunes_same):
                resp = client.get(
                    "/api/rematch/search",
                    params={"artist": "Radiohead", "track": "Karma Police"},
                )

    assert resp.status_code == 200
    data = resp.json()
    candidates = data["candidates"]
    # MB 1개 + iTunes 1개 (중복 제거로 KR은 추가 안 됨)
    assert len(candidates) == 2
    itunes_candidates = [
        c for c in candidates if c["source"] in ("itunes", "itunes-kr")
    ]
    assert len(itunes_candidates) == 1


def test_rematch_apply_itunes_candidate_no_mb_album_id(client):
    """mb_album_id가 없고 album_name이 있으면 MB 조회 없이 직접 태깅한다."""
    with patch(
        "src.api._navidrome_get_song",
        new_callable=AsyncMock,
        return_value={"path": "Artist/track.flac"},
    ):
        with patch("src.api.os.path.exists", return_value=True):
            patcher, mock_get = _patch_http_client_get(client)
            with patcher:
                with patch("src.api.write_album_tag") as mock_write:
                    with patch(
                        "src.api.embed_art_from_url", return_value=True
                    ) as mock_embed:
                        with patch(
                            "src.api.move_to_music_dir",
                            return_value="/app/data/music/Artist/OK Computer/track.flac",
                        ):
                            with patch("src.api.threading.Thread") as mock_thread_cls:
                                mock_thread_cls.return_value = MagicMock()
                                resp = client.post(
                                    "/api/rematch/apply",
                                    json={
                                        "song_id": "nav-song-itunes",
                                        "mb_recording_id": "",
                                        "mb_album_id": "",
                                        "album_name": "OK Computer",
                                        "cover_url": "https://example.com/art.jpg",
                                    },
                                )

    # MB API 호출이 없어야 한다
    mock_get.assert_not_called()
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["album_name"] == "OK Computer"
    mock_write.assert_called_once()
    # embed_art_from_url은 이동 후의 new_file_path로 호출된다
    mock_embed.assert_called_once()


def test_rematch_apply_no_mb_album_id_no_album_name_returns_422(client):
    """mb_album_id와 album_name 모두 없으면 422를 반환한다."""
    with patch(
        "src.api._navidrome_get_song",
        new_callable=AsyncMock,
        return_value={"path": "Artist/track.flac"},
    ):
        with patch("src.api.os.path.exists", return_value=True):
            resp = client.post(
                "/api/rematch/apply",
                json={
                    "song_id": "nav-song-bad",
                    "mb_recording_id": "",
                    "mb_album_id": "",
                },
            )
    assert resp.status_code == 422


def test_rematch_apply_artist_name_rewrites_artist_tag(client, tmp_state_db, tmp_path):
    """artist_name이 주어지면 artist 태그를 새 이름으로 업데이트한다."""
    artist_dir = tmp_path / "OldArtist"
    album_dir = artist_dir / "SameAlbum"
    album_dir.mkdir(parents=True)
    dummy_audio = album_dir / "track.flac"
    dummy_audio.write_bytes(b"fake flac data")

    from src.state import mark_done, mark_pending

    mark_pending(tmp_state_db, "manual-artist-tag", "Track", "OldArtist")
    mark_done(tmp_state_db, "manual-artist-tag", file_path=str(dummy_audio))

    patcher, _ = _patch_http_client_get(
        client,
        return_value=_httpx_response({"title": "SameAlbum"}),
    )
    with patcher:
        with patch("src.api.asyncio.sleep", new_callable=AsyncMock):
            with patch("src.api.write_album_tag"):
                with patch("src.api.write_artist_tag") as mock_write_artist:
                    with patch("src.api.embed_cover_art", return_value=True):
                        with patch("src.api.threading.Thread") as mock_thread_cls:
                            mock_thread_cls.return_value = MagicMock()
                            resp = client.post(
                                "/api/rematch/apply",
                                json={
                                    "mbid": "manual-artist-tag",
                                    "mb_recording_id": "rec-001",
                                    "mb_album_id": "album-001",
                                    "artist_name": "NewArtist",
                                },
                            )

    assert resp.status_code == 200
    mock_write_artist.assert_called_once_with(str(dummy_audio), "NewArtist")


def test_rematch_apply_artist_name_moves_to_new_artist_dir(
    client, tmp_state_db, tmp_path
):
    """artist_name이 주어지면 파일이 새 아티스트 폴더 아래 앨범 폴더로 이동된다."""
    music_root = tmp_path / "music"
    artist_dir = music_root / "OldArtist"
    album_dir = artist_dir / "SameAlbum"
    album_dir.mkdir(parents=True)
    dummy_audio = album_dir / "track.flac"
    dummy_audio.write_bytes(b"fake flac data")

    from src.state import get_download_by_mbid, mark_done, mark_pending

    mark_pending(tmp_state_db, "manual-artist-move", "Track", "OldArtist")
    mark_done(tmp_state_db, "manual-artist-move", file_path=str(dummy_audio))

    patcher, _ = _patch_http_client_get(
        client,
        return_value=_httpx_response({"title": "SameAlbum"}),
    )
    with patcher:
        with patch("src.api.asyncio.sleep", new_callable=AsyncMock):
            with patch("src.api.write_album_tag"):
                with patch("src.api.write_artist_tag"):
                    with patch("src.api.embed_cover_art", return_value=True):
                        with patch("src.api.threading.Thread") as mock_thread_cls:
                            mock_thread_cls.return_value = MagicMock()
                            resp = client.post(
                                "/api/rematch/apply",
                                json={
                                    "mbid": "manual-artist-move",
                                    "mb_recording_id": "rec-001",
                                    "mb_album_id": "album-001",
                                    "artist_name": "NewArtist",
                                },
                            )

    assert resp.status_code == 200
    expected_path = music_root / "NewArtist" / "SameAlbum" / "track.flac"
    assert not dummy_audio.exists(), "원본 파일이 이동되어 있어야 한다"
    assert expected_path.exists(), "새 아티스트/앨범 경로에 파일이 있어야 한다"

    row = get_download_by_mbid(tmp_state_db, "manual-artist-move")
    assert row["file_path"] == str(expected_path)


def test_rematch_apply_artist_and_album_change_moves_correctly(
    client, tmp_state_db, tmp_path
):
    """artist_name과 album_name이 모두 변경되면 새 아티스트/앨범 경로로 이동된다."""
    music_root = tmp_path / "music"
    artist_dir = music_root / "OldArtist"
    album_dir = artist_dir / "OldAlbum"
    album_dir.mkdir(parents=True)
    dummy_audio = album_dir / "track.flac"
    dummy_audio.write_bytes(b"fake flac data")

    from src.state import get_download_by_mbid, mark_done, mark_pending

    mark_pending(tmp_state_db, "manual-both-change", "Track", "OldArtist")
    mark_done(tmp_state_db, "manual-both-change", file_path=str(dummy_audio))

    patcher, _ = _patch_http_client_get(
        client,
        return_value=_httpx_response({"title": "NewAlbum"}),
    )
    with patcher:
        with patch("src.api.asyncio.sleep", new_callable=AsyncMock):
            with patch("src.api.write_album_tag"):
                with patch("src.api.write_artist_tag"):
                    with patch("src.api.embed_cover_art", return_value=True):
                        with patch("src.api.threading.Thread") as mock_thread_cls:
                            mock_thread_cls.return_value = MagicMock()
                            resp = client.post(
                                "/api/rematch/apply",
                                json={
                                    "mbid": "manual-both-change",
                                    "mb_recording_id": "rec-001",
                                    "mb_album_id": "album-001",
                                    "artist_name": "NewArtist",
                                },
                            )

    assert resp.status_code == 200
    expected_path = music_root / "NewArtist" / "NewAlbum" / "track.flac"
    assert not dummy_audio.exists(), "원본 파일이 이동되어 있어야 한다"
    assert expected_path.exists(), "새 아티스트/앨범 경로에 파일이 있어야 한다"

    row = get_download_by_mbid(tmp_state_db, "manual-both-change")
    assert row["file_path"] == str(expected_path)


def test_rematch_apply_no_artist_name_keeps_existing_artist_dir(
    client, tmp_state_db, tmp_path
):
    """artist_name이 없으면 기존 아티스트 폴더를 유지하고 앨범 폴더만 변경된다."""
    music_root = tmp_path / "music"
    artist_dir = music_root / "ExistingArtist"
    old_album_dir = artist_dir / "OldAlbum"
    old_album_dir.mkdir(parents=True)
    dummy_audio = old_album_dir / "track.flac"
    dummy_audio.write_bytes(b"fake flac data")

    from src.state import mark_done, mark_pending

    mark_pending(tmp_state_db, "manual-no-artist", "Track", "ExistingArtist")
    mark_done(tmp_state_db, "manual-no-artist", file_path=str(dummy_audio))

    patcher, _ = _patch_http_client_get(
        client,
        return_value=_httpx_response({"title": "NewAlbum"}),
    )
    with patcher:
        with patch("src.api.asyncio.sleep", new_callable=AsyncMock):
            with patch("src.api.write_album_tag"):
                with patch("src.api.write_artist_tag") as mock_write_artist:
                    with patch("src.api.embed_cover_art", return_value=True):
                        with patch("src.api.threading.Thread") as mock_thread_cls:
                            mock_thread_cls.return_value = MagicMock()
                            resp = client.post(
                                "/api/rematch/apply",
                                json={
                                    "mbid": "manual-no-artist",
                                    "mb_recording_id": "rec-001",
                                    "mb_album_id": "album-001",
                                },
                            )

    assert resp.status_code == 200
    expected_path = artist_dir / "NewAlbum" / "track.flac"
    assert not dummy_audio.exists(), "앨범이 바뀌었으므로 파일이 이동되어야 한다"
    assert expected_path.exists(), (
        "기존 아티스트 폴더 안 새 앨범 경로에 파일이 있어야 한다"
    )
    mock_write_artist.assert_not_called()


def test_rematch_apply_removes_empty_album_dir_after_move(
    client, tmp_state_db, tmp_path
):
    """파일 이동 후 기존 앨범 폴더가 비어있으면 삭제된다."""
    artist_dir = tmp_path / "Artist"
    old_album_dir = artist_dir / "OldAlbum"
    old_album_dir.mkdir(parents=True)
    dummy_audio = old_album_dir / "track.flac"
    dummy_audio.write_bytes(b"fake flac data")

    from src.state import mark_done, mark_pending

    mark_pending(tmp_state_db, "manual-rmdir", "Track", "Artist")
    mark_done(tmp_state_db, "manual-rmdir", file_path=str(dummy_audio))

    patcher, _ = _patch_http_client_get(
        client,
        return_value=_httpx_response({"title": "NewAlbum"}),
    )
    with patcher:
        with patch("src.api.asyncio.sleep", new_callable=AsyncMock):
            with patch("src.api.write_album_tag"):
                with patch("src.api.embed_cover_art", return_value=True):
                    with patch("src.api.threading.Thread") as mock_thread_cls:
                        mock_thread_cls.return_value = MagicMock()
                        resp = client.post(
                            "/api/rematch/apply",
                            json={
                                "mbid": "manual-rmdir",
                                "mb_recording_id": "rec-001",
                                "mb_album_id": "album-001",
                            },
                        )

    assert resp.status_code == 200
    assert not old_album_dir.exists(), "이동 후 빈 앨범 폴더는 삭제되어야 한다"


def test_rematch_apply_updates_artist_in_db_after_move(client, tmp_state_db, tmp_path):
    """artist_name이 주어지면 파일 이동 후 state.db의 artist 컬럼도 업데이트된다."""
    music_root = tmp_path / "music"
    artist_dir = music_root / "OldArtist"
    album_dir = artist_dir / "Album"
    album_dir.mkdir(parents=True)
    dummy_audio = album_dir / "track.flac"
    dummy_audio.write_bytes(b"fake flac data")

    from src.state import get_download_by_mbid, mark_done, mark_pending

    mark_pending(tmp_state_db, "manual-artist-db", "Track", "OldArtist")
    mark_done(tmp_state_db, "manual-artist-db", file_path=str(dummy_audio))

    patcher, _ = _patch_http_client_get(
        client,
        return_value=_httpx_response({"title": "Album"}),
    )
    with patcher:
        with patch("src.api.asyncio.sleep", new_callable=AsyncMock):
            with patch("src.api.write_album_tag"):
                with patch("src.api.write_artist_tag"):
                    with patch("src.api.embed_cover_art", return_value=True):
                        with patch("src.api.threading.Thread") as mock_thread_cls:
                            mock_thread_cls.return_value = MagicMock()
                            resp = client.post(
                                "/api/rematch/apply",
                                json={
                                    "mbid": "manual-artist-db",
                                    "mb_recording_id": "rec-001",
                                    "mb_album_id": "album-001",
                                    "artist_name": "NewArtist",
                                },
                            )

    assert resp.status_code == 200
    row = get_download_by_mbid(tmp_state_db, "manual-artist-db")
    assert row["artist"] == "NewArtist", (
        "state.db의 artist 컬럼이 NewArtist로 업데이트되어야 한다"
    )


# ── resolve_dir 단위 테스트 ──────────────────────────────────────────────────


def test_resolve_dir_reuses_existing_case_insensitive_folder(tmp_path):
    """parent 안에 대소문자만 다른 폴더가 있으면 그 실제 이름을 반환한다."""
    from src.utils.fs import resolve_dir

    existing = tmp_path / "Eminem"
    existing.mkdir()

    result = resolve_dir(str(tmp_path), "eminem")
    assert result == "Eminem"


def test_resolve_dir_returns_sanitized_when_no_match(tmp_path):
    """일치하는 폴더가 없으면 sanitize된 name을 그대로 반환한다."""
    from src.utils.fs import resolve_dir

    result = resolve_dir(str(tmp_path), "NewArtist")
    assert result == "NewArtist"


def test_resolve_dir_returns_sanitized_when_parent_not_exist(tmp_path):
    """parent 디렉토리 자체가 없으면 sanitize된 name을 반환한다."""
    from src.utils.fs import resolve_dir

    nonexistent = str(tmp_path / "no_such_dir")
    result = resolve_dir(nonexistent, "SomeArtist")
    assert result == "SomeArtist"


def test_resolve_dir_does_not_match_file(tmp_path):
    """파일(디렉토리가 아님)은 매칭 대상에서 제외된다."""
    from src.utils.fs import resolve_dir

    file_entry = tmp_path / "eminem"
    file_entry.write_text("not a dir")

    result = resolve_dir(str(tmp_path), "Eminem")
    assert result == "Eminem"


# ── case-insensitive 폴더 충돌 방지 통합 테스트 ──────────────────────────────


def test_rematch_apply_reuses_existing_artist_dir_case_insensitive(
    client, tmp_state_db, tmp_path
):
    """artist_name의 대소문자가 기존 폴더와 달라도 기존 폴더를 재사용한다."""
    music_root = tmp_path / "music"
    existing_artist_dir = music_root / "Eminem"
    old_album_dir = existing_artist_dir / "OldAlbum"
    old_album_dir.mkdir(parents=True)
    dummy_audio = old_album_dir / "track.flac"
    dummy_audio.write_bytes(b"fake flac data")

    from src.state import mark_done, mark_pending

    mark_pending(tmp_state_db, "manual-case-artist", "Track", "Eminem")
    mark_done(tmp_state_db, "manual-case-artist", file_path=str(dummy_audio))

    patcher, _ = _patch_http_client_get(
        client,
        return_value=_httpx_response({"title": "NewAlbum"}),
    )
    with patcher:
        with patch("src.api.asyncio.sleep", new_callable=AsyncMock):
            with patch("src.api.write_album_tag"):
                with patch("src.api.write_artist_tag"):
                    with patch("src.api.embed_cover_art", return_value=True):
                        with patch("src.api.threading.Thread") as mock_thread_cls:
                            mock_thread_cls.return_value = MagicMock()
                            resp = client.post(
                                "/api/rematch/apply",
                                json={
                                    "mbid": "manual-case-artist",
                                    "mb_recording_id": "rec-001",
                                    "mb_album_id": "album-001",
                                    "artist_name": "eminem",
                                },
                            )

    assert resp.status_code == 200
    # 기존 "Eminem" 폴더를 재사용해야 한다 (대소문자 불일치 입력 "eminem"이 들어와도)
    expected_path = music_root / "Eminem" / "NewAlbum" / "track.flac"
    assert expected_path.exists(), "기존 대소문자 폴더(Eminem)를 재사용해야 한다"
    # Linux(case-sensitive)에서는 별도 "eminem" 폴더가 생기지 않아야 한다.
    # macOS는 case-insensitive FS이므로 "eminem".exists() == "Eminem".exists() 가 되어 검사 제외.
    import platform

    if platform.system() == "Linux":
        assert not (music_root / "eminem").exists(), (
            "Linux: 새 소문자 폴더가 생기면 안 된다"
        )


def test_rematch_apply_reuses_existing_album_dir_case_insensitive(
    client, tmp_state_db, tmp_path
):
    """album_name의 대소문자가 기존 폴더와 달라도 기존 앨범 폴더를 재사용한다."""
    music_root = tmp_path / "music"
    artist_dir = music_root / "Artist"
    existing_album_dir = artist_dir / "The Marshall Mathers LP"
    old_album_dir = artist_dir / "OldAlbum"
    old_album_dir.mkdir(parents=True)
    existing_album_dir.mkdir(parents=True)
    dummy_audio = old_album_dir / "track.flac"
    dummy_audio.write_bytes(b"fake flac data")

    from src.state import mark_done, mark_pending

    mark_pending(tmp_state_db, "manual-case-album", "Track", "Artist")
    mark_done(tmp_state_db, "manual-case-album", file_path=str(dummy_audio))

    patcher, _ = _patch_http_client_get(
        client,
        return_value=_httpx_response({"title": "the marshall mathers lp"}),
    )
    with patcher:
        with patch("src.api.asyncio.sleep", new_callable=AsyncMock):
            with patch("src.api.write_album_tag"):
                with patch("src.api.embed_cover_art", return_value=True):
                    with patch("src.api.threading.Thread") as mock_thread_cls:
                        mock_thread_cls.return_value = MagicMock()
                        resp = client.post(
                            "/api/rematch/apply",
                            json={
                                "mbid": "manual-case-album",
                                "mb_recording_id": "rec-001",
                                "mb_album_id": "album-001",
                            },
                        )

    assert resp.status_code == 200
    # 기존 "The Marshall Mathers LP" 폴더를 재사용해야 한다
    expected_path = artist_dir / "The Marshall Mathers LP" / "track.flac"
    assert expected_path.exists(), "기존 대소문자 앨범 폴더를 재사용해야 한다"
    # Linux(case-sensitive)에서는 별도 소문자 폴더가 생기지 않아야 한다.
    import platform

    if platform.system() == "Linux":
        assert not (artist_dir / "the marshall mathers lp").exists(), (
            "Linux: 새 소문자 폴더가 생기면 안 된다"
        )


# ── Input Validation + Rate Limiting ─────────────────────────────────────────


def test_post_download_rejects_long_artist(client):
    resp = client.post("/api/download", json={"artist": "A" * 501, "track": "t"})
    assert resp.status_code == 422


def test_post_download_accepts_max_length_artist(client):
    resp = client.post("/api/download", json={"artist": "A" * 500, "track": "t"})
    assert resp.status_code == 200


def test_rate_limit_returns_429(client):
    """11th request within 60s should return 429."""
    import src.api as api_mod

    api_mod._rate_store.clear()
    for i in range(10):
        resp = client.post("/api/download", json={"artist": f"a{i}", "track": "t"})
        assert resp.status_code == 200, f"Request {i + 1} failed: {resp.status_code}"
    resp = client.post("/api/download", json={"artist": "overflow", "track": "t"})
    assert resp.status_code == 429


def test_rate_limit_not_applied_to_get(client):
    """GET endpoints should not be rate limited."""
    for _ in range(20):
        resp = client.get("/api/downloads")
        assert resp.status_code == 200


# ── Pipeline Interval Settings ────────────────────────────────────────────────


def test_get_pipeline_interval_returns_default(client):
    resp = client.get("/api/settings/pipeline-interval")
    assert resp.status_code == 200
    data = resp.json()
    assert data["interval_hours"] == 6


def test_put_pipeline_interval_updates_value(client):
    resp = client.put(
        "/api/settings/pipeline-interval",
        json={"interval_hours": 12},
    )
    assert resp.status_code == 200
    assert resp.json()["interval_hours"] == 12

    # Verify persisted
    resp2 = client.get("/api/settings/pipeline-interval")
    assert resp2.json()["interval_hours"] == 12


def test_put_pipeline_interval_rejects_invalid(client):
    resp = client.put(
        "/api/settings/pipeline-interval",
        json={"interval_hours": 0},
    )
    assert resp.status_code == 422

    resp2 = client.put(
        "/api/settings/pipeline-interval",
        json={"interval_hours": 25},
    )
    assert resp2.status_code == 422
