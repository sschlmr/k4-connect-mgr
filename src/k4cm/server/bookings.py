"""Booking service: create, change and remove bookings, and answer "what is
booked when" for the rest of the server.

Two bookings on one radio never overlap. A one-off booking that would overlap
is refused; a series is created with the clashing dates skipped (recorded as
`conflict` exceptions naming who holds them) and the member is told which.
Admin overrides and maintenance blocks ("Being serviced") go the other way:
the members' clashing dates are removed, and their owners get a notice.

Occurrences are recomputed from the stored rules rather than stored, and cached
until the next write: a club has few enough bookings that expanding them all is
cheap, and there is then no second copy of the schedule to drift out of step.

Runtime behaviour — holding a radio for its owner, capping other sessions,
opening auto-sessions — is the manager's and monitor's job; this module only
answers questions about the calendar.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from ..models import (
    Booking,
    BookingException,
    BookingKind,
    BookingMode,
    Occurrence,
    Radio,
    User,
    isoformat,
    utcnow,
)
from ..schedule import (
    RepeatSpec,
    ScheduleError,
    build_rrule,
    check_horizon,
    describe_rrule,
    local_starts,
    occurrences,
    parse_duration,
    parse_local_start,
    zone,
)
from .config import ServerConfig
from .db import Database

SCOPES = ("this", "following", "all")
SERVICE_LABEL = "Being serviced"


class BookingError(Exception):
    """Same shape as manager.ManagerError, which wraps it for the API."""

    def __init__(self, status_code: int, message: str, **extra: Any):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.extra = extra


@dataclass
class BookingInput:
    radio: str
    start: str                     # local wall-clock time, "2026-10-20 19:00"
    duration: str | int
    tz: str
    mode: str = "reserve"
    kind: str = "booking"
    repeat: RepeatSpec | None = None
    override: bool = False


@dataclass
class BookingChanges:
    start: str | None = None
    duration: str | int | None = None
    mode: str | None = None
    radio: str | None = None
    repeat: RepeatSpec | None = None
    count: int | None = None       # change only how a series ends
    until: str | None = None


def fmt_local(when: datetime, tz: str) -> str:
    """'Tue 20 Oct 19:00' in the given zone."""
    local = when.astimezone(zone(tz))
    return f"{local:%a} {local.day} {local:%b %H:%M}"


def fmt_day(when: datetime, tz: str) -> str:
    local = when.astimezone(zone(tz))
    return f"{local:%a} {local.day} {local:%b %Y}"


def _who(occ: Occurrence) -> str:
    return SERVICE_LABEL if occ.kind is BookingKind.MAINTENANCE else occ.owner


def _with_end(rule: str, *, count: int | None = None, until: str | None = None) -> str:
    """Replace how an RRULE ends (COUNT or UNTIL), keeping its pattern."""
    parts = [p for p in rule.split(";") if not p.startswith(("COUNT=", "UNTIL="))]
    if count is not None:
        parts.append(f"COUNT={count}")
    if until is not None:
        parts.append(f"UNTIL={until.replace('-', '')}T235959")
    return ";".join(parts)


class BookingService:
    def __init__(self, config: ServerConfig, db: Database, logger: logging.Logger | None = None):
        self.config = config
        self.db = db
        self.logger = logger or logging.getLogger("k4cm.server")
        self._cache: list[Occurrence] | None = None
        self._cache_rev = -1

    # -- reading the calendar ----------------------------------------------

    def invalidate(self) -> None:
        self._cache = None

    def all_occurrences(self) -> list[Occurrence]:
        if self._cache is None or self._cache_rev != self.db.bookings_rev:
            self._cache_rev = self.db.bookings_rev
            exceptions = self.db.all_exceptions()
            occs: list[Occurrence] = []
            for booking in self.db.list_bookings():
                try:
                    occs.extend(occurrences(booking, exceptions.get(booking.id, [])))
                except ScheduleError as e:  # e.g. a zone this host no longer knows
                    self.logger.warning("booking %d cannot be expanded: %s", booking.id, e)
            occs.sort(key=lambda o: o.start)
            self._cache = occs
        return self._cache

    def between(
        self,
        start: datetime,
        end: datetime,
        radio_id: str | None = None,
        owner: str | None = None,
    ) -> list[Occurrence]:
        owner = owner.strip().upper() if owner else None
        return [
            o
            for o in self.all_occurrences()
            if o.overlaps(start, end)
            and (radio_id is None or o.radio_id == radio_id)
            and (owner is None or o.owner == owner)
        ]

    def current(self, radio_id: str, now: datetime) -> Occurrence | None:
        """The occurrence holding this radio right now, if any."""
        for occ in self.all_occurrences():
            if occ.radio_id == radio_id and occ.active_at(now):
                return occ
        return None

    def next_blocking(self, radio_id: str, after: datetime, callsign: str | None = None) -> Occurrence | None:
        """The first occurrence starting at or after `after` that `callsign`
        may not run into: anyone else's booking, or any maintenance block."""
        for occ in self.all_occurrences():
            if occ.radio_id != radio_id or occ.start < after:
                continue
            if callsign and occ.owner == callsign and occ.kind is BookingKind.BOOKING:
                continue
            return occ
        return None

    def blocking_between(
        self, radio_id: str, start: datetime, end: datetime, callsign: str | None = None
    ) -> Occurrence | None:
        """An occurrence overlapping [start, end) that is not `callsign`'s own booking."""
        for occ in self.all_occurrences():
            if occ.radio_id != radio_id or not occ.overlaps(start, end):
                continue
            if callsign and occ.owner == callsign and occ.kind is BookingKind.BOOKING:
                continue
            return occ
        return None

    # -- views -------------------------------------------------------------

    def _radio_view(self, radio_id: str) -> dict[str, Any]:
        radio = self.config.find_radio(radio_id)
        return {"id": radio_id, "name": radio.name if radio else radio_id}

    def occurrence_payload(self, occ: Occurrence, booking: Booking | None = None) -> dict[str, Any]:
        booking = booking or self.db.get_booking(occ.booking_id)
        return {
            "booking_id": occ.booking_id,
            "date": occ.occ_date,
            "radio": self._radio_view(occ.radio_id),
            "owner": occ.owner,
            "kind": occ.kind.value,
            "mode": occ.mode.value,
            "label": SERVICE_LABEL if occ.kind is BookingKind.MAINTENANCE else occ.owner,
            "start": isoformat(occ.start),
            "end": isoformat(occ.end),
            "series": bool(booking and booking.rrule),
        }

    def list_occurrences(
        self,
        start: datetime,
        end: datetime,
        radio_ref: str | None = None,
        owner: str | None = None,
    ) -> list[dict[str, Any]]:
        radio_id = self._radio(radio_ref).id if radio_ref else None
        bookings = {b.id: b for b in self.db.list_bookings()}
        return [
            self.occurrence_payload(o, bookings.get(o.booking_id))
            for o in self.between(start, end, radio_id, owner)
        ]

    def booking_payload(self, booking: Booking) -> dict[str, Any]:
        exceptions = self.db.exceptions_for(booking.id)
        skipped = {e.occ_date: e for e in exceptions if e.action == "skip"}
        dates = []
        for occ in occurrences(booking, exceptions, include_skipped=True):
            exc = skipped.get(occ.occ_date)
            dates.append(
                {
                    "date": occ.occ_date,
                    "start": isoformat(occ.start),
                    "end": isoformat(occ.end),
                    "skipped": exc is not None,
                    "reason": exc.reason if exc else "",
                    "blocked_by": exc.blocked_by if exc else "",
                }
            )
        live = [d for d in dates if not d["skipped"]]
        return {
            "id": booking.id,
            "radio": self._radio_view(booking.radio_id),
            "owner": booking.owner,
            "kind": booking.kind.value,
            "mode": booking.mode.value,
            "tz": booking.tz,
            "start_local": booking.start.isoformat(timespec="minutes"),
            "duration_minutes": booking.duration_minutes,
            "rrule": booking.rrule,
            "repeat": describe_rrule(booking.rrule),
            "dates": dates,
            "first": live[0]["start"] if live else None,
            "last": live[-1]["start"] if live else None,
        }

    def get(self, booking_id: int) -> dict[str, Any]:
        return self.booking_payload(self._booking(booking_id))

    def list_series(self, owner: str | None = None, radio_ref: str | None = None) -> list[dict[str, Any]]:
        radio_id = self._radio(radio_ref).id if radio_ref else None
        return [self.booking_payload(b) for b in self.db.list_bookings(radio_id=radio_id, owner=owner)]

    # -- creating ----------------------------------------------------------

    def create(self, actor: User, req: BookingInput, dry_run: bool = False) -> dict[str, Any]:
        radio = self._radio(req.radio)
        kind = self._enum(BookingKind, req.kind, "kind")
        mode = self._enum(BookingMode, req.mode, "mode")
        if kind is BookingKind.MAINTENANCE and not actor.is_admin:
            raise BookingError(403, "only an administrator can mark a radio as being serviced")
        if req.override and not actor.is_admin:
            raise BookingError(403, "only an administrator can book over other members' bookings")
        override = req.override or kind is BookingKind.MAINTENANCE
        if kind is BookingKind.MAINTENANCE:
            mode = BookingMode.RESERVE

        now = utcnow()
        booking = self._schedule_error(
            lambda: Booking(
                id=0,
                radio_id=radio.id,
                owner=actor.callsign,
                kind=kind,
                mode=mode,
                tz=zone(req.tz).key,
                start=parse_local_start(req.start),
                duration_minutes=parse_duration(req.duration),
                rrule=None,
                created_at=now,
            )
        )
        if req.repeat is not None:
            booking.rrule = self._schedule_error(lambda: build_rrule(req.repeat, booking.start))

        candidates = self._schedule_error(lambda: occurrences(booking))
        if candidates[0].start < now - timedelta(minutes=1):
            raise BookingError(400, f"{fmt_local(candidates[0].start, booking.tz)} is in the past")
        self._schedule_error(lambda: check_horizon(candidates, now))

        skips, affected = self._resolve(booking, candidates, override, now)
        result = self._outcome(booking, candidates, skips, affected, dry_run)
        if dry_run:
            return result

        exceptions = [
            BookingException(booking.id, c.occ_date, "skip", "conflict", blocked_by=_who(b)) for c, b in skips
        ]
        self._displace(affected, actor, kind)
        booking = self.db.create_booking(booking, exceptions)
        self.invalidate()
        self.db.audit(
            actor.callsign,
            "booking.create",
            f"#{booking.id} {kind.value} {radio.id} {booking.start.isoformat(timespec='minutes')} {booking.tz}"
            f" {booking.duration_minutes}min {describe_rrule(booking.rrule)}"
            + (f"; {len(skips)} skipped" if skips else "")
            + (f"; displaced {len(affected)}" if affected else ""),
        )
        result["booking"] = self.booking_payload(booking)
        return result

    def _resolve(
        self,
        booking: Booking,
        candidates: list[Occurrence],
        override: bool,
        now: datetime,
        ignore: set[tuple[int, str]] | None = None,
        ignore_booking: int | None = None,
    ) -> tuple[list[tuple[Occurrence, Occurrence]], list[Occurrence]]:
        """Decide what happens to each candidate date.

        Returns (skips, affected): dates of this booking that must be skipped
        because something holds them, and other members' dates an override
        will remove. A one-off booking that cannot have its date is refused,
        as is a series none of whose dates are free.
        """
        ignore = ignore or set()
        existing = [
            o
            for o in self.all_occurrences()
            if o.end > now
            and o.booking_id != ignore_booking
            and (o.booking_id, o.occ_date) not in ignore
        ]
        skips: list[tuple[Occurrence, Occurrence]] = []
        affected: list[Occurrence] = []
        for cand in candidates:
            if cand.end <= now:
                continue
            blockers = [o for o in existing if o.radio_id == cand.radio_id and o.overlaps(cand.start, cand.end)]
            if not blockers:
                continue
            # An override clears members' bookings, never a maintenance block.
            hard = [b for b in blockers if not override or b.kind is BookingKind.MAINTENANCE]
            if hard:
                skips.append((cand, hard[0]))
            else:
                affected.extend(b for b in blockers if b not in affected)

        live = [c for c in candidates if c.end > now]
        if skips and not booking.rrule:
            cand, blocker = skips[0]
            raise BookingError(
                409,
                f"{self._radio_view(cand.radio_id)['name']} is already booked then: "
                f"{_who(blocker)} {fmt_local(blocker.start, booking.tz)}-"
                f"{blocker.end.astimezone(zone(booking.tz)).strftime('%H:%M')}. Pick another time.",
                conflict=self.occurrence_payload(blocker),
            )
        if live and len(skips) == len(live):
            raise BookingError(409, "every date in that series clashes with an existing booking")
        return skips, affected

    def _outcome(
        self,
        booking: Booking,
        candidates: list[Occurrence],
        skips: list[tuple[Occurrence, Occurrence]],
        affected: list[Occurrence],
        dry_run: bool,
    ) -> dict[str, Any]:
        skipped = {c.occ_date: b for c, b in skips}
        dates = [
            {
                "date": c.occ_date,
                "start": isoformat(c.start),
                "end": isoformat(c.end),
                "skipped": c.occ_date in skipped,
                "blocked_by": _who(skipped[c.occ_date]) if c.occ_date in skipped else "",
            }
            for c in candidates
        ]
        kept = [c for c in candidates if c.occ_date not in skipped]
        tz = booking.tz
        summary = f"Booked {len(kept)} of {len(candidates)}" if booking.rrule else "Booked"
        if dry_run:
            summary = summary.replace("Booked", "Would book", 1)
        if skips:
            summary += ". Skipped " + ", ".join(
                f"{fmt_day(c.start, tz).rsplit(' ', 1)[0]} ({_who(b)})" for c, b in skips
            )
        if affected:
            summary += f". Removes {len(affected)} date(s) from other bookings: " + ", ".join(
                f"{fmt_local(o.start, tz)} ({o.owner})" for o in affected
            )
        return {
            "booking": None,
            "dry_run": dry_run,
            "dates": dates,
            "total": len(candidates),
            "booked": len(kept),
            "last": isoformat(kept[-1].start) if kept else None,
            "skipped": [d for d in dates if d["skipped"]],
            "affected": [self.occurrence_payload(o) for o in affected],
            "summary": summary + ".",
        }

    def _displace(self, affected: list[Occurrence], actor: User, kind: BookingKind) -> None:
        """Remove other members' dates for an override, and tell them."""
        why = "the radio is being serviced" if kind is BookingKind.MAINTENANCE else f"{actor.callsign} booked over it"
        for occ in affected:
            booking = self.db.get_booking(occ.booking_id)
            if booking is None:
                continue
            if booking.rrule:
                self.db.put_exception(
                    BookingException(booking.id, occ.occ_date, "skip", "override", blocked_by=actor.callsign)
                )
            else:
                self.db.delete_booking(booking.id)
            radio = self._radio_view(occ.radio_id)["name"]
            if occ.owner != actor.callsign:
                self.db.add_notice(
                    occ.owner,
                    f"Your booking of {radio} on {fmt_local(occ.start, booking.tz)} was cancelled: {why}.",
                )
            self.db.audit(actor.callsign, "booking.displaced", f"#{booking.id} {occ.occ_date} ({occ.owner})")
        self.invalidate()

    # -- changing ----------------------------------------------------------

    def edit(
        self,
        actor: User,
        booking_id: int,
        changes: BookingChanges,
        scope: str = "all",
        occ_date: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        booking = self._booking(booking_id)
        self._check_owner(actor, booking)
        scope = self._scope(scope, booking, occ_date)
        if scope == "this":
            return self._edit_one(actor, booking, changes, occ_date, dry_run)
        if scope == "following":
            starts = local_starts(booking)
            index = self._index_of(starts, occ_date)
            if index > 0:
                return self._edit_following(actor, booking, changes, starts, index, dry_run)
        return self._edit_all(actor, booking, changes, dry_run)

    def _edit_one(
        self, actor: User, booking: Booking, changes: BookingChanges, occ_date: str, dry_run: bool
    ) -> dict[str, Any]:
        if changes.radio or changes.mode or changes.repeat or changes.count or changes.until:
            raise BookingError(
                400, "a single date can only change its time and length; change the radio, mode or "
                "repeat for this and later dates, or the whole series"
            )
        exceptions = self.db.exceptions_for(booking.id)
        current = next(
            (o for o in occurrences(booking, exceptions) if o.occ_date == occ_date), None
        )
        if current is None:
            raise BookingError(404, f"booking #{booking.id} has no date {occ_date}")
        tz = zone(booking.tz)
        new_start = (
            self._schedule_error(lambda: parse_local_start(changes.start))
            if changes.start
            else current.start.astimezone(tz).replace(tzinfo=None)
        )
        minutes = (
            self._schedule_error(lambda: parse_duration(changes.duration))
            if changes.duration is not None
            else int((current.end - current.start).total_seconds() // 60)
        )
        moved = BookingException(booking.id, occ_date, "move", "owner", new_start=new_start,
                                 new_duration_minutes=minutes)
        others = [e for e in exceptions if e.occ_date != occ_date] + [moved]
        candidate = next(o for o in occurrences(booking, others) if o.occ_date == occ_date)
        now = utcnow()
        if candidate.start < now - timedelta(minutes=1) and candidate.start != current.start:
            raise BookingError(400, f"{fmt_local(candidate.start, booking.tz)} is in the past")
        self._schedule_error(lambda: check_horizon([candidate], now))
        one_off = replace(booking, rrule=None)  # a single date is refused, never skipped
        skips, affected = self._resolve(
            one_off, [candidate], booking.kind is BookingKind.MAINTENANCE, now,
            ignore={(booking.id, occ_date)},
        )
        result = self._outcome(one_off, [candidate], skips, affected, dry_run)
        if dry_run:
            return result
        self._displace(affected, actor, booking.kind)
        if booking.rrule:
            self.db.put_exception(moved)
        else:
            booking.start, booking.duration_minutes = new_start, minutes
            self.db.update_booking(booking)
        self.invalidate()
        self._audit_change(actor, booking, f"date {occ_date} moved to {new_start.isoformat(timespec='minutes')} {minutes}min")
        result["booking"] = self.booking_payload(booking)
        return result

    def _apply_changes(self, booking: Booking, changes: BookingChanges) -> Booking:
        updated = replace(booking)
        if changes.radio:
            updated.radio_id = self._radio(changes.radio).id
        if changes.mode:
            if booking.kind is BookingKind.MAINTENANCE:
                raise BookingError(400, "a maintenance block has no mode")
            updated.mode = self._enum(BookingMode, changes.mode, "mode")
        if changes.start:
            updated.start = self._schedule_error(lambda: parse_local_start(changes.start))
        if changes.duration is not None:
            updated.duration_minutes = self._schedule_error(lambda: parse_duration(changes.duration))
        if changes.repeat is not None:
            updated.rrule = self._schedule_error(lambda: build_rrule(changes.repeat, updated.start))
        elif changes.count is not None or changes.until is not None:
            if not booking.rrule:
                raise BookingError(400, "only a repeating booking has a count or an end date")
            if changes.count is not None and changes.until is not None:
                raise BookingError(400, "give a count or an end date, not both")
            if changes.until is not None:
                # Validate the date the same way a new series would.
                self._schedule_error(lambda: build_rrule(RepeatSpec("daily", until=changes.until), updated.start))
            updated.rrule = _with_end(booking.rrule, count=changes.count, until=changes.until)
        if updated.rrule != booking.rrule:
            updated.ending_notice_sent = False
        return updated

    def _edit_all(self, actor: User, booking: Booking, changes: BookingChanges, dry_run: bool) -> dict[str, Any]:
        updated = self._apply_changes(booking, changes)
        kept = [e for e in self.db.exceptions_for(booking.id) if e.reason != "conflict"]
        now = utcnow()
        candidates = self._schedule_error(lambda: occurrences(updated, kept))
        self._schedule_error(lambda: check_horizon(candidates, now))
        if not booking.rrule and updated.start != booking.start and candidates[0].start < now - timedelta(minutes=1):
            raise BookingError(400, f"{fmt_local(candidates[0].start, updated.tz)} is in the past")
        skips, affected = self._resolve(
            updated, candidates, booking.kind is BookingKind.MAINTENANCE, now, ignore_booking=booking.id
        )
        result = self._outcome(updated, candidates, skips, affected, dry_run)
        if dry_run:
            return result
        self._displace(affected, actor, booking.kind)
        self.db.update_booking(updated)
        self.db.delete_exceptions(booking.id, reason="conflict")
        for cand, blocker in skips:
            self.db.put_exception(
                BookingException(booking.id, cand.occ_date, "skip", "conflict", blocked_by=_who(blocker))
            )
        self.invalidate()
        self._audit_change(actor, updated, f"whole booking: {describe_rrule(updated.rrule)}")
        result["booking"] = self.booking_payload(updated)
        return result

    def _edit_following(
        self,
        actor: User,
        booking: Booking,
        changes: BookingChanges,
        starts: list[datetime],
        index: int,
        dry_run: bool,
    ) -> dict[str, Any]:
        split_date = starts[index].date().isoformat()
        head_rule = _with_end(booking.rrule, until=starts[index - 1].date().isoformat())
        fields = dict(part.split("=", 1) for part in booking.rrule.split(";"))
        tail_rule = (
            _with_end(booking.rrule, count=len(starts) - index) if "COUNT" in fields else booking.rrule
        )
        tail = replace(booking, id=0, start=starts[index], rrule=tail_rule, created_at=utcnow(),
                       ending_notice_sent=False)
        tail = self._apply_changes(tail, changes)

        carried = [
            e for e in self.db.exceptions_for(booking.id)
            if e.occ_date >= split_date and e.reason != "conflict"
        ]
        now = utcnow()
        candidates = self._schedule_error(lambda: occurrences(tail, carried))
        self._schedule_error(lambda: check_horizon(candidates, now))
        later = {(booking.id, d.date().isoformat()) for d in starts[index:]}
        skips, affected = self._resolve(
            tail, candidates, booking.kind is BookingKind.MAINTENANCE, now, ignore=later
        )
        result = self._outcome(tail, candidates, skips, affected, dry_run)
        if dry_run:
            return result
        self._displace(affected, actor, booking.kind)
        booking.rrule = head_rule
        booking.ending_notice_sent = True  # its successor carries on; no "series ending" notice
        self.db.update_booking(booking)
        tail = self.db.create_booking(
            tail,
            [replace(e, booking_id=0) for e in carried]
            + [
                BookingException(0, c.occ_date, "skip", "conflict", blocked_by=_who(b))
                for c, b in skips
            ],
        )
        self.db.delete_exceptions(booking.id, from_date=split_date)
        self.invalidate()
        self._audit_change(actor, tail, f"split from #{booking.id} at {split_date}: {describe_rrule(tail.rrule)}")
        result["booking"] = self.booking_payload(tail)
        result["previous"] = self.booking_payload(booking)
        return result

    # -- removing ----------------------------------------------------------

    def remove(self, actor: User, booking_id: int, scope: str = "all", occ_date: str | None = None) -> dict[str, Any]:
        booking = self._booking(booking_id)
        self._check_owner(actor, booking)
        scope = self._scope(scope, booking, occ_date)
        radio = self._radio_view(booking.radio_id)["name"]
        if scope == "this" and booking.rrule:
            starts = local_starts(booking)
            self._index_of(starts, occ_date)
            self.db.put_exception(BookingException(booking.id, occ_date, "skip", "owner", blocked_by=actor.callsign))
            detail, message = f"date {occ_date} cancelled", f"Cancelled {radio} on {occ_date}."
        elif scope == "following" and booking.rrule:
            starts = local_starts(booking)
            index = self._index_of(starts, occ_date)
            if index == 0:
                return self.remove(actor, booking_id, "all")
            booking.rrule = _with_end(booking.rrule, until=starts[index - 1].date().isoformat())
            booking.ending_notice_sent = True
            self.db.update_booking(booking)
            detail = f"dates from {occ_date} cancelled"
            message = f"Cancelled {radio} from {occ_date} on; the series now ends {starts[index - 1].date()}."
        else:
            self.db.delete_booking(booking.id)
            detail, message = "deleted", f"Deleted booking #{booking.id} ({radio})."
        self.invalidate()
        self._audit_change(actor, booking, detail, action="booking.remove")
        return {"removed": booking.id, "scope": scope, "detail": message}

    # -- notices -----------------------------------------------------------

    def send_ending_notices(self, now: datetime | None = None) -> int:
        """Tell owners a month (configurable) before a series' last date."""
        now = now or utcnow()
        window = self.config.schedule.ending_notice
        sent = 0
        for booking in self.db.list_bookings():
            if not booking.rrule or booking.ending_notice_sent:
                continue
            occs = occurrences(booking, self.db.exceptions_for(booking.id), include_skipped=True)
            if not occs or occs[-1].start <= now or now < occs[-1].start - window:
                continue
            radio = self._radio_view(booking.radio_id)["name"]
            self.db.add_notice(
                booking.owner,
                f"Your series #{booking.id} on {radio} ({describe_rrule(booking.rrule)}) ends "
                f"{fmt_day(occs[-1].start, booking.tz)}. To extend it, run: "
                f"k4cm schedule edit {booking.id} --until YYYY-MM-DD",
            )
            booking.ending_notice_sent = True
            self.db.update_booking(booking)
            sent += 1
        return sent

    # -- helpers -----------------------------------------------------------

    def _radio(self, ref: str) -> Radio:
        radio = self.config.find_radio(ref)
        if radio is None:
            known = ", ".join(sorted(self.config.radios)) or "none configured"
            raise BookingError(404, f"no such radio: {ref} (known radios: {known})")
        return radio

    def _booking(self, booking_id: int) -> Booking:
        booking = self.db.get_booking(booking_id)
        if booking is None:
            raise BookingError(404, f"no such booking: #{booking_id}")
        return booking

    @staticmethod
    def _check_owner(actor: User, booking: Booking) -> None:
        if actor.is_admin:
            return
        if booking.kind is BookingKind.MAINTENANCE or booking.owner != actor.callsign:
            raise BookingError(403, f"booking #{booking.id} belongs to {booking.owner}; only they or an admin can change it")

    @staticmethod
    def _scope(scope: str, booking: Booking, occ_date: str | None) -> str:
        scope = (scope or "all").lower()
        if scope not in SCOPES:
            raise BookingError(400, f"scope must be one of {', '.join(SCOPES)}")
        if scope != "all" and not occ_date:
            raise BookingError(400, f"scope '{scope}' needs the date it applies to (--date YYYY-MM-DD)")
        if scope == "following" and not booking.rrule:
            return "all"
        return scope

    @staticmethod
    def _index_of(starts: list[datetime], occ_date: str | None) -> int:
        for index, start in enumerate(starts):
            if start.date().isoformat() == occ_date:
                return index
        raise BookingError(404, f"that booking has no date {occ_date}")

    @staticmethod
    def _enum(enum_type, value: str, name: str):
        try:
            return enum_type(str(value).lower())
        except ValueError as e:
            raise BookingError(400, f"{name} must be one of {', '.join(m.value for m in enum_type)}") from e

    @staticmethod
    def _schedule_error(fn):
        try:
            return fn()
        except ScheduleError as e:
            raise BookingError(400, str(e)) from e

    def _audit_change(self, actor: User, booking: Booking, detail: str, action: str = "booking.edit") -> None:
        self.db.audit(actor.callsign, action, f"#{booking.id} {booking.radio_id}: {detail}")
        if actor.callsign != booking.owner and booking.kind is BookingKind.BOOKING:
            radio = self._radio_view(booking.radio_id)["name"]
            self.db.add_notice(booking.owner, f"{actor.callsign} changed your booking #{booking.id} on {radio}: {detail}.")
