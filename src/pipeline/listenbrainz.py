import time
from typing import List, Dict, Any

import requests

from src.utils.logger import get_logger

log = get_logger(__name__)

LB_BASE = "https://api.listenbrainz.org/1"
_MB_API = "https://musicbrainz.org/ws/2"
_MB_HEADERS = {"User-Agent": "music-bot/1.0 (https://github.com/music-bot)"}


def _lookup_recording(recording_mbid: str) -> Dict[str, str]:
    """Look up recording artist and title from MusicBrainz by recording MBID.

    Returns dict with 'artist' and 'track_name' keys, empty strings on failure.
    """
    try:
        time.sleep(1)  # rate limit: 1 req/sec
        r = requests.get(
            f"{_MB_API}/recording/{recording_mbid}",
            params={"fmt": "json", "inc": "artist-credits"},
            headers=_MB_HEADERS,
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        track_name = data.get("title", "")
        artist_credits = data.get("artist-credit", [])
        artist_parts = []
        for credit in artist_credits:
            if isinstance(credit, dict):
                artist_obj = credit.get("artist", {})
                name = artist_obj.get("name", "")
                if name:
                    artist_parts.append(name)
                joinphrase = credit.get("joinphrase", "")
                if joinphrase:
                    artist_parts.append(joinphrase)
        artist = "".join(artist_parts).strip()
        return {"artist": artist, "track_name": track_name}
    except Exception as exc:
        log.warning("MB recording lookup failed", mbid=recording_mbid, error=str(exc))
        return {"artist": "", "track_name": ""}


def fetch_recommendations(username: str, token: str, count: int = 25) -> List[Dict[str, Any]]:
    url = f"{LB_BASE}/cf/recommendation/user/{username}/recording"
    headers = {"Authorization": f"Token {token}"}
    params = {"count": count}

    log.info("fetching recommendations", username=username, count=count)
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()

    if not resp.content:
        log.info("no recommendations available (empty response)", username=username)
        return []

    data = resp.json()
    payload = data.get("payload", {})
    recordings = payload.get("mbids", [])

    results = []
    for rec in recordings:
        mbid = rec.get("recording_mbid")
        if not mbid:
            continue
        meta = _lookup_recording(mbid)
        if not meta["artist"] or not meta["track_name"]:
            log.warning(
                "skipping recommendation: missing artist or track name",
                mbid=mbid,
                artist=meta["artist"],
                track_name=meta["track_name"],
            )
            continue
        results.append({
            "mbid": mbid,
            "track_name": meta["track_name"],
            "artist": meta["artist"],
        })

    log.info("recommendations fetched", total=len(results))
    return results
