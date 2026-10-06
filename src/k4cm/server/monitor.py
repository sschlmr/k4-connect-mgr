"""Background monitor: disconnect detection, deadline enforcement, offer expiry.

Decisions here are made from a snapshot that can go stale while the monitor
awaits radios, so each ending passes `still_due` and the manager re-checks it
against the current row: a user who extends mid-tick keeps the radio.

Runs one `tick()` every poll interval. `tick()` is deliberately a plain
coroutine so tests can drive it directly with shortened timers.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime

from .. import cat
from ..models import AssignmentState, utcnow
from .manager import AssignmentManager


class Monitor:
    def __init__(self, manager: AssignmentManager):
        self.manager = manager
        self.config = manager.config
        self.db = manager.db
        self.logger = manager.logger
        self._task: asyncio.Task | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run_forever(), name="k4cm-monitor")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run_forever(self) -> None:
        interval = max(1.0, self.config.monitor.poll_interval_seconds)
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # a monitor crash must not take the server down
                self.logger.exception("monitor tick failed")
            await asyncio.sleep(interval)

    # -- one pass ----------------------------------------------------------

    async def tick(self, now: datetime | None = None) -> None:
        now = now or utcnow()
        self.manager.expire_offers()
        await self.manager.abandon_stale_claims(now)
        await self._enforce_sessions(now)
        await self._probe_idle_radios()

    async def _enforce_sessions(self, now: datetime) -> None:
        session = self.config.session
        for assignment in self.db.active_assignments():
            radio = self.config.find_radio(assignment.radio_id)
            if radio is None:
                self.logger.warning(
                    "active session %d references radio %s, which is no longer in the config",
                    assignment.id, assignment.radio_id,
                )
                continue

            # Deadline first: an acknowledged session ends at its deadline; an
            # unacknowledged one gets the extra ack grace before being taken back.
            def acked_due(a):
                return a.acked and now >= a.expires_at

            def unacked_due(a):
                return not a.acked and now >= a.expires_at + session.ack_grace

            if acked_due(assignment):
                await self.manager.end_assignment(
                    assignment,
                    AssignmentState.EXPIRED,
                    "deadline reached (acknowledged)",
                    "system",
                    still_due=acked_due,
                )
                continue
            if unacked_due(assignment):
                await self.manager.end_assignment(
                    assignment,
                    AssignmentState.EXPIRED,
                    "deadline passed with no acknowledgement from the user",
                    "system",
                    still_due=unacked_due,
                )
                continue

            await self._check_connection(assignment, radio, now)

    async def _check_connection(self, assignment, radio, now: datetime) -> None:
        try:
            count = await cat.client_count(radio.host, radio.cat_port, self.manager.cat_timeout)
            self.manager.reachability[radio.id] = True
            reason = "no client attached"
        except cat.CatError as e:
            count = 0
            self.manager.reachability[radio.id] = False
            reason = f"radio unreachable ({e})"

        if count >= 1:
            self.manager.disconnected_since.pop(radio.id, None)
            return

        since = self.manager.disconnected_since.setdefault(radio.id, now)
        idle = now - since
        if idle >= self.config.monitor.disconnect_grace:
            await self.manager.end_assignment(
                assignment,
                AssignmentState.RECLAIMED,
                f"{reason} for {int(idle.total_seconds() // 60)} min",
                "system",
            )

    async def _probe_idle_radios(self) -> None:
        """Keep the availability view honest for radios nobody is using."""
        for radio in self.config.radios.values():
            if not radio.enabled:
                self.manager.reachability[radio.id] = None
                continue
            if self.db.live_assignment_for_radio(radio.id) is not None:
                continue  # already probed by the connection check
            self.manager.reachability[radio.id] = await cat.reachable(
                radio.host, radio.cat_port, self.manager.cat_timeout
            )
