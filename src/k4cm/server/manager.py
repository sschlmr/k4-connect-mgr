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

Bookings (bookings.py) shape every session: a request is refused while a
radio is held for its booking owner or being serviced, and otherwise its
deadline is capped at the start of the next booking. During someone else's
reservation, once the hold window has passed without them, the radio may be
borrowed — but the owner can take it back at any time.
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
    BookingKind,
    Occurrence,
    Radio,
    isoformat,
    utcnow,
)
from ..schedule import ScheduleError
from . import power as power_control
from .auth import AuthThrottle
from .bookings import SERVICE_LABEL, BookingService, fmt_local
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
        self.bookings = BookingService(config, db, self.logger)
        self._last_ending_check: datetime | None = None

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
        self,
        callsign: str,
        radio_ref: str,
        client_addr: str | None = None,
        duration_minutes: int | None = None,
        tz: str | None = None,
    ) -> dict[str, Any]:
        radio = self.radio_or_error(radio_ref)
        now = utcnow()
        occ = self.bookings.current(radio.id, now)
        owner_slot = occ is not None and occ.kind is BookingKind.BOOKING and occ.owner == callsign

        existing = self.db.live_assignment_for_callsign(callsign)
        if existing is not None:
            if owner_slot and existing.radio_id == radio.id and existing.state is AssignmentState.ACTIVE:
                # Their booking already opened this session (auto mode): hand it over.
                return self.assignment_payload(existing, include_password=True)
            raise ManagerError(
                409,
                f"you already have {existing.radio_id} assigned; release it first",
                assignment=self.assignment_payload(existing),
            )

        if not radio.enabled:
            raise ManagerError(409, f"{radio.name} is disabled by the administrator")

        if duration_minutes is not None and duration_minutes <= 0:
            raise ManagerError(400, "duration must be more than zero minutes")

        if occ is not None and occ.kind is BookingKind.MAINTENANCE:
            raise ManagerError(
                409,
                f"{radio.name}: {SERVICE_LABEL} until {self._fmt(occ.end, tz)}",
                radio_id=radio.id,
                can_queue=self.db.queue_position(radio.id, callsign) is None,
            )

        current = self.db.live_assignment_for_radio(radio.id)
        if current is not None:
            if owner_slot and current.callsign != callsign:
                raise self._reclaim_for_owner(radio, occ, current, callsign, tz)
            raise self._busy_error(radio, callsign, current, tz)

        notices: list[str] = []
        if occ is not None and not owner_slot:
            hold_end = occ.start + self.config.schedule.hold
            if now < hold_end:
                raise ManagerError(
                    409,
                    f"{radio.name} is reserved by {occ.owner} until {self._fmt(occ.end, tz)}. "
                    f"It is held for them until {self._fmt(hold_end, tz)}; if they have not connected "
                    "by then you can request it, or join the queue now.",
                    radio_id=radio.id,
                    reserved_by=occ.owner,
                    free_at=isoformat(hold_end),
                    can_queue=self.db.queue_position(radio.id, callsign) is None,
                )
            notices.append(
                f"{radio.name} is reserved by {occ.owner} until {self._fmt(occ.end, tz)}. If they "
                "connect, you'll be given the usual warning and grace period, then disconnected."
            )

        offer = self.db.offer_for_radio(radio.id)
        if offer is not None and offer.callsign != callsign:
            if owner_slot:
                self.db.set_offer(offer.id, None)  # the reservation's owner comes first
                self.db.audit(offer.callsign, "queue.offer_withdrawn", f"{radio.id}: reserved by {callsign}")
                offer = None
            else:
                raise ManagerError(
                    409,
                    f"{radio.name} is being held for {offer.callsign}, who is next in its queue",
                    radio_id=radio.id,
                    can_queue=self.db.queue_position(radio.id, callsign) is None,
                )

        expires_at = occ.end if owner_slot else now + self.config.session.max_session
        if duration_minutes is not None:
            expires_at = min(expires_at, now + timedelta(minutes=duration_minutes))
        if occ is not None and not owner_slot:
            expires_at = min(expires_at, occ.end)
        blocker = self.bookings.next_blocking(radio.id, now, callsign)
        if blocker is not None and blocker.start < expires_at:
            expires_at = blocker.start
            if expires_at - now < timedelta(minutes=1):
                raise ManagerError(
                    409,
                    f"{radio.name} is booked by {self._who(blocker)} from {self._fmt(blocker.start, tz)}",
                    radio_id=radio.id,
                )
            notices.append(
                f"Your session ends at {self._fmt(expires_at, tz)} because {radio.name} is booked "
                f"by {self._who(blocker)} from then. It can't be extended past that."
            )

        # Phase 1: claim. The checks above give friendly errors; this insert is
        # what actually guarantees exclusivity.
        password = new_password()
        try:
            claim = self.db.claim_radio(radio.id, callsign, password, expires_at, normalize_addr(client_addr))
        except ClaimConflict as e:
            winner = self.db.live_assignment_for_radio(radio.id)
            if winner is not None and winner.callsign != callsign:
                raise self._busy_error(radio, callsign, winner, tz) from e
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
        payload = self.assignment_payload(self.db.get_assignment(claim.id), include_password=True)
        payload["notices"] = notices
        if owner_slot:
            payload["booking"] = self.bookings.occurrence_payload(occ)
        return payload

    def _reclaim_for_owner(
        self, radio: Radio, occ: Occurrence, current: Assignment, owner: str, tz: str | None
    ) -> ManagerError:
        """The booking's owner has arrived while someone borrows the radio.

        The borrower's deadline becomes now, so they get the usual warning and
        ack grace, and the owner goes to the front of the queue to be offered
        the radio the moment it is free.
        """
        if current.expires_at > utcnow():
            self.db.set_expiry(current.id, utcnow())
            self.db.add_notice(
                current.callsign,
                f"{owner} has arrived for their reservation of {radio.name}; your session is ending now.",
            )
            self.db.audit(owner, "booking.reclaim", f"{radio.id} from {current.callsign}")
            self.logger.info("%s reclaiming %s from %s", owner, radio.id, current.callsign)
        self.db.enqueue(radio.id, owner)
        grace = self.config.session.ack_grace_minutes
        return ManagerError(
            409,
            f"{radio.name} is reserved for you. {current.callsign} has been told to hand it over and "
            f"will be disconnected within {grace} min. You're first in line: keep 'k4cm watch' "
            "running to take it when it is offered.",
            radio_id=radio.id,
            reclaiming=True,
            in_use_by=current.callsign,
        )

    def _fmt(self, when: datetime, tz: str | None) -> str:
        """A time for a message, in the member's zone when the client said it."""
        if tz:
            try:
                return fmt_local(when, tz)
            except ScheduleError:
                pass
        return isoformat(when)

    @staticmethod
    def _who(occ: Occurrence) -> str:
        return SERVICE_LABEL.lower() if occ.kind is BookingKind.MAINTENANCE else occ.owner

    def _busy_error(self, radio: Radio, callsign: str, current: Assignment, tz: str | None = None) -> ManagerError:
        return ManagerError(
            409,
            f"{radio.name} is in use by {current.callsign} until {self._fmt(current.expires_at, tz)}",
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
        blocker = self.bookings.blocking_between(assignment.radio_id, base, new_expiry, callsign)
        if blocker is not None:
            raise ManagerError(
                409,
                f"extension refused: {assignment.radio_id} is booked by {self._who(blocker)} "
                f"from {isoformat(blocker.start)}",
                booked_from=isoformat(blocker.start),
            )
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

    def enqueue(self, callsign: str, radio_ref: str, tz: str | None = None) -> dict[str, Any]:
        radio = self.radio_or_error(radio_ref)
        if not radio.enabled:
            raise ManagerError(409, f"{radio.name} is disabled by the administrator")
        active = self.db.live_assignment_for_callsign(callsign)
        if active is not None:
            raise ManagerError(409, f"you already have {active.radio_id} assigned")
        now = utcnow()
        occ = self.bookings.current(radio.id, now)
        held_from_caller = occ is not None and (
            occ.kind is BookingKind.MAINTENANCE
            or (occ.owner != callsign and now < occ.start + self.config.schedule.hold)
        )
        if (
            self.db.live_assignment_for_radio(radio.id) is None
            and self.db.offer_for_radio(radio.id) is None
            and not held_from_caller
        ):
            raise ManagerError(409, f"{radio.name} is free right now — request it instead of queueing")

        already = self.db.queue_position(radio.id, callsign)
        ahead = (already - 1) if already else self.db.queue_depth(radio.id)
        available_by = self.latest_available(radio.id, callsign, ahead, now)
        blocker = self.bookings.blocking_between(
            radio.id, available_by, available_by + timedelta(minutes=1), callsign
        )
        if blocker is not None:
            raise ManagerError(
                409,
                f"not queued: by the time {radio.name} could reach you ({self._fmt(available_by, tz)}) "
                f"it is booked by {self._who(blocker)}, so you would get no time on it",
                radio_id=radio.id,
            )

        entry = self.db.enqueue(radio.id, callsign)
        position = self.db.queue_position(radio.id, callsign)
        self.db.audit(callsign, "queue.join", f"{radio.id} position {position}")
        return {
            "radio": radio.public(),
            "position": position,
            "queued_at": isoformat(entry.requested_at),
            "available_by": isoformat(available_by),
            "detail": f"You're #{position}. Available by {self._fmt(available_by, tz)} at the latest.",
        }

    def latest_available(self, radio_id: str, callsign: str, ahead: int, now: datetime | None = None) -> datetime:
        """The latest time a member queued behind `ahead` others could get the
        radio: the current session and reservation run to their ends, every
        session ahead runs its full length (capped by bookings, as it would be),
        and nobody hands back early. Sessions often end sooner."""
        now = now or utcnow()
        session = self.config.session
        t = now
        live = self.db.live_assignment_for_radio(radio_id)
        if live is not None:
            t = max(t, live.expires_at + session.ack_grace)
        occ = self.bookings.current(radio_id, now)
        if occ is not None and not (occ.owner == callsign and occ.kind is BookingKind.BOOKING):
            t = max(t, occ.end)
        for _ in range(ahead):
            while (busy := self.bookings.current(radio_id, t)) is not None:
                t = busy.end
            end = t + session.max_session
            blocker = self.bookings.next_blocking(radio_id, t)
            if blocker is not None and blocker.start < end:
                end = blocker.start
            t = end + session.ack_grace
        return t

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
        """Hold a free radio for the next user in its queue.

        A reservation changes who is next: its owner jumps the queue, and while
        the radio is held for them (or being serviced) nobody else is offered it.
        """
        if self.db.live_assignment_for_radio(radio_id) is not None:
            return
        if self.db.offer_for_radio(radio_id) is not None:
            return
        queue = self.db.queue_for_radio(radio_id)
        if not queue:
            return
        now = utcnow()
        occ = self.bookings.current(radio_id, now)
        head = queue[0]
        if occ is not None and occ.kind is BookingKind.MAINTENANCE:
            return
        if occ is not None:
            owner_entry = next((e for e in queue if e.callsign == occ.owner), None)
            if owner_entry is not None:
                head = owner_entry
            elif now < occ.start + self.config.schedule.hold:
                return
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

    # -- bookings at run time ------------------------------------------------

    async def enforce_bookings(self, now: datetime | None = None) -> None:
        """One monitor pass over the calendar: move session deadlines to fit
        the bookings, open auto-sessions, and offer radios whose hold ended."""
        now = now or utcnow()
        self._fit_deadlines(now)
        await self._start_auto_sessions(now)
        for radio in self.config.radios.values():
            self.promote_queue(radio.id)
        if self._last_ending_check is None or now - self._last_ending_check >= timedelta(hours=1):
            self._last_ending_check = now
            self.bookings.send_ending_notices(now)

    def session_deadline(self, assignment: Assignment, now: datetime) -> datetime:
        """Where this session's deadline must be, given the bookings.

        Someone else's booking or a maintenance block ends it at its start; a
        borrower (granted after the hold window) may stay until the booking's
        end. A member's own booking is never a limit: an ad-hoc session that
        runs into it is lengthened to cover it.
        """
        deadline = assignment.expires_at
        occ = self.bookings.current(assignment.radio_id, now)
        if occ is not None:
            if occ.kind is BookingKind.MAINTENANCE:
                deadline = min(deadline, occ.start)
            elif occ.owner == assignment.callsign:
                if assignment.started_at < occ.start and deadline < occ.end:
                    deadline = occ.end
            else:
                borrowed = assignment.started_at >= occ.start + self.config.schedule.hold
                deadline = min(deadline, occ.end if borrowed else occ.start)
        blocker = self.bookings.next_blocking(assignment.radio_id, now, assignment.callsign)
        if blocker is not None:
            deadline = min(deadline, blocker.start)
        return deadline

    def _fit_deadlines(self, now: datetime) -> None:
        for assignment in self.db.active_assignments():
            deadline = self.session_deadline(assignment, now)
            if abs((deadline - assignment.expires_at).total_seconds()) < 1:
                continue
            if self.db.set_expiry(assignment.id, deadline):
                change = "booking" if deadline > assignment.expires_at else "capped"
                self.db.audit(
                    "system",
                    f"session.{change}",
                    f"{assignment.radio_id} ({assignment.callsign}) now until {isoformat(deadline)}",
                )

    async def _start_auto_sessions(self, now: datetime) -> None:
        hold = self.config.schedule.hold
        for radio in self.config.radios.values():
            occ = self.bookings.current(radio.id, now)
            if (
                occ is None
                or occ.kind is not BookingKind.BOOKING
                or occ.mode.value != "auto"
                or now >= occ.start + hold
                or self.db.auto_started(occ.booking_id, occ.occ_date)
            ):
                continue
            live = self.db.live_assignment_for_radio(radio.id)
            if live is not None and live.callsign == occ.owner:
                self.db.mark_auto_started(occ.booking_id, occ.occ_date)
                continue
            if live is not None:
                continue  # the previous session is still in its grace period
            elsewhere = self.db.live_assignment_for_callsign(occ.owner)
            if elsewhere is not None:
                self.db.mark_auto_started(occ.booking_id, occ.occ_date)
                self.db.add_notice(
                    occ.owner,
                    f"Your booking of {radio.name} started, but you are on {elsewhere.radio_id}, so no "
                    f"session was opened. Run 'k4cm request {radio.id}' when you are ready.",
                )
                continue
            try:
                await self.request_assignment(occ.owner, radio.id)
            except ManagerError as e:
                self.logger.warning("auto-session for %s on %s not opened yet: %s", occ.owner, radio.id, e.message)
                continue
            self.db.mark_auto_started(occ.booking_id, occ.occ_date)
            self.db.add_notice(
                occ.owner,
                f"Your booking of {radio.name} has started and the session is open. "
                f"'k4cm watch' connects your panel if it is running; otherwise run "
                f"'k4cm request {radio.id}'.",
            )
            self.db.audit("system", "booking.auto_start", f"{radio.id} for {occ.owner} (#{occ.booking_id} {occ.occ_date})")

    def in_owner_hold(self, assignment: Assignment, now: datetime) -> bool:
        """True while a booking owner's session is inside its hold window, when
        no panel attached yet is not a reason to take the radio back."""
        occ = self.bookings.current(assignment.radio_id, now)
        return (
            occ is not None
            and occ.kind is BookingKind.BOOKING
            and occ.owner == assignment.callsign
            and now < occ.start + self.config.schedule.hold
        )

    def opened_by_booking(self, assignment: Assignment, now: datetime) -> bool:
        """True for a session an auto-session booking opened, which a member's
        running `k4cm watch` connects their panel to (the server cannot reach it)."""
        occ = self.bookings.current(assignment.radio_id, now)
        return (
            occ is not None
            and occ.kind is BookingKind.BOOKING
            and occ.mode.value == "auto"
            and occ.owner == assignment.callsign
            and assignment.started_at >= occ.start
        )

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
            "opened_by_booking": self.opened_by_booking(assignment, utcnow()),
            "queue_depth": self.db.queue_depth(assignment.radio_id),
            "warn_period_minutes": session.warn_period_minutes,
            "extension_minutes": session.extension_minutes,
        }
        base = max(utcnow(), assignment.expires_at)
        payload["can_extend"] = payload["queue_depth"] == 0 and self.bookings.blocking_between(
            assignment.radio_id, base, base + session.extension, assignment.callsign
        ) is None
        if include_password:
            payload["password"] = assignment.password
        return payload

    def session_state(self, callsign: str) -> dict[str, Any]:
        """Everything the client's session watcher needs in one poll."""
        state: dict[str, Any] = {
            "callsign": callsign,
            "assignment": None,
            "offer": None,
            "queued": None,
            "notices": self.db.take_notices(callsign),
        }

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
            occ = self.bookings.current(radio.id, utcnow())
            if occ is not None:
                row["booking"] = self.bookings.occurrence_payload(occ)
            upcoming = self.bookings.next_blocking(radio.id, utcnow())
            if upcoming is not None:
                row["next_booking"] = self.bookings.occurrence_payload(upcoming)
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
