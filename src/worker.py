"""
Shared work queue and single worker thread.
Both manual downloads (api.py) and LB pipeline use this module.
"""

import threading
from queue import Empty, Queue
from typing import Optional

from src.utils.logger import get_logger

log = get_logger(__name__)

# Per-job SSE event queues (job_id → Queue)
_job_queues: dict[str, Queue] = {}
_job_queues_lock = threading.Lock()

# Global FIFO work queue
_work_queue: Queue = Queue()

# Shutdown signal for graceful stop
_shutdown_event = threading.Event()


def create_sse_queue(job_id: str) -> Queue:
    q: Queue = Queue()
    with _job_queues_lock:
        _job_queues[job_id] = q
    return q


def get_sse_queue(job_id: str) -> Optional[Queue]:
    with _job_queues_lock:
        return _job_queues.get(job_id)


def remove_sse_queue(job_id: str):
    with _job_queues_lock:
        _job_queues.pop(job_id, None)


def emit(job_id: str, status: str, message: str):
    q = get_sse_queue(job_id)
    if q is not None:
        q.put({"status": status, "message": message})


def enqueue_job(
    job_id: str,
    artist: str,
    track: str,
    source: str = "listenbrainz",
    video_id: Optional[str] = None,
):
    """Put a job into the work queue. emit 'queued' if SSE listener exists."""
    _work_queue.put(
        {
            "job_id": job_id,
            "artist": artist,
            "track": track,
            "source": source,
            "video_id": video_id,
        }
    )
    emit(job_id, "queued", "다운로드 대기 중...")
    log.info(
        "job enqueued",
        job_id=job_id,
        artist=artist,
        track=track,
        queue_size=_work_queue.qsize(),
    )


def worker_loop(cfg, run_job_fn):
    """
    Single worker thread. Processes jobs from _work_queue sequentially.
    run_job_fn(cfg, job_spec) — the actual download+tag+scan logic.
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
