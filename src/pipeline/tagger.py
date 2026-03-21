import base64
import difflib
import re
import shutil
from pathlib import Path

import mutagen
import mutagen.flac
import mutagen.mp4
import mutagen.oggopus
import requests

from src.pipeline.musicbrainz import (
    lookup_recording,
    mb_album_from_recording_id,
    mb_search_recording,
)
from src.utils.fs import sanitize_path_component
from src.utils.logger import get_logger

log = get_logger(__name__)


# ── Format dispatch infrastructure ──────────────────────────────────────────


def _detect_format(path: str) -> str:
    """확장자 기반 포맷 감지."""
    suffix = Path(path).suffix.lower()
    if suffix == ".flac":
        return "flac"
    elif suffix in (".opus", ".ogg"):
        return "opus"
    elif suffix in (".m4a", ".mp4"):
        return "mp4"
    return "generic"


_FORMAT_OPENER = {
    "flac": mutagen.flac.FLAC,
    "opus": mutagen.oggopus.OggOpus,
    "mp4": mutagen.mp4.MP4,
    "generic": mutagen.File,
}

_FORMAT_KEYS = {
    "flac": {
        "artist": "artist",
        "title": "title",
        "album": "album",
        "mb_trackid": "musicbrainz_trackid",
    },
    "opus": {
        "artist": "artist",
        "title": "title",
        "album": "album",
        "mb_trackid": "musicbrainz_trackid",
    },
    "mp4": {
        "artist": "\xa9ART",
        "title": "\xa9nam",
        "album": "\xa9alb",
        "mb_trackid": "----:com.apple.iTunes:MusicBrainz Track Id",
    },
    "generic": {
        "artist": "artist",
        "title": "title",
        "album": "album",
        "mb_trackid": "musicbrainz_trackid",
    },
}


def _wrap_value(fmt: str, key_name: str, value: str):
    """포맷별 값 래핑."""
    if key_name == "mb_trackid" and fmt == "mp4":
        return [mutagen.mp4.MP4FreeForm(value.encode("utf-8"))]
    return [value]


def _embed_flac_art(f, image_data: bytes, content_type: str):
    pic = mutagen.flac.Picture()
    pic.type = 3
    pic.mime = content_type
    pic.data = image_data
    f.clear_pictures()
    f.add_picture(pic)


def _embed_opus_art(f, image_data: bytes, content_type: str):
    pic = mutagen.flac.Picture()
    pic.type = 3
    pic.mime = content_type
    pic.data = image_data
    f["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]


def _embed_mp4_art(f, image_data: bytes, content_type: str):
    fmt_tag = mutagen.mp4.MP4Cover.FORMAT_JPEG
    if "png" in content_type:
        fmt_tag = mutagen.mp4.MP4Cover.FORMAT_PNG
    f["covr"] = [mutagen.mp4.MP4Cover(image_data, imageformat=fmt_tag)]


_ART_EMBEDDER = {
    "flac": _embed_flac_art,
    "opus": _embed_opus_art,
    "mp4": _embed_mp4_art,
}


def _write_tags(file_path: str, artist: str, track_name: str, mb_trackid: str = ""):
    """Write artist, title, and optionally mb_trackid tags to audio file."""
    try:
        fmt = _detect_format(file_path)
        f = _FORMAT_OPENER[fmt](file_path)
        if f is None:
            log.warning("could not open file for tagging", file=file_path)
            return
        keys = _FORMAT_KEYS[fmt]
        f[keys["artist"]] = _wrap_value(fmt, "artist", artist)
        f[keys["title"]] = _wrap_value(fmt, "title", track_name)
        if mb_trackid:
            f[keys["mb_trackid"]] = _wrap_value(fmt, "mb_trackid", mb_trackid)
        f.save()
        log.debug("wrote tags to file", file=file_path, artist=artist, title=track_name)
    except Exception as exc:
        log.warning("could not write tags to file", file=file_path, error=str(exc))


def _write_single_tag(file_path: str, key_name: str, value: str):
    """Write a single tag to audio file."""
    try:
        fmt = _detect_format(file_path)
        f = _FORMAT_OPENER[fmt](file_path)
        if f is None:
            return
        f[_FORMAT_KEYS[fmt][key_name]] = _wrap_value(fmt, key_name, value)
        f.save()
        log.debug(f"wrote {key_name} tag", file=file_path, value=value)
    except Exception as exc:
        log.warning(f"could not write {key_name} tag", file=file_path, error=str(exc))


def write_mb_trackid_tag(file_path: str, recording_id: str):
    """Write MusicBrainz recording ID (mb_trackid) tag to audio file."""
    _write_single_tag(file_path, "mb_trackid", recording_id)


def write_album_tag(file_path: str, album: str):
    """Write album tag to audio file using mutagen."""
    _write_single_tag(file_path, "album", album)


def write_artist_tag(file_path: str, artist: str):
    """Write artist tag to audio file using mutagen."""
    _write_single_tag(file_path, "artist", artist)


def write_title_tag(file_path: str, title: str):
    """Write title tag to audio file using mutagen."""
    _write_single_tag(file_path, "title", title)


def _read_tags(file_path: str) -> dict:
    """Read artist, title, album, mb_trackid from audio file tags.

    Returns dict with keys: artist, title, album, mb_trackid, has_art.
    """
    result = {
        "artist": "",
        "title": "",
        "album": "",
        "mb_trackid": "",
        "has_art": False,
    }
    try:
        fmt = _detect_format(file_path)
        f = _FORMAT_OPENER[fmt](file_path)
        if f is None:
            return result
        keys = _FORMAT_KEYS[fmt]

        for tag in ("artist", "title", "album"):
            val = (f.get(keys[tag]) or [""])[0]
            result[tag] = str(val) if fmt == "generic" else val

        # mb_trackid: MP4 needs bytes decode
        raw_mb = f.get(keys["mb_trackid"])
        if raw_mb:
            if fmt == "mp4":
                result["mb_trackid"] = bytes(raw_mb[0]).decode(
                    "utf-8", errors="replace"
                )
            else:
                val = raw_mb if isinstance(raw_mb, list) else [raw_mb]
                result["mb_trackid"] = str(val[0]) if fmt == "generic" else str(val[0])

        # has_art detection
        if fmt == "flac":
            result["has_art"] = bool(f.pictures)
        elif fmt == "opus":
            result["has_art"] = bool(f.get("metadata_block_picture"))
        elif fmt == "mp4":
            result["has_art"] = bool(f.get("covr"))
    except Exception as exc:
        log.warning("could not read tags from file", file=file_path, error=str(exc))
    return result


_LIVE_TITLE_RE = re.compile(r"\d{4}-\d{2}-\d{2}[,:]")
_LIVE_TITLE_KEYWORDS = ("live", "concert", "festival", "bootleg", "unplugged")


def _is_live_title(title: str) -> bool:
    """Return True if the release title looks like a live event (date-prefixed or keyword match)."""
    if _LIVE_TITLE_RE.search(title):
        return True
    lower = title.lower()
    return any(kw in lower for kw in _LIVE_TITLE_KEYWORDS)


def _pretag(path: Path, artist: str, track_name: str):
    """Write artist/title tags before enrichment."""
    _write_tags(str(path), artist, track_name)


def itunes_search(artist: str, track_name: str, country: str | None = None) -> dict:
    """Search iTunes Search API for album name and cover art URL.

    Returns {"album": str, "artwork_url": str} or empty dict on failure.
    No API key required. No rate limit documented.
    Validates artistName similarity (>= 0.4) before accepting a result.
    country: ISO 3166-1 alpha-2 store code (e.g. "KR"). None uses US store (default).
    """
    term = f"{artist} {track_name}"
    norm_artist = _normalize_for_match(artist)

    try:
        params = {"term": term, "entity": "song", "limit": 5}
        if country:
            params["country"] = country
        r = requests.get(
            "https://itunes.apple.com/search",
            params=params,
            timeout=10,
        )
        r.raise_for_status()
        results = r.json().get("results", [])
        for candidate in results:
            ratio = difflib.SequenceMatcher(
                None, norm_artist, _normalize_for_match(candidate.get("artistName", ""))
            ).ratio()
            if ratio >= 0.4:
                album = candidate.get("collectionName", "")
                artwork_url = candidate.get("artworkUrl100", "").replace(
                    "100x100bb", "600x600bb"
                )
                artist_name = candidate.get("artistName", "")
                track_title = candidate.get("trackName", "")
                log.info(
                    "iTunes search result",
                    artist=artist,
                    track=track_name,
                    album=album,
                    artwork_url=artwork_url,
                    country=country or "US",
                )
                return {
                    "album": album,
                    "artwork_url": artwork_url,
                    "artistName": artist_name,
                    "trackName": track_title,
                }
    except Exception as exc:
        log.warning(
            "iTunes search failed",
            artist=artist,
            track=track_name,
            error=str(exc),
            country=country or "US",
        )

    log.info(
        "iTunes search: no result met artist similarity threshold (0.4)",
        artist=artist,
        track=track_name,
        country=country or "US",
    )
    return {}


def deezer_search(artist: str, track_name: str) -> dict:
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
        artist_name = item.get("artist", {}).get("name", "")
        track_title = item.get("title", "")
        log.info(
            "Deezer search result",
            artist=artist,
            track=track_name,
            album=album,
            artwork_url=artwork_url,
        )
        return {
            "album": album,
            "artwork_url": artwork_url,
            "artistName": artist_name,
            "trackName": track_title,
        }
    except Exception as exc:
        log.warning(
            "Deezer search failed", artist=artist, track=track_name, error=str(exc)
        )
        return {}


def embed_cover_art(file_path: str, mb_albumid: str) -> bool:
    """Download front cover from Cover Art Archive and embed into audio file.

    Returns True on success, False on failure (404, network error, etc.).
    """
    art_url = f"https://coverartarchive.org/release/{mb_albumid}/front"
    try:
        r = requests.get(art_url, timeout=15, allow_redirects=True)
        if r.status_code != 200:
            log.warning(
                "cover art not found", mb_albumid=mb_albumid, status=r.status_code
            )
            return False
        image_data = r.content
        content_type = r.headers.get("Content-Type", "image/jpeg")
        log.info("embedding cover art", file=file_path, size=len(image_data))

        fmt = _detect_format(file_path)
        embedder = _ART_EMBEDDER.get(fmt)
        if embedder is None:
            log.warning("unsupported format for art embedding", file=file_path)
            return False
        f = _FORMAT_OPENER[fmt](file_path)
        embedder(f, image_data, content_type)
        f.save()

        log.info("cover art embedded", file=file_path)
        return True
    except Exception as exc:
        log.warning("cover art embedding failed", file=file_path, error=str(exc))
        return False


def _normalize_for_match(s: str) -> str:
    """Lowercase and strip non-alphanumeric characters for fuzzy comparison."""
    return "".join(c for c in s.lower() if c.isalnum() or c.isspace()).strip()


_FEAT_RE = re.compile(
    r"\s+(?:feat(?:uring)?\.?|ft\.?)\s+.*$",
    re.IGNORECASE,
)
_COMMA_RE = re.compile(r",.*$")


def _primary_artist(artist: str) -> str:
    """Return the primary artist name, stripping featuring/ft./comma suffixes.

    Used only for filesystem path construction; mutagen tags keep the original value.
    Removal order: feat./featuring/ft. patterns first, then comma suffix.
    """
    result = _FEAT_RE.sub("", artist)
    result = _COMMA_RE.sub("", result)
    return result.strip()


def embed_art_from_url(file_path: str, url: str) -> bool:
    """Download image from URL and embed into audio file.

    Returns True on success, False on failure.
    """
    try:
        r = requests.get(url, timeout=15, allow_redirects=True)
        if r.status_code != 200:
            log.warning("thumbnail download failed", url=url, status=r.status_code)
            return False
        image_data = r.content
        content_type = r.headers.get("Content-Type", "image/jpeg")
        log.info(
            "embedding thumbnail as cover art", file=file_path, size=len(image_data)
        )

        fmt = _detect_format(file_path)
        embedder = _ART_EMBEDDER.get(fmt)
        if embedder is None:
            log.warning("unsupported format for art embedding", file=file_path)
            return False
        f = _FORMAT_OPENER[fmt](file_path)
        embedder(f, image_data, content_type)
        f.save()

        log.info("thumbnail embedded as cover art", file=file_path)
        return True
    except Exception as exc:
        log.warning("thumbnail embedding failed", file=file_path, error=str(exc))
        return False


def _enrich_track(
    dest_path: str,
    artist: str = "",
    track_name: str = "",
    yt_metadata: dict | None = None,
    recording_ids: list[str] | None = None,
    mb_recording_title: str = "",
) -> tuple[str, str, str]:
    """Resolve album via iTunes/Deezer, then fetch cover art from CAA/iTunes/Deezer.

    Album resolution priority: iTunes → Deezer → MB recording → YouTube channel.
    Cover art priority: CAA → iTunes → Deezer → YouTube thumbnail.
    canonical_title priority: iTunes trackName → MB recording title → Deezer title.
    Returns (album, canonical_artist, canonical_title) where canonical_artist is the
    normalised artist name from iTunes/Deezer/MB and canonical_title is the normalised
    track title from iTunes/Deezer/MB (or empty string if none found).
    """
    tags = _read_tags(dest_path)
    mb_trackid = tags.get("mb_trackid", "")
    has_album = bool(tags.get("album", ""))
    has_art = tags.get("has_art", False)

    if has_album and has_art:
        log.info("track already enriched, skipping", artist=artist, track=track_name)
        return tags.get("album", ""), tags.get("artist", ""), ""

    # ── 1. Album resolution: iTunes → Deezer first ──────────────────────
    album = ""
    canonical_artist = ""
    canonical_title = ""
    itunes_result: dict = {}
    deezer_result: dict = {}

    if not has_album and artist and track_name:
        # iTunes (most reliable for album names)
        itunes_result = itunes_search(artist, track_name)
        album = itunes_result.get("album", "")
        if album:
            canonical_artist = itunes_result.get("artistName", "")
            canonical_title = itunes_result.get("trackName", "")
            log.info(
                "album resolved via iTunes",
                artist=artist,
                track=track_name,
                album=album,
            )

        # MB recording title — 2순위 canonical_title (iTunes 결과가 없을 때)
        if not canonical_title and mb_recording_title:
            canonical_title = mb_recording_title
            log.info(
                "canonical_title resolved via MB recording title",
                artist=artist,
                track=track_name,
                mb_recording_title=mb_recording_title,
            )

        # Deezer fallback
        if not album:
            deezer_result = deezer_search(artist, track_name)
            album = deezer_result.get("album", "")
            if album:
                canonical_artist = deezer_result.get("artistName", "")
                if not canonical_title:
                    canonical_title = deezer_result.get("trackName", "")
                log.info(
                    "album resolved via Deezer",
                    artist=artist,
                    track=track_name,
                    album=album,
                )

    # ── 2. Build MB recording IDs for cover art (CAA) ────────────────────
    rids_to_try: list[str] = []
    if recording_ids:
        rids_to_try = list(recording_ids)
    elif mb_trackid:
        rids_to_try = [mb_trackid]

    if not rids_to_try and artist and track_name:
        log.info(
            "mb_trackid missing, searching MB by artist+title",
            artist=artist,
            track=track_name,
        )
        rids_to_try, _mb_artist_from_search, _mb_title_from_search = (
            mb_search_recording(artist, track_name)
        )
        if rids_to_try:
            _write_tags(dest_path, artist, track_name, rids_to_try[0])
    elif mb_trackid and recording_ids and mb_trackid not in rids_to_try:
        rids_to_try = [mb_trackid] + rids_to_try

    # ── 3. MB recording → album fallback (only if iTunes/Deezer failed) ──
    mb_albumid_candidates: list[str] = []
    if not album and not has_album:
        for rid in rids_to_try:
            mb_album, mb_albumid_candidates = mb_album_from_recording_id(rid)
            if mb_album and not _is_live_title(mb_album):
                album = mb_album
                if rid != mb_trackid:
                    _write_tags(dest_path, artist, track_name, rid)
                break
            if mb_album:
                log.info(
                    "MB recording yielded live/bad album, trying next candidate",
                    recording_id=rid,
                    album=mb_album,
                )
    # Note: when iTunes/Deezer resolved album, skip CAA — MB recordings
    # are often from compilations/DJ-mixes and would fetch wrong cover art.
    # iTunes/Deezer artwork is used instead (section 4 below).

    # YouTube channel — last resort for album name
    if not album and not has_album and yt_metadata:
        album = yt_metadata.get("channel", "")
        if album:
            log.info(
                "falling back to YouTube channel as album",
                artist=artist,
                track=track_name,
                channel=album,
            )

    # All 4 sources failed — use "Unknown Album" to prevent Navidrome "Non-album" display
    if not album and not has_album:
        album = "Unknown Album"
        log.info(
            "all album sources failed, using Unknown Album fallback",
            artist=artist,
            track=track_name,
        )

    if album and not has_album:
        log.info("setting album tag", artist=artist, track=track_name, album=album)
        write_album_tag(dest_path, album)

    # ── 4. Cover art: CAA → iTunes → Deezer → YouTube thumbnail ─────────
    art_embedded = False
    if not has_art and mb_albumid_candidates:
        for candidate_id in mb_albumid_candidates:
            if embed_cover_art(dest_path, candidate_id):
                art_embedded = True
                break
        if art_embedded:
            has_art = _read_tags(dest_path).get("has_art", True)

    if not has_art:
        # iTunes art
        if not art_embedded:
            if not itunes_result and artist and track_name:
                itunes_result = itunes_search(artist, track_name)
                if not canonical_artist:
                    canonical_artist = itunes_result.get("artistName", "")
            itunes_art = itunes_result.get("artwork_url", "")
            if itunes_art:
                log.info(
                    "embedding cover art from iTunes", artist=artist, track=track_name
                )
                art_embedded = embed_art_from_url(dest_path, itunes_art)

        # Deezer art
        if not art_embedded:
            if not deezer_result and artist and track_name:
                deezer_result = deezer_search(artist, track_name)
                if not canonical_artist:
                    canonical_artist = deezer_result.get("artistName", "")
            deezer_art = deezer_result.get("artwork_url", "")
            if deezer_art:
                log.info(
                    "embedding cover art from Deezer", artist=artist, track=track_name
                )
                art_embedded = embed_art_from_url(dest_path, deezer_art)

        # YouTube thumbnail — last resort
        if not art_embedded and yt_metadata:
            thumbnail_url = yt_metadata.get("thumbnail_url", "")
            if thumbnail_url:
                log.info(
                    "no cover art from CAA/iTunes/Deezer, falling back to YouTube thumbnail",
                    artist=artist,
                    track=track_name,
                )
                art_embedded = embed_art_from_url(dest_path, thumbnail_url)

    return album, canonical_artist, canonical_title


def tag_and_import(
    staging_file: str,
    music_dir: str,
    artist: str = "",
    track_name: str = "",
    yt_metadata: dict | None = None,
    db_path: str | None = None,
    mbid: str | None = None,
) -> tuple[bool, str, str, str, str, str]:
    """Tag staging file in-place, then copy to final music_dir path in one step.

    Returns (success, dest_path, canonical_artist, canonical_title, canonical_album, mb_recording_id).
    All string fields are empty strings on failure or when unavailable.
    """
    path = Path(staging_file)
    if not path.exists():
        log.error("staging file not found", file=staging_file)
        return False, "", "", "", "", ""

    # Search MB for recording IDs (best-effort; failure does not abort import)
    recording_ids: list[str] = []
    mb_artist_name: str = ""
    mb_recording_title: str = ""

    is_lb_track = mbid and not mbid.startswith("manual-")

    if is_lb_track:
        # LB track: already have the correct recording_mbid, look it up directly
        meta = lookup_recording(mbid)
        if meta["artist"] or meta["title"]:
            recording_ids = [mbid]
            mb_artist_name = meta["artist"]
            mb_recording_title = meta["title"]
        else:
            log.warning("MB direct lookup failed, falling back to search", mbid=mbid)
            if artist and track_name:
                recording_ids, mb_artist_name, mb_recording_title = mb_search_recording(
                    artist, track_name
                )
    elif artist and track_name:
        recording_ids, mb_artist_name, mb_recording_title = mb_search_recording(
            artist, track_name
        )

    if not recording_ids:
        log.warning(
            "MB recording not found, continuing with iTunes/Deezer",
            artist=artist,
            track=track_name,
        )

    # Write initial tags to staging file
    _write_tags(
        str(path), artist, track_name, recording_ids[0] if recording_ids else ""
    )

    # Enrich staging file: album from iTunes/Deezer/MB + cover art
    album, canonical_artist, canonical_title = _enrich_track(
        str(path),
        artist=artist,
        track_name=track_name,
        yt_metadata=yt_metadata,
        recording_ids=recording_ids if recording_ids else None,
        mb_recording_title=mb_recording_title,
    )

    # Determine final artist folder name:
    # 1. MB artist-credit name (most authoritative canonical source)
    # 2. canonical_artist from iTunes/Deezer (returned by _enrich_track)
    # 3. original request artist name (last resort)
    effective_artist = mb_artist_name or canonical_artist or artist
    if mb_artist_name and mb_artist_name != artist:
        log.info(
            "using MB artist-credit name for folder",
            original=artist,
            mb_artist=mb_artist_name,
        )
    sanitized_artist = (
        sanitize_path_component(_primary_artist(effective_artist))
        if effective_artist
        else "Unknown Artist"
    )

    # Determine final track filename: canonical from iTunes/Deezer preferred, else original request
    effective_track = canonical_title if canonical_title else (track_name or path.stem)
    sanitized_track = sanitize_path_component(effective_track)
    if canonical_title and canonical_title != track_name:
        log.info(
            "using canonical track title for filename",
            original=track_name,
            canonical=canonical_title,
        )

    # Overwrite artist/title tags on staging file with canonical names before copy
    # effective_artist: full canonical name (feat. not stripped — that's only for folder path)
    # effective_track: canonical title from iTunes/Deezer, or original request
    _write_tags(str(path), effective_artist, effective_track)

    # Compute final destination path based on resolved album
    sanitized_album = sanitize_path_component(album) if album else "Unknown Album"
    dest_dir = Path(music_dir) / sanitized_artist / sanitized_album
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / (sanitized_track + path.suffix)

    result_mb_recording_id = recording_ids[0] if recording_ids else ""

    # If file already exists at dest, treat as already in library
    if dest_path.exists():
        log.info(
            "file already exists in music_dir, treating as duplicate",
            dest=str(dest_path),
        )
        _cleanup_staging(path)
        return (
            True,
            str(dest_path),
            effective_artist,
            effective_track,
            album,
            result_mb_recording_id,
        )

    # Copy enriched staging file to final destination
    try:
        shutil.copy2(str(path), str(dest_path))
        log.info("copied file to music_dir", src=staging_file, dest=str(dest_path))
    except OSError as exc:
        log.error(
            "failed to copy file to music_dir",
            src=staging_file,
            dest=str(dest_path),
            error=str(exc),
        )
        return False, "", "", "", "", ""

    _cleanup_staging(path)
    return (
        True,
        str(dest_path),
        effective_artist,
        effective_track,
        album,
        result_mb_recording_id,
    )


def _cleanup_staging(path: Path):
    try:
        path.unlink()
        log.debug("staging file removed", file=str(path))
    except OSError as exc:
        log.warning("could not remove staging file", error=str(exc))
