import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import List, Optional

from src.utils.logger import get_logger

log = get_logger(__name__)


@contextmanager
def _conn(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: str):
    with _conn(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS downloads (
                mbid        TEXT PRIMARY KEY,
                track_name  TEXT NOT NULL,
                artist      TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'pending',
                attempts    INTEGER NOT NULL DEFAULT 0,
                downloaded_at TEXT,
                error_msg   TEXT
            )
        """)
        # Migrate: add source column if missing
        try:
            conn.execute("ALTER TABLE downloads ADD COLUMN source TEXT DEFAULT 'listenbrainz'")
        except sqlite3.OperationalError:
            pass  # already exists
        # Migrate: add file_path column if missing
        try:
            conn.execute("ALTER TABLE downloads ADD COLUMN file_path TEXT")
        except sqlite3.OperationalError:
            pass  # already exists
        # Migrate: add album column if missing
        try:
            conn.execute("ALTER TABLE downloads ADD COLUMN album TEXT")
        except sqlite3.OperationalError:
            pass  # already exists
        # Migrate: add mb_recording_id column if missing
        try:
            conn.execute("ALTER TABLE downloads ADD COLUMN mb_recording_id TEXT")
        except sqlite3.OperationalError:
            pass  # already exists
    log.info("state.db initialised", path=db_path)


def is_downloaded(db_path: str, mbid: str) -> bool:
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM downloads WHERE mbid = ?", (mbid,)
        ).fetchone()
    return row is not None and row["status"] in ("done", "ignored")


def mark_pending(
    db_path: str, mbid: str, track_name: str, artist: str, source: str = "listenbrainz"
):
    with _conn(db_path) as conn:
        conn.execute("""
            INSERT OR IGNORE INTO downloads (mbid, track_name, artist, status, source)
            VALUES (?, ?, ?, 'pending', ?)
        """, (mbid, track_name, artist, source))


def mark_downloading(db_path: str, mbid: str):
    with _conn(db_path) as conn:
        conn.execute("""
            UPDATE downloads SET status = 'downloading' WHERE mbid = ?
        """, (mbid,))


def mark_done(db_path: str, mbid: str, file_path: str = None, album: str = None):
    with _conn(db_path) as conn:
        conn.execute("""
            UPDATE downloads
            SET status = 'done', downloaded_at = ?, file_path = ?, album = ?
            WHERE mbid = ?
        """, (datetime.utcnow().isoformat(), file_path, album, mbid))


def mark_failed(db_path: str, mbid: str, error: str):
    with _conn(db_path) as conn:
        conn.execute("""
            UPDATE downloads
            SET status = 'failed',
                attempts = attempts + 1,
                error_msg = ?
            WHERE mbid = ?
        """, (error, mbid))


def get_retryable(db_path: str, max_attempts: int = 3) -> List[sqlite3.Row]:
    with _conn(db_path) as conn:
        rows = conn.execute("""
            SELECT mbid, track_name, artist
            FROM downloads
            WHERE status = 'failed' AND attempts < ?
        """, (max_attempts,)).fetchall()
    return [dict(r) for r in rows]


def get_pending_jobs(db_path: str) -> List[dict]:
    """재시작 복구용: pending/downloading 잡을 원래 적재 순서(rowid ASC)로 반환."""
    with _conn(db_path) as conn:
        rows = conn.execute("""
            SELECT mbid, track_name, artist, source, status, attempts
            FROM downloads
            WHERE status IN ('pending', 'downloading')
            ORDER BY rowid ASC
        """).fetchall()
    return [dict(r) for r in rows]


def get_all_downloads(db_path: str, limit: int = 100) -> List[dict]:
    with _conn(db_path) as conn:
        rows = conn.execute("""
            SELECT mbid, track_name, artist, album, status, source,
                   attempts, downloaded_at, error_msg, file_path, mb_recording_id
            FROM downloads
            ORDER BY rowid DESC
            LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def get_download_by_mbid(db_path: str, mbid: str) -> Optional[dict]:
    with _conn(db_path) as conn:
        row = conn.execute("""
            SELECT mbid, track_name, artist, album, status, source,
                   attempts, downloaded_at, error_msg, file_path, mb_recording_id
            FROM downloads
            WHERE mbid = ?
        """, (mbid,)).fetchone()
    return dict(row) if row is not None else None


def update_file_path(db_path: str, mbid: str, new_file_path: str):
    with _conn(db_path) as conn:
        conn.execute(
            "UPDATE downloads SET file_path = ? WHERE mbid = ?",
            (new_file_path, mbid),
        )


def update_track_info(
    db_path: str,
    mbid: str,
    *,
    artist: str | None = None,
    track_name: str | None = None,
    file_path: str | None = None,
    album: str | None = None,
    mb_recording_id: str | None = None,
):
    """아티스트·트랙명·파일경로·앨범명·MB recording ID를 선택적으로 업데이트한다."""
    fields = []
    values = []
    if artist is not None:
        fields.append("artist = ?")
        values.append(artist)
    if track_name is not None:
        fields.append("track_name = ?")
        values.append(track_name)
    if file_path is not None:
        fields.append("file_path = ?")
        values.append(file_path)
    if album is not None:
        fields.append("album = ?")
        values.append(album)
    if mb_recording_id is not None:
        fields.append("mb_recording_id = ?")
        values.append(mb_recording_id)
    if not fields:
        return
    values.append(mbid)
    with _conn(db_path) as conn:
        conn.execute(f"UPDATE downloads SET {', '.join(fields)} WHERE mbid = ?", values)


def mark_ignored(db_path: str, mbid: str):
    """사용자가 명시적으로 제거한 트랙. 파이프라인이 재다운로드하지 않도록 스킵."""
    with _conn(db_path) as conn:
        conn.execute(
            "UPDATE downloads SET status = 'ignored' WHERE mbid = ?",
            (mbid,),
        )


def delete_download(db_path: str, mbid: str):
    with _conn(db_path) as conn:
        conn.execute("DELETE FROM downloads WHERE mbid = ?", (mbid,))
