"""Scheduled and on-demand database backups.

Snapshots use SQLite's online backup API, so they are safe to take while the
server is running. Pointing `backup.directory` at a network share puts copies
off the machine.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from pathlib import Path

from .config import ServerConfig
from .db import Database

BACKUP_PREFIX = "k4cm-"
BACKUP_SUFFIX = ".db"


def run_backup(config: ServerConfig, db: Database, logger: logging.Logger | None = None) -> Path:
    """Take one snapshot and prune old ones. Returns the file written."""
    logger = logger or logging.getLogger("k4cm.server")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = config.backup_dir / f"{BACKUP_PREFIX}{stamp}{BACKUP_SUFFIX}"
    db.backup_to(destination)
    logger.info("database backed up to %s", destination)
    prune_backups(config, logger)
    return destination


def prune_backups(config: ServerConfig, logger: logging.Logger | None = None) -> list[Path]:
    """Delete all but the newest `retention` snapshots. Returns what was removed."""
    logger = logger or logging.getLogger("k4cm.server")
    retention = max(1, config.backup.retention)
    directory = config.backup_dir
    if not directory.exists():
        return []
    snapshots = sorted(
        (p for p in directory.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}") if p.is_file()),
        key=lambda p: p.name,
        reverse=True,
    )
    removed = []
    for stale in snapshots[retention:]:
        with contextlib.suppress(OSError):
            stale.unlink()
            removed.append(stale)
            logger.info("pruned old backup %s", stale)
    return removed


class BackupScheduler:
    def __init__(self, config: ServerConfig, db: Database, logger: logging.Logger | None = None):
        self.config = config
        self.db = db
        self.logger = logger or logging.getLogger("k4cm.server")
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if not self.config.backup.enabled:
            self.logger.info("scheduled backups are disabled in the config")
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run_forever(), name="k4cm-backup")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run_forever(self) -> None:
        interval = max(60.0, self.config.backup.interval_hours * 3600.0)
        while True:
            await asyncio.sleep(interval)
            try:
                await asyncio.to_thread(run_backup, self.config, self.db, self.logger)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("scheduled backup failed")
