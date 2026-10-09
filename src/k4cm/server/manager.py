"""Assignment logic: who gets which K4, and the passwords that enforce it.

Every assignment sets a freshly generated random password on the radio; every
ending — release, reclaim, expiry, force-release — sets another fresh one and
drops the allowed-connection count, so a stale password never grants access.
Passwords are never derived from a previous value.

That lockout only stops *new* connections: a real K4 keeps a client that is
already attached. For a relayed radio, ending a session therefore also cuts the
member's link through the server's relay (see relay.py), which the K4 then
drops within seconds.

Allocation is two-phase so it is race-free. A request first *claims* the radio
in the database (an atomic PENDING insert — see db.py), and only then talks to
the radio. All CAT traffic that changes a radio's password runs under that
radio's lock, so a session ending and a new one starting on the same radio can
never interleave their commands: the new password is always set after the old
session's lockout, never scrambled by it.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from .. import cat
from ..models import (
    Assignment,
    AssignmentState,
    Radio,
    isoformat,
    utcnow,
)
from . import power as power_control
from .auth import AuthThrottle
from .config import ServerConfig
from .db import ClaimConflict, Database
from .relay import RelayManager, normalize_addr

PASSWORD_BYTES = 12


class ManagerError(Exception):
    """An error with an HTTP status and a message meant for the user."""

    def __init__(self, status_code: int, message: str, **extra: Any):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.extra = extra

    def payload(self) -> dict[str, Any]:
        return {"detail": self.message, **self.extra}


def new_password() -> str:
    """A fresh random K4 remote-access password from the OS entropy source."""
    return secrets.token_urlsafe(PASSWORD_BYTES)


class AssignmentManager:
    def __init__(self, config: ServerConfig, db: Database, logger: logging.Logger | None = None):
        self.config = config
        self.db = db
        self.logger = logger or logging.getLogger("k4cm.server")
        self.throttle = AuthThrottle()
        # Filled in by the monitor loop; None means "not probed yet".
        self.reachability: dict[str, bool | None] = {}
        self.disconnected_since: dict[str, datetime] = {}
        self._radio_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.relay = RelayManager(config, db, self.logger)

    # -- radio helpers -----------------------------------------------------

    @property
    def cat_timeout(self) -> float:
        return self.config.monitor.cat_timeout_seconds

    @property
    def claim_timeout(self) -> timedelta:
        """How long a PENDING claim may sit before it is presumed orphaned (for
        example by a server crash mid-setup). Comfortably longer than the
        handful of CAT round-trips that setting a password takes."""
        return timedelta(seconds=max(60.0, 10 * self.cat_timeout))

    def radio_lock(self, radio_id: str) -> asyncio.Lock:
        return self._radio_locks[radio_id]

    def radio_or_error(self, ref: str) -> Radio:
        radio = self.config.find_radio(ref)
        if radio is None:
            known = ", ".join(sorted(r.id for r in self.config.radios.values())) or "none configured"
            raise ManagerError(404, f"no such radio: {ref} (known radios: {known})")
        return radio

    async def _apply_password(self, radio: Radio, password: str) -> None:
        """Set the radio's remote password and admit one client."""
        async with cat.CatClient(radio.host, radio.cat_port, self.cat_timeout) as client:
            await client.send(f"RRP{password};")
            await client.send("RRC1;")
            if not await client.ping():
                raise cat.CatError(f"{radio.name} stopped responding while being configured")
        self.reachability[radio.id] = True

    async def _lock_radio(self, radio: Radio) -> bool:
        """Scramble the password and stop accepting clients. Best effort: a radio
        that is off or unreachable is already inaccessible."""
        try:
            async with cat.CatClient(radio.host, radio.cat_port, self.cat_timeout) as client:
                await client.send(f"RRP{new_password()};")
                await client.send("RRC0;")
                # The round-trip confirms the radio consumed both commands before
                # we report the session closed; commands are processed in order.
                if not await client.ping():
                    raise cat.CatError(f"{radio.name} did not confirm the lockout")
            self.reachability[radio.id] = True
            return True
        except cat.CatError as e:
            self.reachability[radio.id] = False
            self.logger.warning("could not lock radio %s after session end: %s", radio.id, e)
            return False

    # -- assignment --------------------------------------------------------

    async def request_assignment(
        self, callsign: str, radio_ref: str, client_addr: str | None = None
    ) -> dict[str, Any]:
        radio = self.radio_or_error(radio_ref)

        existing = self.db.live_assignment_for_callsign(callsign)
        if existing is not None:
            raise ManagerError(
                409,
                f"you already have {existing.radio_id} assigned; release it first",
                assignment=self.assignment_payload(existing),
            )

        if not radio.enabled:
            raise ManagerError(409, f"{radio.name} is disabled by the administrator")

        current = self.db.live_assignment_for_radio(radio.id)
        if current is not None:
            raise self._busy_error(radio, callsign, current)

        offer = self.db.offer_for_radio(radio.id)
        if offer is not None and offer.callsign != callsign:
            raise ManagerError(
                409,
                f"{radio.name} is being held for {offer.callsign}, who is next in its queue",
                radio_id=radio.id,
                can_queue=self.db.queue_position(radio.id, callsign) is None,
            )

        # Phase 1: claim. The checks above give friendly errors; this insert is
        # what actually guarantees exclusivity.
        password = new_password()
        expires_at = utcnow() + self.config.session.max_session
        try:
            claim = self.db.claim_radio(radio.id, callsign, password, expires_at, normalize_addr(client_addr))
        except ClaimConflict as e:
            winner = self.db.live_assignment_for_radio(radio.id)
            if winner is not None and winner.callsign != callsign:
                raise self._busy_error(radio, callsign, winner) from e
            raise ManagerError(409, "you already have a session starting; wait for it to finish") from e

        # Phase 2: configure the radio, then promote the claim.
        async with self.radio_lock(radio.id):
            try:
                await self._apply_password(radio, password)
            except cat.CatError as e:
                self.reachability[radio.id] = False
                self.db.finish_assignment(
                    claim.id, AssignmentState.FAILED, f"radio unreachable: {e}", AssignmentState.PENDING
                )
                self.db.audit(callsign, "assign.failed", f"{radio.id}: {e}")
                raise ManagerError(
                    503,
                    f"{radio.name} is not reachable, so it cannot be assigned: {e}",
                    radio_id=radio.id,
                ) from e

            if not self.db.activate(claim.id):
                # The monitor gave up on this claim while the radio was slow to
                # answer. Don't leave our password live on the radio.
                await self._lock_radio(radio)
                self.db.audit(callsign, "assign.failed", f"{radio.id}: claim abandoned during setup")
                raise ManagerError(503, f"{radio.name} took too long to configure; try again", radio_id=radio.id)

        if offer is not None and offer.callsign == callsign:
            self.db.remove_queue_entry(offer.id)
        self.db.dequeue(radio.id, callsign)

        self.db.audit(callsign, "assign", f"{radio.id} until {isoformat(expires_at)}")
        self.logger.info("assigned %s to %s until %s", radio.id, callsign, isoformat(expires_at))
        self.disconnected_since.pop(radio.id, None)
        return self.assignment_payload(self.db.get_assignment(claim.id), include_password=True)

    def _busy_error(self, radio: Radio, callsign: str, current: Assignment) -> ManagerError:
        return ManagerError(
            409,
            f"{radio.name} is in use by {current.callsign} until {isoformat(current.expires_at)}",
            radio_id=radio.id,
            in_use_by=current.callsign,
            expires_at=isoformat(current.expires_at),
            can_queue=self.db.queue_position(radio.id, callsign) is None,
            queue_depth=self.db.queue_depth(radio.id),
        )

    async def release(self, callsign: str) -> dict[str, Any]:
        assignment = self._active_or_error(callsign)
        if not await self.end_assignment(assignment, AssignmentState.RELEASED, "released by user", callsign):
            raise ManagerError(404, "you have no active assignment (it had already ended)")
        return {"released": assignment.radio_id, "detail": f"{assignment.radio_id} released"}

    async def end_assignment(
        self,
        assignment: Assignment,
        state: AssignmentState,
        reason: str,
        actor: str,
        still_due: Callable[[Assignment], bool] | None = None,
    ) -> bool:
        """End a session: record it, offer the radio to whoever is next in its
        queue, and lock the radio out. Every path that ends a session comes here.

        `assignment` may be a stale snapshot. The decision is re-made on the
        current row: if it is no longer active, or `still_due` says the reason
        no longer holds (say the user extended in the meantime), nothing happens
        and False is returned.
        """
        async with self.radio_lock(assignment.radio_id):
            current = self.db.get_assignment(assignment.id)
            if current is None or current.state is not AssignmentState.ACTIVE:
                return False
            if still_due is not None and not still_due(current):
                return False
            # No await between the re-check and this conditional update.
            if not self.db.finish_assignment(current.id, state, reason):
                return False
            self.disconnected_since.pop(current.radio_id, None)
            # Sever a relayed link now: the lockout below cannot drop it.
            self.relay.cut(current.radio_id)
            # Offer the radio before any await, so nobody outside the queue can
            # claim it first. A claim made now waits on this lock for its CAT.
            self.promote_queue(current.radio_id)

            radio = self.config.find_radio(current.radio_id)
            locked = await self._lock_radio(radio) if radio else False

        self.db.audit(
            actor,
            f"session.{state.value}",
            f"{current.radio_id} ({current.callsign}): {reason}"
            + ("" if locked else " [radio unreachable while locking]"),
        )
        self.logger.info(
            "session %s ended (%s) for %s on %s: %s",
            current.id, state.value, current.callsign, current.radio_id, reason,
        )
        return True

    async def abandon_stale_claims(self, now: datetime | None = None) -> None:
        """Fail PENDING claims that never completed, so a crash mid-setup
        cannot leave a radio reserved forever."""
        now = now or utcnow()
        for claim in self.db.pending_assignments(started_before=now - self.claim_timeout):
            async with self.radio_lock(claim.radio_id):
                # A request still in flight holds this lock and will have
                # activated or failed its claim by the time we get it.
                if not self.db.finish_assignment(
                    claim.id, AssignmentState.FAILED, "setup never completed", AssignmentState.PENDING
                ):
                    continue
                self.promote_queue(claim.radio_id)
                radio = self.config.find_radio(claim.radio_id)
                if radio is not None:
                    await self._lock_radio(radio)
            self.db.audit("system", "assign.abandoned", f"{claim.radio_id} ({claim.callsign})")
            self.logger.warning("abandoned stale claim %s on %s for %s", claim.id, claim.radio_id, claim.callsign)

    async def link_state(self, callsign: str) -> dict[str, Any]:
        """Whether the caller's assigned radio reports a client attached.

        This is how a member's client confirms its panel connected: a K4/0
        reports CC-1 while a connect is still failing, but the central radio's
        own count only rises when a client is really there.
        """
        assignment = self._active_or_error(callsign)
        radio = self.radio_or_error(assignment.radio_id)
        try:
            count = await cat.client_count(radio.host, radio.cat_port, self.cat_timeout)
        except cat.CatError as e:
            self.reachability[radio.id] = False
            raise ManagerError(503, f"{radio.name} is not answering: {e}", radio_id=radio.id) from e
        self.reachability[radio.id] = True
        return {
            "radio_id": radio.id,
            "attached": count >= 1,
            "clients": max(count, 0),
            "relayed_links": self.relay.link_count(radio.id) if radio.relay_port else None,
        }

    def note_client_addr(self, callsign: str, client_addr: str | None) -> None:
        """Follow a member's address while their session lasts, so the relay
        admits their panel even if their public address changes mid-session.
        Called on each authenticated request the session watcher makes."""
        addr = normalize_addr(client_addr)
        if addr is None:
            return
        assignment = self.db.live_assignment_for_callsign(callsign)
        if assignment is None or not self.db.set_client_addr(assignment.id, addr):
            return
        if assignment.client_addr is not None:  # a change, not the first sighting
            self.db.audit(callsign, "session.address", f"{assignment.radio_id}: now {addr}")

    def ack(self, callsign: str) -> dict[str, Any]:
        assignment = self._active_or_error(callsign)
        self.db.set_acked(assignment.id, True)
        self.db.audit(callsign, "session.ack", f"{assignment.radio_id}: termination acknowledged")
        return self.session_state(callsign)

    async def extend(self, callsign: str) -> dict[str, Any]:
        assignment = self._active_or_error(callsign)
        depth = self.db.queue_depth(assignment.radio_id)
        if depth > 0:
            raise ManagerError(
                409,
                f"extension refused: {depth} user(s) waiting for {assignment.radio_id}",
                queue_depth=depth,
            )
        base = max(utcnow(), assignment.expires_at)
        new_expiry = base + self.config.session.extension
        if not self.db.set_expiry(assignment.id, new_expiry):
            raise ManagerError(404, "you have no active assignment (it had already ended)")
        self.db.audit(callsign, "session.extend", f"{assignment.radio_id} until {isoformat(new_expiry)}")
        self.logger.info("extended %s for %s to %s", assignment.radio_id, callsign, isoformat(new_expiry))
        return self.session_state(callsign)

    def _active_or_error(self, callsign: str) -> Assignment:
        assignment = self.db.live_assignment_for_callsign(callsign)
        if assignment is None:
            raise ManagerError(404, "you have no active assignment")
        if assignment.state is AssignmentState.PENDING:
            raise ManagerError(409, f"your session on {assignment.radio_id} is still being set up; try again shortly")
        return assignment

    # -- queue -------------------------------------------------------------

    def enqueue(self, callsign: str, radio_ref: str) -> dict[str, Any]:
        radio = self.radio_or_error(radio_ref)
        if not radio.enabled:
            raise ManagerError(409, f"{radio.name} is disabled by the administrator")
        active = self.db.live_assignment_for_callsign(callsign)
        if active is not None:
            raise ManagerError(409, f"you already have {active.radio_id} assigned")
        if self.db.live_assignment_for_radio(radio.id) is None and self.db.offer_for_radio(radio.id) is None:
            raise ManagerError(409, f"{radio.name} is free right now — request it instead of queueing")
        entry = self.db.enqueue(radio.id, callsign)
        position = self.db.queue_position(radio.id, callsign)
        self.db.audit(callsign, "queue.join", f"{radio.id} position {position}")
        return {
            "radio": radio.public(),
            "position": position,
            "queued_at": isoformat(entry.requested_at),
        }

    def leave_queue(self, callsign: str) -> dict[str, Any]:
        entry = self.db.queue_entry_for_callsign(callsign)
        if entry is None:
            raise ManagerError(404, "you are not in any queue")
        self.db.remove_queue_entry(entry.id)
        self.db.audit(callsign, "queue.leave", entry.radio_id)
        if entry.offered_at is not None:
            self.promote_queue(entry.radio_id)
        return {"left": entry.radio_id}

    def promote_queue(self, radio_id: str) -> None:
        """Hold a free radio for the next user in its queue."""
        if self.db.live_assignment_for_radio(radio_id) is not None:
            return
        if self.db.offer_for_radio(radio_id) is not None:
            return
        queue = self.db.queue_for_radio(radio_id)
        if not queue:
            return
        head = queue[0]
        self.db.set_offer(head.id, utcnow())
        self.db.audit(head.callsign, "queue.offer", f"{radio_id} held for {self.config.session.queue_hold_minutes} min")
        self.logger.info("offering %s to %s (head of queue)", radio_id, head.callsign)

    def expire_offers(self) -> None:
        """Drop offers whose hold window has lapsed and move to the next in line."""
        now = utcnow()
        for offer in self.db.outstanding_offers():
            if offer.offered_at is None:
                continue
            if now - offer.offered_at <= self.config.session.queue_hold:
                continue
            self.db.remove_queue_entry(offer.id)
            self.db.audit(offer.callsign, "queue.offer_lapsed", offer.radio_id)
            self.logger.info("offer of %s to %s lapsed", offer.radio_id, offer.callsign)
            self.promote_queue(offer.radio_id)

    async def accept_offer(self, callsign: str, client_addr: str | None = None) -> dict[str, Any]:
        entry = self.db.queue_entry_for_callsign(callsign)
        if entry is None or entry.offered_at is None:
            raise ManagerError(404, "you have no radio being held for you")
        return await self.request_assignment(callsign, entry.radio_id, client_addr)

    def decline_offer(self, callsign: str) -> dict[str, Any]:
        entry = self.db.queue_entry_for_callsign(callsign)
        if entry is None or entry.offered_at is None:
            raise ManagerError(404, "you have no radio being held for you")
        self.db.remove_queue_entry(entry.id)
        self.db.audit(callsign, "queue.offer_declined", entry.radio_id)
        self.promote_queue(entry.radio_id)
        return {"declined": entry.radio_id}

    # -- views -------------------------------------------------------------

    def assignment_payload(self, assignment: Assignment, include_password: bool = True) -> dict[str, Any]:
        radio = self.config.find_radio(assignment.radio_id)
        session = self.config.session
        payload: dict[str, Any] = {
            "id": assignment.id,
            "radio": radio.public() if radio else {"id": assignment.radio_id, "name": assignment.radio_id},
            "callsign": assignment.callsign,
            "started_at": isoformat(assignment.started_at),
            "expires_at": isoformat(assignment.expires_at),
            "seconds_remaining": assignment.seconds_remaining(),
            "expiring": assignment.is_expiring(session.warn_period),
            "acked": assignment.acked,
            "state": assignment.state.value,
            "queue_depth": self.db.queue_depth(assignment.radio_id),
            "warn_period_minutes": session.warn_period_minutes,
            "extension_minutes": session.extension_minutes,
        }
        payload["can_extend"] = payload["queue_depth"] == 0
        if include_password:
            payload["password"] = assignment.password
        return payload

    def session_state(self, callsign: str) -> dict[str, Any]:
        """Everything the client's session watcher needs in one poll."""
        state: dict[str, Any] = {"callsign": callsign, "assignment": None, "offer": None, "queued": None}

        assignment = self.db.live_assignment_for_callsign(callsign)
        if assignment is not None:
            state["assignment"] = self.assignment_payload(assignment)

        entry = self.db.queue_entry_for_callsign(callsign)
        if entry is not None:
            radio = self.config.find_radio(entry.radio_id)
            radio_view = radio.public() if radio else {"id": entry.radio_id, "name": entry.radio_id}
            if entry.offered_at is not None:
                hold_until = entry.offered_at + self.config.session.queue_hold
                state["offer"] = {
                    "radio": radio_view,
                    "offered_at": isoformat(entry.offered_at),
                    "hold_until": isoformat(hold_until),
                    "seconds_remaining": int((hold_until - utcnow()).total_seconds()),
                }
            else:
                state["queued"] = {
                    "radio": radio_view,
                    "position": self.db.queue_position(entry.radio_id, callsign),
                    "queued_at": isoformat(entry.requested_at),
                }
        return state

    def status(self, include_callsigns: bool = True) -> list[dict[str, Any]]:
        rows = []
        for radio in self.config.radios.values():
            assignment = self.db.live_assignment_for_radio(radio.id)
            offer = self.db.offer_for_radio(radio.id)
            row: dict[str, Any] = {
                **radio.public(),
                "reachable": self.reachability.get(radio.id),
                "queue_depth": self.db.queue_depth(radio.id),
                "in_use": assignment is not None,
                "held_for": offer.callsign if offer is not None else None,
            }
            if assignment is not None:
                row["user"] = assignment.callsign if include_callsigns else "in use"
                row["since"] = isoformat(assignment.started_at)
                row["expires_at"] = isoformat(assignment.expires_at)
                row["seconds_remaining"] = assignment.seconds_remaining()
            rows.append(row)
        return rows

    # -- power -------------------------------------------------------------

    async def power_action(self, radio_ref: str, action: str, actor: str) -> dict[str, Any]:
        radio = self.radio_or_error(radio_ref)
        action = action.lower()
        assignment = self.db.live_assignment_for_radio(radio.id)
        if assignment is not None and action in ("off", "cycle"):
            raise ManagerError(
                409,
                f"{radio.name} is in use by {assignment.callsign}; force-release the session first",
            )
        try:
            if action == "off":
                detail = await power_control.power_off(radio, self.cat_timeout)
            elif action == "on":
                detail = await power_control.power_on(radio, self.logger)
            elif action == "cycle":
                detail = await power_control.power_cycle(radio, self.cat_timeout, self.logger)
            else:
                raise ManagerError(400, f"unknown power action: {action} (use on, off or cycle)")
        except power_control.PowerError as e:
            self.db.audit(actor, "power.failed", f"{radio.id} {action}: {e}")
            raise ManagerError(502, str(e)) from e
        self.db.audit(actor, f"power.{action}", f"{radio.id}: {detail}")
        self.logger.info("power %s on %s by %s: %s", action, radio.id, actor, detail)
        self.reachability.pop(radio.id, None)
        return {"radio_id": radio.id, "action": action, "detail": detail}

    # -- admin -------------------------------------------------------------

    async def force_release(self, assignment_id: int, actor: str) -> dict[str, Any]:
        assignment = self.db.get_assignment(assignment_id)
        if assignment is None:
            raise ManagerError(404, f"no such assignment: {assignment_id}")
        if assignment.state is not AssignmentState.ACTIVE or not await self.end_assignment(
            assignment, AssignmentState.FORCED, f"force-released by {actor}", actor
        ):
            current = self.db.get_assignment(assignment_id)
            raise ManagerError(409, f"assignment {assignment_id} is not active ({current.state.value})")
        return {"forced": assignment.radio_id, "callsign": assignment.callsign}

    async def reload_radios(self, actor: str) -> dict[str, Any]:
        try:
            count = self.config.reload_radios()
        except (OSError, ValueError) as e:
            raise ManagerError(400, f"could not reload radio inventory: {e}") from e
        await self.relay.sync()
        known = set(self.config.radios)
        for radio_id in list(self.reachability):
            if radio_id not in known:
                self.reachability.pop(radio_id, None)
        self.db.audit(actor, "radios.reload", f"{count} radio(s) loaded")
        self.logger.info("radio inventory reloaded by %s: %d radio(s)", actor, count)
        orphans = sorted(
            {a.radio_id for a in self.db.active_assignments() if a.radio_id not in known}
        )
        return {"radios": count, "orphaned_active_assignments": orphans}
