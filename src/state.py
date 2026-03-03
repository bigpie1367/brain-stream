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
    log.info("state.db initialised", path=db_path)


def is_downloaded(db_path: str, mbid: str) -> bool:
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM downloads WHERE mbid = ?", (mbid,)
        ).fetchone()
    return row is not None and row["status"] == "done"


def mark_pending(db_path: str, mbid: str, track_name: str, artist: str, source: str = "listenbrainz"):
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


def mark_done(db_path: str, mbid: str):
    with _conn(db_path) as conn:
        conn.execute("""
            UPDATE downloads
            SET status = 'done', downloaded_at = ?
            WHERE mbid = ?
        """, (datetime.utcnow().isoformat(), mbid))


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


def get_all_downloads(db_path: str, limit: int = 100) -> List[dict]:
    with _conn(db_path) as conn:
        rows = conn.execute("""
            SELECT mbid, track_name, artist, status, source,
                   attempts, downloaded_at, error_msg
            FROM downloads
            ORDER BY rowid DESC
            LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]
