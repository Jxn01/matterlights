"""Shared logging setup, so every entry point writes one readable timeline.

Kept in its own module rather than in :mod:`matterlights.main` because the
session-end helper must not import the sync loop -- that would drag in ``mss``
and the DXGI capture stack, which is a lot of import time to pay while Windows
is already tearing the session down.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


FORMAT = "%(asctime)s %(levelname)s %(message)s"


def configure_logging(log_path: Path | None, *, rotating: bool = True) -> None:
    """Send logs to the console and, when configured, to ``log_path``.

    ``rotating`` is False for short-lived processes that share the log file with
    a running sync loop: two :class:`RotatingFileHandler` instances rolling the
    same file on Windows fight over a locked file. Appending is safe; rotating
    is not, and a short-lived helper has no business rotating anyway.
    """

    root_logger = logging.getLogger()
    formatter = logging.Formatter(FORMAT)

    root_logger.handlers.clear()
    root_logger.setLevel(logging.INFO)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    if log_path is None:
        return

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if rotating:
            file_handler: logging.Handler = RotatingFileHandler(
                log_path,
                maxBytes=1_048_576,
                backupCount=3,
                encoding="utf-8",
            )
        else:
            file_handler = logging.FileHandler(log_path, encoding="utf-8")
    except OSError:
        # Never let a locked or unwritable log file stop the caller: the whole
        # point of the session-end path is to get one command out in time.
        root_logger.warning("Could not open log file %s", log_path, exc_info=True)
        return

    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)
