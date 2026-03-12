import difflib
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

_COVER_KEYWORDS = re.compile(
    r"\b(cover|remix|karaoke|instrumental|rendition|tribute|parody"
    r"|8[- ]?bit|piano\s+version|violin\s+version)\b",
    re.IGNORECASE,
)


def _is_live(title: str) -> bool:
    """Return True if the title contains a live-performance keyword."""
    return bool(_LIVE_KEYWORDS.search(title))


def _is_cover(title: str) -> bool:
    """Return True if the title suggests a cover/remix/karaoke version."""
    return bool(_COVER_KEYWORDS.search(title))


def _normalize(s: str) -> str:
    return "".join(c for c in s.lower() if c.isalnum() or c.isspace()).strip()


def _channel_score(entry: dict, artist: str) -> float:
    """Return a bonus score (negative = better) for official channels."""
    channel = (entry.get("channel") or entry.get("uploader") or "").lower()
    norm_artist = _normalize(artist)

    # Artist's official channel or VEVO
    if (
        norm_artist
        and difflib.SequenceMatcher(None, norm_artist, _normalize(channel)).ratio() >= 0.8
    ):
        return -200
    if "vevo" in channel:
        return -150
    # YouTube "Topic" auto-generated channels (e.g. "Eminem - Topic")
    if "topic" in channel:
        return -100
    return 0


def _select_best_entry(
    entries: list[dict],
    mb_duration: Optional[float],
    artist: str = "",
    track_name: str = "",
) -> dict:
    """Select the best YouTube entry using a scoring system.

    Scoring factors (lower = better):
    - Cover/remix/karaoke title: +1000 penalty (skipped if user explicitly
      searched for a cover/remix in track_name)
    - Live performance title: +500 penalty
    - Official channel (artist/VEVO/Topic): -200 to -100 bonus
    - Duration proximity to MB: abs difference in seconds
    """
    if not entries:
        raise ValueError("entries list is empty")

    # If user explicitly wants a cover/remix, don't penalize those
    user_wants_cover = _is_cover(track_name)

    def score(e: dict) -> float:
        s = 0.0
        title = e.get("title") or ""
        if not user_wants_cover and _is_cover(title):
            s += 1000
        if _is_live(title):
            s += 500
        s += _channel_score(e, artist)
        if mb_duration is not None:
            s += abs((e.get("duration") or 0) - mb_duration)
        return s

    return min(entries, key=score)


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
    search_query = f"ytsearch5:{artist} {track_name} official audio"
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
                    selected_entry = _select_best_entry(entries, mb_duration, artist, track_name)
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

    opts_list = (
        [_flac_opts(output_template), _opus_opts(output_template)]
        if prefer_flac
        else [_opus_opts(output_template)]
    )

    while True:
        # Pick next candidate from remaining entries
        if remaining_entries:
            candidate_entry = _select_best_entry(
                remaining_entries, mb_duration, artist, track_name
            )
            url = _entry_url(candidate_entry)
            if not url or url in attempted_urls:
                remaining_entries = [e for e in remaining_entries if e is not candidate_entry]
                continue
            download_target = url
        else:
            # All entries exhausted (or no entries from the start) — final fallback
            download_target = f"ytsearch1:{artist} {track_name} official audio"

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
            if (
                not remaining_entries
                and download_target == f"ytsearch1:{artist} {track_name} official audio"
            ):
                # ytsearch1 fallback also blocked — give up
                break
            continue

        # If we reach here without returning, all format opts failed for this target
        # and it was not a blocked error — break out to avoid infinite loop
        if download_target == f"ytsearch1:{artist} {track_name} official audio":
            break
        remaining_entries = [e for e in remaining_entries if _entry_url(e) != download_target]
        if not remaining_entries:
            # No more entries — try ytsearch1 fallback once
            continue

    log.error("all download attempts failed", mbid=mbid)
    return None, None


def search_candidates(artist: str, track_name: str) -> list[dict]:
    """YouTube 후보 5개를 검색하고 메타데이터만 반환 (다운로드 없음).

    반환: [
        {
            "video_id": str,
            "title": str,
            "channel": str,
            "duration": int,  # seconds
            "thumbnail_url": str,
            "url": str,  # https://www.youtube.com/watch?v={video_id}
            "is_live": bool,
            "is_cover": bool,
        },
        ...
    ]
    """
    query = f"ytsearch5:{artist} {track_name}"
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(query, download=False)
            if not info:
                return []
            raw_entries = info.get("entries") or [info]
            entries = [e for e in raw_entries if e]
            results = []
            for entry in entries:
                video_id = entry.get("id") or ""
                title = entry.get("title") or ""
                channel = entry.get("channel") or entry.get("uploader") or ""
                duration = entry.get("duration") or 0
                thumbnail_url = entry.get("thumbnail") or ""
                url = f"https://www.youtube.com/watch?v={video_id}" if video_id else ""
                results.append(
                    {
                        "video_id": video_id,
                        "title": title,
                        "channel": channel,
                        "duration": int(duration),
                        "thumbnail_url": thumbnail_url,
                        "url": url,
                        "is_live": _is_live(title),
                        "is_cover": _is_cover(title),
                    }
                )
            return results
    except Exception as exc:
        log.warning(
            "search_candidates failed", artist=artist, track=track_name, error=str(exc)
        )
        return []


def download_track_by_id(video_id: str, mbid: str, staging_dir: str) -> tuple[str, dict]:
    """특정 YouTube video_id로 직접 다운로드.

    반환: (file_path, yt_metadata)
    file_path: staging_dir/{mbid}.flac 또는 .opus
    yt_metadata: {"thumbnail_url": str, "channel": str}
    """
    os.makedirs(staging_dir, exist_ok=True)
    url = f"https://www.youtube.com/watch?v={video_id}"
    output_template = str(Path(staging_dir) / f"{mbid}.%(ext)s")

    log.info("downloading by video_id", video_id=video_id, mbid=mbid)

    for opts in (_flac_opts(output_template), _opus_opts(output_template)):
        try:
            yt_metadata = None
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if info:
                    thumbnail_url = info.get("thumbnail", "")
                    channel = info.get("channel") or info.get("uploader", "")
                    if thumbnail_url or channel:
                        yt_metadata = {"thumbnail_url": thumbnail_url, "channel": channel}

            for ext in ("flac", "opus", "webm", "m4a", "mp3"):
                candidate_file = Path(staging_dir) / f"{mbid}.{ext}"
                if candidate_file.exists():
                    log.info("download_by_id complete", file=str(candidate_file))
                    return str(candidate_file), yt_metadata or {}

        except Exception as exc:
            log.warning(
                "download_track_by_id attempt failed",
                video_id=video_id,
                error=str(exc),
                fmt=opts.get("format"),
            )
            continue

    raise RuntimeError(f"download_track_by_id failed for video_id={video_id}")
