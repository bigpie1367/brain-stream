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
