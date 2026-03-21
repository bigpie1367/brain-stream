import os
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
class MusicDirConfig:
    music_dir: str = "/app/data/music"


@dataclass
class NavidromeConfig:
    url: str = "http://navidrome:4533/navidrome"
    username: str = "admin"
    password: str = ""


@dataclass
class SchedulerConfig:
    interval_hours: int = 6


@dataclass
class AppConfig:
    listenbrainz: ListenBrainzConfig
    download: DownloadConfig = field(default_factory=DownloadConfig)
    beets: MusicDirConfig = field(default_factory=MusicDirConfig)
    navidrome: NavidromeConfig = field(default_factory=NavidromeConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    state_db: str = "/app/db/state.db"
    log_level: str = "INFO"
    log_file: Optional[str] = "/app/data/logs/music-bot.log"


def load_config() -> AppConfig:
    listenbrainz = ListenBrainzConfig(
        username=os.environ.get("LB_USERNAME", ""),
        token=os.environ.get("LB_TOKEN", ""),
    )

    download = DownloadConfig()

    beets = MusicDirConfig()

    navidrome = NavidromeConfig(
        url=os.environ.get("NAVIDROME_URL", "http://navidrome:4533/navidrome"),
        username=os.environ.get("NAVIDROME_USER", "admin"),
        password=os.environ.get("NAVIDROME_PASSWORD", ""),
    )

    scheduler = SchedulerConfig()

    return AppConfig(
        listenbrainz=listenbrainz,
        download=download,
        beets=beets,
        navidrome=navidrome,
        scheduler=scheduler,
    )
