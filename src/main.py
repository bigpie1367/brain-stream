import random
import threading
import time

import uvicorn

import src.api as api_module
import src.worker as worker_module
from src.config import load_config
from src.pipeline.listenbrainz import (
    fetch_lb_radio,
    fetch_recommendations,
    fetch_user_top_artists,
)
from src.pipeline.musicbrainz import lookup_recording
from src.state import (
    get_download_by_mbid,
    get_pending_jobs,
    get_retryable,
    get_setting,
    init_db,
    is_downloaded,
    mark_failed,
    mark_pending,
    set_setting,
)
from src.utils.logger import get_logger, setup_logger

log = get_logger(__name__)


def run_pipeline(cfg):
    log.info("pipeline started")
    db = cfg.state_db
    target = cfg.listenbrainz.recommendation_count  # default 25
    cf_target = round(target * 0.8)
    radio_target = target - cf_target
    cf_exhausted = False

    # ── 1. CF 추천 (취향 80%) ──
    cf_tracks = []
    try:
        offset = int(get_setting(db, "cf_offset", "0"))
        cf_tracks = fetch_recommendations(
            cfg.listenbrainz.username,
            cfg.listenbrainz.token,
            count=cf_target,
            offset=offset,
        )
        if cf_tracks:
            # 모델 갱신 감지
            new_first = cf_tracks[0]["mbid"]
            old_first = get_setting(db, "cf_first_mbid", "")
            if old_first and new_first != old_first and offset > 0:
                log.info("CF model refreshed, resetting offset")
                offset = 0
                cf_tracks = fetch_recommendations(
                    cfg.listenbrainz.username,
                    cfg.listenbrainz.token,
                    count=cf_target,
                    offset=0,
                )
                new_first = cf_tracks[0]["mbid"] if cf_tracks else ""
            if cf_tracks:
                set_setting(db, "cf_first_mbid", new_first)
                set_setting(db, "cf_offset", str(offset + len(cf_tracks)))
            else:
                cf_exhausted = True
                radio_target += cf_target
        else:
            cf_exhausted = True
            radio_target += cf_target
            log.info("CF pool exhausted, shifting target to radio")
    except Exception as exc:
        log.error("CF fetch failed, proceeding with radio only", error=str(exc))
        cf_exhausted = True
        radio_target += cf_target

    # ── 2. LB Radio (탐색 20%) ──
    radio_tracks = []
    try:
        top_artists = fetch_user_top_artists(
            cfg.listenbrainz.username, range_="quarter", count=10
        )
        if not top_artists:
            top_artists = fetch_user_top_artists(
                cfg.listenbrainz.username, range_="all_time", count=10
            )
        if top_artists:
            seed = random.choice(top_artists)
            prompt = f"artist:({seed['artist_name']})"
            log.info("lb-radio seed artist", artist=seed["artist_name"])
            radio_tracks = fetch_lb_radio(prompt, mode="easy")
            radio_tracks = radio_tracks[:radio_target]
    except Exception as exc:
        log.warning("radio fetch failed", error=str(exc))

    # Radio 실패 시 CF 폴백 (CF가 소진되지 않은 경우에만)
    if not radio_tracks and not cf_exhausted:
        cur_offset = int(get_setting(db, "cf_offset", "0"))
        extra = fetch_recommendations(
            cfg.listenbrainz.username,
            cfg.listenbrainz.token,
            count=radio_target,
            offset=cur_offset,
        )
        radio_tracks = extra
        if extra:
            set_setting(db, "cf_offset", str(cur_offset + len(extra)))

    # ── 3. 중복 필터링 ──
    seen = set()
    unique = []
    for t in cf_tracks + radio_tracks:
        if t["mbid"] not in seen:
            seen.add(t["mbid"])
            unique.append(t)
    new_tracks = [t for t in unique if not is_downloaded(db, t["mbid"])]
    log.info("tracks to process", new=len(new_tracks), total=len(unique))

    # ── 4. 재시도 큐 추가 ──
    retryable = get_retryable(db)
    if retryable:
        log.info("retrying failed tracks", count=len(retryable))
        new_tracks = retryable + new_tracks

    if not new_tracks:
        log.info("nothing new to download")
        return

    # ── 5. enqueue ──
    for track in new_tracks:
        mbid = track["mbid"]
        artist = track.get("artist", "")
        track_name = track.get("track_name", "")

        if (not artist or not track_name) and not mbid.startswith("manual-"):
            log.info("retry track missing metadata, re-looking up from MB", mbid=mbid)
            meta = lookup_recording(mbid)
            artist = artist or meta.get("artist", "")
            track_name = track_name or meta.get("title", "")
            if not artist or not track_name:
                log.warning("MB lookup still empty, skipping", mbid=mbid)
                mark_failed(db, mbid, "MB lookup returned empty artist/track")
                continue

        mark_pending(db, mbid, track_name, artist)
        worker_module.enqueue_job(
            job_id=mbid,
            artist=artist,
            track=track_name,
            source="listenbrainz",
        )

    log.info("pipeline finished — jobs enqueued")


def _run_scheduler(cfg):
    last_run = time.time()  # 초기 실행은 별도 스레드에서 이미 수행
    default_interval = str(cfg.scheduler.interval_hours)
    while not worker_module._shutdown_event.is_set():
        worker_module._shutdown_event.wait(60)
        try:
            interval = int(
                get_setting(cfg.state_db, "pipeline_interval_hours", default_interval)
            )
        except (ValueError, TypeError):
            interval = cfg.scheduler.interval_hours
        if time.time() - last_run >= interval * 3600:
            try:
                run_pipeline(cfg)
            except Exception:
                log.exception("pipeline run failed in scheduler")
            last_run = time.time()


def _reload_pending_jobs(cfg):
    """DB에서 status='pending'/'downloading' 잡을 큐에 재적재 (재시작 복구)."""
    rows = get_pending_jobs(cfg.state_db)
    requeued = 0
    skipped = 0
    for row in rows:
        if row["status"] == "downloading":
            # 크래시로 중단된 잡 — attempts 증가 후 max_attempts 초과 여부 확인
            mark_failed(cfg.state_db, row["mbid"], "interrupted by restart")
            updated = get_download_by_mbid(cfg.state_db, row["mbid"])
            if updated and updated["attempts"] >= 3:
                log.warning("job exceeded max attempts, skipping", job_id=row["mbid"])
                skipped += 1
                continue
        worker_module.enqueue_job(
            job_id=row["mbid"],
            artist=row["artist"],
            track=row["track_name"],
            source=row.get("source", "listenbrainz"),
        )
        requeued += 1
    if requeued or skipped:
        log.info("pending jobs reloaded", requeued=requeued, skipped=skipped)


def main():
    cfg = load_config()
    setup_logger(cfg.log_level, cfg.log_file)

    log.info("music-bot starting", interval_hours=cfg.scheduler.interval_hours)
    init_db(cfg.state_db)

    # Inject config into API module
    api_module._cfg = cfg

    # Worker thread (single, sequential — non-daemon for graceful shutdown)
    from src.jobs import run_download_job

    worker_thread = threading.Thread(
        target=worker_module.worker_loop,
        args=(cfg, run_download_job),
        daemon=False,
        name="worker",
    )
    worker_thread.start()

    def _shutdown_worker():
        log.info("shutdown: signaling worker to stop")
        worker_module._shutdown_event.set()
        worker_thread.join(timeout=30)
        if worker_thread.is_alive():
            log.warning("shutdown: worker did not stop within 30s, proceeding")
        else:
            log.info("shutdown: worker stopped cleanly")
        # Clean up yt-dlp thread pool
        from src.pipeline.downloader import _yt_executor

        _yt_executor.shutdown(wait=False)

    # Reload interrupted jobs from previous run
    _reload_pending_jobs(cfg)

    # Initial pipeline run (background)
    threading.Thread(target=run_pipeline, args=(cfg,), daemon=True).start()

    # Scheduler (background)
    threading.Thread(target=_run_scheduler, args=(cfg,), daemon=True).start()

    # uvicorn on main thread (blocking)
    try:
        uvicorn.run(api_module.app, host="0.0.0.0", port=8000, log_level="warning")
    finally:
        _shutdown_worker()


if __name__ == "__main__":
    main()
