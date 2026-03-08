import os
import re
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


_LIVE_KEYWORDS = re.compile(
    r"\b(live|concert|tour|festival|acoustic\s+version|unplugged)\b",
    re.IGNORECASE,
)


def _is_live(title: str) -> bool:
    """Return True if the title contains a live-performance keyword (word-boundary match)."""
    return bool(_LIVE_KEYWORDS.search(title))


def _select_best_entry(entries: list[dict], mb_duration: Optional[float]) -> dict:
    """Select the best YouTube entry based on proximity to MB duration.

    Live-performance entries (title contains live/concert/tour/festival/
    acoustic version/unplugged at word boundaries) are penalised so that
    studio recordings are preferred.  If every candidate is a live entry,
    the least-bad live entry is returned rather than failing.

    If mb_duration is None, non-live entries are preferred; among ties the
    first entry in the list wins.
    """
    if not entries:
        raise ValueError("entries list is empty")

    non_live = [e for e in entries if not _is_live(e.get("title") or "")]
    live_only = not non_live

    candidates = entries if live_only else non_live

    if mb_duration is None:
        return candidates[0]

    best = min(candidates, key=lambda e: abs((e.get("duration") or 0) - mb_duration))
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
    entries: list[dict] = []
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
                raw_entries = info.get("entries") or [info]
                entries = [e for e in raw_entries if e]
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

    _BLOCKED_KEYWORDS = ("payment", "members-only", "members only", "private", "unavailable")

    def _is_blocked_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        return any(kw in msg for kw in _BLOCKED_KEYWORDS)

    def _entry_url(entry: dict) -> Optional[str]:
        return entry.get("webpage_url") or entry.get("url")

    # Try entries one by one (skip blocked videos), fall back to ytsearch1 if all fail
    remaining_entries = list(entries) if entries else []
    download_target: Optional[str] = None
    attempted_urls: list[str] = []

    opts_list = [_flac_opts(output_template), _opus_opts(output_template)] if prefer_flac \
        else [_opus_opts(output_template)]

    while True:
        # Pick next candidate from remaining entries
        if remaining_entries:
            candidate_entry = _select_best_entry(remaining_entries, mb_duration)
            url = _entry_url(candidate_entry)
            if not url or url in attempted_urls:
                remaining_entries = [e for e in remaining_entries if e is not candidate_entry]
                continue
            download_target = url
        else:
            # All entries exhausted (or no entries from the start) — final fallback
            download_target = f"ytsearch1:{artist} {track_name}"

        attempted_urls.append(download_target)

        blocked = False
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
                    candidate_file = Path(staging_dir) / f"{mbid}.{ext}"
                    if candidate_file.exists():
                        log.info("download complete", file=str(candidate_file))
                        return str(candidate_file), yt_metadata

            except yt_dlp.utils.DownloadError as exc:
                if _is_blocked_error(exc):
                    log.warning(
                        "video blocked (payment/private/members-only), trying next candidate",
                        url=download_target,
                        error=str(exc),
                    )
                    blocked = True
                    break  # skip remaining opts, move to next entry
                log.warning("download attempt failed", error=str(exc), opts=opts.get("format"))
                continue

        if blocked:
            # Remove the blocked entry and retry with next candidate
            remaining_entries = [e for e in remaining_entries if _entry_url(e) != download_target]
            if not remaining_entries and download_target == f"ytsearch1:{artist} {track_name}":
                # ytsearch1 fallback also blocked — give up
                break
            continue

        # If we reach here without returning, all format opts failed for this target
        # and it was not a blocked error — break out to avoid infinite loop
        if download_target == f"ytsearch1:{artist} {track_name}":
            break
        remaining_entries = [e for e in remaining_entries if _entry_url(e) != download_target]
        if not remaining_entries:
            # No more entries — try ytsearch1 fallback once
            continue

    log.error("all download attempts failed", mbid=mbid)
    return None, None
