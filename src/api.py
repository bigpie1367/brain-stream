import json
import threading
import uuid
from queue import Queue, Empty
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.state import (
    mark_pending, mark_downloading, mark_done, mark_failed, get_all_downloads
)
from src.pipeline.downloader import download_track
from src.pipeline.tagger import tag_and_import
from src.pipeline.navidrome import trigger_scan, wait_for_scan
from src.utils.logger import get_logger

log = get_logger(__name__)

app = FastAPI(title="Music Bot")
app.mount("/static", StaticFiles(directory="src/static"), name="static")

# Injected by main.py after config is loaded
_cfg = None

# job_id → Queue of SSE event dicts
_job_queues: dict[str, Queue] = {}


# ── Models ──────────────────────────────────────────────────────────────────

class DownloadRequest(BaseModel):
    artist: str
    track: str


# ── Helpers ─────────────────────────────────────────────────────────────────

def _emit(job_id: str, status: str, message: str):
    q = _job_queues.get(job_id)
    if q is not None:
        q.put({"status": status, "message": message})


def _run_download_job(job_id: str, artist: str, track: str):
    cfg = _cfg
    mbid = job_id  # use job_id as the unique key in the DB

    try:
        _emit(job_id, "downloading", "YouTube 검색 중...")
        mark_downloading(cfg.state_db, mbid)

        file_path = download_track(
            mbid=mbid,
            artist=artist,
            track_name=track,
            staging_dir=cfg.download.staging_dir,
            prefer_flac=cfg.download.prefer_flac,
        )
        if not file_path:
            mark_failed(cfg.state_db, mbid, "download failed")
            _emit(job_id, "failed", "다운로드 실패")
            return

        _emit(job_id, "tagging", "beets 태깅 중...")
        success = tag_and_import(file_path, cfg.beets.music_dir, artist=artist, track_name=track)
        if not success:
            mark_failed(cfg.state_db, mbid, "beets import failed")
            _emit(job_id, "failed", "태깅 실패")
            return

        mark_done(cfg.state_db, mbid)

        _emit(job_id, "scanning", "Navidrome 스캔 중...")
        if trigger_scan(cfg.navidrome.url, cfg.navidrome.username, cfg.navidrome.password):
            wait_for_scan(cfg.navidrome.url, cfg.navidrome.username, cfg.navidrome.password)

        _emit(job_id, "done", "완료")

    except Exception as exc:
        log.error("manual download job failed", job_id=job_id, error=str(exc))
        try:
            mark_failed(cfg.state_db, mbid, str(exc))
        except Exception:
            pass
        _emit(job_id, "failed", f"오류: {exc}")


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    with open("src/static/index.html", encoding="utf-8") as f:
        return f.read()


@app.post("/api/download")
async def start_download(req: DownloadRequest):
    if not _cfg:
        raise HTTPException(status_code=503, detail="config not loaded yet")

    job_id = "manual-" + uuid.uuid4().hex[:8]
    _job_queues[job_id] = Queue()

    mark_pending(
        _cfg.state_db,
        mbid=job_id,
        track_name=req.track,
        artist=req.artist,
        source="manual",
    )

    threading.Thread(
        target=_run_download_job,
        args=(job_id, req.artist, req.track),
        daemon=True,
    ).start()

    return {"job_id": job_id}


@app.get("/api/sse/{job_id}")
async def sse_stream(job_id: str):
    if job_id not in _job_queues:
        raise HTTPException(status_code=404, detail="job not found")

    def event_generator():
        q = _job_queues[job_id]
        while True:
            try:
                event = q.get(timeout=30)
            except Empty:
                # Send a keep-alive comment
                yield ": keep-alive\n\n"
                continue

            payload = json.dumps(event, ensure_ascii=False)
            yield f"data: {payload}\n\n"

            if event.get("status") in ("done", "failed"):
                # Clean up after a short delay to allow client to receive final event
                _job_queues.pop(job_id, None)
                break

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/downloads")
async def list_downloads():
    if not _cfg:
        raise HTTPException(status_code=503, detail="config not loaded yet")
    return get_all_downloads(_cfg.state_db)


@app.post("/api/pipeline/run")
async def trigger_pipeline():
    if not _cfg:
        raise HTTPException(status_code=503, detail="config not loaded yet")

    from src.main import run_pipeline
    threading.Thread(target=run_pipeline, args=(_cfg,), daemon=True).start()
    return {"status": "started"}
