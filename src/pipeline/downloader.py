import os
from pathlib import Path
from typing import Optional

import yt_dlp

from src.utils.logger import get_logger

log = get_logger(__name__)


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


def download_track(
    mbid: str,
    artist: str,
    track_name: str,
    staging_dir: str,
    prefer_flac: bool = True,
) -> tuple[Optional[str], Optional[dict]]:
    os.makedirs(staging_dir, exist_ok=True)
    query = f"ytsearch1:{artist} {track_name}"
    output_template = str(Path(staging_dir) / f"{mbid}.%(ext)s")

    log.info("downloading", mbid=mbid, artist=artist, track=track_name)

    opts_list = [_flac_opts(output_template), _opus_opts(output_template)] if prefer_flac \
        else [_opus_opts(output_template)]

    for opts in opts_list:
        try:
            yt_metadata = None
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(query, download=True)
                if info:
                    # ytsearch1 returns a playlist-like dict with 'entries'
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
