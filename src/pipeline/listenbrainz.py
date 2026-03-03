from typing import List, Dict, Any

import requests

from src.utils.logger import get_logger

log = get_logger(__name__)

LB_BASE = "https://api.listenbrainz.org/1"


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
        results.append({
            "mbid": mbid,
            "track_name": rec.get("recording_name", ""),
            "artist": rec.get("artist_name", ""),
        })

    log.info("recommendations fetched", total=len(results))
    return results
