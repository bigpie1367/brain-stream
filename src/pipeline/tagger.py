import os
import subprocess
import threading
import time
from pathlib import Path

import mediafile
import mutagen
import mutagen.flac
import mutagen.mp4
import mutagen.oggopus
import requests

from src.utils.logger import get_logger

log = get_logger(__name__)

_IMPORT_LOG = "/app/data/logs/beets-import.log"
_beet_lock = threading.Lock()  # beet import는 동시 실행 시 log가 오염되므로 직렬화
_BEET_IMPORT_RETRIES = 3  # skip 감지 시 최대 재시도 횟수
_BEET_IMPORT_RETRY_DELAY = 10  # 재시도 전 대기 시간(초) — MB API 타임아웃 회복용
_MB_API = "https://musicbrainz.org/ws/2"
_MB_HEADERS = {"User-Agent": "music-bot/1.0 (https://github.com/music-bot)"}


def _mb_album_from_recording_id(recording_id: str) -> tuple[str, str]:
    """Get (album_title, mb_albumid) from a MusicBrainz recording ID."""
    try:
        time.sleep(1)  # rate limit
        r = requests.get(
            f"{_MB_API}/recording/{recording_id}",
            params={"fmt": "json", "inc": "releases+release-groups"},
            headers=_MB_HEADERS,
            timeout=10,
        )
        r.raise_for_status()
        releases = r.json().get("releases", [])
        if not releases:
            return "", ""

        # Prefer official studio albums; fall back to first release
        for rel in releases:
            status = rel.get("status", "")
            rtype = rel.get("release-group", {}).get("primary-type", "")
            if status == "Official" and rtype == "Album":
                album = rel.get("title", "")
                mbid = rel.get("id", "")
                if album:
                    log.info(
                        "resolved album from MB recording",
                        recording_id=recording_id,
                        album=album,
                        mb_albumid=mbid,
                    )
                    return album, mbid

        album = releases[0].get("title", "")
        mbid = releases[0].get("id", "")
        if album:
            log.info(
                "resolved album (fallback) from MB recording",
                recording_id=recording_id,
                album=album,
                mb_albumid=mbid,
            )
        return album, mbid

    except Exception as exc:
        log.warning("MB recording lookup failed", recording_id=recording_id, error=str(exc))
        return "", ""


def _pretag(path: Path, artist: str, track_name: str):
    """Write artist/title tags so beets can match against MusicBrainz."""
    try:
        suffix = path.suffix.lower()
        if suffix == ".flac":
            f = mutagen.flac.FLAC(path)
            f["artist"] = artist
            f["title"] = track_name
            f.save()
        elif suffix in (".opus", ".ogg"):
            f = mutagen.oggopus.OggOpus(path)
            f["artist"] = [artist]
            f["title"] = [track_name]
            f.save()
        elif suffix in (".m4a", ".mp4"):
            f = mutagen.mp4.MP4(path)
            f["\xa9ART"] = [artist]
            f["\xa9nam"] = [track_name]
            f.save()
        else:
            f = mutagen.File(path)
            if f is not None:
                f["artist"] = artist
                f["title"] = track_name
                f.save()
        log.debug("pre-tagged file", file=str(path), artist=artist, title=track_name)
    except Exception as exc:
        log.warning("pre-tag failed (continuing anyway)", file=str(path), error=str(exc))


def _import_log_size() -> int:
    try:
        return os.path.getsize(_IMPORT_LOG)
    except OSError:
        return 0


def _import_log_tail(offset: int) -> str:
    try:
        with open(_IMPORT_LOG, "r", errors="replace") as f:
            f.seek(offset)
            return f.read()
    except OSError:
        return ""


def _beet(*args: str, timeout: int = 60) -> tuple[bool, str]:
    """Run a beet subcommand. Returns (success, combined_output)."""
    result = subprocess.run(
        ["beet"] + list(args),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    output = result.stdout + result.stderr
    if result.returncode != 0:
        log.warning("beet subcommand failed", args=args, stderr=result.stderr.strip())
    return result.returncode == 0, output


def _embed_cover_art(file_path: str, mb_albumid: str):
    """Download front cover from Cover Art Archive and embed into audio file."""
    art_url = f"https://coverartarchive.org/release/{mb_albumid}/front"
    try:
        r = requests.get(art_url, timeout=15, allow_redirects=True)
        if r.status_code != 200:
            log.warning("cover art not found", mb_albumid=mb_albumid, status=r.status_code)
            return
        image_data = r.content
        content_type = r.headers.get("Content-Type", "image/jpeg")
        log.info("embedding cover art", file=file_path, size=len(image_data))

        suffix = Path(file_path).suffix.lower()
        if suffix == ".flac":
            f = mutagen.flac.FLAC(file_path)
            pic = mutagen.flac.Picture()
            pic.type = 3  # front cover
            pic.mime = content_type
            pic.data = image_data
            f.clear_pictures()
            f.add_picture(pic)
            f.save()
        elif suffix in (".opus", ".ogg"):
            f = mutagen.oggopus.OggOpus(file_path)
            import base64

            pic = mutagen.flac.Picture()
            pic.type = 3
            pic.mime = content_type
            pic.data = image_data
            f["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
            f.save()
        elif suffix in (".m4a", ".mp4"):
            f = mutagen.mp4.MP4(file_path)
            fmt = mutagen.mp4.MP4Cover.FORMAT_JPEG
            if "png" in content_type:
                fmt = mutagen.mp4.MP4Cover.FORMAT_PNG
            f["covr"] = [mutagen.mp4.MP4Cover(image_data, imageformat=fmt)]
            f.save()
        else:
            log.warning("unsupported format for art embedding", file=file_path)
            return

        log.info("cover art embedded", file=file_path)
    except Exception as exc:
        log.warning("cover art embedding failed", file=file_path, error=str(exc))


def _find_imported_file(music_dir: str, artist: str, track_name: str) -> str:
    """Find the FLAC/Opus file beets imported for this track."""
    ok, output = _beet("list", "-f", "$path", f"artist:{artist}", f"title:{track_name}")
    if ok and output.strip():
        return output.strip().split("\n")[0]
    return ""


def _enrich_track(artist: str, track_name: str, music_dir: str):
    """After singleton import: resolve album from MB and fetch art."""
    # Find the imported file to get the mb_trackid beets wrote
    imported_path = _find_imported_file(music_dir, artist, track_name)
    if not imported_path:
        log.warning(
            "could not find imported track for enrichment", artist=artist, track=track_name
        )
        return

    try:
        mf = mediafile.MediaFile(imported_path)
        mb_trackid = mf.mb_trackid
        has_album = bool(mf.album)
        has_art = bool(mf.images)
    except Exception as exc:
        log.warning("could not read imported file metadata", file=imported_path, error=str(exc))
        return

    # 이미 앨범과 아트가 모두 있으면 enrichment 불필요
    if has_album and has_art:
        log.info("track already enriched, skipping", artist=artist, track=track_name)
        return

    # Resolve album using the exact MusicBrainz recording ID
    album, mb_albumid = "", ""
    if mb_trackid:
        album, mb_albumid = _mb_album_from_recording_id(mb_trackid)

    if album and not has_album:
        log.info("setting album tag via beet modify", artist=artist, track=track_name, album=album)
        # mb_albumid는 설정하지 않음: 트랙마다 다른 release를 매칭하면 Navidrome이 같은 앨범을 2개로 쪼갬
        _beet("modify", "-y", f"artist:{artist}", f"title:{track_name}", f"album={album}")

    # Fetch and embed album art directly from Cover Art Archive
    if mb_albumid and not has_art:
        _embed_cover_art(imported_path, mb_albumid)


def tag_and_import(
    staging_file: str,
    music_dir: str,
    artist: str = "",
    track_name: str = "",
) -> bool:
    path = Path(staging_file)
    if not path.exists():
        log.error("staging file not found", file=staging_file)
        return False

    if artist and track_name:
        _pretag(path, artist, track_name)

    log.info("running beets import", file=staging_file)

    try:
        # Lock: beet import를 직렬화해 import log 오염 방지
        with _beet_lock:
            log_offset = _import_log_size()
            ok, _ = _beet("import", "-q", "-s", str(path), timeout=120)
            new_log = _import_log_tail(log_offset)

        if not ok:
            log.error("beets import command failed", file=staging_file)
            return False

        already_in_library = new_log and "duplicate-skip" in new_log.lower()
        skipped = new_log and "skip" in new_log.lower() and not already_in_library

        if skipped:
            log.error(
                "beets skipped the file (no match found)",
                file=staging_file,
                beets_log=new_log.strip(),
            )
            return False

        if already_in_library:
            log.info("track already in library", file=staging_file)
        else:
            log.info("beets import succeeded", file=staging_file)

        # enrichment: 신규 임포트든 duplicate든 앨범/아트가 없으면 실행
        if artist and track_name:
            _enrich_track(artist, track_name, music_dir)

        _cleanup_staging(path)
        return True

    except subprocess.TimeoutExpired:
        log.error("beets import timed out", file=staging_file)
        return False
    except FileNotFoundError:
        log.error("beet command not found — is beets installed?")
        return False


def _cleanup_staging(path: Path):
    try:
        path.unlink()
        log.debug("staging file removed", file=str(path))
    except OSError as exc:
        log.warning("could not remove staging file", error=str(exc))
