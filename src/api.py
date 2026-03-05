import hashlib
import json
import secrets
import threading
import uuid
from queue import Queue, Empty
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
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


# ── Subsonic proxy ────────────────────────────────────────────────────────────

# hop-by-hop 헤더는 프록시 시 포워딩하지 않는다 (RFC 2616 §13.5.1)
_HOP_BY_HOP = frozenset([
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
])

# 응답에서 포워딩할 미디어 관련 헤더
_FORWARD_RESPONSE_HEADERS = frozenset([
    "content-type", "content-length", "content-range", "accept-ranges",
    "last-modified", "etag", "cache-control",
])


@app.api_route("/rest/{path:path}", methods=["GET", "POST", "HEAD"])
async def subsonic_proxy(path: str, request: Request):
    if not _cfg:
        raise HTTPException(status_code=503, detail="config not loaded yet")

    target_url = f"{_cfg.navidrome.url.rstrip('/')}/rest/{path}"

    # 포워딩할 요청 헤더 필터링 (hop-by-hop 제외)
    forward_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }

    try:
        async with httpx.AsyncClient() as client:
            async with client.stream(
                request.method,
                target_url,
                params=dict(request.query_params),
                headers=forward_headers,
                content=await request.body(),
                timeout=60.0,
            ) as upstream:
                # 포워딩할 응답 헤더만 추출
                response_headers = {
                    k: v for k, v in upstream.headers.items()
                    if k.lower() in _FORWARD_RESPONSE_HEADERS
                }

                async def generate():
                    async for chunk in upstream.aiter_bytes():
                        yield chunk

                return StreamingResponse(
                    generate(),
                    status_code=upstream.status_code,
                    headers=response_headers,
                )

    except httpx.ConnectError:
        log.error("subsonic proxy: navidrome connection failed", url=target_url)
        raise HTTPException(status_code=503, detail="navidrome unavailable")
    except httpx.TimeoutException:
        log.error("subsonic proxy: navidrome request timed out", url=target_url)
        raise HTTPException(status_code=503, detail="navidrome request timed out")


# 클라이언트에서 온 Subsonic 인증 파라미터 — brainstream이 자동 주입하므로 제거
_SUBSONIC_AUTH_PARAMS = frozenset(["u", "t", "s", "p"])


@app.get("/api/subsonic/{path:path}")
async def subsonic_authed_proxy(path: str, request: Request):
    """navidrome 인증(MD5 토큰)을 자동 주입하는 Subsonic API 프록시.
    프론트엔드는 navidrome 계정 정보 없이 이 엔드포인트만 사용하면 된다."""
    if not _cfg:
        raise HTTPException(status_code=503, detail="config not loaded yet")

    # MD5 인증 파라미터 생성
    salt = secrets.token_hex(6)
    token = hashlib.md5(f"{_cfg.navidrome.password}{salt}".encode()).hexdigest()
    auth_params = {
        "u": _cfg.navidrome.username,
        "t": token,
        "s": salt,
        "v": "1.16.1",
        "c": "brainstream",
        "f": "json",
    }

    # 클라이언트 쿼리 파라미터에서 인증 관련 키 제거 후 auth_params와 합산
    client_params = {
        k: v for k, v in request.query_params.items()
        if k.lower() not in _SUBSONIC_AUTH_PARAMS
    }
    # 클라이언트가 f(format)를 명시하면 덮어쓰기 허용; 아니면 json 기본값 사용
    merged_params = {**auth_params, **client_params}

    target_url = f"{_cfg.navidrome.url.rstrip('/')}/rest/{path}"

    try:
        async with httpx.AsyncClient() as client:
            async with client.stream(
                "GET",
                target_url,
                params=merged_params,
                timeout=60.0,
            ) as upstream:
                response_headers = {
                    k: v for k, v in upstream.headers.items()
                    if k.lower() in _FORWARD_RESPONSE_HEADERS
                }

                async def generate():
                    async for chunk in upstream.aiter_bytes():
                        yield chunk

                return StreamingResponse(
                    generate(),
                    status_code=upstream.status_code,
                    headers=response_headers,
                )

    except httpx.ConnectError:
        log.error("subsonic authed proxy: navidrome connection failed", url=target_url)
        raise HTTPException(status_code=503, detail="navidrome unavailable")
    except httpx.TimeoutException:
        log.error("subsonic authed proxy: navidrome request timed out", url=target_url)
        raise HTTPException(status_code=503, detail="navidrome request timed out")
