# P0 Stability Hardening Design

**Date**: 2026-03-17
**Status**: Approved
**Scope**: 5 independent defensive hardening items — no architecture changes

---

## 1. Graceful Shutdown + Worker Auto-Recovery

**Files**: `src/main.py`, `src/worker.py`

**Problem**: Daemon threads cause abrupt termination on `docker stop`. If worker thread crashes, all job processing stops permanently.

**Design**:
- Add `_shutdown_event = threading.Event()` in `worker.py`
- `main.py` registers `signal.signal(SIGTERM, handler)` and `signal.signal(SIGINT, handler)` → sets `_shutdown_event`
- `worker_loop()` checks `_shutdown_event.is_set()` between jobs → exits loop cleanly if set
- Worker thread changed from daemon to **non-daemon**; `main.py` calls `thread.join(timeout=300)` on shutdown
- Worker loop body wrapped in `try/except Exception` → log error, continue loop (auto-recovery on crash)
- Pipeline/scheduler threads remain daemon (non-critical, can be killed safely)

**Behavior on `docker stop`**:
1. SIGTERM received → `_shutdown_event.set()`
2. Current job finishes (or timeout after 5 min)
3. Worker thread exits → main thread proceeds to uvicorn shutdown
4. If job was in progress and timeout hit → Docker sends SIGKILL after grace period

---

## 2. yt-dlp Call Timeout

**File**: `src/pipeline/downloader.py`

**Problem**: `ydl.extract_info()` and download have no timeout — can block worker indefinitely.

**Design**:
- Set yt-dlp option `socket_timeout: 30` for network-level timeout
- Set yt-dlp option `extractor_retries: 3` for transient failures
- Wrap `ydl.extract_info(download=False)` in a helper that uses `concurrent.futures.ThreadPoolExecutor` with `future.result(timeout=60)` for metadata extraction
- Wrap `ydl.extract_info(download=True)` with `future.result(timeout=600)` (10 min) for actual download
- On `TimeoutError` → raise `yt_dlp.utils.DownloadError("timeout")` to reuse existing error handling path
- On timeout, the yt-dlp thread may still be running — acceptable since it will eventually hit `socket_timeout` and die

---

## 3. API Input Validation + Rate Limiting

**File**: `src/api.py`

### Input Validation
- `DownloadRequest.artist`: `Field(max_length=500)`
- `DownloadRequest.track`: `Field(max_length=500)`
- `EditRequest` fields: `Field(max_length=500)` each
- `RematchApplyRequest` fields: `Field(max_length=100)` for IDs
- `RematchSearchRequest` fields: `Field(max_length=500)` for artist/track
- Pydantic auto-returns 422 on violation — no custom error handling needed

### Rate Limiting (In-Memory Sliding Window)
- **Middleware**: `RateLimitMiddleware` added to FastAPI app
- **Data structure**: `dict[str, list[float]]` — IP → list of request timestamps
- **Key**: `request.client.host`
- **Limits**:
  - `POST /api/download`: 10 req/min
  - `POST /api/pipeline/run`: 2 req/min
  - `POST /api/rematch/apply`: 10 req/min
  - `POST /api/edit/`: 10 req/min
  - All other endpoints: no limit
- **On exceed**: Return `429 Too Many Requests` with `Retry-After` header
- **Cleanup**: On each request, prune timestamps older than window (60s)
- **Memory bound**: Single-user app, negligible memory usage

---

## 4. SSE Queue TTL Cleanup

**File**: `src/worker.py`

**Problem**: Orphaned SSE queues accumulate when clients disconnect without cleanup.

**Design**:
- Change `_job_queues: dict[str, Queue]` → `_job_queues: dict[str, tuple[Queue, float]]` where float is `time.time()` at creation
- `get_sse_queue()` returns only the Queue (interface unchanged for callers)
- `create_sse_queue()` stores `(Queue(), time.time())`
- `emit()` checks TTL before putting — if queue age > 1800s (30 min), auto-remove and skip
- `worker_loop()` after each job: sweep all queues, remove those older than 30 min
- `remove_sse_queue()` unchanged (normal cleanup path)

---

## 5. Log Rotation

**File**: `src/utils/logger.py`

**Problem**: `FileHandler` grows unbounded → disk full.

**Design**:
- Replace `logging.FileHandler` → `logging.handlers.RotatingFileHandler`
- `maxBytes=50_000_000` (50 MB per file)
- `backupCount=5` (keep 5 rotated files → max ~300 MB total)
- No other changes to structlog configuration

---

## Testing Strategy

- Each item is independently testable
- Existing `pytest` suite must pass after all changes
- Manual verification via `docker compose up --build` and basic download test
