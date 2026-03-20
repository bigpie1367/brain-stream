"""MusicBrainz API client — shared constants, lookup, and search functions."""

import difflib
import time

import requests

from src.pipeline.tagger import _is_live_title, _normalize_for_match
from src.utils.logger import get_logger

log = get_logger(__name__)

MB_API = "https://musicbrainz.org/ws/2"
MB_HEADERS = {
    "User-Agent": "brainstream/1.0 (https://github.com/bigpie1367/brain-stream)"
}
MB_SEARCH_URL = f"{MB_API}/recording"


def lookup_recording(mbid: str) -> dict[str, str]:
    """Look up MB recording by mbid.

    Returns {"artist": str, "title": str}, empty strings on failure.
    Consolidates tagger._lookup_recording_by_mbid and listenbrainz._lookup_recording.
    """
    try:
        time.sleep(1)  # rate limit
        r = requests.get(
            f"{MB_API}/recording/{mbid}",
            params={"fmt": "json", "inc": "artist-credits"},
            headers=MB_HEADERS,
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
        log.warning("MB recording lookup failed", mbid=mbid, error=str(exc))
        return {"artist": "", "title": ""}


# ── Internal helpers ─────────────────────────────────────────────────────────


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


def _mb_lookup_artist_ids(artist: str, limit: int = 3) -> list[str]:
    """Search MB artist API by name, return list of artist MBIDs (up to `limit`)."""
    try:
        time.sleep(1)  # rate limit
        r = requests.get(
            f"{MB_API}/artist",
            params={"query": f'artistname:"{artist}"', "fmt": "json", "limit": limit},
            headers=MB_HEADERS,
            timeout=10,
        )
        r.raise_for_status()
        artists = r.json().get("artists", [])
        return [a["id"] for a in artists if a.get("id")]
    except Exception as exc:
        log.warning("MB artist lookup failed", artist=artist, error=str(exc))
        return []


# ── Public API ───────────────────────────────────────────────────────────────


def mb_search_recording(artist: str, track_name: str) -> tuple[list[str], str, str]:
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
            f"{MB_API}/recording",
            params={
                "query": strict_query,
                "fmt": "json",
                "limit": 5,
                "inc": "artist-credits",
            },
            headers=MB_HEADERS,
            timeout=10,
        )
        r.raise_for_status()
        recordings = r.json().get("recordings", [])
        if recordings:
            candidates = _collect_recording_candidates(recordings, track_name)
            if candidates:
                mb_artist_name = _extract_mb_artist_name(recordings)
                mb_recording_title = _extract_mb_recording_title(
                    recordings, candidates[0]
                )
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
            f"{MB_API}/recording",
            params={"query": query, "fmt": "json", "limit": 5, "inc": "artist-credits"},
            headers=MB_HEADERS,
            timeout=10,
        )
        r.raise_for_status()
        recordings = r.json().get("recordings", [])
        if recordings:
            candidates = _collect_recording_candidates(recordings, track_name)
            if candidates:
                mb_artist_name = _extract_mb_artist_name(recordings)
                mb_recording_title = _extract_mb_recording_title(
                    recordings, candidates[0]
                )
                log.info(
                    "MB plain search found recordings",
                    artist=artist,
                    track=track_name,
                    recording_ids=candidates,
                    mb_artist_name=mb_artist_name,
                    mb_recording_title=mb_recording_title,
                )
                return candidates, mb_artist_name, mb_recording_title

        # Stage 2.5: artist ID 기반 검색 (아티스트명이 다른 언어/표기로 인덱싱된 경우 대응)
        artist_ids = _mb_lookup_artist_ids(artist)
        for arid in artist_ids:
            time.sleep(1)  # rate limit
            try:
                r = requests.get(
                    f"{MB_API}/recording",
                    params={
                        "query": f'arid:{arid} AND recording:"{track_name}"',
                        "fmt": "json",
                        "limit": 5,
                    },
                    headers=MB_HEADERS,
                    timeout=10,
                )
                r.raise_for_status()
                recordings = r.json().get("recordings", [])
                for rec in recordings:
                    rec_title = rec.get("title", "")
                    if (
                        difflib.SequenceMatcher(
                            None, rec_title.lower(), track_name.lower()
                        ).ratio()
                        < 0.4
                    ):
                        continue
                    rec_id = rec.get("id")
                    if not rec_id:
                        continue
                    credits = rec.get("artist-credit", [])
                    mb_artist = "".join(
                        c.get("artist", {}).get("name", "") + c.get("joinphrase", "")
                        for c in credits
                        if isinstance(c, dict)
                    ).strip()
                    log.info(
                        "MB stage 2.5 match",
                        recording=rec_title,
                        artist=mb_artist,
                        arid=arid,
                    )
                    return [rec_id], mb_artist, rec_title
            except Exception as exc:
                log.warning("MB stage 2.5 search failed", arid=arid, error=str(exc))
                continue

        # Fallback: recording-only search (no artist filter) — pick best artist match
        log.info(
            "MB artistname+recording search returned 0 results, trying recording-only fallback",
            artist=artist,
            track=track_name,
        )
        time.sleep(1)  # rate limit
        r2 = requests.get(
            f"{MB_API}/recording",
            params={
                "query": f'recording:"{track_name}"',
                "fmt": "json",
                "limit": 5,
                "inc": "artist-credits+aliases",
            },
            headers=MB_HEADERS,
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
                    credit_artist = (
                        credit.get("artist", {}) if isinstance(credit, dict) else {}
                    )
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
        log.warning(
            "MB recording search failed",
            artist=artist,
            track=track_name,
            error=str(exc),
        )
        return [], "", ""


def mb_album_from_recording_id(recording_id: str) -> tuple[str, list[str]]:
    """Get (album_title, mb_albumid_candidates) from a MusicBrainz recording ID.

    Returns up to 3 Official Album release IDs to try for Cover Art Archive.
    Falls back to the first release if no Official Album found.
    """
    try:
        time.sleep(1)  # rate limit
        r = requests.get(
            f"{MB_API}/recording/{recording_id}",
            params={"fmt": "json", "inc": "releases+release-groups"},
            headers=MB_HEADERS,
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
        log.warning(
            "MB recording lookup failed", recording_id=recording_id, error=str(exc)
        )
        return "", []
