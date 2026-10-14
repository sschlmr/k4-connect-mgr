"""End-to-end: the `k4cm schedule` commands and `request --duration` against a live server."""

from __future__ import annotations

from datetime import datetime, timedelta

from test_e2e import live, run_cli  # noqa: F401  (live is a fixture)

from k4cm.client.config import load_config
from k4cm.schedule import zone


def tomorrow_at(hour: int) -> datetime:
    tz = zone(load_config().local_timezone)
    return (datetime.now(tz) + timedelta(days=1)).replace(hour=hour, minute=0, second=0, microsecond=0)


async def test_book_list_show_skip_and_remove(live):  # noqa: F811
    start = tomorrow_at(19)
    added = await run_cli(
        "schedule", "add", "k4a", "--start", start.strftime("%Y-%m-%d %H:%M"), "--duration", "2h",
        "--repeat", "daily", "--count", "3", "--yes",
    )
    assert added.exit_code == 0, added.output
    assert "Booked 3 of 3" in added.output and "Booking #1" in added.output
    assert "last on" in added.output

    listed = await run_cli("schedule", "list", "--mine")
    assert listed.exit_code == 0, listed.output
    assert listed.output.count("K4 Alpha") == 3 and "W1AW (you)" in listed.output

    day2 = (start + timedelta(days=1)).date().isoformat()
    skipped = await run_cli("schedule", "skip", "1", "--date", day2)
    assert skipped.exit_code == 0, skipped.output

    shown = await run_cli("schedule", "show", "1")
    assert "daily, 3 times" in shown.output and "skipped (cancelled)" in shown.output

    removed = await run_cli("schedule", "remove", "1", "--yes")
    assert removed.exit_code == 0 and "Deleted booking #1" in removed.output


async def test_booking_needs_yes_when_not_interactive(live):  # noqa: F811
    result = await run_cli(
        "schedule", "add", "k4a", "--start", tomorrow_at(10).strftime("%Y-%m-%d %H:%M"), "--duration", "1h"
    )
    assert result.exit_code == 2
    assert "--yes" in result.output
    assert live.fixture.db.list_bookings() == []


async def test_bad_end_date_is_caught_before_the_server(live):  # noqa: F811
    result = await run_cli(
        "schedule", "add", "k4a", "--start", tomorrow_at(10).strftime("%Y-%m-%d %H:%M"), "--duration", "1h",
        "--repeat", "weekly", "--until", "12/31/2026", "--yes",
    )
    assert result.exit_code == 2 and "ISO" in result.output


async def test_service_block_is_admin_only(live):  # noqa: F811
    result = await run_cli(
        "admin", "service", "k4a", "--start", tomorrow_at(9).strftime("%Y-%m-%d %H:%M"), "--duration", "4h",
        "--yes",
    )
    assert result.exit_code == 1 and "administrator" in result.output


async def test_request_with_a_duration(live):  # noqa: F811
    result = await run_cli("request", "k4a", "--duration", "30m", "--no-watch", "--no-connect")
    assert result.exit_code == 0, result.output
    assert "Time allowed: 30m" in result.output or "Time allowed: 29m" in result.output
