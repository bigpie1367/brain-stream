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


def _mb_album_from_recording_id(recording_id: str) -> tuple[str, list[str]]:
    """Get (album_title, mb_albumid_candidates) from a MusicBrainz recording ID.

    Returns up to 3 Official Album release IDs to try for Cover Art Archive.
    Falls back to the first release if no Official Album found.
    """
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
            return "", []

        # Collect Official Album releases (up to 3) for CAA fallback
        official_album_releases = []
        for rel in releases:
            status = rel.get("status", "")
            rtype = rel.get("release-group", {}).get("primary-type", "")
            if status == "Official" and rtype == "Album":
                mbid = rel.get("id", "")
                if mbid:
                    official_album_releases.append(rel)
                if len(official_album_releases) >= 3:
                    break

        if official_album_releases:
            album = official_album_releases[0].get("title", "")
            candidates = [rel.get("id", "") for rel in official_album_releases if rel.get("id")]
            if album:
                log.info(
                    "resolved album from MB recording",
                    recording_id=recording_id,
                    album=album,
                    mb_albumid_candidates=candidates,
                )
            return album, candidates

        # Fallback: use the first release regardless of type
        album = releases[0].get("title", "")
        mbid = releases[0].get("id", "")
        candidates = [mbid] if mbid else []
        if album:
            log.info(
                "resolved album (fallback) from MB recording",
                recording_id=recording_id,
                album=album,
                mb_albumid_candidates=candidates,
            )
        return album, candidates

    except Exception as exc:
        log.warning("MB recording lookup failed", recording_id=recording_id, error=str(exc))
        return "", []


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


def _embed_cover_art(file_path: str, mb_albumid: str) -> bool:
    """Download front cover from Cover Art Archive and embed into audio file.

    Returns True on success, False on failure (404, network error, etc.).
    """
    art_url = f"https://coverartarchive.org/release/{mb_albumid}/front"
    try:
        r = requests.get(art_url, timeout=15, allow_redirects=True)
        if r.status_code != 200:
            log.warning("cover art not found", mb_albumid=mb_albumid, status=r.status_code)
            return False
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
            return False

        log.info("cover art embedded", file=file_path)
        return True
    except Exception as exc:
        log.warning("cover art embedding failed", file=file_path, error=str(exc))
        return False


def _find_imported_files(artist: str, track_name: str) -> list[str]:
    """Find all FLAC/Opus files beets imported for this track (artist+title query)."""
    ok, output = _beet("list", "-f", "$path", f"artist:{artist}", f"title:{track_name}")
    if ok and output.strip():
        return [p for p in output.strip().split("\n") if p.strip()]
    return []


def _find_imported_file_by_path(staging_path: str) -> str:
    """Find the imported file using staging file path as a hint.

    Uses beet list with a path: query derived from the filename stem,
    falling back to empty string if not found.
    This avoids relying on artist/track strings which may be empty.
    """
    stem = Path(staging_path).stem
    ok, output = _beet("list", "-f", "$path", f"path:{stem}")
    if ok and output.strip():
        paths = [p for p in output.strip().split("\n") if p.strip()]
        if paths:
            return paths[0]
    return ""


def _embed_art_from_url(file_path: str, url: str):
    """Download image from URL and embed into audio file."""
    try:
        r = requests.get(url, timeout=15, allow_redirects=True)
        if r.status_code != 200:
            log.warning("thumbnail download failed", url=url, status=r.status_code)
            return
        image_data = r.content
        content_type = r.headers.get("Content-Type", "image/jpeg")
        log.info("embedding thumbnail as cover art", file=file_path, size=len(image_data))

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
            import base64
            f = mutagen.oggopus.OggOpus(file_path)
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

        log.info("thumbnail embedded as cover art", file=file_path)
    except Exception as exc:
        log.warning("thumbnail embedding failed", file=file_path, error=str(exc))


def _enrich_track(
    staging_path: str,
    music_dir: str,
    artist: str = "",
    track_name: str = "",
    yt_metadata: dict | None = None,
):
    """After singleton import: resolve album from MB and fetch art.

    Uses artist/track to find imported files when available; falls back to
    staging file path stem query so enrichment runs even when artist/track
    are empty strings (e.g. some LB pipeline entries).
    """
    # Find all imported files matching this track (Bug 2: handle multiple paths)
    imported_paths: list[str] = []
    if artist and track_name:
        imported_paths = _find_imported_files(artist, track_name)

    # Bug 3 fallback: if artist/track empty or lookup returned nothing, use path stem
    if not imported_paths:
        fallback = _find_imported_file_by_path(staging_path)
        if fallback:
            imported_paths = [fallback]

    if not imported_paths:
        log.warning(
            "could not find imported track for enrichment",
            artist=artist,
            track=track_name,
            staging_path=staging_path,
        )
        return

    # Read metadata from the first matched file to get mb_trackid and current state
    primary_path = imported_paths[0]
    try:
        mf = mediafile.MediaFile(primary_path)
        mb_trackid = mf.mb_trackid
        has_album = bool(mf.album)
        has_art = bool(mf.images)
    except Exception as exc:
        log.warning("could not read imported file metadata", file=primary_path, error=str(exc))
        return

    # 이미 앨범과 아트가 모두 있으면 enrichment 불필요
    if has_album and has_art:
        log.info("track already enriched, skipping", artist=artist, track=track_name)
        return

    # Resolve album using the exact MusicBrainz recording ID
    album = ""
    mb_albumid_candidates: list[str] = []
    if mb_trackid:
        album, mb_albumid_candidates = _mb_album_from_recording_id(mb_trackid)

    if album and not has_album:
        log.info("setting album tag via beet modify", artist=artist, track=track_name, album=album)
        # mb_albumid는 설정하지 않음: 트랙마다 다른 release를 매칭하면 Navidrome이 같은 앨범을 2개로 쪼갬
        # Bug 3: artist/track이 비어있을 때도 동작하도록 path: 조건으로 파일 특정
        if artist and track_name:
            _beet("modify", "-y", f"artist:{artist}", f"title:{track_name}", f"album={album}")
        else:
            _beet("modify", "-y", f"path:{primary_path}", f"album={album}")

    # Bug 1: CAA에서 여러 release 후보를 순서대로 시도
    art_embedded = False
    successful_candidate = None
    if not has_art and mb_albumid_candidates:
        for candidate_id in mb_albumid_candidates:
            if _embed_cover_art(primary_path, candidate_id):
                art_embedded = True
                successful_candidate = candidate_id
                break

        # Bug 2: 나머지 매칭 파일에도 동일한 아트 임베딩 (성공한 candidate 기준)
        if art_embedded and successful_candidate and len(imported_paths) > 1:
            for extra_path in imported_paths[1:]:
                _embed_cover_art(extra_path, successful_candidate)

        # 아트 임베딩 후 has_art 갱신 (YouTube 폴백 진입 여부 결정용)
        if art_embedded:
            try:
                has_art = bool(mediafile.MediaFile(primary_path).images)
            except Exception:
                has_art = True  # 실패해도 폴백 시도 방지

    # MB에서 앨범 정보를 찾지 못한 경우 YouTube 메타데이터로 폴백
    # Bug 1: CAA 전부 실패 시 has_art가 False인 채로 이 분기에 진입 가능
    if yt_metadata:
        try:
            if not album and not has_album:
                channel = yt_metadata.get("channel", "")
                if channel:
                    log.info(
                        "MB album not found, falling back to YouTube channel as album",
                        artist=artist,
                        track=track_name,
                        channel=channel,
                    )
                    if artist and track_name:
                        _beet("modify", "-y", f"artist:{artist}", f"title:{track_name}", f"album={channel}")
                    else:
                        _beet("modify", "-y", f"path:{primary_path}", f"album={channel}")

            if not has_art:
                thumbnail_url = yt_metadata.get("thumbnail_url", "")
                if thumbnail_url:
                    log.info(
                        "no cover art, falling back to YouTube thumbnail",
                        artist=artist,
                        track=track_name,
                        thumbnail_url=thumbnail_url,
                    )
                    _embed_art_from_url(primary_path, thumbnail_url)
                    # Bug 2: 나머지 파일에도 YouTube 썸네일 임베딩
                    for extra_path in imported_paths[1:]:
                        _embed_art_from_url(extra_path, thumbnail_url)
        except Exception as exc:
            log.warning("YouTube metadata fallback failed", artist=artist, track=track_name, error=str(exc))


def tag_and_import(
    staging_file: str,
    music_dir: str,
    artist: str = "",
    track_name: str = "",
    yt_metadata: dict | None = None,
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

        # Bug 3: artist/track 유무와 관계없이 항상 enrichment 실행
        _enrich_track(staging_file, music_dir, artist=artist, track_name=track_name, yt_metadata=yt_metadata)

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


def beet_remove_track(mbid: str, artist: str = "", track_name: str = "") -> list[str]:
    """Remove track(s) from beets library and disk. Returns list of removed file paths.

    Query order:
    1. mb_trackid:{mbid}  — works for real MB UUIDs (LB tracks)
    2. artist:"{artist}" title:"{track_name}"  — fallback for manual downloads
    """
    removed: list[str] = []

    # Collect candidate paths
    paths: list[str] = []

    # Try mb_trackid first (works for LB tracks with real MB UUIDs)
    ok, output = _beet("list", "-f", "$path", f"mb_trackid:{mbid}")
    if ok and output.strip():
        paths = [p for p in output.strip().split("\n") if p.strip()]

    # Fallback: artist + title query
    if not paths and artist and track_name:
        ok, output = _beet("list", "-f", "$path", f"artist:{artist}", f"title:{track_name}")
        if ok and output.strip():
            paths = [p for p in output.strip().split("\n") if p.strip()]

    # Filesystem fallback: beets DB가 비어있을 때 직접 탐색
    if not paths:
        log.info(
            "beet_remove_track: beet query returned nothing, falling back to filesystem scan",
            mbid=mbid,
            artist=artist,
            track=track_name,
        )
        music_root = "/app/data/music"
        track_lower = track_name.lower() if track_name else ""
        artist_lower = artist.lower() if artist else ""
        for dirpath, _dirnames, filenames in os.walk(music_root):
            for fname in filenames:
                if not fname.lower().endswith((".flac", ".opus")):
                    continue
                if track_lower and track_lower not in fname.lower():
                    continue
                full_path = os.path.join(dirpath, fname)
                if artist_lower and artist_lower not in full_path.lower():
                    continue
                paths.append(full_path)

    if not paths:
        log.info("beet_remove_track: no files found (beet query + filesystem)", mbid=mbid, artist=artist, track=track_name)
        return removed

    for file_path in paths:
        ok, out = _beet("remove", "-d", "-f", f"path:{file_path}")
        if ok:
            log.info("beet remove succeeded", file=file_path)
            removed.append(file_path)
        else:
            log.warning("beet remove failed, attempting direct os.remove", file=file_path, output=out)
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    log.info("direct os.remove succeeded", file=file_path)
                    removed.append(file_path)
                except OSError as exc:
                    log.warning("direct os.remove failed", file=file_path, error=str(exc))

    return removed
