"""
tests/unit/test_state.py
state.py의 StateDB 함수군 단위 테스트
"""

from src.state import (
    find_active_download,
    get_all_downloads,
    get_download_by_mbid,
    get_downloads_page,
    get_retryable,
    is_downloaded,
    mark_done,
    mark_downloading,
    mark_failed,
    mark_ignored,
    mark_pending,
    update_file_path,
    update_track_info,
)


# ── mark_pending / get_all_downloads ──────────────────────────────────────────


def test_mark_pending_creates_row(tmp_state_db):
    mark_pending(tmp_state_db, "mbid-001", "Creep", "Radiohead")
    rows = get_all_downloads(tmp_state_db)
    assert len(rows) == 1
    row = rows[0]
    assert row["mbid"] == "mbid-001"
    assert row["track_name"] == "Creep"
    assert row["artist"] == "Radiohead"
    assert row["status"] == "pending"


def test_mark_pending_default_source_is_listenbrainz(tmp_state_db):
    mark_pending(tmp_state_db, "mbid-002", "OK Computer", "Radiohead")
    rows = get_all_downloads(tmp_state_db)
    assert rows[0]["source"] == "listenbrainz"


def test_mark_pending_custom_source(tmp_state_db):
    mark_pending(
        tmp_state_db, "manual-abc123", "Bohemian Rhapsody", "Queen", source="manual"
    )
    rows = get_all_downloads(tmp_state_db)
    assert rows[0]["source"] == "manual"


# ── 중복 insert 무시 (dedup) ──────────────────────────────────────────────────


def test_mark_pending_duplicate_ignored(tmp_state_db):
    """같은 mbid를 두 번 insert해도 레코드는 하나만 남아야 한다."""
    mark_pending(tmp_state_db, "mbid-dup", "Track A", "Artist A")
    mark_pending(tmp_state_db, "mbid-dup", "Track A (2nd)", "Artist A")
    rows = get_all_downloads(tmp_state_db)
    assert len(rows) == 1
    # 첫 번째 insert 값이 유지되어야 한다
    assert rows[0]["track_name"] == "Track A"


# ── mark_downloading ──────────────────────────────────────────────────────────


def test_mark_downloading_changes_status(tmp_state_db):
    mark_pending(tmp_state_db, "mbid-dl", "Song", "Artist")
    mark_downloading(tmp_state_db, "mbid-dl")
    rows = get_all_downloads(tmp_state_db)
    assert rows[0]["status"] == "downloading"


# ── mark_done ────────────────────────────────────────────────────────────────


def test_mark_done_changes_status(tmp_state_db):
    mark_pending(tmp_state_db, "mbid-done", "Song", "Artist")
    mark_done(tmp_state_db, "mbid-done")
    rows = get_all_downloads(tmp_state_db)
    assert rows[0]["status"] == "done"
    assert rows[0]["downloaded_at"] is not None


def test_mark_done_stores_file_path(tmp_state_db):
    """mark_done에 file_path를 전달하면 DB에 저장된다."""
    mark_pending(tmp_state_db, "mbid-fp", "Song", "Artist")
    mark_done(
        tmp_state_db, "mbid-fp", file_path="/app/data/music/Artist/Album/Song.flac"
    )
    rows = get_all_downloads(tmp_state_db)
    assert rows[0]["file_path"] == "/app/data/music/Artist/Album/Song.flac"


def test_mark_done_file_path_defaults_to_none(tmp_state_db):
    """file_path를 전달하지 않으면 None으로 저장된다."""
    mark_pending(tmp_state_db, "mbid-fp2", "Song", "Artist")
    mark_done(tmp_state_db, "mbid-fp2")
    rows = get_all_downloads(tmp_state_db)
    assert rows[0]["file_path"] is None


def test_is_downloaded_true_after_mark_done(tmp_state_db):
    mark_pending(tmp_state_db, "mbid-chk", "Song", "Artist")
    mark_done(tmp_state_db, "mbid-chk")
    assert is_downloaded(tmp_state_db, "mbid-chk") is True


def test_is_downloaded_true_when_pending(tmp_state_db):
    """pending 상태도 is_downloaded=True — 파이프라인이 중복 enqueue하지 않도록."""
    mark_pending(tmp_state_db, "mbid-pnd", "Song", "Artist")
    assert is_downloaded(tmp_state_db, "mbid-pnd") is True


def test_is_downloaded_false_when_not_exist(tmp_state_db):
    assert is_downloaded(tmp_state_db, "nonexistent-mbid") is False


def test_is_downloaded_true_after_mark_ignored(tmp_state_db):
    mark_pending(tmp_state_db, "mbid-ign", "Song", "Artist")
    mark_ignored(tmp_state_db, "mbid-ign")
    assert is_downloaded(tmp_state_db, "mbid-ign") is True


def test_mark_ignored_sets_status(tmp_state_db):
    mark_pending(tmp_state_db, "mbid-ign2", "Song", "Artist")
    mark_ignored(tmp_state_db, "mbid-ign2")
    row = get_download_by_mbid(tmp_state_db, "mbid-ign2")
    assert row["status"] == "ignored"


def test_mark_ignored_excluded_from_retryable(tmp_state_db):
    mark_pending(tmp_state_db, "mbid-ign3", "Song", "Artist")
    mark_ignored(tmp_state_db, "mbid-ign3")
    retryable = get_retryable(tmp_state_db, max_attempts=3)
    assert not any(r["mbid"] == "mbid-ign3" for r in retryable)


def test_mark_ignored_included_in_get_all_downloads(tmp_state_db):
    mark_pending(tmp_state_db, "mbid-ign4", "Song", "Artist")
    mark_ignored(tmp_state_db, "mbid-ign4")
    rows = get_all_downloads(tmp_state_db)
    assert any(r["mbid"] == "mbid-ign4" and r["status"] == "ignored" for r in rows)


# ── mark_failed ───────────────────────────────────────────────────────────────


def test_mark_failed_changes_status(tmp_state_db):
    mark_pending(tmp_state_db, "mbid-fail", "Song", "Artist")
    mark_failed(tmp_state_db, "mbid-fail", "download error")
    rows = get_all_downloads(tmp_state_db)
    assert rows[0]["status"] == "failed"
    assert rows[0]["error_msg"] == "download error"


def test_mark_failed_increments_attempts(tmp_state_db):
    mark_pending(tmp_state_db, "mbid-retry", "Song", "Artist")
    mark_failed(tmp_state_db, "mbid-retry", "error 1")
    mark_failed(tmp_state_db, "mbid-retry", "error 2")
    rows = get_all_downloads(tmp_state_db)
    assert rows[0]["attempts"] == 2


# ── get_retryable ─────────────────────────────────────────────────────────────


def test_get_retryable_returns_failed_under_max(tmp_state_db):
    mark_pending(tmp_state_db, "mbid-r1", "Song A", "Artist A")
    mark_failed(tmp_state_db, "mbid-r1", "err")  # attempts=1
    retryable = get_retryable(tmp_state_db, max_attempts=3)
    assert any(r["mbid"] == "mbid-r1" for r in retryable)


def test_get_retryable_excludes_max_attempts(tmp_state_db):
    """attempts가 max_attempts 이상이면 get_retryable에서 제외된다."""
    mark_pending(tmp_state_db, "mbid-ex", "Song B", "Artist B")
    mark_failed(tmp_state_db, "mbid-ex", "err")
    mark_failed(tmp_state_db, "mbid-ex", "err")
    mark_failed(tmp_state_db, "mbid-ex", "err")  # attempts=3
    retryable = get_retryable(tmp_state_db, max_attempts=3)
    assert not any(r["mbid"] == "mbid-ex" for r in retryable)


def test_get_retryable_excludes_done(tmp_state_db):
    mark_pending(tmp_state_db, "mbid-ok", "Song C", "Artist C")
    mark_done(tmp_state_db, "mbid-ok")
    retryable = get_retryable(tmp_state_db, max_attempts=3)
    assert not any(r["mbid"] == "mbid-ok" for r in retryable)


# ── get_all_downloads 정렬 / limit ────────────────────────────────────────────


def test_get_all_downloads_returns_latest_first(tmp_state_db):
    mark_pending(tmp_state_db, "mbid-first", "First", "Artist")
    mark_pending(tmp_state_db, "mbid-second", "Second", "Artist")
    rows = get_all_downloads(tmp_state_db)
    # ORDER BY rowid DESC — 최신 항목이 앞에 와야 한다
    assert rows[0]["mbid"] == "mbid-second"
    assert rows[1]["mbid"] == "mbid-first"


def test_get_all_downloads_limit(tmp_state_db):
    for i in range(5):
        mark_pending(tmp_state_db, f"mbid-{i}", f"Track {i}", "Artist")
    rows = get_all_downloads(tmp_state_db, limit=3)
    assert len(rows) == 3


# ── update_file_path ──────────────────────────────────────────────────────────


def test_update_file_path_changes_stored_path(tmp_state_db):
    """update_file_path가 file_path 컬럼을 새 값으로 업데이트한다."""
    mark_pending(tmp_state_db, "mbid-upfp", "Track", "Artist")
    mark_done(tmp_state_db, "mbid-upfp", file_path="/old/path/track.flac")

    update_file_path(tmp_state_db, "mbid-upfp", "/new/path/track.flac")

    row = get_download_by_mbid(tmp_state_db, "mbid-upfp")
    assert row["file_path"] == "/new/path/track.flac"


def test_update_file_path_nonexistent_mbid_does_not_raise(tmp_state_db):
    """존재하지 않는 mbid에 대해 update_file_path를 호출해도 예외가 발생하지 않는다."""
    update_file_path(
        tmp_state_db, "mbid-ghost", "/some/path/track.flac"
    )  # should not raise


# ── update_track_info ─────────────────────────────────────────────────────────


def test_update_track_info_updates_artist(tmp_state_db):
    """update_track_info로 artist 컬럼을 업데이트할 수 있다."""
    mark_pending(tmp_state_db, "mbid-ti1", "Track", "OldArtist")
    update_track_info(tmp_state_db, "mbid-ti1", artist="NewArtist")
    row = get_download_by_mbid(tmp_state_db, "mbid-ti1")
    assert row["artist"] == "NewArtist"


def test_update_track_info_updates_file_path(tmp_state_db):
    """update_track_info로 file_path 컬럼을 업데이트할 수 있다."""
    mark_pending(tmp_state_db, "mbid-ti2", "Track", "Artist")
    mark_done(tmp_state_db, "mbid-ti2", file_path="/old/path/track.flac")
    update_track_info(tmp_state_db, "mbid-ti2", file_path="/new/path/track.flac")
    row = get_download_by_mbid(tmp_state_db, "mbid-ti2")
    assert row["file_path"] == "/new/path/track.flac"


def test_update_track_info_updates_both(tmp_state_db):
    """update_track_info로 artist와 file_path를 동시에 업데이트할 수 있다."""
    mark_pending(tmp_state_db, "mbid-ti3", "Track", "OldArtist")
    mark_done(tmp_state_db, "mbid-ti3", file_path="/old/path/track.flac")
    update_track_info(
        tmp_state_db, "mbid-ti3", artist="NewArtist", file_path="/new/path/track.flac"
    )
    row = get_download_by_mbid(tmp_state_db, "mbid-ti3")
    assert row["artist"] == "NewArtist"
    assert row["file_path"] == "/new/path/track.flac"


def test_update_track_info_no_fields_is_noop(tmp_state_db):
    """필드를 하나도 지정하지 않으면 아무 변화 없이 반환된다."""
    mark_pending(tmp_state_db, "mbid-ti4", "Track", "Artist")
    mark_done(tmp_state_db, "mbid-ti4", file_path="/path/track.flac")
    update_track_info(tmp_state_db, "mbid-ti4")  # no fields — should be no-op
    row = get_download_by_mbid(tmp_state_db, "mbid-ti4")
    assert row["artist"] == "Artist"
    assert row["file_path"] == "/path/track.flac"


def test_update_track_info_nonexistent_mbid_does_not_raise(tmp_state_db):
    """존재하지 않는 mbid에 update_track_info를 호출해도 예외가 발생하지 않는다."""
    update_track_info(
        tmp_state_db, "mbid-ghost2", artist="SomeArtist"
    )  # should not raise


def test_update_track_info_updates_mb_recording_id(tmp_state_db):
    """update_track_info로 mb_recording_id 컬럼을 업데이트할 수 있다."""
    mark_pending(tmp_state_db, "mbid-rec1", "Track", "Artist")
    update_track_info(tmp_state_db, "mbid-rec1", mb_recording_id="some-recording-uuid")
    row = get_download_by_mbid(tmp_state_db, "mbid-rec1")
    assert row["mb_recording_id"] == "some-recording-uuid"


def test_get_all_downloads_includes_mb_recording_id(tmp_state_db):
    """get_all_downloads 결과에 mb_recording_id 컬럼이 포함된다."""
    mark_pending(tmp_state_db, "mbid-rec2", "Track", "Artist")
    update_track_info(tmp_state_db, "mbid-rec2", mb_recording_id="rec-uuid-001")
    rows = get_all_downloads(tmp_state_db)
    assert rows[0]["mb_recording_id"] == "rec-uuid-001"


def test_get_download_by_mbid_includes_mb_recording_id(tmp_state_db):
    """get_download_by_mbid 결과에 mb_recording_id 컬럼이 포함된다."""
    mark_pending(tmp_state_db, "mbid-rec3", "Track", "Artist")
    update_track_info(tmp_state_db, "mbid-rec3", mb_recording_id="rec-uuid-002")
    row = get_download_by_mbid(tmp_state_db, "mbid-rec3")
    assert row["mb_recording_id"] == "rec-uuid-002"


def test_mb_recording_id_defaults_to_none(tmp_state_db):
    """mb_recording_id를 설정하지 않으면 None으로 조회된다."""
    mark_pending(tmp_state_db, "mbid-rec4", "Track", "Artist")
    row = get_download_by_mbid(tmp_state_db, "mbid-rec4")
    assert row["mb_recording_id"] is None


# ── mark_done album COALESCE ──────────────────────────────────────────────────


def test_mark_done_preserves_album_when_none(tmp_state_db):
    """mark_done(album=None) should NOT overwrite existing album value."""
    mark_pending(tmp_state_db, "mbid-album-test", "Creep", "Radiohead")
    update_track_info(tmp_state_db, "mbid-album-test", album="The Bends")
    mark_done(tmp_state_db, "mbid-album-test", file_path="/music/test.flac", album=None)
    row = get_download_by_mbid(tmp_state_db, "mbid-album-test")
    assert row["album"] == "The Bends", (
        "album should be preserved when mark_done album=None"
    )


def test_mark_done_overwrites_album_when_provided(tmp_state_db):
    """mark_done(album='X') should update album to 'X'."""
    mark_pending(tmp_state_db, "mbid-album-test2", "Creep", "Radiohead")
    update_track_info(tmp_state_db, "mbid-album-test2", album="The Bends")
    mark_done(
        tmp_state_db,
        "mbid-album-test2",
        file_path="/music/test.flac",
        album="OK Computer",
    )
    row = get_download_by_mbid(tmp_state_db, "mbid-album-test2")
    assert row["album"] == "OK Computer"


# ── get_downloads_page ────────────────────────────────────────────────────────


def test_get_downloads_page_returns_paginated(tmp_state_db):
    for i in range(5):
        mark_pending(tmp_state_db, f"mbid-{i}", f"Track {i}", "Artist")
    result = get_downloads_page(tmp_state_db, limit=2, offset=0)
    assert result["total"] == 5
    assert len(result["items"]) == 2
    assert result["limit"] == 2
    assert result["offset"] == 0


def test_get_downloads_page_offset(tmp_state_db):
    for i in range(5):
        mark_pending(tmp_state_db, f"mbid-{i}", f"Track {i}", "Artist")
    result = get_downloads_page(tmp_state_db, limit=2, offset=2)
    assert len(result["items"]) == 2
    assert result["offset"] == 2


def test_get_downloads_page_search(tmp_state_db):
    mark_pending(tmp_state_db, "mbid-1", "Creep", "Radiohead")
    mark_pending(tmp_state_db, "mbid-2", "Karma Police", "Radiohead")
    mark_pending(tmp_state_db, "mbid-3", "Bohemian Rhapsody", "Queen")
    result = get_downloads_page(tmp_state_db, search="Radiohead")
    assert result["total"] == 2


# ── find_active_download ──────────────────────────────────────────────────────


def test_find_active_download_found(tmp_state_db):
    mark_pending(tmp_state_db, "mbid-1", "Creep", "Radiohead")
    result = find_active_download(tmp_state_db, "Radiohead", "Creep")
    assert result is not None
    assert result["mbid"] == "mbid-1"


def test_find_active_download_not_found(tmp_state_db):
    result = find_active_download(tmp_state_db, "Radiohead", "Creep")
    assert result is None


def test_find_active_download_ignores_failed(tmp_state_db):
    mark_pending(tmp_state_db, "mbid-1", "Creep", "Radiohead")
    mark_failed(tmp_state_db, "mbid-1", "error")
    result = find_active_download(tmp_state_db, "Radiohead", "Creep")
    assert result is None


# ── settings 테이블 ──────────────────────────────────────────────────────────


def test_get_setting_returns_default_when_not_set(tmp_state_db):
    from src.state import get_setting

    assert get_setting(tmp_state_db, "cf_offset", "0") == "0"


def test_set_and_get_setting(tmp_state_db):
    from src.state import get_setting, set_setting

    set_setting(tmp_state_db, "cf_offset", "25")
    assert get_setting(tmp_state_db, "cf_offset", "0") == "25"


def test_set_setting_overwrites_existing(tmp_state_db):
    from src.state import get_setting, set_setting

    set_setting(tmp_state_db, "cf_offset", "25")
    set_setting(tmp_state_db, "cf_offset", "50")
    assert get_setting(tmp_state_db, "cf_offset", "0") == "50"


def test_get_setting_returns_default_when_empty_string_key(tmp_state_db):
    from src.state import get_setting

    assert get_setting(tmp_state_db, "nonexistent") == ""
