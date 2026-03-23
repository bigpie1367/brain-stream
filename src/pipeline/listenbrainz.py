from typing import Any, Dict, List

import requests

from src.pipeline.musicbrainz import lookup_recording
from src.utils.logger import get_logger

log = get_logger(__name__)

LB_BASE = "https://api.listenbrainz.org/1"


def fetch_recommendations(
    username: str, token: str, count: int = 25, offset: int = 0
) -> List[Dict[str, Any]]:
    url = f"{LB_BASE}/cf/recommendation/user/{username}/recording"
    headers = {"Authorization": f"Token {token}"}
    params = {"count": count, "offset": offset}

    log.info("fetching recommendations", username=username, count=count, offset=offset)
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
        meta = lookup_recording(mbid)
        if not meta["artist"] or not meta["title"]:
            log.warning(
                "skipping recommendation: missing artist or track name",
                mbid=mbid,
                artist=meta["artist"],
                track_name=meta["title"],
            )
            continue
        results.append(
            {
                "mbid": mbid,
                "track_name": meta["title"],
                "artist": meta["artist"],
            }
        )

    log.info("recommendations fetched", total=len(results))
    return results
