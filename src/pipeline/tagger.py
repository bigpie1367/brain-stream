import difflib
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


def _mb_search_recording(artist: str, track_name: str) -> str:
    """Search MusicBrainz for a recording by artist and title.

    Uses artistname: field (includes aliases) instead of artist: (canonical only).
    Falls back to recording-only search if artistname+recording returns 0 results.
    Returns the first matched recording ID (UUID), or empty string on failure.
    """
    try:
        time.sleep(1)  # rate limit
        query = f"artistname:{artist} AND recording:{track_name}"
        r = requests.get(
            f"{_MB_API}/recording",
            params={"query": query, "fmt": "json", "limit": 5},
            headers=_MB_HEADERS,
            timeout=10,
        )
        r.raise_for_status()
        recordings = r.json().get("recordings", [])
        if recordings:
            recording_id = recordings[0].get("id", "")
            if recording_id:
                log.info(
                    "MB search found recording",
                    artist=artist,
                    track=track_name,
                    recording_id=recording_id,
                )
            return recording_id

        # Fallback: recording-only search (no artist filter) — pick best artist match
        log.info(
            "MB artistname+recording search returned 0 results, trying recording-only fallback",
            artist=artist,
            track=track_name,
        )
        time.sleep(1)  # rate limit
        r2 = requests.get(
            f"{_MB_API}/recording",
            params={"query": f"recording:{track_name}", "fmt": "json", "limit": 5},
            headers=_MB_HEADERS,
            timeout=10,
        )
        r2.raise_for_status()
        recordings2 = r2.json().get("recordings", [])
        if recordings2:
            norm_artist = _normalize_for_match(artist)
            best_id = ""
            best_ratio = 0.0
            for rec in recordings2:
                credits = rec.get("artist-credit", [])
                for credit in credits:
                    credit_artist = credit.get("artist", {}) if isinstance(credit, dict) else {}
                    candidate_name = credit_artist.get("name", "") or credit_artist.get("sort-name", "")
                    if not candidate_name:
                        continue
                    ratio = difflib.SequenceMatcher(
                        None, norm_artist, _normalize_for_match(candidate_name)
                    ).ratio()
                    if ratio > best_ratio:
                        best_ratio = ratio
                        best_id = rec.get("id", "")
            if best_ratio >= 0.3 and best_id:
                log.info(
                    "MB recording-only fallback found recording",
                    artist=artist,
                    track=track_name,
                    recording_id=best_id,
                    artist_similarity=round(best_ratio, 3),
                )
                return best_id
            log.info(
                "MB recording-only fallback: no result met artist similarity threshold (0.3)",
                artist=artist,
                track=track_name,
                best_ratio=round(best_ratio, 3),
            )
        return ""
    except Exception as exc:
        log.warning("MB recording search failed", artist=artist, track=track_name, error=str(exc))
        return ""


def _write_mb_trackid(file_path: str, mb_trackid: str):
    """Write MusicBrainz recording ID to file tags to avoid re-searching next run."""
    try:
        suffix = Path(file_path).suffix.lower()
        if suffix == ".flac":
            f = mutagen.flac.FLAC(file_path)
            f["musicbrainz_trackid"] = mb_trackid
            f.save()
        elif suffix in (".opus", ".ogg"):
            f = mutagen.oggopus.OggOpus(file_path)
            f["musicbrainz_trackid"] = [mb_trackid]
            f.save()
        elif suffix in (".m4a", ".mp4"):
            f = mutagen.mp4.MP4(file_path)
            f["----:com.apple.iTunes:MusicBrainz Track Id"] = [
                mutagen.mp4.MP4FreeForm(mb_trackid.encode())
            ]
            f.save()
        else:
            f = mutagen.File(file_path)
            if f is not None:
                f["musicbrainz_trackid"] = mb_trackid
                f.save()
        log.debug("wrote mb_trackid to file", file=file_path, mb_trackid=mb_trackid)
    except Exception as exc:
        log.warning("could not write mb_trackid to file", file=file_path, error=str(exc))


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

        def _release_date_key(rel):
            d = rel.get("date", "") or ""
            return d if d else "9999"

        # Collect Official Album releases for CAA fallback
        official_album_releases = []
        for rel in releases:
            status = rel.get("status", "")
            rtype = rel.get("release-group", {}).get("primary-type", "")
            if status == "Official" and rtype == "Album":
                mbid = rel.get("id", "")
                if mbid:
                    official_album_releases.append(rel)

        if official_album_releases:
            official_album_releases.sort(key=_release_date_key)
            top = official_album_releases[:3]
            album = top[0].get("title", "")
            candidates = [rel.get("id", "") for rel in top if rel.get("id")]
            if album:
                log.info(
                    "resolved album from MB recording",
                    recording_id=recording_id,
                    album=album,
                    mb_albumid_candidates=candidates,
                )
            return album, candidates

        # Fallback: use releases sorted by date, pick earliest
        releases_with_id = [rel for rel in releases if rel.get("id")]
        if not releases_with_id:
            return "", []
        releases_with_id.sort(key=_release_date_key)
        album = releases_with_id[0].get("title", "")
        mbid = releases_with_id[0].get("id", "")
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


def _itunes_search(artist: str, track_name: str) -> dict:
    """Search iTunes Search API for album name and cover art URL.

    Returns {"album": str, "artwork_url": str} or empty dict on failure.
    No API key required. No rate limit documented.
    Validates artistName similarity (>= 0.4) before accepting a result.
    """
    try:
        term = f"{artist} {track_name}"
        r = requests.get(
            "https://itunes.apple.com/search",
            params={"term": term, "entity": "song", "limit": 5},
            timeout=10,
        )
        r.raise_for_status()
        results = r.json().get("results", [])
        if not results:
            return {}
        norm_artist = _normalize_for_match(artist)
        item = None
        for candidate in results:
            candidate_artist = candidate.get("artistName", "")
            ratio = difflib.SequenceMatcher(
                None, norm_artist, _normalize_for_match(candidate_artist)
            ).ratio()
            if ratio >= 0.4:
                item = candidate
                break
        if item is None:
            log.info(
                "iTunes search: no result met artist similarity threshold (0.4)",
                artist=artist,
                track=track_name,
            )
            return {}
        album = item.get("collectionName", "")
        artwork_url = item.get("artworkUrl100", "")
        if artwork_url:
            # Upgrade to 600x600 for better quality
            artwork_url = artwork_url.replace("100x100bb", "600x600bb")
        log.info("iTunes search result", artist=artist, track=track_name, album=album, artwork_url=artwork_url)
        return {"album": album, "artwork_url": artwork_url}
    except Exception as exc:
        log.warning("iTunes search failed", artist=artist, track=track_name, error=str(exc))
        return {}


def _deezer_search(artist: str, track_name: str) -> dict:
    """Search Deezer API for album name and cover art URL.

    Returns {"album": str, "artwork_url": str} or empty dict on failure.
    No API key required.
    Validates artist.name similarity (>= 0.4) before accepting a result.
    """
    try:
        q = f'artist:"{artist}" track:"{track_name}"'
        r = requests.get(
            "https://api.deezer.com/search",
            params={"q": q, "limit": 5},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json().get("data", [])
        if not data:
            return {}
        norm_artist = _normalize_for_match(artist)
        item = None
        for candidate in data:
            candidate_artist = candidate.get("artist", {}).get("name", "")
            ratio = difflib.SequenceMatcher(
                None, norm_artist, _normalize_for_match(candidate_artist)
            ).ratio()
            if ratio >= 0.4:
                item = candidate
                break
        if item is None:
            log.info(
                "Deezer search: no result met artist similarity threshold (0.4)",
                artist=artist,
                track=track_name,
            )
            return {}
        album_obj = item.get("album", {})
        album = album_obj.get("title", "")
        artwork_url = album_obj.get("cover_xl", "")
        log.info("Deezer search result", artist=artist, track=track_name, album=album, artwork_url=artwork_url)
        return {"album": album, "artwork_url": artwork_url}
    except Exception as exc:
        log.warning("Deezer search failed", artist=artist, track=track_name, error=str(exc))
        return {}


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


def _normalize_for_match(s: str) -> str:
    """Lowercase and strip non-alphanumeric characters for fuzzy comparison."""
    return "".join(c for c in s.lower() if c.isalnum() or c.isspace()).strip()


def _find_imported_files(artist: str, track_name: str) -> list[str]:
    """Find all FLAC/Opus files beets imported for this artist.

    Searches by artist only to avoid title mismatch (e.g. user input "butterfly"
    vs beets-stored "Butter-Fly"). Selects the best match by track_name similarity,
    falling back to the last item (most recently added) if no similarity found.
    """
    ok, output = _beet("list", "-f", "$path", f"artist:{artist}")
    if not ok or not output.strip():
        return []

    all_paths = [p for p in output.strip().split("\n") if p.strip()]
    if not all_paths:
        return []

    if not track_name:
        return [all_paths[-1]]

    # Select path whose filename best matches track_name (case-insensitive, no special chars)
    norm_track = _normalize_for_match(track_name)
    best_path = None
    for p in all_paths:
        norm_filename = _normalize_for_match(Path(p).stem)
        if norm_track in norm_filename or norm_filename in norm_track:
            best_path = p
            break

    if best_path is None:
        # No similarity match: use the last item (most recently added)
        best_path = all_paths[-1]

    return [best_path]


def _find_imported_file_by_path(staging_path: str, artist: str = "") -> str:
    """Find the most recently imported file for the given artist.

    The staging filename is not preserved in the beets library path, so
    path-stem lookup is unreliable. Instead, search by artist and return
    the last result (most recently added).
    Falls back to empty string if not found.
    """
    if not artist:
        return ""
    ok, output = _beet("list", "-f", "$path", f"artist:{artist}")
    if ok and output.strip():
        paths = [p for p in output.strip().split("\n") if p.strip()]
        if paths:
            return paths[-1]
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

    # Fallback: if artist/track empty or lookup returned nothing, search by artist only
    if not imported_paths:
        fallback = _find_imported_file_by_path(staging_path, artist=artist)
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
    # If beets import did not write mb_trackid (matching failed), search MB directly
    album = ""
    mb_albumid_candidates: list[str] = []
    if not mb_trackid and artist and track_name:
        log.info(
            "mb_trackid missing after beet import, searching MB by artist+title",
            artist=artist,
            track=track_name,
        )
        mb_trackid = _mb_search_recording(artist, track_name)
        if mb_trackid:
            _write_mb_trackid(primary_path, mb_trackid)

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

    # CAA에서 여러 release 후보를 순서대로 시도
    art_embedded = False
    successful_candidate = None
    if not has_art and mb_albumid_candidates:
        for candidate_id in mb_albumid_candidates:
            if _embed_cover_art(primary_path, candidate_id):
                art_embedded = True
                successful_candidate = candidate_id
                break

        # 나머지 매칭 파일에도 동일한 아트 임베딩 (성공한 candidate 기준)
        if art_embedded and successful_candidate and len(imported_paths) > 1:
            for extra_path in imported_paths[1:]:
                _embed_cover_art(extra_path, successful_candidate)

        if art_embedded:
            try:
                has_art = bool(mediafile.MediaFile(primary_path).images)
            except Exception:
                has_art = True  # 실패해도 폴백 시도 방지

    # iTunes/Deezer 결과 캐시 (앨범명·아트 양쪽에 재사용해 중복 API 호출 방지)
    itunes_result: dict = {}
    deezer_result: dict = {}

    # 앨범명 fallback: MB 실패 → iTunes → Deezer → YouTube channel
    if not album and not has_album and artist and track_name:
        itunes_result = _itunes_search(artist, track_name)
        album = itunes_result.get("album", "")
        if album:
            log.info("album resolved via iTunes", artist=artist, track=track_name, album=album)

        if not album:
            deezer_result = _deezer_search(artist, track_name)
            album = deezer_result.get("album", "")
            if album:
                log.info("album resolved via Deezer", artist=artist, track=track_name, album=album)

        if not album and yt_metadata:
            album = yt_metadata.get("channel", "")
            if album:
                log.info(
                    "MB album not found, falling back to YouTube channel as album",
                    artist=artist,
                    track=track_name,
                    channel=album,
                )

        if album:
            if artist and track_name:
                _beet("modify", "-y", f"artist:{artist}", f"title:{track_name}", f"album={album}")
            else:
                _beet("modify", "-y", f"path:{primary_path}", f"album={album}")

    # 커버아트 fallback: CAA 실패 → iTunes → Deezer → YouTube 썸네일
    if not has_art:
        # iTunes fallback
        if not art_embedded:
            if not itunes_result and artist and track_name:
                itunes_result = _itunes_search(artist, track_name)
            itunes_art = itunes_result.get("artwork_url", "")
            if itunes_art:
                log.info("embedding cover art from iTunes", artist=artist, track=track_name, url=itunes_art)
                _embed_art_from_url(primary_path, itunes_art)
                art_embedded = True
                for extra_path in imported_paths[1:]:
                    _embed_art_from_url(extra_path, itunes_art)

        # Deezer fallback
        if not art_embedded:
            if not deezer_result and artist and track_name:
                deezer_result = _deezer_search(artist, track_name)
            deezer_art = deezer_result.get("artwork_url", "")
            if deezer_art:
                log.info("embedding cover art from Deezer", artist=artist, track=track_name, url=deezer_art)
                _embed_art_from_url(primary_path, deezer_art)
                art_embedded = True
                for extra_path in imported_paths[1:]:
                    _embed_art_from_url(extra_path, deezer_art)

        # YouTube 썸네일 — 최후 수단
        if not art_embedded and yt_metadata:
            thumbnail_url = yt_metadata.get("thumbnail_url", "")
            if thumbnail_url:
                log.info(
                    "no cover art from CAA/iTunes/Deezer, falling back to YouTube thumbnail",
                    artist=artist,
                    track=track_name,
                    thumbnail_url=thumbnail_url,
                )
                _embed_art_from_url(primary_path, thumbnail_url)
                for extra_path in imported_paths[1:]:
                    _embed_art_from_url(extra_path, thumbnail_url)


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
