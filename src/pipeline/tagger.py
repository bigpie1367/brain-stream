import difflib
import os
import re
import shutil
import time
from pathlib import Path

import mutagen
import mutagen.flac
import mutagen.mp4
import mutagen.oggopus
import requests

from src.utils.logger import get_logger

log = get_logger(__name__)

_MB_API = "https://musicbrainz.org/ws/2"
_MB_HEADERS = {"User-Agent": "music-bot/1.0 (https://github.com/music-bot)"}


def _sanitize_filename(name: str) -> str:
    """Remove filesystem-unsafe characters and limit length to 255."""
    sanitized = re.sub(r'[/\\:*?"<>|\x00-\x1f]', "_", name)
    sanitized = sanitized.strip(". ")
    if not sanitized:
        sanitized = "_"
    return sanitized[:255]


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


def _write_tags(file_path: str, artist: str, track_name: str, mb_trackid: str = ""):
    """Write artist, title, and optionally mb_trackid tags to audio file."""
    try:
        suffix = Path(file_path).suffix.lower()
        if suffix == ".flac":
            f = mutagen.flac.FLAC(file_path)
            f["artist"] = artist
            f["title"] = track_name
            if mb_trackid:
                f["musicbrainz_trackid"] = mb_trackid
            f.save()
        elif suffix in (".opus", ".ogg"):
            f = mutagen.oggopus.OggOpus(file_path)
            f["artist"] = [artist]
            f["title"] = [track_name]
            if mb_trackid:
                f["musicbrainz_trackid"] = [mb_trackid]
            f.save()
        elif suffix in (".m4a", ".mp4"):
            f = mutagen.mp4.MP4(file_path)
            f["\xa9ART"] = [artist]
            f["\xa9nam"] = [track_name]
            if mb_trackid:
                f["----:com.apple.iTunes:MusicBrainz Track Id"] = [
                    mutagen.mp4.MP4FreeForm(mb_trackid.encode())
                ]
            f.save()
        else:
            f = mutagen.File(file_path)
            if f is not None:
                f["artist"] = artist
                f["title"] = track_name
                if mb_trackid:
                    f["musicbrainz_trackid"] = mb_trackid
                f.save()
        log.debug("wrote tags to file", file=file_path, artist=artist, title=track_name)
    except Exception as exc:
        log.warning("could not write tags to file", file=file_path, error=str(exc))


def _write_album_tag(file_path: str, album: str):
    """Write album tag to audio file using mutagen."""
    try:
        suffix = Path(file_path).suffix.lower()
        if suffix == ".flac":
            f = mutagen.flac.FLAC(file_path)
            f["album"] = album
            f.save()
        elif suffix in (".opus", ".ogg"):
            f = mutagen.oggopus.OggOpus(file_path)
            f["album"] = [album]
            f.save()
        elif suffix in (".m4a", ".mp4"):
            f = mutagen.mp4.MP4(file_path)
            f["\xa9alb"] = [album]
            f.save()
        else:
            f = mutagen.File(file_path)
            if f is not None:
                f["album"] = album
                f.save()
        log.debug("wrote album tag", file=file_path, album=album)
    except Exception as exc:
        log.warning("could not write album tag", file=file_path, error=str(exc))


def _read_tags(file_path: str) -> dict:
    """Read artist, title, album, mb_trackid from audio file tags.

    Returns dict with keys: artist, title, album, mb_trackid, has_art.
    """
    result = {"artist": "", "title": "", "album": "", "mb_trackid": "", "has_art": False}
    try:
        suffix = Path(file_path).suffix.lower()
        if suffix == ".flac":
            f = mutagen.flac.FLAC(file_path)
            result["artist"] = (f.get("artist") or [""])[0]
            result["title"] = (f.get("title") or [""])[0]
            result["album"] = (f.get("album") or [""])[0]
            result["mb_trackid"] = (f.get("musicbrainz_trackid") or [""])[0]
            result["has_art"] = bool(f.pictures)
        elif suffix in (".opus", ".ogg"):
            f = mutagen.oggopus.OggOpus(file_path)
            result["artist"] = (f.get("artist") or [""])[0]
            result["title"] = (f.get("title") or [""])[0]
            result["album"] = (f.get("album") or [""])[0]
            result["mb_trackid"] = (f.get("musicbrainz_trackid") or [""])[0]
            result["has_art"] = bool(f.get("metadata_block_picture"))
        elif suffix in (".m4a", ".mp4"):
            f = mutagen.mp4.MP4(file_path)
            result["artist"] = (f.get("\xa9ART") or [""])[0]
            result["title"] = (f.get("\xa9nam") or [""])[0]
            result["album"] = (f.get("\xa9alb") or [""])[0]
            raw_mb = f.get("----:com.apple.iTunes:MusicBrainz Track Id")
            if raw_mb:
                result["mb_trackid"] = bytes(raw_mb[0]).decode("utf-8", errors="replace")
            result["has_art"] = bool(f.get("covr"))
        else:
            f = mutagen.File(file_path)
            if f is not None:
                result["artist"] = str((f.get("artist") or [""])[0])
                result["title"] = str((f.get("title") or [""])[0])
                result["album"] = str((f.get("album") or [""])[0])
                result["mb_trackid"] = str((f.get("musicbrainz_trackid") or [""])[0])
    except Exception as exc:
        log.warning("could not read tags from file", file=file_path, error=str(exc))
    return result


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
    """Write artist/title tags before enrichment."""
    _write_tags(str(path), artist, track_name)


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
            return False

        log.info("cover art embedded", file=file_path)
        return True
    except Exception as exc:
        log.warning("cover art embedding failed", file=file_path, error=str(exc))
        return False


def _normalize_for_match(s: str) -> str:
    """Lowercase and strip non-alphanumeric characters for fuzzy comparison."""
    return "".join(c for c in s.lower() if c.isalnum() or c.isspace()).strip()


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
    dest_path: str,
    artist: str = "",
    track_name: str = "",
    yt_metadata: dict | None = None,
):
    """Resolve album from MB and fetch cover art. Writes tags directly via mutagen."""
    tags = _read_tags(dest_path)
    mb_trackid = tags.get("mb_trackid", "")
    has_album = bool(tags.get("album", ""))
    has_art = tags.get("has_art", False)

    if has_album and has_art:
        log.info("track already enriched, skipping", artist=artist, track=track_name)
        return

    # If mb_trackid not yet in file, search MB directly
    if not mb_trackid and artist and track_name:
        log.info(
            "mb_trackid missing, searching MB by artist+title",
            artist=artist,
            track=track_name,
        )
        mb_trackid = _mb_search_recording(artist, track_name)
        if mb_trackid:
            _write_tags(dest_path, artist, track_name, mb_trackid)

    album = ""
    mb_albumid_candidates: list[str] = []
    if mb_trackid:
        album, mb_albumid_candidates = _mb_album_from_recording_id(mb_trackid)

    if album and not has_album:
        log.info("setting album tag", artist=artist, track=track_name, album=album)
        _write_album_tag(dest_path, album)

    # CAA: try multiple release candidates in order
    art_embedded = False
    successful_candidate = None
    if not has_art and mb_albumid_candidates:
        for candidate_id in mb_albumid_candidates:
            if _embed_cover_art(dest_path, candidate_id):
                art_embedded = True
                successful_candidate = candidate_id
                break

        if art_embedded:
            # Re-read to confirm
            has_art = _read_tags(dest_path).get("has_art", True)

    # iTunes/Deezer result cache (avoid duplicate API calls)
    itunes_result: dict = {}
    deezer_result: dict = {}

    # Album fallback: MB failed → iTunes → Deezer → YouTube channel
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
            _write_album_tag(dest_path, album)

    # Cover art fallback: CAA failed → iTunes → Deezer → YouTube thumbnail
    if not has_art:
        # iTunes fallback
        if not art_embedded:
            if not itunes_result and artist and track_name:
                itunes_result = _itunes_search(artist, track_name)
            itunes_art = itunes_result.get("artwork_url", "")
            if itunes_art:
                log.info("embedding cover art from iTunes", artist=artist, track=track_name, url=itunes_art)
                _embed_art_from_url(dest_path, itunes_art)
                art_embedded = True

        # Deezer fallback
        if not art_embedded:
            if not deezer_result and artist and track_name:
                deezer_result = _deezer_search(artist, track_name)
            deezer_art = deezer_result.get("artwork_url", "")
            if deezer_art:
                log.info("embedding cover art from Deezer", artist=artist, track=track_name, url=deezer_art)
                _embed_art_from_url(dest_path, deezer_art)
                art_embedded = True

        # YouTube thumbnail — last resort
        if not art_embedded and yt_metadata:
            thumbnail_url = yt_metadata.get("thumbnail_url", "")
            if thumbnail_url:
                log.info(
                    "no cover art from CAA/iTunes/Deezer, falling back to YouTube thumbnail",
                    artist=artist,
                    track=track_name,
                    thumbnail_url=thumbnail_url,
                )
                _embed_art_from_url(dest_path, thumbnail_url)


def tag_and_import(
    staging_file: str,
    music_dir: str,
    artist: str = "",
    track_name: str = "",
    yt_metadata: dict | None = None,
) -> tuple[bool, str]:
    """Tag staging file, copy to music_dir, enrich with MB metadata and cover art.

    Returns (success, dest_path). dest_path is empty string on failure.
    """
    path = Path(staging_file)
    if not path.exists():
        log.error("staging file not found", file=staging_file)
        return False, ""

    # Search MB for recording ID
    recording_id = ""
    if artist and track_name:
        recording_id = _mb_search_recording(artist, track_name)
        if not recording_id:
            log.error(
                "MB recording not found, aborting import",
                artist=artist,
                track=track_name,
            )
            _cleanup_staging(path)
            return False, ""

    # Build destination path: music_dir/{artist}/{Unknown Album}/{track}.ext
    sanitized_artist = _sanitize_filename(artist) if artist else "Unknown Artist"
    sanitized_track = _sanitize_filename(track_name) if track_name else _sanitize_filename(path.stem)
    dest_dir = Path(music_dir) / sanitized_artist / "Unknown Album"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / (sanitized_track + path.suffix)

    # If file already exists at dest, treat as already in library
    if dest_path.exists():
        log.info("file already exists in music_dir, treating as duplicate", dest=str(dest_path))
        _cleanup_staging(path)
        _enrich_track(str(dest_path), artist=artist, track_name=track_name, yt_metadata=yt_metadata)
        return True, str(dest_path)

    # Copy staging file to destination
    try:
        shutil.copy2(str(path), str(dest_path))
        log.info("copied file to music_dir", src=staging_file, dest=str(dest_path))
    except OSError as exc:
        log.error("failed to copy file to music_dir", src=staging_file, dest=str(dest_path), error=str(exc))
        return False, ""

    # Write initial tags (artist, title, mb_trackid)
    _write_tags(str(dest_path), artist, track_name, recording_id)

    # Enrich: album from MB + cover art
    _enrich_track(str(dest_path), artist=artist, track_name=track_name, yt_metadata=yt_metadata)

    _cleanup_staging(path)
    return True, str(dest_path)


def _cleanup_staging(path: Path):
    try:
        path.unlink()
        log.debug("staging file removed", file=str(path))
    except OSError as exc:
        log.warning("could not remove staging file", error=str(exc))
