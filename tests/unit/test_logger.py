import logging
from logging.handlers import RotatingFileHandler

from src.utils.logger import setup_logger


def test_log_file_uses_rotating_handler(tmp_path):
    """Log file should use RotatingFileHandler, not plain FileHandler."""
    log_file = str(tmp_path / "test.log")
    setup_logger("INFO", log_file)

    root = logging.getLogger()
    rotating_handlers = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
    assert len(rotating_handlers) >= 1, (
        f"Expected RotatingFileHandler but found: {[type(h).__name__ for h in root.handlers]}"
    )

    handler = rotating_handlers[0]
    assert handler.maxBytes == 50_000_000
    assert handler.backupCount == 5

    # Cleanup: remove handlers to avoid polluting other tests
    for h in list(root.handlers):
        root.removeHandler(h)
