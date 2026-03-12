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


def _pick_best_recording(recordings: list, track_name: str = "") -> str:
    """Official Album (no secondary types) release를 가진 recording 우선 선택.

    track_name이 주어지면 recording title과의 유사도(>= 0.8)를 검증한다.
    1순위: title 유사도 >= 0.8 + Official Album release 있는 recording
    2순위: title 유사도 >= 0.8인 첫 번째 recording
    3순위: 유사도 무관 첫 번째 recording (fallback)
    """
    norm_target = _normalize_for_match(track_name) if track_name else ""

    # 1순위: Official Album release 있고 title 유사도 >= 0.8
    for rec in recordings:
        if norm_target:
            ratio = difflib.SequenceMatcher(
                None, norm_target, _normalize_for_match(rec.get("title", ""))
            ).ratio()
            if ratio < 0.8:
                continue
        for rel in rec.get("releases", []):
            rg = rel.get("release-group", {})
            if (
                rel.get("status") == "Official"
                and rg.get("primary-type") == "Album"
                and not rg.get("secondary-types")
            ):
                return rec.get("id", "")

    # 2순위: title 유사도 >= 0.8이면 첫 번째 recording
    if norm_target:
        for rec in recordings:
            ratio = difflib.SequenceMatcher(
                None, norm_target, _normalize_for_match(rec.get("title", ""))
            ).ratio()
            if ratio >= 0.8:
                return rec.get("id", "")

    # 3순위: track_name이 주어졌는데 유사도 0.8 이상인 recording이 없으면 빈 문자열 반환
    if norm_target:
        return ""
    return recordings[0].get("id", "") if recordings else ""


def _collect_recording_candidates(recordings: list, track_name: str = "") -> list[str]:
    """recordings 목록에서 title 유사도 >= 0.8인 후보 ID들을 반환한다 (최대 3개).

    _pick_best_recording의 1순위 ID를 앞에 두고, 나머지 유사도 >= 0.8 후보를 이어붙인다.
    중복 제거 후 최대 3개 반환.
    """
    best = _pick_best_recording(recordings, track_name)
    norm_target = _normalize_for_match(track_name) if track_name else ""

    candidates: list[str] = []
    if best:
        candidates.append(best)

    if norm_target:
        for rec in recordings:
            rid = rec.get("id", "")
            if not rid or rid == best:
                continue
            ratio = difflib.SequenceMatcher(
                None, norm_target, _normalize_for_match(rec.get("title", ""))
            ).ratio()
            if ratio >= 0.8:
                candidates.append(rid)
                if len(candidates) >= 3:
                    break
    elif not norm_target and not best:
        # track_name도 없고 best도 없으면 첫 번째
        for rec in recordings[:3]:
            rid = rec.get("id", "")
            if rid and rid not in candidates:
                candidates.append(rid)

    return candidates[:3]


def _extract_mb_artist_name(recordings: list) -> str:
    """Extract the primary artist name from the first recording's artist-credit."""
    for rec in recordings:
        credits = rec.get("artist-credit", [])
        for credit in credits:
            if not isinstance(credit, dict):
                continue
            name = credit.get("artist", {}).get("name", "")
            if name:
                return name
    return ""


def _extract_mb_recording_title(recordings: list, best_id: str) -> str:
    """Extract the title of the recording with the given ID from the recordings list."""
    for rec in recordings:
        if rec.get("id") == best_id:
            return rec.get("title", "")
    return ""


def _lookup_recording_by_mbid(mbid: str) -> dict[str, str]:
    """Look up MB recording directly by mbid. Returns {artist, title}, empty strings on failure."""
    try:
        time.sleep(1)  # rate limit
        r = requests.get(
            f"{_MB_API}/recording/{mbid}",
            params={"fmt": "json", "inc": "artist-credits"},
            headers=_MB_HEADERS,
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        title = data.get("title", "")
        artist_credits = data.get("artist-credit", [])
        artist_parts = []
        for credit in artist_credits:
            if isinstance(credit, dict):
                name = credit.get("artist", {}).get("name", "")
                if name:
                    artist_parts.append(name)
                joinphrase = credit.get("joinphrase", "")
                if joinphrase:
                    artist_parts.append(joinphrase)
        artist = "".join(artist_parts).strip()
        return {"artist": artist, "title": title}
    except Exception as exc:
        log.warning("MB direct recording lookup failed", mbid=mbid, error=str(exc))
        return {"artist": "", "title": ""}


def _mb_search_recording(artist: str, track_name: str) -> tuple[list[str], str, str]:
    """Search MusicBrainz for recordings by artist and title.

    Uses artistname: field (includes aliases) instead of artist: (canonical only).
    First tries a strict query (Official Album, no Live/Compilation/Soundtrack/
    Mixtape/DJ-mix/Remix secondary-types) to prefer studio recordings over live
    versions. Falls back to the plain artistname+recording query if the strict
    query returns no results. Falls back further to a recording-only search if
    that also returns 0 results.
    Returns (candidate_recording_ids, mb_artist_name, mb_recording_title).
    candidate_recording_ids: up to 3, deduplicated, or empty list on failure.
    mb_artist_name: primary artist name from artist-credit, or empty string.
    mb_recording_title: title of the best-matched recording, or empty string.
    """
    try:
        # Attempt 1: strict query — Official Album, exclude Live/Compilation/Soundtrack/Mixtape/DJ-mix/Remix
        time.sleep(1)  # rate limit
        strict_query = (
            f'artistname:"{artist}" AND recording:"{track_name}"'
            " AND primarytype:Album AND status:Official"
            " AND NOT secondarytype:Live"
            " AND NOT secondarytype:Compilation"
            " AND NOT secondarytype:Soundtrack"
            " AND NOT secondarytype:Mixtape/Street"
            " AND NOT secondarytype:DJ-mix"
            " AND NOT secondarytype:Remix"
        )
        r = requests.get(
            f"{_MB_API}/recording",
            params={"query": strict_query, "fmt": "json", "limit": 5, "inc": "artist-credits"},
            headers=_MB_HEADERS,
            timeout=10,
        )
        r.raise_for_status()
        recordings = r.json().get("recordings", [])
        if recordings:
            candidates = _collect_recording_candidates(recordings, track_name)
            if candidates:
                mb_artist_name = _extract_mb_artist_name(recordings)
                mb_recording_title = _extract_mb_recording_title(recordings, candidates[0])
                log.info(
                    "MB strict search found recordings",
                    artist=artist,
                    track=track_name,
                    recording_ids=candidates,
                    mb_artist_name=mb_artist_name,
                    mb_recording_title=mb_recording_title,
                )
                return candidates, mb_artist_name, mb_recording_title

        # Attempt 2: plain query (no release-type filter)
        log.info(
            "MB strict search returned 0 results, falling back to plain artistname+recording query",
            artist=artist,
            track=track_name,
        )
        time.sleep(1)  # rate limit
        query = f'artistname:"{artist}" AND recording:"{track_name}"'
        r = requests.get(
            f"{_MB_API}/recording",
            params={"query": query, "fmt": "json", "limit": 5, "inc": "artist-credits"},
            headers=_MB_HEADERS,
            timeout=10,
        )
        r.raise_for_status()
        recordings = r.json().get("recordings", [])
        if recordings:
            candidates = _collect_recording_candidates(recordings, track_name)
            if candidates:
                mb_artist_name = _extract_mb_artist_name(recordings)
                mb_recording_title = _extract_mb_recording_title(recordings, candidates[0])
                log.info(
                    "MB plain search found recordings",
                    artist=artist,
                    track=track_name,
                    recording_ids=candidates,
                    mb_artist_name=mb_artist_name,
                    mb_recording_title=mb_recording_title,
                )
                return candidates, mb_artist_name, mb_recording_title

        # Fallback: recording-only search (no artist filter) — pick best artist match
        log.info(
            "MB artistname+recording search returned 0 results, trying recording-only fallback",
            artist=artist,
            track=track_name,
        )
        time.sleep(1)  # rate limit
        r2 = requests.get(
            f"{_MB_API}/recording",
            params={
                "query": f'recording:"{track_name}"',
                "fmt": "json",
                "limit": 5,
                "inc": "artist-credits+aliases",
            },
            headers=_MB_HEADERS,
            timeout=10,
        )
        r2.raise_for_status()
        recordings2 = r2.json().get("recordings", [])
        if recordings2:
            norm_artist = _normalize_for_match(artist)
            best_id = ""
            best_ratio = 0.0
            best_artist_name = ""
            for rec in recordings2:
                credits = rec.get("artist-credit", [])
                for credit in credits:
                    credit_artist = credit.get("artist", {}) if isinstance(credit, dict) else {}
                    candidate_names = []
                    if credit_artist.get("name"):
                        candidate_names.append(credit_artist["name"])
                    if credit_artist.get("sort-name"):
                        candidate_names.append(credit_artist["sort-name"])
                    for alias in credit_artist.get("aliases", []):
                        if alias.get("name"):
                            candidate_names.append(alias["name"])
                    if not candidate_names:
                        continue
                    ratio = max(
                        (
                            difflib.SequenceMatcher(
                                None, norm_artist, _normalize_for_match(n)
                            ).ratio()
                            for n in candidate_names
                        ),
                        default=0.0,
                    )
                    if ratio > best_ratio:
                        best_ratio = ratio
                        best_id = rec.get("id", "")
                        best_artist_name = credit_artist.get("name", "")
            if best_ratio >= 0.3 and best_id:
                best_recording_title = _extract_mb_recording_title(recordings2, best_id)
                log.info(
                    "MB recording-only fallback found recording",
                    artist=artist,
                    track=track_name,
                    recording_id=best_id,
                    mb_artist_name=best_artist_name,
                    mb_recording_title=best_recording_title,
                    artist_similarity=round(best_ratio, 3),
                )
                return [best_id], best_artist_name, best_recording_title
            log.info(
                "MB recording-only fallback: no result met artist similarity threshold (0.3)",
                artist=artist,
                track=track_name,
                best_ratio=round(best_ratio, 3),
            )
        return [], "", ""
    except Exception as exc:
        log.warning("MB recording search failed", artist=artist, track=track_name, error=str(exc))
        return [], "", ""


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


def _write_artist_tag(file_path: str, artist: str):
    """Write artist tag to audio file using mutagen."""
    try:
        suffix = Path(file_path).suffix.lower()
        if suffix == ".flac":
            f = mutagen.flac.FLAC(file_path)
            f["artist"] = [artist]
            f.save()
        elif suffix in (".opus", ".ogg"):
            f = mutagen.oggopus.OggOpus(file_path)
            f["artist"] = [artist]
            f.save()
        elif suffix in (".m4a", ".mp4"):
            f = mutagen.mp4.MP4(file_path)
            f["\xa9ART"] = [artist]
            f.save()
        else:
            f = mutagen.File(file_path)
            if f is not None:
                f["artist"] = artist
                f.save()
        log.debug("wrote artist tag", file=file_path, artist=artist)
    except Exception as exc:
        log.warning("could not write artist tag", file=file_path, error=str(exc))


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


_LIVE_TITLE_RE = re.compile(r"\d{4}-\d{2}-\d{2}[,:]")
_LIVE_TITLE_KEYWORDS = ("live", "concert", "festival", "bootleg", "unplugged")


def _is_live_title(title: str) -> bool:
    """Return True if the release title looks like a live event (date-prefixed or keyword match)."""
    if _LIVE_TITLE_RE.search(title):
        return True
    lower = title.lower()
    return any(kw in lower for kw in _LIVE_TITLE_KEYWORDS)


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

        def _has_secondary_types(rel):
            return bool(rel.get("release-group", {}).get("secondary-types", []))

        # Collect Official Album releases for CAA fallback
        # Exclude releases with secondary-types (e.g. Live, Compilation, Soundtrack)
        # Also exclude releases whose title looks like a live event
        official_album_releases = []
        for rel in releases:
            status = rel.get("status", "")
            rtype = rel.get("release-group", {}).get("primary-type", "")
            if (
                status == "Official"
                and rtype == "Album"
                and not _has_secondary_types(rel)
                and not _is_live_title(rel.get("title", ""))
            ):
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

        # Fallback: prefer Official releases without secondary-types, then title-filter,
        # then any release — pick earliest by date.
        releases_with_id = [rel for rel in releases if rel.get("id")]
        if not releases_with_id:
            return "", []
        releases_with_id.sort(key=_release_date_key)

        # Prefer: Official, no secondary-types, non-live title
        for candidate in releases_with_id:
            if (
                candidate.get("status") == "Official"
                and not _has_secondary_types(candidate)
                and not _is_live_title(candidate.get("title", ""))
            ):
                album = candidate.get("title", "")
                mbid = candidate.get("id", "")
                candidates = [mbid] if mbid else []
                if album:
                    log.info(
                        "resolved album (fallback: official non-live) from MB recording",
                        recording_id=recording_id,
                        album=album,
                        mb_albumid_candidates=candidates,
                    )
                return album, candidates

        # Prefer: no secondary-types, non-live title (any status)
        for candidate in releases_with_id:
            if not _has_secondary_types(candidate) and not _is_live_title(
                candidate.get("title", "")
            ):
                album = candidate.get("title", "")
                mbid = candidate.get("id", "")
                candidates = [mbid] if mbid else []
                if album:
                    log.info(
                        "resolved album (fallback: non-live any status) from MB recording",
                        recording_id=recording_id,
                        album=album,
                        mb_albumid_candidates=candidates,
                    )
                return album, candidates

        # Last resort: pick earliest release regardless of type
        album = releases_with_id[0].get("title", "")
        mbid = releases_with_id[0].get("id", "")
        candidates = [mbid] if mbid else []
        if album:
            log.info(
                "resolved album (fallback: any release) from MB recording",
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


def _itunes_search(artist: str, track_name: str, country: str | None = None) -> dict:
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
                artwork_url = candidate.get("artworkUrl100", "").replace("100x100bb", "600x600bb")
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


def _embed_art_from_url(file_path: str, url: str) -> bool:
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
            return False

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
        itunes_result = _itunes_search(artist, track_name)
        album = itunes_result.get("album", "")
        if album:
            canonical_artist = itunes_result.get("artistName", "")
            canonical_title = itunes_result.get("trackName", "")
            log.info("album resolved via iTunes", artist=artist, track=track_name, album=album)

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
            deezer_result = _deezer_search(artist, track_name)
            album = deezer_result.get("album", "")
            if album:
                canonical_artist = deezer_result.get("artistName", "")
                if not canonical_title:
                    canonical_title = deezer_result.get("trackName", "")
                log.info("album resolved via Deezer", artist=artist, track=track_name, album=album)

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
        rids_to_try, _mb_artist_from_search, _mb_title_from_search = _mb_search_recording(
            artist, track_name
        )
        if rids_to_try:
            _write_tags(dest_path, artist, track_name, rids_to_try[0])
    elif mb_trackid and recording_ids and mb_trackid not in rids_to_try:
        rids_to_try = [mb_trackid] + rids_to_try

    # ── 3. MB recording → album fallback (only if iTunes/Deezer failed) ──
    mb_albumid_candidates: list[str] = []
    if not album and not has_album:
        for rid in rids_to_try:
            mb_album, mb_albumid_candidates = _mb_album_from_recording_id(rid)
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
        _write_album_tag(dest_path, album)

    # ── 4. Cover art: CAA → iTunes → Deezer → YouTube thumbnail ─────────
    art_embedded = False
    if not has_art and mb_albumid_candidates:
        for candidate_id in mb_albumid_candidates:
            if _embed_cover_art(dest_path, candidate_id):
                art_embedded = True
                break
        if art_embedded:
            has_art = _read_tags(dest_path).get("has_art", True)

    if not has_art:
        # iTunes art
        if not art_embedded:
            if not itunes_result and artist and track_name:
                itunes_result = _itunes_search(artist, track_name)
                if not canonical_artist:
                    canonical_artist = itunes_result.get("artistName", "")
            itunes_art = itunes_result.get("artwork_url", "")
            if itunes_art:
                log.info("embedding cover art from iTunes", artist=artist, track=track_name)
                _embed_art_from_url(dest_path, itunes_art)
                art_embedded = True

        # Deezer art
        if not art_embedded:
            if not deezer_result and artist and track_name:
                deezer_result = _deezer_search(artist, track_name)
                if not canonical_artist:
                    canonical_artist = deezer_result.get("artistName", "")
            deezer_art = deezer_result.get("artwork_url", "")
            if deezer_art:
                log.info("embedding cover art from Deezer", artist=artist, track=track_name)
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
                )
                _embed_art_from_url(dest_path, thumbnail_url)

    return album, canonical_artist, canonical_title


def tag_and_import(
    staging_file: str,
    music_dir: str,
    artist: str = "",
    track_name: str = "",
    yt_metadata: dict | None = None,
    db_path: str | None = None,
    mbid: str | None = None,
) -> tuple[bool, str, str, str, str]:
    """Tag staging file in-place, then copy to final music_dir path in one step.

    Returns (success, dest_path, canonical_artist, canonical_title, canonical_album).
    canonical_artist, canonical_title and canonical_album are empty strings on failure or when unavailable.
    """
    path = Path(staging_file)
    if not path.exists():
        log.error("staging file not found", file=staging_file)
        return False, "", "", "", ""

    # Search MB for recording IDs (best-effort; failure does not abort import)
    recording_ids: list[str] = []
    mb_artist_name: str = ""
    mb_recording_title: str = ""

    is_lb_track = mbid and not mbid.startswith("manual-")

    if is_lb_track:
        # LB track: already have the correct recording_mbid, look it up directly
        meta = _lookup_recording_by_mbid(mbid)
        if meta["artist"] or meta["title"]:
            recording_ids = [mbid]
            mb_artist_name = meta["artist"]
            mb_recording_title = meta["title"]
        else:
            log.warning("MB direct lookup failed, falling back to search", mbid=mbid)
            if artist and track_name:
                recording_ids, mb_artist_name, mb_recording_title = _mb_search_recording(
                    artist, track_name
                )
    elif artist and track_name:
        recording_ids, mb_artist_name, mb_recording_title = _mb_search_recording(
            artist, track_name
        )

    if not recording_ids:
        log.warning(
            "MB recording not found, continuing with iTunes/Deezer",
            artist=artist,
            track=track_name,
        )

    # Write initial tags to staging file
    _write_tags(str(path), artist, track_name, recording_ids[0] if recording_ids else "")

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
        _sanitize_filename(_primary_artist(effective_artist))
        if effective_artist
        else "Unknown Artist"
    )

    # Determine final track filename: canonical from iTunes/Deezer preferred, else original request
    effective_track = canonical_title if canonical_title else (track_name or path.stem)
    sanitized_track = _sanitize_filename(effective_track)
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
    sanitized_album = _sanitize_filename(album) if album else "Unknown Album"
    dest_dir = Path(music_dir) / sanitized_artist / sanitized_album
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / (sanitized_track + path.suffix)

    # If file already exists at dest, treat as already in library
    if dest_path.exists():
        log.info("file already exists in music_dir, treating as duplicate", dest=str(dest_path))
        _cleanup_staging(path)
        return True, str(dest_path), effective_artist, effective_track, album

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
        return False, "", "", "", ""

    _cleanup_staging(path)
    return True, str(dest_path), effective_artist, effective_track, album


def _cleanup_staging(path: Path):
    try:
        path.unlink()
        log.debug("staging file removed", file=str(path))
    except OSError as exc:
        log.warning("could not remove staging file", error=str(exc))


# Public aliases for use by api.py (rematch endpoints)
mb_search_recording = _mb_search_recording
mb_album_from_recording_id = _mb_album_from_recording_id
embed_cover_art = _embed_cover_art
embed_art_from_url = _embed_art_from_url
write_album_tag = _write_album_tag
write_artist_tag = _write_artist_tag
itunes_search = _itunes_search
