"""Rotating file + console logging for the server.

Under Task Scheduler the server runs hidden and its stderr goes nowhere, so
anything that stops it starting must reach the log file too; otherwise the
only evidence is the watchdog relaunching it every few minutes.
"""

from __future__ import annotations

import contextlib
import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import ServerConfig

LOGGER_NAME = "k4cm.server"
_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def configure_logging(config: ServerConfig) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(getattr(logging, config.storage.log_level.upper(), logging.INFO))
    logger.handlers.clear()
    logger.propagate = False

    formatter = logging.Formatter(_FORMAT)

    log_path = config.log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=config.storage.log_max_bytes,
        backupCount=config.storage.log_backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    logger.addHandler(console)

    # uvicorn reports its own startup failures (e.g. the port already in use)
    # on this logger; with log_config=None it would have no handler at all.
    server_errors = logging.getLogger("uvicorn.error")
    server_errors.handlers[:] = [file_handler, console]
    server_errors.propagate = False

    return logger


def log_startup_failure(log_path: Path, message: str) -> None:
    """Append why the server could not start, for failures that happen before
    (or instead of) configure_logging, such as an unreadable config."""
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} ERROR   {LOGGER_NAME}: cannot start: {message}\n"
    with contextlib.suppress(OSError):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(line)
