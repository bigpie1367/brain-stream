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


def fetch_user_top_artists(
    username: str, range_: str = "quarter", count: int = 10
) -> List[Dict[str, Any]]:
    """유저 탑 아티스트 조회. API 실패 시 빈 리스트 반환."""
    url = f"{LB_BASE}/stats/user/{username}/artists"
    params = {"range": range_, "count": count}
    try:
        log.info("fetching top artists", username=username, range=range_, count=count)
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        artists = data.get("payload", {}).get("artists", [])
        log.info("top artists fetched", count=len(artists))
        return [
            {"artist_name": a["artist_name"], "artist_mbid": a.get("artist_mbid", "")}
            for a in artists
            if a.get("artist_name")
        ]
    except Exception as exc:
        log.warning("failed to fetch top artists", error=str(exc))
        return []


def fetch_lb_radio(prompt: str, token: str, mode: str = "easy") -> List[Dict[str, Any]]:
    """LB Radio API 호출. JSPF 파싱하여 [{mbid, artist, track_name}, ...] 반환.
    API 실패 시 빈 리스트 반환.
    """
    url = f"{LB_BASE}/explore/lb-radio"
    headers = {"Authorization": f"Token {token}"}
    params = {"prompt": prompt, "mode": mode}
    try:
        log.info("fetching lb-radio", prompt=prompt, mode=mode)
        resp = requests.get(url, headers=headers, params=params, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        tracks = (
            data.get("payload", {})
            .get("jspf", {})
            .get("playlist", {})
            .get("tracks", [])
        )
        results = []
        for t in tracks:
            identifier = t.get("identifier", "")
            if not identifier:
                continue
            mbid = identifier.rstrip("/").split("/")[-1]
            artist = t.get("creator", "")
            title = t.get("title", "")
            if not artist or not title:
                meta = lookup_recording(mbid)
                artist = artist or meta.get("artist", "")
                title = title or meta.get("title", "")
            if not artist or not title:
                log.warning("skipping radio track: missing metadata", mbid=mbid)
                continue
            results.append({"mbid": mbid, "artist": artist, "track_name": title})
        log.info("lb-radio tracks fetched", count=len(results))
        return results
    except Exception as exc:
        log.warning("failed to fetch lb-radio", error=str(exc))
        return []
