import os
import time
from pathlib import Path
from typing import Optional

import requests
import yt_dlp

from src.utils.logger import get_logger

log = get_logger(__name__)

_MB_API = "https://musicbrainz.org/ws/2"
_MB_HEADERS = {"User-Agent": "music-bot/1.0 (https://github.com/music-bot)"}
_DURATION_WARN_THRESHOLD = 90  # seconds


def _mb_recording_duration(artist: str, track_name: str) -> Optional[float]:
    """Search MusicBrainz for a recording and return expected duration in seconds.

    Returns None on failure or when duration is not available.
    """
    try:
        time.sleep(1)  # rate limit
        query = f"artist:{artist} AND recording:{track_name}"
        r = requests.get(
            f"{_MB_API}/recording",
            params={"query": query, "fmt": "json", "limit": 5},
            headers=_MB_HEADERS,
            timeout=10,
        )
        r.raise_for_status()
        recordings = r.json().get("recordings", [])
        if not recordings:
            return None
        length_ms = recordings[0].get("length")
        if length_ms is None:
            return None
        duration_sec = length_ms / 1000.0
        log.info(
            "MB duration fetched",
            artist=artist,
            track=track_name,
            duration_sec=duration_sec,
        )
        return duration_sec
    except Exception as exc:
        log.warning("MB duration lookup failed", artist=artist, track=track_name, error=str(exc))
        return None


def _flac_opts(output_template: str) -> dict:
    return {
        "format": "bestaudio[ext=flac]/bestaudio[acodec=flac]/bestaudio",
        "outtmpl": output_template,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "flac",
            }
        ],
        "quiet": True,
        "no_warnings": True,
    }


def _opus_opts(output_template: str) -> dict:
    return {
        "format": "bestaudio[ext=webm]/bestaudio",
        "outtmpl": output_template,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "opus",
            }
        ],
        "quiet": True,
        "no_warnings": True,
    }


def _select_best_entry(entries: list[dict], mb_duration: Optional[float]) -> dict:
    """Select the best YouTube entry based on proximity to MB duration.

    If mb_duration is None, returns the first entry.
    """
    if not entries:
        raise ValueError("entries list is empty")
    if mb_duration is None:
        return entries[0]
    best = min(entries, key=lambda e: abs((e.get("duration") or 0) - mb_duration))
    return best


def download_track(
    mbid: str,
    artist: str,
    track_name: str,
    staging_dir: str,
    prefer_flac: bool = True,
) -> tuple[Optional[str], Optional[dict]]:
    os.makedirs(staging_dir, exist_ok=True)
    output_template = str(Path(staging_dir) / f"{mbid}.%(ext)s")

    log.info("downloading", mbid=mbid, artist=artist, track=track_name)

    mb_duration = _mb_recording_duration(artist, track_name)

    # Step 1: fetch metadata for top 5 results without downloading to pick the best entry
    search_query = f"ytsearch5:{artist} {track_name}"
    selected_entry = None
    try:
        meta_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
            "skip_download": True,
        }
        with yt_dlp.YoutubeDL(meta_opts) as ydl:
            info = ydl.extract_info(search_query, download=False)
            if info:
                entries = info.get("entries") or [info]
                entries = [e for e in entries if e]
                if entries:
                    selected_entry = _select_best_entry(entries, mb_duration)
                    yt_dur = selected_entry.get("duration")
                    log.info(
                        "selected YouTube result",
                        title=selected_entry.get("title", ""),
                        yt_duration=yt_dur,
                        mb_duration=mb_duration,
                    )
                    if mb_duration is not None and yt_dur is not None:
                        diff = abs(yt_dur - mb_duration)
                        if diff > _DURATION_WARN_THRESHOLD:
                            log.warning(
                                "YouTube duration deviates significantly from MB duration",
                                yt_duration=yt_dur,
                                mb_duration=mb_duration,
                                diff_seconds=diff,
                                artist=artist,
                                track=track_name,
                            )
    except Exception as exc:
        log.warning("metadata fetch failed, falling back to direct download", error=str(exc))

    # Build the actual download URL: use selected entry URL, or fall back to ytsearch1
    if selected_entry and selected_entry.get("webpage_url"):
        download_target = selected_entry["webpage_url"]
    elif selected_entry and selected_entry.get("url"):
        download_target = selected_entry["url"]
    else:
        download_target = f"ytsearch1:{artist} {track_name}"

    opts_list = [_flac_opts(output_template), _opus_opts(output_template)] if prefer_flac \
        else [_opus_opts(output_template)]

    for opts in opts_list:
        try:
            yt_metadata = None
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(download_target, download=True)
                if info:
                    entry = info.get("entries", [info])[0] if "entries" in info else info
                    thumbnail_url = entry.get("thumbnail", "")
                    channel = entry.get("channel") or entry.get("uploader", "")
                    if thumbnail_url or channel:
                        yt_metadata = {"thumbnail_url": thumbnail_url, "channel": channel}

            # Find the downloaded file
            for ext in ("flac", "opus", "webm", "m4a", "mp3"):
                candidate = Path(staging_dir) / f"{mbid}.{ext}"
                if candidate.exists():
                    log.info("download complete", file=str(candidate))
                    return str(candidate), yt_metadata

        except yt_dlp.utils.DownloadError as exc:
            log.warning("download attempt failed", error=str(exc), opts=opts.get("format"))
            continue

    log.error("all download attempts failed", mbid=mbid)
    return None, None
