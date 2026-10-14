"""Bookings: recurrence, conflicts, overrides, edits, and how bookings shape sessions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from k4cm.models import AssignmentState, Booking, BookingKind, BookingMode, utcnow
from k4cm.schedule import (
    RepeatSpec,
    ScheduleError,
    build_rrule,
    describe_rrule,
    occurrences,
    parse_duration,
    parse_until,
)
from k4cm.schedule import parse_local_start as wall
from k4cm.server.bookings import BookingChanges, BookingError, BookingInput
from k4cm.server.manager import ManagerError


def at(minutes: int) -> datetime:
    """A UTC time `minutes` from now, on a whole minute."""
    return (utcnow() + timedelta(minutes=minutes)).replace(second=0, microsecond=0)


def text(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%d %H:%M")


@pytest.fixture
def members(server):
    server.add_user("ADMIN", is_admin=True)
    server.add_user("W1AW")
    server.add_user("K2ABC")
    return {c: server.db.get_user(c) for c in ("ADMIN", "W1AW", "K2ABC")}


def book(server, members, who, start, duration="2h", radio="k4a", **kw):
    return server.manager.bookings.create(
        members[who],
        BookingInput(radio=radio, start=text(start), duration=duration, tz="UTC", **kw),
        dry_run=kw.pop("dry_run", False) if "dry_run" in kw else False,
    )


def series(booking_id, server):
    return server.manager.bookings.get(booking_id)


# -- recurrence rules -------------------------------------------------------


def _expand(rule, start="2026-10-20 19:00", tz="America/New_York", minutes=120):
    b = Booking(0, "k4a", "W1AW", BookingKind.BOOKING, BookingMode.RESERVE, tz,
                wall(start), minutes, rule, utcnow())
    return occurrences(b)


def test_weekly_on_chosen_days():
    rule = build_rrule(RepeatSpec("weekly", on=["tue", "thu"], count=4), wall("2026-10-20 19:00"))
    assert rule == "FREQ=WEEKLY;BYDAY=TU,TH;COUNT=4"
    assert [o.occ_date for o in _expand(rule)] == ["2026-10-20", "2026-10-22", "2026-10-27", "2026-10-29"]


def test_monthly_by_position_including_last():
    rule = build_rrule(RepeatSpec("monthly", on=["2nd-tue", "last-fri"], count=4), wall("2026-10-01 19:00"))
    assert [o.occ_date for o in _expand(rule, "2026-10-01 19:00")] == [
        "2026-10-13", "2026-10-30", "2026-11-10", "2026-11-27",
    ]
    assert describe_rrule(rule) == "monthly on 2nd Tue, last Fri, 4 times"


def test_monthly_by_date_and_last_day():
    rule = build_rrule(RepeatSpec("monthly", on=["15", "last"], count=4), wall("2026-10-01 19:00"))
    assert [o.occ_date for o in _expand(rule, "2026-10-01 19:00")] == [
        "2026-10-15", "2026-10-31", "2026-11-15", "2026-11-30",
    ]


def test_dates_after_the_28th_are_refused():
    with pytest.raises(ScheduleError, match="1 to 28"):
        build_rrule(RepeatSpec("monthly", on=["30"], count=3), wall("2026-10-01 19:00"))


def test_daily_every_n_days():
    rule = build_rrule(RepeatSpec("daily", every=2, count=3), wall("2026-10-20 19:00"))
    assert [o.occ_date for o in _expand(rule)] == ["2026-10-20", "2026-10-22", "2026-10-24"]


def test_series_keeps_local_time_across_daylight_saving():
    rule = build_rrule(RepeatSpec("weekly", count=3), wall("2026-10-27 19:00"))
    occs = _expand(rule, "2026-10-27 19:00")
    # New York leaves daylight saving on 1 Nov: 19:00 local is 23:00 then 00:00 UTC.
    assert [o.start.hour for o in occs] == [23, 0, 0]
    assert all(o.end - o.start == timedelta(hours=2) for o in occs)


def test_end_date_is_iso_only_and_need_not_be_an_occurrence():
    with pytest.raises(ScheduleError, match="ISO"):
        parse_until("12/31/2026")
    rule = build_rrule(RepeatSpec("monthly", on=["3rd-tue"], until="2026-12-31"), wall("2026-10-20 19:00"))
    assert [o.occ_date for o in _expand(rule)] == ["2026-10-20", "2026-11-17", "2026-12-15"]


def test_a_series_must_end():
    with pytest.raises(ScheduleError, match="exactly one"):
        build_rrule(RepeatSpec("weekly"), wall("2026-10-20 19:00"))


def test_durations():
    assert parse_duration("2h") == 120
    assert parse_duration("1h30m") == 90
    assert parse_duration("90m") == 90
    assert parse_duration("2:15") == 135
    with pytest.raises(ScheduleError):
        parse_duration("soon")


# -- creating and conflicts --------------------------------------------------


async def test_overlapping_one_off_is_refused(server, members):
    book(server, members, "W1AW", at(60))
    with pytest.raises(BookingError) as excinfo:
        book(server, members, "K2ABC", at(90))
    assert excinfo.value.status_code == 409
    assert "W1AW" in excinfo.value.message


async def test_other_radio_is_not_a_conflict(server, members):
    book(server, members, "W1AW", at(60))
    assert book(server, members, "K2ABC", at(60), radio="k4b")["booking"]["id"]


async def test_series_skips_clashing_dates_and_says_whose(server, members):
    first = at(60 * 24 * 7)
    book(server, members, "W1AW", first)
    result = book(server, members, "K2ABC", first, repeat=RepeatSpec("daily", count=10))
    assert result["booked"] == 9 and result["total"] == 10
    assert result["skipped"][0]["blocked_by"] == "W1AW"
    assert "Booked 9 of 10" in result["summary"] and "W1AW" in result["summary"]
    shown = series(result["booking"]["id"], server)
    assert sum(d["skipped"] for d in shown["dates"]) == 1


async def test_dry_run_saves_nothing(server, members):
    result = server.manager.bookings.create(
        members["W1AW"], BookingInput(radio="k4a", start=text(at(60)), duration="1h", tz="UTC"), dry_run=True
    )
    assert result["dry_run"] and result["booking"] is None
    assert server.db.list_bookings() == []


async def test_bookings_reach_at_most_a_year(server, members):
    with pytest.raises(BookingError, match="one year"):
        book(server, members, "W1AW", at(60), repeat=RepeatSpec("weekly", count=60))


async def test_past_start_is_refused(server, members):
    with pytest.raises(BookingError, match="past"):
        book(server, members, "W1AW", at(-120))


# -- permissions, maintenance and overrides ------------------------------------


async def test_only_owner_or_admin_may_change_a_booking(server, members):
    booking_id = book(server, members, "W1AW", at(60))["booking"]["id"]
    with pytest.raises(BookingError) as excinfo:
        server.manager.bookings.remove(members["K2ABC"], booking_id)
    assert excinfo.value.status_code == 403
    server.manager.bookings.remove(members["ADMIN"], booking_id)
    assert server.db.get_booking(booking_id) is None
    assert any("ADMIN" in n["message"] for n in server.db.take_notices("W1AW"))


async def test_maintenance_is_admin_only(server, members):
    with pytest.raises(BookingError) as excinfo:
        book(server, members, "W1AW", at(60), kind="maintenance")
    assert excinfo.value.status_code == 403


async def test_maintenance_removes_member_dates_and_tells_them(server, members):
    start = at(60 * 24 * 3)
    one_off = book(server, members, "W1AW", start)["booking"]["id"]
    weekly = book(server, members, "K2ABC", start + timedelta(hours=3), duration="1h",
                  repeat=RepeatSpec("daily", count=3))["booking"]["id"]
    result = book(server, members, "ADMIN", start, duration="5h", kind="maintenance")
    assert len(result["affected"]) == 2
    assert server.db.get_booking(one_off) is None
    assert sum(d["skipped"] for d in series(weekly, server)["dates"]) == 1
    assert "being serviced" in server.db.take_notices("W1AW")[0]["message"]
    assert server.db.take_notices("K2ABC")


async def test_members_cannot_override(server, members):
    with pytest.raises(BookingError) as excinfo:
        book(server, members, "W1AW", at(60), override=True)
    assert excinfo.value.status_code == 403


# -- editing and removing ---------------------------------------------------


async def test_move_a_single_date(server, members):
    start = at(60 * 24)
    booking_id = book(server, members, "W1AW", start, repeat=RepeatSpec("daily", count=3))["booking"]["id"]
    day2 = (start + timedelta(days=1)).date().isoformat()
    new_time = start + timedelta(days=1, hours=3)
    server.manager.bookings.edit(members["W1AW"], booking_id, BookingChanges(start=text(new_time), duration="1h"),
                                 scope="this", occ_date=day2)
    dates = {d["date"]: d for d in series(booking_id, server)["dates"]}
    assert dates[day2]["start"].startswith(new_time.strftime("%Y-%m-%dT%H:%M"))


async def test_moving_a_date_onto_another_booking_is_refused(server, members):
    start = at(60 * 24)
    booking_id = book(server, members, "W1AW", start, repeat=RepeatSpec("daily", count=3))["booking"]["id"]
    book(server, members, "K2ABC", start + timedelta(days=1, hours=4))
    with pytest.raises(BookingError) as excinfo:
        server.manager.bookings.edit(
            members["W1AW"], booking_id, BookingChanges(start=text(start + timedelta(days=1, hours=3))),
            scope="this", occ_date=(start + timedelta(days=1)).date().isoformat(),
        )
    assert excinfo.value.status_code == 409


async def test_edit_this_and_later_splits_the_series(server, members):
    start = at(60 * 24)
    booking_id = book(server, members, "W1AW", start, repeat=RepeatSpec("daily", count=5))["booking"]["id"]
    split = (start + timedelta(days=2)).date().isoformat()
    result = server.manager.bookings.edit(
        members["W1AW"], booking_id, BookingChanges(duration="30m"), scope="following", occ_date=split
    )
    head = series(booking_id, server)
    tail = result["booking"]
    assert len(head["dates"]) == 2
    assert len(tail["dates"]) == 3 and tail["duration_minutes"] == 30
    assert tail["dates"][0]["date"] == split


async def test_extend_a_series_by_changing_its_end(server, members):
    start = at(60 * 24)
    booking_id = book(server, members, "W1AW", start, repeat=RepeatSpec("daily", count=2))["booking"]["id"]
    server.manager.bookings.edit(members["W1AW"], booking_id, BookingChanges(count=4))
    assert len(series(booking_id, server)["dates"]) == 4


async def test_remove_one_date_and_the_rest(server, members):
    start = at(60 * 24)
    booking_id = book(server, members, "W1AW", start, repeat=RepeatSpec("daily", count=4))["booking"]["id"]
    day2 = (start + timedelta(days=1)).date().isoformat()
    day3 = (start + timedelta(days=2)).date().isoformat()
    server.manager.bookings.remove(members["W1AW"], booking_id, "this", day2)
    server.manager.bookings.remove(members["W1AW"], booking_id, "following", day3)
    dates = series(booking_id, server)["dates"]
    assert [d["skipped"] for d in dates] == [False, True]


async def test_series_ending_notice_is_sent_once(server, members):
    book(server, members, "W1AW", at(60 * 24), repeat=RepeatSpec("daily", count=3))
    assert server.manager.bookings.send_ending_notices() == 1
    assert server.manager.bookings.send_ending_notices() == 0
    assert "--until" in server.db.take_notices("W1AW")[0]["message"]


# -- bookings shaping sessions --------------------------------------------------


async def test_ad_hoc_session_ends_at_the_next_booking(server, members):
    start = at(90)
    book(server, members, "K2ABC", start)
    result = await server.manager.request_assignment("W1AW", "k4a")
    assert result["expires_at"].startswith(start.strftime("%Y-%m-%dT%H:%M"))
    assert any("booked by K2ABC" in n for n in result["notices"])
    assert result["can_extend"] is False


async def test_extension_refused_when_it_would_run_into_a_booking(server, members):
    await server.manager.request_assignment("W1AW", "k4a")
    assignment = server.db.live_assignment_for_callsign("W1AW")
    server.db.set_expiry(assignment.id, at(30))
    book(server, members, "K2ABC", at(60))
    with pytest.raises(ManagerError, match="booked by K2ABC"):
        await server.manager.extend("W1AW")


async def test_duration_shortens_a_session(server, members):
    result = await server.manager.request_assignment("W1AW", "k4a", duration_minutes=30)
    assert 25 * 60 < result["seconds_remaining"] <= 30 * 60


async def test_radio_is_held_for_its_owner(server, members):
    book(server, members, "K2ABC", at(0))
    with pytest.raises(ManagerError) as excinfo:
        await server.manager.request_assignment("W1AW", "k4a")
    assert excinfo.value.extra["reserved_by"] == "K2ABC"
    result = await server.manager.request_assignment("K2ABC", "k4a")
    assert result["booking"]["owner"] == "K2ABC"
    assert result["expires_at"].startswith((at(0) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"))


async def test_borrow_after_hold_then_owner_reclaims(server, members):
    server.config.schedule.hold_minutes = 0
    book(server, members, "K2ABC", at(0))
    borrowed = await server.manager.request_assignment("W1AW", "k4a")
    assert any("reserved by K2ABC" in n for n in borrowed["notices"])

    with pytest.raises(ManagerError) as excinfo:
        await server.manager.request_assignment("K2ABC", "k4a")
    assert excinfo.value.extra["reclaiming"] is True
    borrower = server.db.live_assignment_for_callsign("W1AW")
    assert borrower.expires_at <= utcnow()
    assert server.db.queue_position("k4a", "K2ABC") == 1
    assert any("arrived" in n["message"] for n in server.db.take_notices("W1AW"))

    await server.manager.end_assignment(borrower, AssignmentState.EXPIRED, "test", "system")
    assert server.db.offer_for_radio("k4a").callsign == "K2ABC"


async def test_maintenance_refuses_requests(server, members):
    book(server, members, "ADMIN", at(0), kind="maintenance")
    with pytest.raises(ManagerError, match="Being serviced"):
        await server.manager.request_assignment("W1AW", "k4a")


async def test_new_booking_caps_a_running_session(server, members):
    await server.manager.request_assignment("W1AW", "k4a")
    assert server.manager.session_state("W1AW")["assignment"]["opened_by_booking"] is False
    book(server, members, "K2ABC", at(45))
    await server.manager.enforce_bookings()
    assert server.db.live_assignment_for_callsign("W1AW").expires_at == at(45)


async def test_auto_session_opens_and_waits_for_the_owner(server, members):
    book(server, members, "K2ABC", at(0), mode="auto")
    await server.manager.enforce_bookings()
    session = server.db.live_assignment_for_callsign("K2ABC")
    assert session is not None and session.radio_id == "k4a"
    assert server.radio_a.password == session.password
    # The owner's watcher is told the booking opened it, so it connects the panel.
    assert server.manager.session_state("K2ABC")["assignment"]["opened_by_booking"] is True
    # Asking for it again hands over the session the booking opened.
    again = await server.manager.request_assignment("K2ABC", "k4a")
    assert again["id"] == session.id
    # No panel yet, but the owner is inside the hold window: not reclaimed.
    server.config.monitor.disconnect_grace_minutes = 0
    await server.monitor.tick()
    assert server.db.live_assignment_for_callsign("K2ABC") is not None


async def test_queue_reports_latest_wait_and_refuses_useless_places(server, members):
    await server.manager.request_assignment("W1AW", "k4a")
    session = server.db.live_assignment_for_callsign("W1AW")
    server.db.set_expiry(session.id, at(30))
    queued = server.manager.enqueue("K2ABC", "k4a")
    assert queued["available_by"].startswith(at(35).strftime("%Y-%m-%dT%H:%M"))
    server.manager.leave_queue("K2ABC")

    book(server, members, "ADMIN", at(35), duration="1h")
    with pytest.raises(ManagerError, match="no time"):
        server.manager.enqueue("K2ABC", "k4a")


# -- HTTP ----------------------------------------------------------------


async def test_booking_api_round_trip(api, server):
    headers = server.headers("W1AW")
    body = {"radio": "k4b", "start": text(at(60 * 24)), "duration": "2h", "tz": "UTC",
            "repeat": {"freq": "weekly", "count": 3}}
    preview = await api.post("/bookings?dry_run=true", json=body, headers=headers)
    assert preview.status_code == 201 and preview.json()["booking"] is None
    created = (await api.post("/bookings", json=body, headers=headers)).json()
    booking_id = created["booking"]["id"]

    listing = (await api.get("/bookings", params={"end": text(at(60 * 24 * 30)).replace(" ", "T")},
                             headers=headers)).json()
    assert len(listing["occurrences"]) == 3

    member = server.headers("ADMIN")
    edited = await api.patch(f"/bookings/{booking_id}", json={"duration": "1h"}, headers=member)
    assert edited.json()["booking"]["duration_minutes"] == 60

    refused = await api.post("/bookings", json=body | {"repeat": None}, headers=member)
    assert refused.status_code == 409

    removed = await api.delete(f"/bookings/{booking_id}", headers=headers)
    assert removed.json()["removed"] == booking_id
    notices = (await api.get("/notices", headers=headers)).json()["notices"]
    assert any("ADMIN changed" in n["message"] for n in notices)
