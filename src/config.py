import os
import yaml
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ListenBrainzConfig:
    username: str
    token: str
    recommendation_count: int = 25


@dataclass
class DownloadConfig:
    staging_dir: str = "/app/data/staging"
    prefer_flac: bool = True


@dataclass
class BeetsConfig:
    music_dir: str = "/app/data/music"


@dataclass
class NavidromeConfig:
    url: str = "http://navidrome:4533"
    username: str = "admin"
    password: str = ""


@dataclass
class SchedulerConfig:
    interval_hours: int = 6


@dataclass
class AppConfig:
    listenbrainz: ListenBrainzConfig
    download: DownloadConfig = field(default_factory=DownloadConfig)
    beets: BeetsConfig = field(default_factory=BeetsConfig)
    navidrome: NavidromeConfig = field(default_factory=NavidromeConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    state_db: str = "/app/data/state.db"
    log_level: str = "INFO"
    log_file: Optional[str] = "/app/data/logs/music-bot.log"


def load_config(path: str = "/app/config.yaml") -> AppConfig:
    with open(path, "r") as f:
        raw = yaml.safe_load(f)

    lb_raw = raw.get("listenbrainz", {})
    listenbrainz = ListenBrainzConfig(
        username=os.environ.get("LB_USERNAME", lb_raw.get("username", "")),
        token=os.environ.get("LB_TOKEN", lb_raw.get("token", "")),
        recommendation_count=lb_raw.get("recommendation_count", 25),
    )

    dl_raw = raw.get("download", {})
    download = DownloadConfig(
        staging_dir=dl_raw.get("staging_dir", "/app/data/staging"),
        prefer_flac=dl_raw.get("prefer_flac", True),
    )

    beets_raw = raw.get("beets", {})
    beets = BeetsConfig(
        music_dir=beets_raw.get("music_dir", "/app/data/music"),
    )

    nd_raw = raw.get("navidrome", {})
    navidrome = NavidromeConfig(
        url=nd_raw.get("url", "http://navidrome:4533"),
        username=os.environ.get("NAVIDROME_USER", nd_raw.get("username", "admin")),
        password=os.environ.get("NAVIDROME_PASSWORD", nd_raw.get("password", "")),
    )

    sched_raw = raw.get("scheduler", {})
    scheduler = SchedulerConfig(
        interval_hours=sched_raw.get("interval_hours", 6),
    )

    return AppConfig(
        listenbrainz=listenbrainz,
        download=download,
        beets=beets,
        navidrome=navidrome,
        scheduler=scheduler,
        state_db=raw.get("state_db", "/app/data/state.db"),
        log_level=raw.get("log_level", "INFO"),
        log_file=raw.get("log_file", "/app/data/logs/music-bot.log"),
    )
