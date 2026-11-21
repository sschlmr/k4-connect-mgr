"""Bookings: recurrence rules, expansion to concrete time slots, conflicts.

A recurring booking is stored as an RFC 5545 RRULE plus a naive *local* start
time and the owner's IANA time zone, and is expanded in local time before being
converted to UTC. That is what keeps a weekly 19:00 booking at 19:00 on both
sides of a daylight-saving change; expanding in UTC would drift it by an hour.

Members never see RRULE text. `RepeatSpec` is the friendly form the CLI and the
calendar send ("weekly on tue,thu", "monthly on 3rd-tue", "monthly on 1,15,last")
and `build_rrule` turns it into a rule. Dates of the month stop at 28 so that
every month has them; "last" covers month ends.

Every series must end (a count or an ISO end date), and no occurrence may start
more than `HORIZON` from now: there are no open-ended bookings to expand.

Each occurrence is keyed by the local date it would have had (`occ_date`).
Exceptions — a date skipped, or moved/resized on its own — hang on that key, so
a moved occurrence keeps its identity.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrulestr

from .models import Booking, BookingException, Occurrence

HORIZON = timedelta(days=365)

FREQUENCIES = ("daily", "weekly", "monthly", "yearly")

_WEEKDAYS = {
    "mo": "MO", "mon": "MO", "monday": "MO",
    "tu": "TU", "tue": "TU", "tues": "TU", "tuesday": "TU",
    "we": "WE", "wed": "WE", "wednesday": "WE",
    "th": "TH", "thu": "TH", "thur": "TH", "thurs": "TH", "thursday": "TH",
    "fr": "FR", "fri": "FR", "friday": "FR",
    "sa": "SA", "sat": "SA", "saturday": "SA",
    "su": "SU", "sun": "SU", "sunday": "SU",
}
_WEEKDAY_ORDER = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
_WEEKDAY_NAMES = dict(zip(_WEEKDAY_ORDER, ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], strict=True))
_POSITIONS = {"1st": 1, "first": 1, "2nd": 2, "second": 2, "3rd": 3, "third": 3,
              "4th": 4, "fourth": 4, "last": -1}
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ScheduleError(ValueError):
    """A booking request that cannot be honoured; the message is for the member."""


@dataclass
class RepeatSpec:
    """How a booking repeats, in the terms members use.

    `on` depends on `freq`: weekdays for weekly ("tue", "thu"); for monthly,
    either dates ("1", "15", "last") or positions ("3rd-tue", "last-fri").
    Exactly one of `count` and `until` ends the series.
    """

    freq: str
    every: int = 1
    on: list[str] = field(default_factory=list)
    count: int | None = None
    until: str | None = None       # ISO local date, inclusive


# -- parsing ---------------------------------------------------------------


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ScheduleError(f"unknown time zone: {name!r}") from e


START_HELP = (
    "now, noon, 9:00, 2pm, today 14:30, tomorrow 9am, tue 19:00 or 2026-10-20 19:00"
)
_START_ERROR = f"start must be one of: {START_HELP} — not {{text!r}}"
_CLOCK = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$")
_ISO_START = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[ t]+|$)")


def _parse_clock(text: str) -> time | None:
    """'noon', '9:00', '09:00', '14:30', '2pm', '2:30pm' → time; None if not a time.

    A bare number with no minutes and no am/pm ('2', '14') is refused: it is
    unclear whether '2' means 02:00 or 14:00.
    """
    if text == "noon":
        return time(12, 0)
    match = _CLOCK.fullmatch(text)
    if not match:
        return None
    hour, minute, half = int(match.group(1)), int(match.group(2) or 0), match.group(3)
    if half:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if half == "pm" else 0)
    elif match.group(2) is None:
        return None
    if hour > 23 or minute > 59:
        return None
    return time(hour, minute)


def parse_local_start(text: str, tz: str | ZoneInfo | None = None, now: datetime | None = None) -> datetime:
    """A start as members type it → naive local datetime, to the minute.

    Besides '2026-10-20 19:00' (or with a T), this takes the words admins reach
    for when they need a radio *now*: 'now', 'noon', a time today ('9:00',
    '2pm', 'at 14:30'), 'today'/'tomorrow' or a weekday plus a time. Words are
    relative, so they need the booking's zone and are resolved where the
    booking is made — on the server, at each call — which keeps a 'now'
    preview valid however long the admin takes to confirm it.

    A relative start that has already passed is an error rather than being
    rolled forward: moving a block to another day without being asked is the
    kind of slip the preview cannot be relied on to catch.
    """
    raw = " ".join(text.strip().lower().split())
    raw = raw.removeprefix("at ")
    iso = _ISO_START.match(raw)
    if iso:
        day_text, rest = iso.group(1), raw[iso.end():]
        try:
            day = date.fromisoformat(day_text)
        except ValueError as e:
            raise ScheduleError(f"{day_text!r} is not a real date") from e
        clock = _parse_clock(rest.removeprefix("at "))
        if clock is None:
            if re.search(r"[+-]\d{2}:?\d{2}$|z$", rest):
                raise ScheduleError("give the start in your local time, without a time-zone offset")
            raise ScheduleError(_START_ERROR.format(text=text))
        return datetime.combine(day, clock)

    if tz is None:
        raise ScheduleError(_START_ERROR.format(text=text))
    zone_info = tz if isinstance(tz, ZoneInfo) else zone(tz)
    here = (now or datetime.now(UTC)).astimezone(zone_info).replace(tzinfo=None, second=0, microsecond=0)
    if raw == "now":
        return here

    day_word, _, rest = raw.partition(" ")
    if day_word == "today":
        day, label = here.date(), "today"
    elif day_word == "tomorrow":
        day, label = here.date() + timedelta(days=1), "tomorrow"
    elif day_word in _WEEKDAYS:
        ahead = (_WEEKDAY_ORDER.index(_WEEKDAYS[day_word]) - here.weekday()) % 7
        day = here.date() + timedelta(days=ahead)
        label = "today" if ahead == 0 else f"{day:%a} {day.day} {day:%b}"
    else:
        day, label, rest = here.date(), "today", raw
    clock = _parse_clock(rest.removeprefix("at "))
    if clock is None:
        raise ScheduleError(_START_ERROR.format(text=text))
    value = datetime.combine(day, clock)
    if value < here:
        raise ScheduleError(
            f"{clock:%H:%M} {label} has already passed (it is {here:%H:%M} now). "
            f"Did you mean 'tomorrow {clock:%H:%M}', or 'now'?"
        )
    return value


_DURATION = re.compile(r"^(?:(\d+)h)?\s*(?:(\d+)m)?$")


def parse_duration(text: str | int) -> int:
    """'2h', '90m', '1h30m', '2:00' or a number of minutes → minutes."""
    if isinstance(text, int):
        minutes = text
    else:
        raw = text.strip().lower()
        if re.fullmatch(r"\d+", raw):
            minutes = int(raw)
        elif re.fullmatch(r"\d+:\d{2}", raw):
            hours, mins = raw.split(":")
            minutes = int(hours) * 60 + int(mins)
        else:
            match = _DURATION.fullmatch(raw)
            if not match or not (match.group(1) or match.group(2)):
                raise ScheduleError(f"duration must look like 2h, 90m or 1h30m, not {text!r}")
            minutes = int(match.group(1) or 0) * 60 + int(match.group(2) or 0)
    if minutes <= 0:
        raise ScheduleError("duration must be more than zero")
    return minutes


def parse_until(text: str) -> date:
    """End dates are ISO only: 12/11/2026 means different days to different members."""
    raw = text.strip()
    if not _ISO_DATE.fullmatch(raw):
        raise ScheduleError(f"end date must be an ISO date such as 2026-12-31, not {text!r}")
    try:
        return date.fromisoformat(raw)
    except ValueError as e:
        raise ScheduleError(f"{text!r} is not a real date") from e


def _weekday(token: str) -> str:
    code = _WEEKDAYS.get(token.strip().lower())
    if code is None:
        raise ScheduleError(f"{token!r} is not a day of the week (use mon, tue, ...)")
    return code


def _month_tokens(tokens: list[str]) -> tuple[str, str]:
    """Monthly 'on' values → ('BYMONTHDAY', '1,15,-1') or ('BYDAY', '3TU,-1FR')."""
    dates, positions = [], []
    for token in tokens:
        t = token.strip().lower()
        if t == "last":
            dates.append(-1)
        elif t.isdigit():
            day = int(t)
            if not 1 <= day <= 28:
                raise ScheduleError(
                    f"day {day} is not allowed: use 1 to 28, or 'last' for the last day of the month"
                )
            dates.append(day)
        elif "-" in t:
            pos, _, wd = t.partition("-")
            if pos not in _POSITIONS:
                raise ScheduleError(f"{token!r}: use 1st, 2nd, 3rd, 4th or last, as in 3rd-tue")
            positions.append(f"{_POSITIONS[pos]}{_weekday(wd)}")
        else:
            raise ScheduleError(f"{token!r} is neither a date (1-28, last) nor a position (3rd-tue)")
    if dates and positions:
        raise ScheduleError("repeat monthly either by date (1,15) or by position (3rd-tue), not both")
    if dates:
        return "BYMONTHDAY", ",".join(str(d) for d in sorted(set(dates), key=lambda d: (d < 0, d)))
    return "BYDAY", ",".join(dict.fromkeys(positions))


def build_rrule(spec: RepeatSpec, start: datetime) -> str:
    """Turn a RepeatSpec into an RRULE body (no DTSTART)."""
    freq = spec.freq.strip().lower()
    if freq not in FREQUENCIES:
        raise ScheduleError(f"repeat must be one of {', '.join(FREQUENCIES)}")
    if spec.every < 1:
        raise ScheduleError("'every' must be at least 1")
    if (spec.count is None) == (spec.until is None):
        raise ScheduleError("a repeating booking needs exactly one of a count or an end date")
    if spec.count is not None and spec.count < 1:
        raise ScheduleError("count must be at least 1")

    parts = [f"FREQ={freq.upper()}"]
    if spec.every > 1:
        parts.append(f"INTERVAL={spec.every}")
    tokens = [t for t in spec.on if t.strip()]
    if freq == "weekly":
        days = sorted({_weekday(t) for t in tokens}, key=_WEEKDAY_ORDER.index) if tokens else [
            _WEEKDAY_ORDER[start.weekday()]
        ]
        parts.append(f"BYDAY={','.join(days)}")
    elif freq == "monthly":
        if not tokens:
            if start.day > 28:
                raise ScheduleError(
                    "the start is after the 28th: say which day to repeat on (1-28 or 'last')"
                )
            tokens = [str(start.day)]
        key, value = _month_tokens(tokens)
        parts.append(f"{key}={value}")
    elif tokens:
        raise ScheduleError(f"'on' is only used with weekly or monthly repeats, not {freq}")

    if spec.count is not None:
        parts.append(f"COUNT={spec.count}")
    else:
        end = parse_until(spec.until)
        if end < start.date():
            raise ScheduleError(f"the end date {end} is before the start {start.date()}")
        parts.append(f"UNTIL={end.strftime('%Y%m%d')}T235959")
    return ";".join(parts)


def describe_rrule(rule: str | None) -> str:
    """A short human description of a stored rule, for listings."""
    if not rule:
        return "once"
    fields = dict(part.split("=", 1) for part in rule.split(";"))
    freq = fields["FREQ"].lower()
    every = int(fields.get("INTERVAL", 1))
    unit = {"daily": "day", "weekly": "week", "monthly": "month", "yearly": "year"}[freq]
    text = freq if every == 1 else f"every {every} {unit}s"
    if "BYDAY" in fields:
        names = []
        for code in fields["BYDAY"].split(","):
            pos, wd = code[:-2], code[-2:]
            day = _WEEKDAY_NAMES[wd]
            if pos:
                ordinal = {"1": "1st", "2": "2nd", "3": "3rd", "4": "4th", "-1": "last"}[pos]
                names.append(f"{ordinal} {day}")
            else:
                names.append(day)
        text += " on " + ", ".join(names)
    if "BYMONTHDAY" in fields:
        days = ["last day" if d == "-1" else _ordinal(int(d)) for d in fields["BYMONTHDAY"].split(",")]
        text += " on the " + ", ".join(days)
    if "COUNT" in fields:
        text += f", {fields['COUNT']} times"
    if "UNTIL" in fields:
        u = fields["UNTIL"]
        text += f", until {u[0:4]}-{u[4:6]}-{u[6:8]}"
    return text


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


# -- expansion -------------------------------------------------------------


def local_starts(booking: Booking) -> list[datetime]:
    """Every naive local start the rule produces, before exceptions."""
    if not booking.rrule:
        return [booking.start]
    return list(rrulestr(booking.rrule, dtstart=booking.start))


def to_utc(local: datetime, tz: ZoneInfo) -> datetime:
    return local.replace(tzinfo=tz).astimezone(UTC)


def occurrences(
    booking: Booking,
    exceptions: list[BookingException] | None = None,
    include_skipped: bool = False,
) -> list[Occurrence]:
    """The booking's concrete time slots in UTC, oldest first.

    Skipped dates are left out unless `include_skipped`; moved dates appear at
    their new time under their original key.
    """
    tz = zone(booking.tz)
    by_date = {e.occ_date: e for e in exceptions or []}
    result = []
    for local in local_starts(booking):
        key = local.date().isoformat()
        start, minutes = local, booking.duration_minutes
        exc = by_date.get(key)
        if exc is not None and exc.action == "skip" and not include_skipped:
            continue
        if exc is not None and exc.action == "move":
            start = exc.new_start or start
            minutes = exc.new_duration_minutes or minutes
        begin = to_utc(start, tz)
        result.append(
            Occurrence(
                booking_id=booking.id,
                occ_date=key,
                radio_id=booking.radio_id,
                owner=booking.owner,
                kind=booking.kind,
                mode=booking.mode,
                start=begin,
                end=begin + timedelta(minutes=minutes),
                reason=booking.reason,
            )
        )
    result.sort(key=lambda o: o.start)
    return result


def check_horizon(occs: list[Occurrence], now: datetime) -> None:
    if not occs:
        raise ScheduleError("that repeat pattern produces no dates")
    limit = now + HORIZON
    late = [o for o in occs if o.start > limit]
    if late:
        raise ScheduleError(
            f"bookings can reach at most one year ahead (until {limit.date()}); "
            f"this one runs to {occs[-1].start.date()}. Use a smaller count or an earlier end date."
        )


def conflicts(
    candidates: list[Occurrence], existing: list[Occurrence]
) -> list[tuple[Occurrence, Occurrence]]:
    """Pairs (candidate, existing) that overlap on the same radio."""
    by_radio: dict[str, list[Occurrence]] = {}
    for occ in existing:
        by_radio.setdefault(occ.radio_id, []).append(occ)
    clashes = []
    for cand in candidates:
        for occ in by_radio.get(cand.radio_id, []):
            if occ.overlaps(cand.start, cand.end):
                clashes.append((cand, occ))
                break
    return clashes
