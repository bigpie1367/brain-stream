"""
tests/unit/test_state.py
state.py의 StateDB 함수군 단위 테스트
"""
import pytest

from src.state import (
    mark_pending,
    mark_downloading,
    mark_done,
    mark_failed,
    get_all_downloads,
    get_retryable,
    is_downloaded,
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
    mark_pending(tmp_state_db, "manual-abc123", "Bohemian Rhapsody", "Queen", source="manual")
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


def test_is_downloaded_true_after_mark_done(tmp_state_db):
    mark_pending(tmp_state_db, "mbid-chk", "Song", "Artist")
    mark_done(tmp_state_db, "mbid-chk")
    assert is_downloaded(tmp_state_db, "mbid-chk") is True


def test_is_downloaded_false_when_pending(tmp_state_db):
    mark_pending(tmp_state_db, "mbid-pnd", "Song", "Artist")
    assert is_downloaded(tmp_state_db, "mbid-pnd") is False


def test_is_downloaded_false_when_not_exist(tmp_state_db):
    assert is_downloaded(tmp_state_db, "nonexistent-mbid") is False


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
