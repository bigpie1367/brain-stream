import threading
import time

import schedule
import uvicorn

import src.api as api_module
from src.config import load_config
from src.pipeline.downloader import download_track
from src.pipeline.listenbrainz import _lookup_recording, fetch_recommendations
from src.pipeline.navidrome import trigger_scan, wait_for_scan
from src.pipeline.tagger import tag_and_import
from src.state import get_retryable, init_db, is_downloaded, mark_done, mark_failed, mark_pending, update_track_info
from src.utils.logger import get_logger, setup_logger

log = get_logger(__name__)


def run_pipeline(cfg):
    log.info("pipeline started")

    # 1. Fetch recommendations
    try:
        tracks = fetch_recommendations(
            cfg.listenbrainz.username,
            cfg.listenbrainz.token,
            cfg.listenbrainz.recommendation_count,
        )
    except Exception as exc:
        log.error("failed to fetch recommendations", error=str(exc))
        return

    # 2. Filter already downloaded
    new_tracks = [t for t in tracks if not is_downloaded(cfg.state_db, t["mbid"])]
    log.info("tracks to process", new=len(new_tracks), total=len(tracks))

    # 3. Add retryable failures
    retryable = get_retryable(cfg.state_db)
    if retryable:
        log.info("retrying failed tracks", count=len(retryable))
        new_tracks = retryable + new_tracks

    if not new_tracks:
        log.info("nothing new to download")
        return

    imported_any = False
    for track in new_tracks:
        mbid = track["mbid"]
        artist = track.get("artist", "")
        track_name = track.get("track_name", "")

        # Retry tracks from state.db may have empty artist/track_name if the original
        # LB lookup failed. Re-lookup from MB if mbid is a real UUID (not "manual-").
        if (not artist or not track_name) and not mbid.startswith("manual-"):
            log.info("retry track missing artist/track, re-looking up from MB", mbid=mbid)
            meta = _lookup_recording(mbid)
            artist = meta.get("artist", "")
            track_name = meta.get("track_name", "")
            if not artist or not track_name:
                log.warning("MB lookup still empty after retry, skipping track", mbid=mbid)
                mark_failed(cfg.state_db, mbid, "MB lookup returned empty artist/track")
                continue

        mark_pending(cfg.state_db, mbid, track_name, artist)

        # 4. Download
        file_path, yt_metadata = download_track(
            mbid=mbid,
            artist=artist,
            track_name=track_name,
            staging_dir=cfg.download.staging_dir,
            prefer_flac=cfg.download.prefer_flac,
        )
        if not file_path:
            mark_failed(cfg.state_db, mbid, "download failed")
            continue

        # 5. Tag + import
        success, dest_path, canonical_artist, canonical_title = tag_and_import(
            file_path, cfg.beets.music_dir, artist=artist, track_name=track_name, yt_metadata=yt_metadata,
            db_path=cfg.state_db, mbid=mbid,
        )
        if success:
            mark_done(cfg.state_db, mbid, file_path=dest_path)
            if canonical_artist or canonical_title:
                update_track_info(
                    cfg.state_db,
                    mbid,
                    artist=canonical_artist if canonical_artist else None,
                    track_name=canonical_title if canonical_title else None,
                )
            imported_any = True
        else:
            mark_failed(cfg.state_db, mbid, "tagging failed")

    # 6. Trigger Navidrome scan if anything was imported
    if imported_any:
        if trigger_scan(cfg.navidrome.url, cfg.navidrome.username, cfg.navidrome.password):
            wait_for_scan(cfg.navidrome.url, cfg.navidrome.username, cfg.navidrome.password)

    log.info("pipeline finished")


def _run_scheduler(cfg):
    schedule.every(cfg.scheduler.interval_hours).hours.do(run_pipeline, cfg)
    while True:
        schedule.run_pending()
        time.sleep(60)


def main():
    cfg = load_config()
    setup_logger(cfg.log_level, cfg.log_file)

    log.info("music-bot starting", interval_hours=cfg.scheduler.interval_hours)
    init_db(cfg.state_db)

    # Inject config into API module
    api_module._cfg = cfg

    # Initial pipeline run (background)
    threading.Thread(target=run_pipeline, args=(cfg,), daemon=True).start()

    # Scheduler (background)
    threading.Thread(target=_run_scheduler, args=(cfg,), daemon=True).start()

    # uvicorn on main thread (blocking)
    uvicorn.run(api_module.app, host="0.0.0.0", port=8000, log_level="warning")


if __name__ == "__main__":
    main()
