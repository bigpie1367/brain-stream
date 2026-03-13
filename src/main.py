import threading
import time

import schedule
import uvicorn

import src.api as api_module
import src.worker as worker_module
from src.config import load_config
from src.pipeline.listenbrainz import _lookup_recording, fetch_recommendations
from src.state import get_all_downloads, get_pending_jobs, get_retryable, init_db, is_downloaded, mark_failed, mark_pending
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
        worker_module.enqueue_job(
            job_id=mbid,
            artist=artist,
            track=track_name,
            source="listenbrainz",
        )

    log.info("pipeline finished — jobs enqueued")


def _run_scheduler(cfg):
    schedule.every(cfg.scheduler.interval_hours).hours.do(run_pipeline, cfg)
    while True:
        schedule.run_pending()
        time.sleep(60)


def _reload_pending_jobs(cfg):
    """DB에서 status='pending'/'downloading' 잡을 큐에 재적재 (재시작 복구)."""
    rows = get_pending_jobs(cfg.state_db)
    for row in rows:
        worker_module.enqueue_job(
            job_id=row["mbid"],
            artist=row["artist"],
            track=row["track_name"],
            source=row.get("source", "listenbrainz"),
        )
    if rows:
        log.info("pending jobs reloaded", count=len(rows))


def main():
    cfg = load_config()
    setup_logger(cfg.log_level, cfg.log_file)

    log.info("music-bot starting", interval_hours=cfg.scheduler.interval_hours)
    init_db(cfg.state_db)

    # Inject config into API module
    api_module._cfg = cfg

    # Worker thread (single, sequential)
    from src.api import _run_download_job
    threading.Thread(
        target=worker_module.worker_loop,
        args=(cfg, _run_download_job),
        daemon=True,
        name="worker",
    ).start()

    # Reload interrupted jobs from previous run
    _reload_pending_jobs(cfg)

    # Initial pipeline run (background)
    threading.Thread(target=run_pipeline, args=(cfg,), daemon=True).start()

    # Scheduler (background)
    threading.Thread(target=_run_scheduler, args=(cfg,), daemon=True).start()

    # uvicorn on main thread (blocking)
    uvicorn.run(api_module.app, host="0.0.0.0", port=8000, log_level="warning")


if __name__ == "__main__":
    main()
