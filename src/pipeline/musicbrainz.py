"""MusicBrainz API client — shared constants and lookup functions."""

import time

import requests

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
