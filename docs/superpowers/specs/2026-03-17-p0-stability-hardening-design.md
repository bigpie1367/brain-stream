# P0 Stability Hardening Design

**Date**: 2026-03-17
**Status**: Approved (rev.2 — post spec review)
**Scope**: 5 independent defensive hardening items — no architecture changes

---

## 1. Graceful Shutdown + Worker Auto-Recovery

**Files**: `src/main.py`, `src/worker.py`, `docker-compose.local.yml`, `docker-compose.prod.yml`

**Problem**: Daemon threads cause abrupt termination on `docker stop`. If worker thread crashes, all job processing stops permanently.

**Design**:
- Add `_shutdown_event = threading.Event()` in `worker.py`
- Use `atexit.register()` in `main.py` to set `_shutdown_event` and join worker thread — avoids conflict with uvicorn's own signal handler overwriting ours
- Control flow: SIGTERM → uvicorn exits → `atexit` handler fires → `_shutdown_event.set()` → `worker_thread.join(timeout=30)`
- `worker_loop()` checks `_shutdown_event.is_set()` between jobs → exits loop cleanly if set
- `_work_queue.get(timeout=2)` — reduced from 5s to 2s for faster shutdown responsiveness
- Worker thread changed from daemon to **non-daemon**
- Worker loop body: existing `try/except` already present — verify it logs and continues (auto-recovery)
- Pipeline/scheduler threads remain daemon (non-critical, can be killed safely)
- **Docker grace period**: Set `stop_grace_period: 40s` in both docker-compose files to accommodate join timeout (30s) + buffer

**Behavior on `docker stop`**:
1. SIGTERM received → uvicorn shuts down → `atexit` handler fires
2. `_shutdown_event.set()` → worker finishes current job or `_work_queue.get()` times out (≤2s)
3. `worker_thread.join(30)` — wait up to 30s for worker to finish
4. If join times out → process exits, Docker waits remaining grace period, then SIGKILL
5. On next startup: `downloading` status jobs → `mark_failed(attempts++)` → re-queue if attempts < 3 (existing recovery)

**Trade-off**: If yt-dlp is mid-download (up to 600s timeout from Item 2), the 30s join will time out and Docker SIGKILLs. Accepted — restart recovery handles this. The alternative (300s join) would block container restarts unacceptably.

---

## 2. yt-dlp Call Timeout

**File**: `src/pipeline/downloader.py`

**Problem**: `ydl.extract_info()` and download have no timeout — can block worker indefinitely.

**Design**:
- Set yt-dlp options: `socket_timeout: 30`, `extractor_retries: 3`
- Module-level shared `ThreadPoolExecutor(max_workers=2)` — one for current call, one buffer for overlapping cleanup
- Helper function `_run_with_timeout(fn, timeout_sec)`:
  - `future = executor.submit(fn)` → `future.result(timeout=timeout_sec)`
  - On `TimeoutError` → raise `yt_dlp.utils.DownloadError("operation timed out after {timeout_sec}s")`
- Timeout values as module constants: `EXTRACT_TIMEOUT = 60`, `DOWNLOAD_TIMEOUT = 600`

**Covered functions** (all yt-dlp call sites):
- `download_track()` — extract_info(download=False): 60s, extract_info(download=True): 600s
- `download_track_by_id()` — extract_info(download=True): 600s
- `search_candidates()` — extract_info(download=False): 60s

**Orphaned thread risk**: On timeout, the yt-dlp thread continues until `socket_timeout` (30s) kills it. Worst case: 2 orphaned threads (executor max_workers=2 bounds this). Acceptable for a single-worker system.

---

## 3. API Input Validation + Rate Limiting

**File**: `src/api.py`

### Input Validation

**POST body fields** (Pydantic `Field(max_length=...)`):
- `DownloadRequest.artist`: `Field(max_length=500)`
- `DownloadRequest.track`: `Field(max_length=500)`
- `EditRequest` fields: `Field(max_length=500)` each
- `RematchApplyRequest` fields: `Field(max_length=100)` for IDs
- Pydantic auto-returns 422 on violation

**GET query parameters** (FastAPI `Query(max_length=...)`):
- `GET /api/rematch/search`: artist, track — `Query(max_length=500)`
- `GET /api/download/candidates`: artist, track — `Query(max_length=500)`

### Rate Limiting (In-Memory Sliding Window)
- **Middleware**: `RateLimitMiddleware` added to FastAPI app
- **Data structure**: `dict[str, list[float]]` — IP → list of request timestamps
- **Key**: `request.client.host` — fallback to `"unknown"` if None (proxy edge case)
- **Thread safety**: Single uvicorn worker assumed (single-process). Document this assumption.
- **Limits** (POST endpoints only):
  - `POST /api/download`: 10 req/min
  - `POST /api/pipeline/run`: 2 req/min
  - `POST /api/rematch/apply`: 10 req/min
  - `POST /api/edit/`: 10 req/min
  - `DELETE /api/downloads/`: 10 req/min
  - All other endpoints: no limit
- **On exceed**: Return `429 Too Many Requests` with `Retry-After` header
- **Cleanup**: On each request, prune timestamps older than window (60s)

---

## 4. SSE Queue TTL Cleanup

**File**: `src/worker.py`

**Problem**: Orphaned SSE queues accumulate when clients disconnect without cleanup.

**Design**:
- Change `_job_queues: dict[str, Queue]` → `_job_queues: dict[str, tuple[Queue, float]]` where float is **last activity time** (`time.time()`)
- `get_sse_queue()` returns only the Queue (interface unchanged for callers)
- `create_sse_queue()` stores `(Queue(), time.time())`
- `emit()` updates last-activity timestamp on every put (keeps active jobs alive)
- `worker_loop()` after each job: sweep all queues, remove those with last-activity > 1800s (30 min) ago
- `remove_sse_queue()` unchanged (normal cleanup path)

**Key change from rev.1**: TTL measured from **last activity**, not creation time. This prevents killing queues for legitimately long-running jobs (slow download + queue wait could exceed 30 min).

**Race condition note**: `sse_stream` generator in api.py holds a direct Queue reference. Removing from `_job_queues` doesn't free the queue while a client is connected — this is correct (cleanup only affects truly orphaned queues).

---

## 5. Log Rotation

**File**: `src/utils/logger.py`

**Problem**: `FileHandler` grows unbounded → disk full.

**Design**:
- Replace `logging.FileHandler` → `logging.handlers.RotatingFileHandler`
- `maxBytes=50_000_000` (50 MB per file)
- `backupCount=5` (keep 5 rotated files → max ~300 MB total)

**Pre-existing issue**: structlog uses `PrintLoggerFactory` which bypasses stdlib logging and writes to stdout via `print()`. The FileHandler only captures stdlib logging output (uvicorn, libraries). This is a pre-existing limitation, not introduced by this change. Fixing structlog's logger factory (switching to `structlog.stdlib.LoggerFactory`) is out of scope for P0 — file as separate enhancement. The rotation still provides value for uvicorn/library logs.

---

## Testing Strategy

Each item has specific test criteria:

1. **Graceful shutdown**: Unit test — `_shutdown_event.set()` causes `worker_loop()` to exit within 3s
2. **yt-dlp timeout**: Unit test — `_run_with_timeout()` raises `DownloadError` after specified timeout
3. **Rate limiting**: Integration test — 11th request within 60s to `/api/download` returns 429
4. **SSE TTL**: Unit test — queue with last-activity > 30 min ago is removed by sweep
5. **Log rotation**: Verify `RotatingFileHandler` is configured (no runtime test needed)

Existing `pytest` suite must pass. Manual verification via `docker compose up --build`.

**Rollback**: Each item is in a separate commit. Revert individual commits if regression found.
