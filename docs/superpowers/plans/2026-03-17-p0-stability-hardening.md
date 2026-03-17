# P0 Stability Hardening Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden the music-bot with graceful shutdown, yt-dlp timeouts, API rate limiting, SSE queue cleanup, and log rotation.

**Architecture:** 5 independent defensive changes — each in its own task and commit. No new files created; all modifications to existing files. Tests added per task.

**Tech Stack:** Python 3.12, FastAPI, threading, concurrent.futures, structlog, yt-dlp, pytest

**Spec:** `docs/superpowers/specs/2026-03-17-p0-stability-hardening-design.md`

---

### Task 1: Graceful Shutdown + Worker Auto-Recovery

**Files:**
- Modify: `src/worker.py:1-88` (add shutdown event, modify worker_loop)
- Modify: `src/main.py:105-134` (atexit handler, non-daemon worker)
- Modify: `docker-compose.local.yml:14-30` (stop_grace_period)
- Modify: `docker-compose.prod.yml:14-35` (stop_grace_period)
- Test: `tests/unit/test_worker.py` (new file)

- [ ] **Step 1: Write failing test for shutdown event**

Create `tests/unit/test_worker.py`:

```python
import threading
import time
from unittest.mock import MagicMock

from src.worker import _shutdown_event, _work_queue, worker_loop


def test_worker_loop_exits_on_shutdown_event():
    """worker_loop should exit cleanly when _shutdown_event is set."""
    _shutdown_event.clear()
    _work_queue.queue.clear()

    loop_exited = threading.Event()

    def run():
        worker_loop(MagicMock(), lambda cfg, job: None)
        loop_exited.set()

    t = threading.Thread(target=run)
    t.start()

    time.sleep(0.1)
    _shutdown_event.set()
    loop_exited.wait(timeout=5)
    assert loop_exited.is_set(), "worker_loop did not exit after shutdown_event was set"
    _shutdown_event.clear()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_worker.py::test_worker_loop_exits_on_shutdown_event -v`
Expected: FAIL — `_shutdown_event` does not exist in `src.worker`

- [ ] **Step 3: Implement shutdown event in worker.py**

In `src/worker.py`, add after line 18 (`_work_queue: Queue = Queue()`):

```python
# Cooperative shutdown signal
_shutdown_event = threading.Event()
```

Modify `worker_loop()` (lines 71-87) to:

```python
def worker_loop(cfg, run_job_fn):
    """
    Single worker thread. Processes jobs from _work_queue sequentially.
    run_job_fn(cfg, job_spec) — the actual download+tag+scan logic.
    Exits cleanly when _shutdown_event is set.
    """
    log.info("worker loop started")
    while not _shutdown_event.is_set():
        try:
            job = _work_queue.get(timeout=2)
        except Empty:
            continue
        try:
            run_job_fn(cfg, job)
        except Exception as e:
            log.error("worker: unhandled exception", job_id=job.get("job_id"), error=str(e))
        finally:
            _work_queue.task_done()
    log.info("worker loop stopped (shutdown event received)")
```

Key changes: `while True` → `while not _shutdown_event.is_set()`, `timeout=5` → `timeout=2`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_worker.py::test_worker_loop_exits_on_shutdown_event -v`
Expected: PASS

- [ ] **Step 5: Write failing test for auto-recovery on exception**

Add to `tests/unit/test_worker.py`:

```python
def test_worker_loop_continues_after_exception():
    """worker_loop should catch exceptions and continue processing."""
    _shutdown_event.clear()
    _work_queue.queue.clear()

    call_count = {"n": 0}

    def failing_then_ok(cfg, job):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("intentional test error")

    _work_queue.put({"job_id": "test-1"})
    _work_queue.put({"job_id": "test-2"})

    t = threading.Thread(target=worker_loop, args=(MagicMock(), failing_then_ok))
    t.start()

    time.sleep(1)
    _shutdown_event.set()
    t.join(timeout=5)

    assert call_count["n"] == 2, f"Expected 2 calls but got {call_count['n']}"
    _shutdown_event.clear()
```

- [ ] **Step 6: Run test to verify it passes** (already implemented by existing try/except)

Run: `pytest tests/unit/test_worker.py::test_worker_loop_continues_after_exception -v`
Expected: PASS (existing try/except already handles this)

- [ ] **Step 7: Implement atexit shutdown in main.py**

In `src/main.py`, add to imports (line 1):

```python
import atexit
```

Modify `main()` function (lines 105-134). Replace:

```python
    # Worker thread (single, sequential)
    from src.api import _run_download_job
    threading.Thread(
        target=worker_module.worker_loop,
        args=(cfg, _run_download_job),
        daemon=True,
        name="worker",
    ).start()
```

With:

```python
    # Worker thread (single, sequential, non-daemon for graceful shutdown)
    from src.api import _run_download_job
    worker_thread = threading.Thread(
        target=worker_module.worker_loop,
        args=(cfg, _run_download_job),
        daemon=False,
        name="worker",
    )
    worker_thread.start()

    def _on_exit():
        log.info("shutdown: signaling worker to stop")
        worker_module._shutdown_event.set()
        worker_thread.join(timeout=30)
        if worker_thread.is_alive():
            log.warning("shutdown: worker did not stop within 30s, proceeding")
        else:
            log.info("shutdown: worker stopped cleanly")

    atexit.register(_on_exit)
```

**Important**: Place the `atexit.register(_on_exit)` call immediately after `worker_thread.start()`, BEFORE `_reload_pending_jobs(cfg)`. This ensures that if `_reload_pending_jobs` crashes, the non-daemon worker thread can still be stopped via the atexit handler.

- [ ] **Step 8: Add stop_grace_period to docker-compose files**

In `docker-compose.local.yml`, add after line 16 (`restart: unless-stopped`):

```yaml
    stop_grace_period: 40s
```

In `docker-compose.prod.yml`, add after line 17 (`restart: unless-stopped`):

```yaml
    stop_grace_period: 40s
```

- [ ] **Step 9: Run full test suite**

Run: `pytest tests/ -v`
Expected: All existing tests PASS, 2 new tests PASS

- [ ] **Step 10: Commit**

```bash
git add src/worker.py src/main.py docker-compose.local.yml docker-compose.prod.yml tests/unit/test_worker.py
git commit -m "Feat: graceful shutdown + worker auto-recovery (P0-1)"
```

---

### Task 2: yt-dlp Call Timeout

**Files:**
- Modify: `src/pipeline/downloader.py:1-433` (add timeout wrapper, apply to all extract_info calls)
- Test: `tests/unit/test_downloader.py` (add timeout tests)

- [ ] **Step 1: Write failing test for timeout helper**

Add to `tests/unit/test_downloader.py`:

```python
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import yt_dlp

from src.pipeline.downloader import _run_with_timeout


def test_run_with_timeout_raises_on_timeout():
    """_run_with_timeout should raise DownloadError when function exceeds timeout."""
    def slow_fn():
        time.sleep(10)

    with pytest.raises(yt_dlp.utils.DownloadError, match="timed out"):
        _run_with_timeout(slow_fn, timeout_sec=0.5)


def test_run_with_timeout_returns_result():
    """_run_with_timeout should return function result when within timeout."""
    result = _run_with_timeout(lambda: 42, timeout_sec=5)
    assert result == 42
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_downloader.py::test_run_with_timeout_raises_on_timeout -v`
Expected: FAIL — `_run_with_timeout` does not exist

- [ ] **Step 3: Implement timeout wrapper in downloader.py**

In `src/pipeline/downloader.py`, add after imports (after line 13):

```python
from concurrent.futures import ThreadPoolExecutor

# Timeout constants (seconds)
EXTRACT_TIMEOUT = 60   # metadata extraction
DOWNLOAD_TIMEOUT = 600  # actual download (10 min)

_yt_executor = ThreadPoolExecutor(max_workers=2)


def _run_with_timeout(fn, timeout_sec: float):
    """Run fn in a thread pool with timeout. Raises DownloadError on timeout."""
    future = _yt_executor.submit(fn)
    try:
        return future.result(timeout=timeout_sec)
    except TimeoutError:
        raise yt_dlp.utils.DownloadError(
            f"operation timed out after {timeout_sec}s"
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_downloader.py::test_run_with_timeout_raises_on_timeout tests/unit/test_downloader.py::test_run_with_timeout_returns_result -v`
Expected: PASS

- [ ] **Step 5: Apply timeout + socket_timeout to download_track()**

In `download_track()`, modify the metadata fetch block (lines 207-214). Replace:

```python
        meta_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
            "skip_download": True,
        }
        with yt_dlp.YoutubeDL(meta_opts) as ydl:
            info = ydl.extract_info(search_query, download=False)
```

With:

```python
        meta_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
            "skip_download": True,
            "socket_timeout": 30,
            "extractor_retries": 3,
        }
        with yt_dlp.YoutubeDL(meta_opts) as ydl:
            info = _run_with_timeout(
                lambda: ydl.extract_info(search_query, download=False),
                EXTRACT_TIMEOUT,
            )
```

Add `"socket_timeout": 30` and `"extractor_retries": 3` to `_flac_opts()` and `_opus_opts()` dicts (lines 54-66, 69-81):

```python
        "socket_timeout": 30,
        "extractor_retries": 3,
```

Modify the download call (line 283). Replace:

```python
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(download_target, download=True)
```

With:

```python
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = _run_with_timeout(
                        lambda: ydl.extract_info(download_target, download=True),
                        DOWNLOAD_TIMEOUT,
                    )
```

- [ ] **Step 6: Apply timeout to search_candidates()**

In `search_candidates()` (line 360). Replace:

```python
            info = ydl.extract_info(query, download=False)
```

With:

```python
            info = _run_with_timeout(
                lambda: ydl.extract_info(query, download=False),
                EXTRACT_TIMEOUT,
            )
```

Add `"socket_timeout": 30` and `"extractor_retries": 3` to `ydl_opts` dict (lines 352-357).

- [ ] **Step 7: Apply timeout to download_track_by_id()**

In `download_track_by_id()` (line 410). Replace:

```python
                info = ydl.extract_info(url, download=True)
```

With:

```python
                info = _run_with_timeout(
                    lambda: ydl.extract_info(url, download=True),
                    DOWNLOAD_TIMEOUT,
                )
```

Add `"socket_timeout": 30` to both `_flac_opts` and `_opus_opts` (already done in Step 5).

- [ ] **Step 8: Run full test suite**

Run: `pytest tests/ -v`
Expected: All tests PASS (existing download tests use mocks, unaffected by timeout wrapper)

- [ ] **Step 9: Commit**

```bash
git add src/pipeline/downloader.py tests/unit/test_downloader.py
git commit -m "Feat: yt-dlp 호출 타임아웃 추가 (P0-2)"
```

---

### Task 3: API Input Validation + Rate Limiting

**Files:**
- Modify: `src/api.py:63-82` (Pydantic max_length)
- Modify: `src/api.py:192-193, 388-389` (Query max_length)
- Modify: `src/api.py:53` (add middleware)
- Test: `tests/integration/test_api.py` (add validation + rate limit tests)

- [ ] **Step 1: Write failing test for input length validation**

Add to `tests/integration/test_api.py`:

```python
def test_post_download_rejects_long_artist(client):
    resp = client.post("/api/download", json={"artist": "A" * 501, "track": "t"})
    assert resp.status_code == 422


def test_post_download_accepts_max_length_artist(client):
    resp = client.post("/api/download", json={"artist": "A" * 500, "track": "t"})
    assert resp.status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_api.py::test_post_download_rejects_long_artist -v`
Expected: FAIL — 200 instead of 422

- [ ] **Step 3: Add max_length to Pydantic models**

In `src/api.py`, add `Field` to import (line 22):

```python
from pydantic import BaseModel, Field
```

Modify `DownloadRequest` (lines 63-66):

```python
class DownloadRequest(BaseModel):
    artist: str = Field(max_length=500)
    track: str = Field(max_length=500)
    video_id: Optional[str] = None
```

Modify `RematchApplyRequest` (lines 69-76):

```python
class RematchApplyRequest(BaseModel):
    song_id: str | None = Field(default=None, max_length=100)
    mbid: str | None = Field(default=None, max_length=100)
    mb_recording_id: str = Field(max_length=100)
    mb_album_id: str = Field(max_length=100)
    album_name: str = Field(default="", max_length=500)
    artist_name: str = Field(default="", max_length=500)
    cover_url: str = Field(default="", max_length=2000)
```

Modify `EditRequest` (lines 79-82):

```python
class EditRequest(BaseModel):
    artist: Optional[str] = Field(default=None, max_length=500)
    album: Optional[str] = Field(default=None, max_length=500)
    track_name: Optional[str] = Field(default=None, max_length=500)
```

- [ ] **Step 4: Add max_length to GET query parameters**

Modify `get_download_candidates` (line 193). Add `Query` import:

```python
from fastapi import FastAPI, HTTPException, Query, Request
```

```python
@app.get("/api/download/candidates")
async def get_download_candidates(
    artist: str = Query(max_length=500),
    track: str = Query(max_length=500),
):
```

Modify `rematch_search` (line 389):

```python
@app.get("/api/rematch/search")
async def rematch_search(
    artist: str = Query(max_length=500),
    track: str = Query(max_length=500),
):
```

- [ ] **Step 5: Run validation test to verify it passes**

Run: `pytest tests/integration/test_api.py::test_post_download_rejects_long_artist tests/integration/test_api.py::test_post_download_accepts_max_length_artist -v`
Expected: PASS

- [ ] **Step 6: Write failing test for rate limiting**

Add to `tests/integration/test_api.py`:

```python
def test_rate_limit_returns_429(client):
    """11th request within 60s should return 429."""
    for i in range(10):
        resp = client.post("/api/download", json={"artist": f"a{i}", "track": "t"})
        assert resp.status_code == 200, f"Request {i+1} failed: {resp.status_code}"
    resp = client.post("/api/download", json={"artist": "overflow", "track": "t"})
    assert resp.status_code == 429


def test_rate_limit_not_applied_to_get(client):
    """GET endpoints should not be rate limited."""
    for _ in range(20):
        resp = client.get("/api/downloads")
        assert resp.status_code == 200
```

- [ ] **Step 7: Run test to verify it fails**

Run: `pytest tests/integration/test_api.py::test_rate_limit_returns_429 -v`
Expected: FAIL — all requests return 200

- [ ] **Step 8: Implement rate limiting middleware**

In `src/api.py`, add after `app = FastAPI(title="Music Bot")` (line 53):

```python
# ── Rate Limiting ────────────────────────────────────────────────────────────

import time as _time

_RATE_LIMITS: dict[str, int] = {
    "POST /api/download": 10,
    "POST /api/pipeline/run": 2,
    "POST /api/rematch/apply": 10,
    "POST /api/edit/": 10,
    "DELETE /api/downloads/": 10,
}
_rate_window = 60  # seconds
_rate_store: dict[str, list[float]] = {}


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    method = request.method
    path = request.url.path

    # Find matching rate limit rule
    limit = None
    for pattern, max_req in _RATE_LIMITS.items():
        rule_method, rule_path = pattern.split(" ", 1)
        if method == rule_method and path.startswith(rule_path):
            limit = max_req
            break

    if limit is not None:
        client_ip = request.client.host if request.client else "unknown"
        key = f"{client_ip}:{method} {path}"
        now = _time.time()

        timestamps = _rate_store.get(key, [])
        timestamps = [t for t in timestamps if now - t < _rate_window]
        _rate_store[key] = timestamps

        if len(timestamps) >= limit:
            from starlette.responses import JSONResponse
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests"},
                headers={"Retry-After": str(_rate_window)},
            )

        timestamps.append(now)
        _rate_store[key] = timestamps

    return await call_next(request)
```

- [ ] **Step 9: Run rate limiting test to verify it passes**

Run: `pytest tests/integration/test_api.py::test_rate_limit_returns_429 tests/integration/test_api.py::test_rate_limit_not_applied_to_get -v`
Expected: PASS

- [ ] **Step 10: Run full test suite**

Run: `pytest tests/ -v`
Expected: All tests PASS

**REQUIRED**: Existing tests will fail due to rate limiting. Add `_rate_store.clear()` to the `client` fixture in `tests/conftest.py`, before `yield`:

```python
import src.api as api_mod
api_mod._rate_store.clear()
```

This MUST be done as part of this step, not conditionally.

- [ ] **Step 11: Commit**

```bash
git add src/api.py tests/integration/test_api.py tests/conftest.py
git commit -m "Feat: API 입력 길이 제한 + Rate Limiting (P0-3)"
```

---

### Task 4: SSE Queue TTL Cleanup

**Files:**
- Modify: `src/worker.py:13-41` (change _job_queues type, add TTL logic)
- Test: `tests/unit/test_worker.py` (add TTL tests)

- [ ] **Step 1: Write failing test for TTL cleanup**

Add to `tests/unit/test_worker.py`:

```python
import time as _time
from src.worker import create_sse_queue, get_sse_queue, emit, _job_queues, _job_queues_lock, _cleanup_expired_queues


def test_expired_queue_removed_by_cleanup():
    """Queues with last_active > 30 min should be removed by cleanup."""
    q = create_sse_queue("ttl-test-1")
    # Manually backdate the last_active timestamp
    with _job_queues_lock:
        _job_queues["ttl-test-1"] = (q, _time.time() - 2000)

    _cleanup_expired_queues()
    assert get_sse_queue("ttl-test-1") is None


def test_active_queue_not_removed_by_cleanup():
    """Recently active queues should survive cleanup."""
    q = create_sse_queue("ttl-test-2")
    _cleanup_expired_queues()
    assert get_sse_queue("ttl-test-2") is not None
    # Clean up
    from src.worker import remove_sse_queue
    remove_sse_queue("ttl-test-2")


def test_emit_updates_last_active():
    """emit() should update the last_active timestamp."""
    create_sse_queue("ttl-test-3")
    with _job_queues_lock:
        _job_queues["ttl-test-3"] = (_job_queues["ttl-test-3"][0], _time.time() - 1000)

    emit("ttl-test-3", "downloading", "test")
    with _job_queues_lock:
        _, last_active = _job_queues["ttl-test-3"]
    assert _time.time() - last_active < 5  # updated within last 5 seconds
    # Clean up
    from src.worker import remove_sse_queue
    remove_sse_queue("ttl-test-3")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_worker.py::test_expired_queue_removed_by_cleanup -v`
Expected: FAIL — `_cleanup_expired_queues` does not exist, `_job_queues` is `dict[str, Queue]` not tuple

- [ ] **Step 3: Implement TTL-based queue management**

In `src/worker.py`, add `time` import at top:

```python
import time
```

Change `_job_queues` type (line 14):

```python
# Per-job SSE event queues (job_id → (Queue, last_active_timestamp))
_job_queues: dict[str, tuple[Queue, float]] = {}
```

Modify `create_sse_queue()` (lines 21-25):

```python
def create_sse_queue(job_id: str) -> Queue:
    q: Queue = Queue()
    with _job_queues_lock:
        _job_queues[job_id] = (q, time.time())
    return q
```

Modify `get_sse_queue()` (lines 28-30):

```python
def get_sse_queue(job_id: str) -> Optional[Queue]:
    with _job_queues_lock:
        entry = _job_queues.get(job_id)
        return entry[0] if entry else None
```

Modify `remove_sse_queue()` (lines 33-35) — unchanged, just pop works on tuples too.

Modify `emit()` (lines 38-41):

```python
def emit(job_id: str, status: str, message: str):
    with _job_queues_lock:
        entry = _job_queues.get(job_id)
        if entry is not None:
            q, _ = entry
            _job_queues[job_id] = (q, time.time())  # update last_active
            q.put({"status": status, "message": message})
```

Add new function after `emit()`:

```python
_QUEUE_TTL = 1800  # 30 minutes


def _cleanup_expired_queues():
    """Remove SSE queues that have been inactive for > _QUEUE_TTL seconds."""
    now = time.time()
    with _job_queues_lock:
        expired = [jid for jid, (_, last_active) in _job_queues.items() if now - last_active > _QUEUE_TTL]
        for jid in expired:
            del _job_queues[jid]
    if expired:
        log.info("cleaned up expired SSE queues", count=len(expired))
```

Add cleanup call in `worker_loop()`, after the `finally` block (after `_work_queue.task_done()`):

```python
        finally:
            _work_queue.task_done()
        _cleanup_expired_queues()
```

- [ ] **Step 4: Run TTL tests to verify they pass**

Run: `pytest tests/unit/test_worker.py -v`
Expected: All worker tests PASS

- [ ] **Step 5: Run full test suite**

Run: `pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/worker.py tests/unit/test_worker.py
git commit -m "Feat: SSE 큐 TTL 기반 자동 정리 (P0-4)"
```

---

### Task 5: Log Rotation

**Files:**
- Modify: `src/utils/logger.py:34-38` (FileHandler → RotatingFileHandler)
- Test: `tests/unit/test_logger.py` (new file)

- [ ] **Step 1: Write failing test for RotatingFileHandler**

Create `tests/unit/test_logger.py`:

```python
import logging
from logging.handlers import RotatingFileHandler

from src.utils.logger import setup_logger


def test_log_file_uses_rotating_handler(tmp_path):
    """Log file should use RotatingFileHandler, not plain FileHandler."""
    log_file = str(tmp_path / "test.log")
    setup_logger("INFO", log_file)

    root = logging.getLogger()
    rotating_handlers = [
        h for h in root.handlers if isinstance(h, RotatingFileHandler)
    ]
    assert len(rotating_handlers) >= 1, (
        f"Expected RotatingFileHandler but found: {[type(h).__name__ for h in root.handlers]}"
    )

    handler = rotating_handlers[0]
    assert handler.maxBytes == 50_000_000
    assert handler.backupCount == 5

    # Cleanup: remove handlers to avoid polluting other tests
    for h in list(root.handlers):
        root.removeHandler(h)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_logger.py::test_log_file_uses_rotating_handler -v`
Expected: FAIL — finds FileHandler, not RotatingFileHandler

- [ ] **Step 3: Implement RotatingFileHandler**

In `src/utils/logger.py`, modify the import (line 1):

```python
import logging
import logging.handlers
import os
import sys
```

Modify lines 34-38 (the file handler setup):

```python
    if log_file:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=50_000_000, backupCount=5
        )
        file_handler.setFormatter(logging.Formatter("%(message)s"))
        logging.getLogger().addHandler(file_handler)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_logger.py -v`
Expected: PASS

- [ ] **Step 5: Run full test suite**

Run: `pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/utils/logger.py tests/unit/test_logger.py
git commit -m "Feat: 로그 로테이션 적용 (P0-5)"
```

---

### Task 6: Final Verification

**Files:** None (read-only verification)

- [ ] **Step 1: Run full test suite**

Run: `pytest tests/ -v --tb=short`
Expected: All tests PASS, 0 failures

- [ ] **Step 2: Verify Docker build**

Run: `docker compose -f docker-compose.local.yml build`
Expected: Build succeeds

- [ ] **Step 3: Update documentation**

Update `CLAUDE.md` Architecture section to note:
- Worker thread is non-daemon with graceful shutdown via `atexit`
- yt-dlp calls have 60s/600s timeout
- POST endpoints rate-limited (see spec for limits)
- SSE queues auto-cleaned after 30 min inactivity
- Logs rotated at 50MB × 5 files
