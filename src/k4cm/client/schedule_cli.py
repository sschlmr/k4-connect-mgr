"""`k4cm schedule ...` — book radios ahead of time, and the admin's
"Being serviced" blocks.

Every booking command previews first (the server's dry run) and shows the dates
it will create, which ones are skipped because someone else holds them, and the
last date of a series, before asking to go ahead. A repeat pattern is easy to
get subtly wrong, and the list of dates makes that obvious before anything is
saved. Non-interactive use needs --yes.

Times are typed and shown in the member's local zone (the computer's, or
`[schedule] timezone` in config.toml); the server keeps the zone with the
booking so a series follows local time across daylight-saving changes.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import typer

from ..schedule import ScheduleError, parse_duration, parse_until
from .api import ApiError, ServerApi
from .config import ClientConfig

schedule_app = typer.Typer(help="Book radios ahead of time and see who has booked what", no_args_is_help=True)

REPEAT_HELP = "Repeat: daily, weekly, monthly or yearly"
ON_HELP = "Weekly: tue,thu. Monthly: dates 1,15,last or positions 3rd-tue,last-fri"


# -- shared helpers ------------------------------------------------------------


def _main():
    # Imported late: main.py imports this module to register the command group.
    from . import main

    return main


def zone_of(cfg: ClientConfig) -> ZoneInfo:
    try:
        return ZoneInfo(cfg.local_timezone)
    except (LookupError, ValueError):
        return ZoneInfo("UTC")


def local(iso: str, cfg: ClientConfig) -> datetime:
    return datetime.fromisoformat(iso).astimezone(zone_of(cfg))


def fmt_when(iso: str, cfg: ClientConfig, with_year: bool = True) -> str:
    d = local(iso, cfg)
    return f"{d:%a} {d.day} {d:%b}" + (f" {d.year}" if with_year else "") + f" {d:%H:%M}"


def fmt_span(start: str, end: str, cfg: ClientConfig) -> str:
    a, b = local(start, cfg), local(end, cfg)
    tail = f"{b:%H:%M}" if a.date() == b.date() else f"{b:%a} {b.day} {b:%b} {b:%H:%M}"
    return f"{a:%a} {a.day} {a:%b %Y} {a:%H:%M}-{tail}"


def print_notices(notices: list[dict[str, Any]] | None) -> None:
    for notice in notices or []:
        _main().warn(f"Notice: {notice['message']}")


def fetch_notices(api: ServerApi) -> None:
    try:
        print_notices(api.notices())
    except ApiError:
        pass


def _repeat(
    repeat: str | None, every: int, on: str | None, count: int | None, until: str | None
) -> dict[str, Any] | None:
    main = _main()
    if repeat is None:
        if on or count is not None or until or every != 1:
            main.err("--every, --on, --count and --until need --repeat")
            raise typer.Exit(code=2)
        return None
    if (count is None) == (until is None):
        main.err("a repeating booking needs exactly one of --count or --until")
        raise typer.Exit(code=2)
    if until:
        try:
            parse_until(until)
        except ScheduleError as e:
            main.err(str(e))
            raise typer.Exit(code=2)
    return {
        "freq": repeat.lower(),
        "every": every,
        "on": [t.strip() for t in (on or "").split(",") if t.strip()],
        "count": count,
        "until": until,
    }


def _check_duration(duration: str | None) -> None:
    if duration is None:
        return
    try:
        parse_duration(duration)
    except ScheduleError as e:
        _main().err(str(e))
        raise typer.Exit(code=2)


def _print_preview(result: dict[str, Any], cfg: ClientConfig) -> None:
    for d in result["dates"]:
        line = f"  {fmt_span(d['start'], d['end'], cfg)}"
        if d["skipped"]:
            typer.secho(f"{line}   skipped: {d['blocked_by']}", fg=typer.colors.YELLOW)
        else:
            typer.echo(line)
    for occ in result.get("affected") or []:
        _main().warn(
            f"  removes {occ['owner']}'s booking {fmt_span(occ['start'], occ['end'], cfg)} (#{occ['booking_id']})"
        )
    if result.get("last") and result["total"] > 1:
        typer.echo(f"{result['booked']} date(s), last on {fmt_when(result['last'], cfg)}")


def _confirm(question: str, yes: bool) -> bool:
    main = _main()
    if yes:
        return True
    if not main.interactive():
        main.err("Not an interactive terminal: add --yes to go ahead without confirming.")
        raise typer.Exit(code=2)
    return typer.confirm(question, default=False)


def _book(api: ServerApi, cfg: ClientConfig, body: dict[str, Any], yes: bool) -> None:
    main = _main()
    try:
        preview = api.create_booking(body, dry_run=True)
    except ApiError as e:
        main.fail(e)
    _print_preview(preview, cfg)
    if not _confirm("Book these?" if preview["total"] > 1 else "Book it?", yes):
        typer.echo("Nothing booked.")
        return
    try:
        result = api.create_booking(body)
    except ApiError as e:
        main.fail(e)
    main.ok(f"{result['summary']} Booking #{result['booking']['id']}.")


# -- commands ----------------------------------------------------------------


@schedule_app.command("add")
def add(
    radio: str = typer.Argument(..., help="Which K4 (id or name)"),
    start: str = typer.Option(..., "--start", help='Local start time, e.g. "2026-10-20 19:00"'),
    duration: str = typer.Option(..., "--duration", help="e.g. 2h, 90m, 1h30m"),
    mode: str = typer.Option("reserve", "--mode", help="reserve (hold it for you) or auto (open the session)"),
    repeat: str = typer.Option(None, "--repeat", help=REPEAT_HELP),
    every: int = typer.Option(1, "--every", help="Every N days/weeks/months"),
    on: str = typer.Option(None, "--on", help=ON_HELP),
    count: int = typer.Option(None, "--count", help="How many times (dates skipped for clashes count)"),
    until: str = typer.Option(None, "--until", help="Last day, ISO date such as 2026-12-31"),
    override: bool = typer.Option(False, "--override", help="Admin: book over members' bookings"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask for confirmation"),
) -> None:
    """Book a radio, once or on a repeating pattern."""
    api, cfg = _main().get_api()
    _check_duration(duration)
    body = {
        "radio": radio,
        "start": start,
        "duration": duration,
        "mode": mode,
        "repeat": _repeat(repeat, every, on, count, until),
        "override": override,
    }
    _book(api, cfg, body, yes)


@schedule_app.command("list")
def list_bookings(
    mine: bool = typer.Option(False, "--mine", help="Only my bookings"),
    member: str = typer.Option(None, "--member", help="Only this member's bookings"),
    radio: str = typer.Option(None, "--radio", help="Only this radio"),
    from_: str = typer.Option(None, "--from", help="First day, YYYY-MM-DD (default today)"),
    to: str = typer.Option(None, "--to", help="Last day, YYYY-MM-DD (default two weeks on)"),
) -> None:
    """List bookings: everyone's (default), yours, or another member's."""
    main = _main()
    api, cfg = main.get_api()
    tz = zone_of(cfg)
    try:
        first = parse_until(from_) if from_ else datetime.now(tz).date()
        last = parse_until(to) if to else first + timedelta(days=13)
    except ScheduleError as e:
        main.err(str(e))
        raise typer.Exit(code=2)
    start = datetime.combine(first, time(), tz).isoformat()
    end = datetime.combine(last + timedelta(days=1), time(), tz).isoformat()
    owner = cfg.callsign if mine else member
    try:
        result = api.bookings(start, end, radio, owner)
    except ApiError as e:
        main.fail(e)
    fetch_notices(api)

    rows = result["occurrences"]
    if not rows:
        typer.echo(f"No bookings from {first} to {last}.")
        return
    typer.echo(f"{'WHEN':<34} {'RADIO':<14} {'WHO':<16} {'MODE':<8} BOOKING")
    for occ in rows:
        who = occ["label"] + (" (you)" if occ["owner"] == cfg.callsign and occ["kind"] == "booking" else "")
        mode = "-" if occ["kind"] == "maintenance" else occ["mode"]
        typer.echo(
            f"{fmt_span(occ['start'], occ['end'], cfg):<34} {occ['radio']['name']:<14} {who:<16} {mode:<8} "
            f"#{occ['booking_id']}" + (" (series)" if occ["series"] else "")
        )


@schedule_app.command("show")
def show(booking_id: int = typer.Argument(..., help="Booking number, as shown by 'k4cm schedule list'")) -> None:
    """Show one booking or series, with every date and any skipped ones."""
    main = _main()
    api, cfg = main.get_api()
    try:
        b = api.get_booking(booking_id)
    except ApiError as e:
        main.fail(e)
    hours, mins = divmod(b["duration_minutes"], 60)
    kind = "Being serviced" if b["kind"] == "maintenance" else f"{b['mode']} booking"
    typer.echo(f"#{b['id']} {b['radio']['name']}: {kind} by {b['owner']}")
    typer.echo(f"  {b['repeat']}, {hours}h{mins:02d} each, times in {b['tz']}")
    for d in b["dates"]:
        line = f"  {d['date']}  {fmt_span(d['start'], d['end'], cfg)}"
        if d["skipped"]:
            why = {"conflict": f"held by {d['blocked_by']}", "override": f"removed by {d['blocked_by']}"}.get(
                d["reason"], "cancelled"
            )
            typer.secho(f"{line}   skipped ({why})", fg=typer.colors.YELLOW)
        else:
            typer.echo(line)


@schedule_app.command("edit")
def edit(
    booking_id: int = typer.Argument(..., help="Booking number"),
    scope: str = typer.Option("all", "--scope", help="this (one date), following (this and later), all"),
    on_date: str = typer.Option(None, "--date", help="The date a 'this' or 'following' edit applies to"),
    start: str = typer.Option(None, "--start", help='New local start, e.g. "2026-10-20 20:00"'),
    duration: str = typer.Option(None, "--duration", help="New length, e.g. 90m"),
    mode: str = typer.Option(None, "--mode", help="reserve or auto"),
    radio: str = typer.Option(None, "--radio", help="Move to another radio"),
    repeat: str = typer.Option(None, "--repeat", help=REPEAT_HELP + " (replaces the pattern)"),
    every: int = typer.Option(1, "--every"),
    on: str = typer.Option(None, "--on", help=ON_HELP),
    count: int = typer.Option(None, "--count", help="New number of dates (keeps the pattern)"),
    until: str = typer.Option(None, "--until", help="New last day, ISO date (keeps the pattern)"),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Change a booking: one date, this and later dates, or the whole series."""
    main = _main()
    api, cfg = main.get_api()
    _check_duration(duration)
    body: dict[str, Any] = {"scope": scope, "date": on_date, "start": start, "duration": duration,
                            "mode": mode, "radio": radio}
    if repeat:
        body["repeat"] = _repeat(repeat, every, on, count, until)
    else:
        if until:
            try:
                parse_until(until)
            except ScheduleError as e:
                main.err(str(e))
                raise typer.Exit(code=2)
        body.update(count=count, until=until)
    try:
        preview = api.edit_booking(booking_id, body, dry_run=True)
    except ApiError as e:
        main.fail(e)
    _print_preview(preview, cfg)
    if not _confirm("Make this change?", yes):
        typer.echo("Nothing changed.")
        return
    try:
        result = api.edit_booking(booking_id, body)
    except ApiError as e:
        main.fail(e)
    note = f" Later dates are now booking #{result['booking']['id']}." if result.get("previous") else ""
    main.ok(result["summary"].replace("Booked", "Updated", 1) + note)


@schedule_app.command("skip")
def skip(
    booking_id: int = typer.Argument(..., help="Booking number"),
    on_date: str = typer.Option(..., "--date", help="The date to cancel, YYYY-MM-DD"),
) -> None:
    """Cancel one date of a series."""
    main = _main()
    api, _ = main.get_api()
    try:
        main.ok(api.delete_booking(booking_id, "this", on_date)["detail"])
    except ApiError as e:
        main.fail(e)


@schedule_app.command("remove")
def remove(
    booking_id: int = typer.Argument(..., help="Booking number"),
    scope: str = typer.Option("all", "--scope", help="this, following or all"),
    on_date: str = typer.Option(None, "--date", help="The date for 'this' or 'following'"),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Delete a booking, or part of a series."""
    main = _main()
    api, _ = main.get_api()
    what = {"all": f"booking #{booking_id}", "this": f"#{booking_id} on {on_date}",
            "following": f"#{booking_id} from {on_date} on"}.get(scope, f"#{booking_id}")
    if not _confirm(f"Delete {what}?", yes):
        typer.echo("Nothing deleted.")
        return
    try:
        main.ok(api.delete_booking(booking_id, scope, on_date)["detail"])
    except ApiError as e:
        main.fail(e)


def service(
    radio: str = typer.Argument(..., help="Which K4"),
    start: str = typer.Option(..., "--start", help='Local start, e.g. "2026-10-20 09:00"'),
    duration: str = typer.Option(..., "--duration", help="e.g. 4h"),
    repeat: str = typer.Option(None, "--repeat", help=REPEAT_HELP),
    every: int = typer.Option(1, "--every"),
    on: str = typer.Option(None, "--on", help=ON_HELP),
    count: int = typer.Option(None, "--count"),
    until: str = typer.Option(None, "--until", help="ISO date"),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Mark a radio as Being serviced. Members' bookings in that time are removed and they are told."""
    api, cfg = _main().get_api()
    _check_duration(duration)
    body = {
        "radio": radio,
        "start": start,
        "duration": duration,
        "kind": "maintenance",
        "repeat": _repeat(repeat, every, on, count, until),
    }
    _book(api, cfg, body, yes)
