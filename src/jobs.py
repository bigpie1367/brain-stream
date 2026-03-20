"""Download job execution logic — extracted from api.py."""

import glob as _glob
import os

import src.worker as worker
from src.pipeline.downloader import download_track, download_track_by_id
from src.pipeline.navidrome import trigger_scan, wait_for_scan
from src.pipeline.tagger import tag_and_import
from src.state import (
    get_download_by_mbid,
    mark_done,
    mark_downloading,
    mark_failed,
    update_track_info,
)
from src.utils.logger import get_logger

log = get_logger(__name__)


def run_download_job(cfg, job_spec: dict):
    """Execute a download+tag+scan job. Called by worker_loop."""
    job_id = job_spec["job_id"]
    artist = job_spec["artist"]
    track = job_spec["track"]
    video_id = job_spec.get("video_id")
    mbid = job_id  # use job_id as the unique key in the DB

    try:
        # Fix 3: copy2 완료 후 mark_done 직전 크래시 대응
        # file_path가 이미 기록되어 있고 파일도 존재하면 다운로드/태깅 스킵
        existing = get_download_by_mbid(cfg.state_db, mbid)
        if (
            existing
            and existing.get("file_path")
            and os.path.exists(existing["file_path"])
        ):
            log.info(
                "file already exists, skipping download",
                mbid=mbid,
                path=existing["file_path"],
            )
            mark_done(
                cfg.state_db, mbid, existing["file_path"], album=existing.get("album")
            )
            worker.emit(job_id, "scanning", "Navidrome 스캔 중...")
            if trigger_scan(
                cfg.navidrome.url, cfg.navidrome.username, cfg.navidrome.password
            ):
                wait_for_scan(
                    cfg.navidrome.url, cfg.navidrome.username, cfg.navidrome.password
                )
            worker.emit(job_id, "done", "완료")
            return

        # Fix 1: 잡 시작 전 staging 잔류 파일 정리 (.part, .flac, .opus 등)
        for leftover in _glob.glob(os.path.join(cfg.download.staging_dir, f"{mbid}*")):
            try:
                os.remove(leftover)
                log.info("removed leftover staging file", path=leftover)
            except OSError:
                pass

        worker.emit(job_id, "downloading", "YouTube 검색 중...")
        mark_downloading(cfg.state_db, mbid)

        if video_id:
            file_path, yt_metadata = download_track_by_id(
                video_id=video_id,
                mbid=mbid,
                staging_dir=cfg.download.staging_dir,
            )
        else:
            file_path, yt_metadata = download_track(
                mbid=mbid,
                artist=artist,
                track_name=track,
                staging_dir=cfg.download.staging_dir,
                prefer_flac=cfg.download.prefer_flac,
            )
        if not file_path:
            mark_failed(cfg.state_db, mbid, "download failed")
            worker.emit(job_id, "failed", "다운로드 실패")
            return

        worker.emit(job_id, "tagging", "태깅 중...")
        (
            success,
            dest_path,
            canonical_artist,
            canonical_title,
            canonical_album,
            mb_recording_id,
        ) = tag_and_import(
            file_path,
            cfg.beets.music_dir,
            artist=artist,
            track_name=track,
            yt_metadata=yt_metadata,
            db_path=cfg.state_db,
            mbid=mbid,
        )
        if not success:
            mark_failed(cfg.state_db, mbid, "tagging failed")
            worker.emit(job_id, "failed", "태깅 실패")
            return

        mark_done(
            cfg.state_db,
            mbid,
            file_path=dest_path,
            album=canonical_album if canonical_album else None,
        )

        # LB 트랙은 mbid 자체가 MB recording UUID이므로 tagger 반환값 대신 mbid 우선 사용
        final_mb_recording_id = (
            mbid if not mbid.startswith("manual-") else mb_recording_id
        )

        if (
            canonical_artist
            or canonical_title
            or canonical_album
            or final_mb_recording_id
        ):
            update_track_info(
                cfg.state_db,
                mbid,
                artist=canonical_artist if canonical_artist else None,
                track_name=canonical_title if canonical_title else None,
                album=canonical_album if canonical_album else None,
                mb_recording_id=final_mb_recording_id
                if final_mb_recording_id
                else None,
            )

        worker.emit(job_id, "scanning", "Navidrome 스캔 중...")
        if trigger_scan(
            cfg.navidrome.url, cfg.navidrome.username, cfg.navidrome.password
        ):
            wait_for_scan(
                cfg.navidrome.url, cfg.navidrome.username, cfg.navidrome.password
            )

        worker.emit(job_id, "done", "완료")

    except Exception as exc:
        log.error("manual download job failed", job_id=job_id, error=str(exc))
        try:
            mark_failed(cfg.state_db, mbid, str(exc))
        except Exception:
            pass
        worker.emit(job_id, "failed", f"오류: {exc}")
