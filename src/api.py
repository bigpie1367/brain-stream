import base64
import glob as _glob
import hashlib
import json
import os
import re
import secrets
import shutil
import threading
import time
import uuid
from queue import Empty
from typing import Optional

import httpx
import mutagen.flac
import mutagen.oggopus
import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import src.worker as worker
from src.pipeline.downloader import download_track, download_track_by_id, search_candidates
from src.pipeline.navidrome import trigger_scan, wait_for_scan
from src.pipeline.tagger import (
    embed_art_from_url,
    embed_cover_art,
    itunes_search,
    tag_and_import,
    write_album_tag,
    write_artist_tag,
    write_mb_trackid_tag,
    write_title_tag,
)
from src.state import (
    delete_download,
    get_all_downloads,
    get_download_by_mbid,
    mark_done,
    mark_downloading,
    mark_failed,
    mark_ignored,
    mark_pending,
    update_file_path,
    update_track_info,
)
from src.utils.logger import get_logger

log = get_logger(__name__)

app = FastAPI(title="Music Bot")
app.mount("/static", StaticFiles(directory="src/static"), name="static")

# Injected by main.py after config is loaded
_cfg = None


# ── Models ──────────────────────────────────────────────────────────────────


class DownloadRequest(BaseModel):
    artist: str
    track: str
    video_id: Optional[str] = None  # 선택 모드에서 특정 영상 지정 시


class RematchApplyRequest(BaseModel):
    song_id: str | None = None
    mbid: str | None = None
    mb_recording_id: str
    mb_album_id: str
    album_name: str = ""
    artist_name: str = ""
    cover_url: str = ""


class EditRequest(BaseModel):
    artist: Optional[str] = None
    album: Optional[str] = None
    track_name: Optional[str] = None


# ── Helpers ─────────────────────────────────────────────────────────────────


def _run_download_job(cfg, job_spec: dict):
    job_id = job_spec["job_id"]
    artist = job_spec["artist"]
    track = job_spec["track"]
    video_id = job_spec.get("video_id")
    mbid = job_id  # use job_id as the unique key in the DB

    try:
        # Fix 3: copy2 완료 후 mark_done 직전 크래시 대응
        # file_path가 이미 기록되어 있고 파일도 존재하면 다운로드/태깅 스킵
        existing = get_download_by_mbid(cfg.state_db, mbid)
        if existing and existing.get("file_path") and os.path.exists(existing["file_path"]):
            log.info("file already exists, skipping download", mbid=mbid, path=existing["file_path"])
            mark_done(cfg.state_db, mbid, existing["file_path"], album=existing.get("album"))
            worker.emit(job_id, "scanning", "Navidrome 스캔 중...")
            if trigger_scan(cfg.navidrome.url, cfg.navidrome.username, cfg.navidrome.password):
                wait_for_scan(cfg.navidrome.url, cfg.navidrome.username, cfg.navidrome.password)
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
        success, dest_path, canonical_artist, canonical_title, canonical_album, mb_recording_id = tag_and_import(
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

        mark_done(cfg.state_db, mbid, file_path=dest_path, album=canonical_album if canonical_album else None)

        # LB 트랙은 mbid 자체가 MB recording UUID이므로 tagger 반환값 대신 mbid 우선 사용
        final_mb_recording_id = mbid if not mbid.startswith("manual-") else mb_recording_id

        if canonical_artist or canonical_title or canonical_album or final_mb_recording_id:
            update_track_info(
                cfg.state_db,
                mbid,
                artist=canonical_artist if canonical_artist else None,
                track_name=canonical_title if canonical_title else None,
                album=canonical_album if canonical_album else None,
                mb_recording_id=final_mb_recording_id if final_mb_recording_id else None,
            )

        worker.emit(job_id, "scanning", "Navidrome 스캔 중...")
        if trigger_scan(cfg.navidrome.url, cfg.navidrome.username, cfg.navidrome.password):
            wait_for_scan(cfg.navidrome.url, cfg.navidrome.username, cfg.navidrome.password)

        worker.emit(job_id, "done", "완료")

    except Exception as exc:
        log.error("manual download job failed", job_id=job_id, error=str(exc))
        try:
            mark_failed(cfg.state_db, mbid, str(exc))
        except Exception:
            pass
        worker.emit(job_id, "failed", f"오류: {exc}")


# ── Routes ───────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def index():
    with open("src/static/index.html", encoding="utf-8") as f:
        return f.read()


@app.get("/api/download/candidates")
async def get_download_candidates(artist: str, track: str):
    """YouTube 후보 목록 반환 (다운로드 없음)"""
    candidates = search_candidates(artist, track)
    return {"candidates": candidates}


@app.post("/api/download")
async def start_download(req: DownloadRequest):
    if not _cfg:
        raise HTTPException(status_code=503, detail="config not loaded yet")

    job_id = "manual-" + uuid.uuid4().hex[:8]
    worker.create_sse_queue(job_id)

    mark_pending(
        _cfg.state_db,
        mbid=job_id,
        track_name=req.track,
        artist=req.artist,
        source="manual",
    )

    worker.enqueue_job(
        job_id=job_id,
        artist=req.artist,
        track=req.track,
        source="manual",
        video_id=req.video_id,
    )

    return {"job_id": job_id}


@app.get("/api/sse/{job_id}")
async def sse_stream(job_id: str):
    if worker.get_sse_queue(job_id) is None:
        raise HTTPException(status_code=404, detail="job not found")

    def event_generator():
        q = worker.get_sse_queue(job_id)
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
                worker.remove_sse_queue(job_id)
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


@app.get("/api/stream/{mbid}")
async def stream_track(mbid: str):
    if not _cfg:
        raise HTTPException(status_code=503, detail="config not loaded yet")
    record = get_download_by_mbid(_cfg.state_db, mbid)
    if not record or not record.get("file_path"):
        raise HTTPException(status_code=404, detail="File not found")
    file_path = record["file_path"]
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found on disk")
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".flac":
        media_type = "audio/flac"
    elif ext == ".opus":
        media_type = "audio/ogg; codecs=opus"
    else:
        media_type = "audio/mpeg"
    return FileResponse(file_path, media_type=media_type)


@app.get("/api/downloads/{mbid}/detail")
async def get_download_detail(mbid: str):
    if not _cfg:
        raise HTTPException(status_code=503, detail="config not loaded yet")

    record = get_download_by_mbid(_cfg.state_db, mbid)
    if record is None:
        raise HTTPException(status_code=404, detail="not found")

    file_path = record.get("file_path")
    if not file_path or not os.path.exists(file_path):
        return {"album_name": None, "year": None, "cover_art": None}

    album_name = None
    year = None
    cover_art = None
    try:
        lower = file_path.lower()
        if lower.endswith(".flac"):
            audio = mutagen.flac.FLAC(file_path)
            tags = audio.get("album")
            if tags:
                album_name = tags[0]
            date_tags = audio.get("date") or audio.get("year")
            if date_tags:
                year = date_tags[0]
            try:
                pics = audio.pictures
                if pics:
                    pic = pics[0]
                    cover_art = f"data:{pic.mime};base64,{base64.b64encode(pic.data).decode()}"
            except Exception:
                cover_art = None
        elif lower.endswith(".opus") or lower.endswith(".ogg"):
            audio = mutagen.oggopus.OggOpus(file_path)
            tags = audio.get("album")
            if tags:
                album_name = tags[0]
            date_tags = audio.get("date") or audio.get("year")
            if date_tags:
                year = date_tags[0]
            try:
                raw = audio.get("METADATA_BLOCK_PICTURE", [None])[0]
                if raw:
                    pic = mutagen.flac.Picture(base64.b64decode(raw))
                    cover_art = f"data:{pic.mime};base64,{base64.b64encode(pic.data).decode()}"
            except Exception:
                cover_art = None
    except Exception:
        album_name = None

    return {"album_name": album_name, "year": year, "cover_art": cover_art}


@app.delete("/api/downloads/{mbid}")
async def delete_download_entry(mbid: str):
    if not _cfg:
        raise HTTPException(status_code=503, detail="config not loaded yet")

    record = get_download_by_mbid(_cfg.state_db, mbid)
    if record is None:
        raise HTTPException(status_code=404, detail="not found")

    file_path = record.get("file_path") or ""
    files_removed = 0
    if file_path:
        try:
            os.remove(file_path)
            files_removed = 1
            log.info("removed file", file=file_path)
            # 빈 폴더 정리 (앨범 → 아티스트 순)
            album_dir = os.path.dirname(file_path)
            artist_dir = os.path.dirname(album_dir)
            try:
                if os.path.isdir(album_dir) and not os.listdir(album_dir):
                    os.rmdir(album_dir)
                    if os.path.isdir(artist_dir) and not os.listdir(artist_dir):
                        os.rmdir(artist_dir)
            except Exception as exc:
                log.warning("delete: failed to remove empty dirs", error=str(exc))
        except FileNotFoundError:
            log.info("file already gone, skipping removal", file=file_path)
        except OSError as exc:
            log.warning("could not remove file", file=file_path, error=str(exc))

    mark_ignored(_cfg.state_db, mbid)

    if files_removed:
        threading.Thread(
            target=trigger_scan,
            args=(_cfg.navidrome.url, _cfg.navidrome.username, _cfg.navidrome.password),
            daemon=True,
        ).start()

    log.info("download entry deleted", mbid=mbid, files_removed=files_removed)
    return {"deleted": True, "files_removed": files_removed}


_MB_SEARCH_URL = "https://musicbrainz.org/ws/2/recording"
_MB_SEARCH_HEADERS = {"User-Agent": "brainstream/1.0"}


@app.get("/api/rematch/search")
async def rematch_search(artist: str, track: str):
    """Search for album candidates to rematch a track.

    Queries MusicBrainz (up to 10 results) then appends an iTunes candidate if found.
    Returns combined results with a 'source' field on each candidate.
    """
    if not _cfg:
        raise HTTPException(status_code=503, detail="config not loaded yet")

    # Stage 1: strict query with artistname + recording fields
    try:
        r = requests.get(
            _MB_SEARCH_URL,
            params={
                "query": f'artistname:"{artist}" AND recording:"{track}"',
                "fmt": "json",
                "limit": 10,
            },
            headers=_MB_SEARCH_HEADERS,
            timeout=10,
        )
        r.raise_for_status()
        recordings = r.json().get("recordings", [])
    except Exception as exc:
        log.error("rematch_search: MB stage1 failed", error=str(exc))
        return {"candidates": []}

    time.sleep(1)

    # Stage 2: plain freetext query when stage 1 returns nothing
    if not recordings:
        try:
            r = requests.get(
                _MB_SEARCH_URL,
                params={
                    "query": f"{artist} {track}",
                    "fmt": "json",
                    "limit": 10,
                },
                headers=_MB_SEARCH_HEADERS,
                timeout=10,
            )
            r.raise_for_status()
            recordings = r.json().get("recordings", [])
        except Exception as exc:
            log.error("rematch_search: MB stage2 failed", error=str(exc))
            return {"candidates": []}

        time.sleep(1)

    if not recordings:
        return {"candidates": []}

    candidates = []
    seen_album_ids: set[str] = set()

    for rec in recordings[:5]:
        recording_id = rec.get("id")
        releases = rec.get("releases", [])
        credits = rec.get("artist-credit", [])
        artist_name = "".join(
            c.get("artist", {}).get("name", "") + c.get("joinphrase", "")
            for c in credits
            if isinstance(c, dict)
        ).strip() or artist

        for release in releases[:3]:
            mb_album_id = release.get("id")
            if not mb_album_id or mb_album_id in seen_album_ids:
                continue
            seen_album_ids.add(mb_album_id)

            album_name = release.get("title", "")
            date = release.get("date", "")
            year = int(date[:4]) if date and date[:4].isdigit() else None

            candidates.append(
                {
                    "source": "musicbrainz",
                    "mb_recording_id": recording_id,
                    "mb_album_id": mb_album_id,
                    "album_name": album_name,
                    "artist_name": artist_name,
                    "year": year,
                    "cover_url": f"https://coverartarchive.org/release/{mb_album_id}/front",
                }
            )

            if len(candidates) >= 10:
                break
        if len(candidates) >= 10:
            break

    # Append iTunes candidates (US + KR stores) at the end, deduplicated by album name
    seen_itunes_albums: set[str] = set()
    for country in (None, "KR"):
        try:
            itunes_result = itunes_search(artist, track, country=country)
        except Exception:
            itunes_result = {}
        if itunes_result:
            album_name = itunes_result.get("album", "")
            if album_name and album_name not in seen_itunes_albums:
                seen_itunes_albums.add(album_name)
                store_label = "itunes-kr" if country == "KR" else "itunes"
                candidates.append(
                    {
                        "mb_recording_id": "",
                        "mb_album_id": "",
                        "album_name": album_name,
                        "artist_name": artist,
                        "year": "",
                        "cover_url": itunes_result.get("artwork_url", ""),
                        "source": store_label,
                    }
                )

    return {"candidates": candidates}


def _sanitize_path_component(name: str) -> str:
    """파일시스템 안전 문자열로 변환 (tagger.py _sanitize_filename과 동일 규칙)."""
    sanitized = re.sub(r'[/\\:*?"<>|\x00-\x1f]', "_", name)
    sanitized = sanitized.strip(". ")
    return sanitized or "Unknown"


def _resolve_dir(parent: str, name: str) -> str:
    """대소문자 무시 기준으로 parent 안에 name과 동일한 폴더가 있으면 그 실제 이름 반환.
    없으면 sanitize된 name 그대로 반환."""
    sanitized = _sanitize_path_component(name)
    if os.path.isdir(parent):
        lower = sanitized.lower()
        for entry in os.listdir(parent):
            if entry.lower() == lower and os.path.isdir(os.path.join(parent, entry)):
                return entry
    return sanitized


def _navidrome_get_song(url: str, username: str, password: str, song_id: str) -> dict:
    """Call Navidrome getSong and return the song dict, or raise on failure."""
    salt = secrets.token_hex(6)
    token = hashlib.md5(f"{password}{salt}".encode()).hexdigest()
    params = {
        "u": username,
        "t": token,
        "s": salt,
        "v": "1.16.1",
        "c": "brainstream",
        "f": "json",
        "id": song_id,
    }
    endpoint = f"{url.rstrip('/')}/rest/getSong"
    resp = requests.get(endpoint, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    subsonic = data.get("subsonic-response", {})
    if subsonic.get("status") != "ok":
        raise RuntimeError(f"getSong failed: {subsonic.get('error', data)}")
    return subsonic.get("song", {})


@app.post("/api/rematch/apply")
async def rematch_apply(req: RematchApplyRequest):
    """Apply a manual album rematch to a song file.

    1. Resolves the file path via state.db (mbid) or Navidrome getSong (song_id).
    2. Fetches album name from MusicBrainz release.
    3. Rewrites album tag (mb_albumid is NOT written to avoid Navidrome album split).
    4. Embeds cover art from Cover Art Archive.
    5. Triggers Navidrome rescan.
    """
    if not _cfg:
        raise HTTPException(status_code=503, detail="config not loaded yet")

    if req.song_id is None and req.mbid is None:
        raise HTTPException(status_code=422, detail="song_id or mbid is required")

    # 1. Resolve file path
    if req.mbid is not None:
        # Downloads 탭에서 호출: state.db에서 file_path 직접 조회
        record = get_download_by_mbid(_cfg.state_db, req.mbid)
        if record is None:
            raise HTTPException(status_code=404, detail=f"mbid not found: {req.mbid}")
        file_path = record.get("file_path")
        if not file_path:
            raise HTTPException(status_code=500, detail="file_path not recorded in state.db")
        if not os.path.exists(file_path):
            raise HTTPException(status_code=404, detail=f"audio file not found: {file_path}")
    else:
        # Navidrome 탭에서 호출: getSong으로 경로 조회
        try:
            song = _navidrome_get_song(
                _cfg.navidrome.url,
                _cfg.navidrome.username,
                _cfg.navidrome.password,
                req.song_id,
            )
        except Exception as exc:
            log.error("rematch_apply: getSong failed", song_id=req.song_id, error=str(exc))
            raise HTTPException(status_code=500, detail=f"getSong failed: {exc}")

        raw_path = song.get("path", "")
        if not raw_path:
            raise HTTPException(status_code=500, detail="getSong returned no path")

        # Navidrome may return an absolute container path or a relative path
        if raw_path.startswith("/"):
            file_path = raw_path
        else:
            file_path = f"/app/data/music/{raw_path}"
        if not os.path.exists(file_path):
            raise HTTPException(status_code=404, detail=f"audio file not found: {file_path}")

    # 2. Fetch album name
    _MB_API = "https://musicbrainz.org/ws/2"
    _MB_HEADERS = {"User-Agent": "music-bot/1.0 (https://github.com/music-bot)"}

    if req.mb_album_id:
        try:
            time.sleep(1)  # rate limit
            r = requests.get(
                f"{_MB_API}/release/{req.mb_album_id}",
                params={"fmt": "json"},
                headers=_MB_HEADERS,
                timeout=10,
            )
            r.raise_for_status()
            album_name = r.json().get("title", "")
        except Exception as exc:
            log.error(
                "rematch_apply: MB release lookup failed",
                mb_album_id=req.mb_album_id,
                error=str(exc),
            )
            raise HTTPException(status_code=500, detail=f"MB release lookup failed: {exc}")
        if not album_name:
            raise HTTPException(status_code=500, detail="MB release returned no title")
    else:
        album_name = req.album_name
        if not album_name:
            raise HTTPException(
                status_code=422, detail="album_name is required when mb_album_id is empty"
            )

    # 3. Rewrite album tag (mb_albumid NOT written — prevents Navidrome album split)
    try:
        write_album_tag(file_path, album_name)
    except Exception as exc:
        log.error("rematch_apply: write_album_tag failed", file=file_path, error=str(exc))
        raise HTTPException(status_code=500, detail=f"tag write failed: {exc}")

    # 3-mb. Write mb_trackid tag if mb_recording_id is provided
    if req.mb_recording_id:
        try:
            write_mb_trackid_tag(file_path, req.mb_recording_id)
        except Exception as exc:
            log.warning("rematch_apply: write_mb_trackid_tag failed", error=str(exc))

    # 3-artist. Rewrite artist tag if artist_name is provided
    if req.artist_name:
        try:
            write_artist_tag(file_path, req.artist_name)
        except Exception as exc:
            log.warning("rematch_apply: write_artist_tag failed", error=str(exc))

    # 3-1. Move file to new album directory if album name or artist name changed
    filename = os.path.basename(file_path)
    old_album_dir = os.path.dirname(file_path)
    current_artist_dir = os.path.dirname(old_album_dir)
    music_root = os.path.dirname(current_artist_dir)

    if req.artist_name:
        new_artist_name = _resolve_dir(music_root, req.artist_name)
        new_artist_dir = os.path.join(music_root, new_artist_name)
    else:
        new_artist_dir = current_artist_dir

    new_album_dir = os.path.join(new_artist_dir, _resolve_dir(new_artist_dir, album_name))
    new_file_path = os.path.join(new_album_dir, filename)

    if new_file_path != file_path:
        try:
            os.makedirs(new_album_dir, exist_ok=True)
            shutil.move(file_path, new_file_path)
            file_path = new_file_path
            log.info(
                "rematch_apply: file moved to new album dir",
                new_path=file_path,
                album=album_name,
            )
        except Exception as exc:
            log.error("rematch_apply: file move failed", error=str(exc))
            raise HTTPException(status_code=500, detail=f"file move failed: {exc}")

        # 빈 폴더 정리
        try:
            if os.path.isdir(old_album_dir) and not os.listdir(old_album_dir):
                os.rmdir(old_album_dir)
                # 상위 아티스트 폴더도 비어있으면 삭제
                if os.path.isdir(current_artist_dir) and not os.listdir(current_artist_dir):
                    os.rmdir(current_artist_dir)
        except Exception as exc:
            log.warning("rematch_apply: failed to remove empty dirs", error=str(exc))

        if req.mbid is not None:
            try:
                update_track_info(
                    _cfg.state_db,
                    req.mbid,
                    artist=req.artist_name if req.artist_name else None,
                    file_path=file_path,
                )
            except Exception as exc:
                log.warning(
                    "rematch_apply: state.db update failed",
                    mbid=req.mbid,
                    error=str(exc),
                )

    # 4. Embed cover art
    if req.mb_album_id:
        art_ok = embed_cover_art(file_path, req.mb_album_id)
        if not art_ok:
            log.warning(
                "rematch_apply: CAA cover art not available, skipping",
                mb_album_id=req.mb_album_id,
            )
    elif req.cover_url:
        art_ok = embed_art_from_url(file_path, req.cover_url)
        if not art_ok:
            log.warning(
                "rematch_apply: cover art embed from URL failed",
                cover_url=req.cover_url,
            )

    # 4-1. Update album (and optionally mb_recording_id) in state.db
    if req.mbid is not None:
        try:
            update_track_info(
                _cfg.state_db,
                req.mbid,
                album=album_name,
                mb_recording_id=req.mb_recording_id if req.mb_recording_id else None,
            )
        except Exception as exc:
            log.warning(
                "rematch_apply: state.db update failed",
                mbid=req.mbid,
                error=str(exc),
            )

    # 5. Trigger Navidrome rescan (fire-and-forget)
    threading.Thread(
        target=trigger_scan,
        args=(_cfg.navidrome.url, _cfg.navidrome.username, _cfg.navidrome.password),
        daemon=True,
    ).start()

    log.info(
        "rematch applied",
        song_id=req.song_id,
        mbid=req.mbid,
        mb_album_id=req.mb_album_id,
        album_name=album_name,
        file=file_path,
    )
    return {"status": "ok", "album_name": album_name}


@app.post("/api/edit/{song_id}")
async def edit_metadata(song_id: str, req: EditRequest):
    """Directly edit artist / album / track_name metadata for a downloaded track.

    Updates mutagen tags, moves the file to the new path if necessary,
    updates state.db, and triggers a Navidrome rescan.
    """
    if not _cfg:
        raise HTTPException(status_code=503, detail="config not loaded yet")

    # 1. Look up current record
    record = get_download_by_mbid(_cfg.state_db, song_id)
    if record is None:
        raise HTTPException(status_code=404, detail="song not found")

    old_file_path = record.get("file_path")
    if not old_file_path:
        raise HTTPException(status_code=404, detail="file_path is not recorded")
    if not os.path.exists(old_file_path):
        raise HTTPException(status_code=404, detail=f"audio file not found: {old_file_path}")

    # 2. Resolve final values (None → keep existing; DB NULL treated as "")
    new_artist = req.artist if req.artist is not None else (record.get("artist") or "")
    new_album = req.album if req.album is not None else (record.get("album") or "")
    new_track_name = req.track_name if req.track_name is not None else (record.get("track_name") or "")

    # 3. No-op if nothing changed
    if (
        new_artist == (record.get("artist") or "")
        and new_album == (record.get("album") or "")
        and new_track_name == (record.get("track_name") or "")
    ):
        return {"ok": True, "file_path": old_file_path}

    # 4. Write mutagen tags (artist / album / title; mb_trackid untouched)
    try:
        write_artist_tag(old_file_path, new_artist)
        write_album_tag(old_file_path, new_album)
        write_title_tag(old_file_path, new_track_name)
    except Exception as exc:
        log.error("edit_metadata: tag write failed", song_id=song_id, error=str(exc))
        raise HTTPException(status_code=500, detail=f"tag write failed: {exc}")

    # 5. Move file if artist / album / track_name changed
    ext = os.path.splitext(old_file_path)[1]
    music_root = _cfg.beets.music_dir
    new_artist_dir = os.path.join(music_root, _sanitize_path_component(new_artist))
    new_album_dir = os.path.join(new_artist_dir, _sanitize_path_component(new_album))
    new_filename = _sanitize_path_component(new_track_name) + ext
    new_file_path = os.path.join(new_album_dir, new_filename)

    if new_file_path != old_file_path:
        if os.path.exists(new_file_path):
            raise HTTPException(
                status_code=409,
                detail=f"file already exists at new path: {new_file_path}",
            )
        try:
            os.makedirs(new_album_dir, exist_ok=True)
            shutil.move(old_file_path, new_file_path)
        except Exception as exc:
            log.error("edit_metadata: file move failed", song_id=song_id, error=str(exc))
            raise HTTPException(status_code=500, detail=f"file move failed: {exc}")

        # 빈 폴더 정리 (앨범 → 아티스트 순)
        old_album_dir = os.path.dirname(old_file_path)
        old_artist_dir = os.path.dirname(old_album_dir)
        try:
            if os.path.isdir(old_album_dir) and not os.listdir(old_album_dir):
                os.rmdir(old_album_dir)
                if os.path.isdir(old_artist_dir) and not os.listdir(old_artist_dir):
                    os.rmdir(old_artist_dir)
        except Exception as exc:
            log.warning("edit_metadata: failed to remove empty dirs", error=str(exc))

    final_file_path = new_file_path if new_file_path != old_file_path else old_file_path

    # 6. Update state.db
    try:
        update_track_info(
            _cfg.state_db,
            song_id,
            artist=new_artist,
            track_name=new_track_name,
            album=new_album,
            file_path=final_file_path,
        )
    except Exception as exc:
        log.warning("edit_metadata: state.db update failed", song_id=song_id, error=str(exc))

    # 7. Trigger Navidrome rescan (fire-and-forget)
    threading.Thread(
        target=trigger_scan,
        args=(_cfg.navidrome.url, _cfg.navidrome.username, _cfg.navidrome.password),
        daemon=True,
    ).start()

    log.info(
        "metadata edited",
        song_id=song_id,
        artist=new_artist,
        album=new_album,
        track_name=new_track_name,
        file_path=final_file_path,
    )
    return {"ok": True, "file_path": final_file_path}


@app.post("/api/pipeline/run")
async def trigger_pipeline():
    if not _cfg:
        raise HTTPException(status_code=503, detail="config not loaded yet")

    from src.main import run_pipeline

    threading.Thread(target=run_pipeline, args=(_cfg,), daemon=True).start()
    return {"status": "started"}


# ── Subsonic proxy ────────────────────────────────────────────────────────────

# hop-by-hop 헤더는 프록시 시 포워딩하지 않는다 (RFC 2616 §13.5.1)
_HOP_BY_HOP = frozenset(
    [
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    ]
)

# 응답 헤더 중 포워딩 제외 목록 (hop-by-hop + 프록시가 직접 관리하는 헤더)
_HOP_BY_HOP_RESPONSE = _HOP_BY_HOP | frozenset(["content-length", "content-encoding"])


@app.api_route("/rest/{path:path}", methods=["GET", "POST", "HEAD"])
async def subsonic_proxy(path: str, request: Request):
    if not _cfg:
        raise HTTPException(status_code=503, detail="config not loaded yet")

    target_url = f"{_cfg.navidrome.url.rstrip('/')}/rest/{path}"

    # 포워딩할 요청 헤더 필터링 (hop-by-hop 제외)
    forward_headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}

    request_body = await request.body()

    client = httpx.AsyncClient(timeout=60.0)
    try:
        upstream = await client.send(
            client.build_request(
                request.method,
                target_url,
                params=dict(request.query_params),
                headers=forward_headers,
                content=request_body,
            ),
            stream=True,
        )
    except httpx.ConnectError:
        await client.aclose()
        log.error("subsonic proxy: navidrome connection failed", url=target_url)
        raise HTTPException(status_code=503, detail="navidrome unavailable")
    except httpx.TimeoutException:
        await client.aclose()
        log.error("subsonic proxy: navidrome request timed out", url=target_url)
        raise HTTPException(status_code=503, detail="navidrome request timed out")

    response_headers = {
        k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_BY_HOP_RESPONSE
    }

    async def generate():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        generate(),
        status_code=upstream.status_code,
        headers=response_headers,
    )


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
        k: v for k, v in request.query_params.items() if k.lower() not in _SUBSONIC_AUTH_PARAMS
    }
    # 클라이언트가 f(format)를 명시하면 덮어쓰기 허용; 아니면 json 기본값 사용
    merged_params = {**auth_params, **client_params}

    target_url = f"{_cfg.navidrome.url.rstrip('/')}/rest/{path}"

    client = httpx.AsyncClient(timeout=60.0)
    try:
        upstream = await client.send(
            client.build_request("GET", target_url, params=merged_params),
            stream=True,
        )
    except httpx.ConnectError:
        await client.aclose()
        log.error("subsonic authed proxy: navidrome connection failed", url=target_url)
        raise HTTPException(status_code=503, detail="navidrome unavailable")
    except httpx.TimeoutException:
        await client.aclose()
        log.error("subsonic authed proxy: navidrome request timed out", url=target_url)
        raise HTTPException(status_code=503, detail="navidrome request timed out")

    response_headers = {
        k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_BY_HOP_RESPONSE
    }

    async def generate():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        generate(),
        status_code=upstream.status_code,
        headers=response_headers,
    )


# ── Navidrome web UI proxy ────────────────────────────────────────────────────

_NAVIDROME_BASE = "http://navidrome:4533"


@app.get("/navidrome")
async def navidrome_redirect():
    """/navidrome (trailing slash 없음) → /navidrome/ redirect."""
    return RedirectResponse(url="/navidrome/", status_code=301)


_NAVIDROME_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


@app.api_route("/navidrome/{path:path}", methods=_NAVIDROME_METHODS)
async def navidrome_proxy(path: str, request: Request):
    """Navidrome 웹 UI 투명 프록시. 인증 주입 없이 요청을 그대로 전달한다."""
    target_url = f"{_NAVIDROME_BASE}/navidrome/{path}"

    forward_headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}

    request_body = await request.body()

    client = httpx.AsyncClient(timeout=60.0)
    try:
        upstream = await client.send(
            client.build_request(
                request.method,
                target_url,
                params=dict(request.query_params),
                headers=forward_headers,
                content=request_body,
            ),
            stream=True,
        )
    except httpx.ConnectError:
        await client.aclose()
        log.error("navidrome proxy: connection failed", url=target_url)
        raise HTTPException(status_code=503, detail="navidrome unavailable")
    except httpx.TimeoutException:
        await client.aclose()
        log.error("navidrome proxy: request timed out", url=target_url)
        raise HTTPException(status_code=503, detail="navidrome request timed out")

    response_headers = {
        k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_BY_HOP_RESPONSE
    }

    # 리다이렉트 응답: Location의 내부 주소를 /navidrome 경로로 재작성
    if upstream.status_code in (301, 302, 303, 307, 308):
        location = upstream.headers.get("location", "")
        if location.startswith(_NAVIDROME_BASE):
            location = location[len(_NAVIDROME_BASE) :]
        # strip 후에도 /navidrome으로 시작하지 않는 절대경로는 /navidrome prefix 보정
        # 예: /app → /navidrome/app
        if location.startswith("/") and not location.startswith("/navidrome"):
            location = "/navidrome" + location
        await upstream.aclose()
        await client.aclose()
        return RedirectResponse(url=location, status_code=upstream.status_code)

    async def generate():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        generate(),
        status_code=upstream.status_code,
        headers=response_headers,
    )
