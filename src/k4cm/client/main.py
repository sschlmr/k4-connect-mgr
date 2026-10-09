"""`k4cm` — the local client command line.

    k4cm config              set up server URL, callsign, API key, local radio
    k4cm request <radio>     ask for a specific K4 and connect your panel to it
    k4cm watch               resume the session watcher (timeout negotiation)
    k4cm release             disconnect and hand the radio back
    k4cm status              what every K4 at the site is doing
    k4cm admin ...           administrative commands (admin key required)
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from typing import Any

import typer

from . import radio as local_radio
from .api import ApiError, ServerApi
from .config import (
    ClientConfig,
    ConfigError,
    config_path,
    fetch_server_certificate,
    load_config,
    pinned_fingerprint,
    save_config,
    save_pinned_certificate,
)

app = typer.Typer(help="K4 Connect Manager — request and manage remote K4 sessions", no_args_is_help=True)
admin_app = typer.Typer(help="Administrative commands (require an admin API key)", no_args_is_help=True)
app.add_typer(admin_app, name="admin")

POLL_SECONDS = 30


# -- helpers ---------------------------------------------------------------


def err(message: str) -> None:
    typer.secho(message, fg=typer.colors.RED, err=True)


def ok(message: str) -> None:
    typer.secho(message, fg=typer.colors.GREEN)


def warn(message: str) -> None:
    typer.secho(message, fg=typer.colors.YELLOW)


def get_api() -> tuple[ServerApi, ClientConfig]:
    try:
        config = load_config()
        config.validate()
        return ServerApi(config), config
    except (ConfigError, ApiError) as e:
        err(str(e))
        raise typer.Exit(code=2)


def fail(e: ApiError) -> None:
    err(str(e))
    raise typer.Exit(code=1)


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def minutes(seconds: int | None) -> str:
    if seconds is None:
        return "?"
    if seconds < 0:
        return "overdue"
    if seconds < 90:
        return f"{seconds}s"
    return f"{seconds // 60}m"


# -- config ----------------------------------------------------------------


@app.command()
def config() -> None:
    """Set up (or update) this machine's configuration, pinning the server certificate."""
    path = config_path()
    try:
        current = load_config(path)
    except ConfigError:
        current = ClientConfig(path=path)

    typer.echo(f"Writing {path}\n")
    server_url = typer.prompt("Server URL", default=current.server_url or "https://mh01.net:8443")
    callsign = typer.prompt("Your callsign", default=current.callsign or "").strip().upper()
    api_key = typer.prompt(
        "Your API key (from the station administrator)",
        default=current.api_key or "",
        hide_input=bool(current.api_key),
        show_default=False,
    ).strip()
    radio_host = typer.prompt(
        "CAT address of your local K4 / K4-0 (blank if none)",
        default=current.radio_host or "",
        show_default=bool(current.radio_host),
    ).strip()
    radio_port = current.radio_cat_port
    if radio_host:
        radio_port = int(typer.prompt("CAT port of your local radio", default=current.radio_cat_port))

    updated = ClientConfig(
        server_url=server_url.strip(),
        callsign=callsign,
        api_key=api_key,
        radio_host=radio_host,
        radio_cat_port=radio_port,
        path=path,
    )

    try:
        updated.validate()
    except ConfigError as e:
        err(str(e))
        raise typer.Exit(code=2)

    if updated.server_url.startswith("https://"):
        _pin_certificate(updated)

    save_config(updated, path)
    ok(f"\nSaved {path}")

    try:
        api = ServerApi(updated)
        health = api.health()
        ok(f"Server reachable: k4cm {health.get('version')} at {updated.server_url}")
    except ApiError as e:
        warn(f"Config saved, but the server could not be contacted yet: {e}")


def _pin_certificate(config: ClientConfig) -> None:
    typer.echo("\nFetching the server's TLS certificate…")
    try:
        pem, fingerprint = fetch_server_certificate(config.server_url)
    except ConfigError as e:
        warn(str(e))
        warn("Skipping certificate pinning for now — re-run 'k4cm config' when the server is up.")
        return

    existing = pinned_fingerprint(config)
    typer.echo("\nSHA-256 fingerprint presented by the server:")
    typer.secho(f"  {fingerprint}", bold=True)
    if existing and existing != fingerprint:
        warn(f"\nThis differs from the certificate already pinned here:\n  {existing}")
        warn("Only continue if the administrator rotated the certificate.")
    typer.echo("\nConfirm this matches the fingerprint published by your station administrator.")

    if interactive() and not typer.confirm("Trust this certificate?", default=False):
        err("Certificate not trusted — configuration aborted.")
        raise typer.Exit(code=2)

    path = save_pinned_certificate(config, pem)
    ok(f"Certificate pinned: {path}")


# -- request / release -----------------------------------------------------


@app.command()
def request(
    radio: str = typer.Argument(..., help="Which K4 to request (id or name)"),
    no_watch: bool = typer.Option(False, "--no-watch", help="Return to the shell instead of watching"),
    no_connect: bool = typer.Option(
        False, "--no-connect", help="Get the assignment but do not drive the local radio"
    ),
    queue: bool = typer.Option(False, "--queue", "-q", help="Join the queue if the radio is busy"),
) -> None:
    """Request a specific K4 and connect your panel to it."""
    api, cfg = get_api()

    try:
        assignment = api.request_assignment(radio)
    except ApiError as e:
        if e.status_code == 409 and e.payload.get("can_queue"):
            err(str(e))
            _offer_queue(api, radio, queue)
            raise typer.Exit(code=1)
        fail(e)

    _report_assignment(assignment)

    if no_connect or not cfg.has_local_radio:
        if not cfg.has_local_radio:
            warn("No local radio is configured, so nothing was connected automatically.")
        _print_manual_details(assignment)
    else:
        try:
            detail = local_radio.connect(
                cfg,
                assignment["radio"]["connect_host"],
                assignment["radio"]["connect_port"],
                assignment["password"],
                _server_sees_panel(api),
            )
            ok(f"Local radio: {detail}")
        except local_radio.RadioError as e:
            err(f"Assignment succeeded but your local radio could not be connected: {e}")
            warn("Handing the K4 back so it is not left tied up…")
            try:
                api.release()
                warn("Released.")
            except ApiError as release_error:
                err(f"Could not release the assignment: {release_error}")
                _print_manual_details(assignment)
            raise typer.Exit(code=1)

    if not no_watch:
        _watch_loop(api, cfg)


def _server_sees_panel(api: ServerApi) -> Callable[[], bool]:
    """Confirmation for local_radio.connect: does the central radio report a
    client attached? The panel's own CC is no evidence (see client/radio.py)."""

    def attached() -> bool:
        try:
            return bool(api.link_state().get("attached"))
        except ApiError:
            return False

    return attached


def _offer_queue(api: ServerApi, radio: str, auto_queue: bool) -> None:
    if not auto_queue and not (interactive() and typer.confirm("Join the queue for it?", default=True)):
        return
    try:
        result = api.join_queue(radio)
    except ApiError as e:
        err(str(e))
        return
    ok(f"Queued for {result['radio']['name']} at position {result['position']}.")
    typer.echo("Run 'k4cm watch' to be told when it is offered to you.")


def _report_assignment(assignment: dict[str, Any]) -> None:
    info = assignment["radio"]
    ok(f"\n{info['name']} is yours until {assignment['expires_at']}.")
    if info.get("description"):
        typer.echo(f"  {info['description']}")
    typer.echo(f"  Time allowed: {minutes(assignment.get('seconds_remaining'))}")


def _print_manual_details(assignment: dict[str, Any]) -> None:
    info = assignment["radio"]
    typer.echo("\nConnect your panel manually with these details:")
    typer.echo(f"  Host:     {info['connect_host']}")
    typer.echo(f"  Port:     {info['connect_port']}")
    typer.echo(f"  Password: {assignment['password']}")


@app.command()
def release() -> None:
    """Disconnect your panel and hand the radio back."""
    api, cfg = get_api()
    radio_message = None
    if cfg.has_local_radio:
        try:
            radio_message = local_radio.disconnect(cfg)
        except local_radio.RadioError as e:
            warn(f"Could not disconnect your local radio: {e}")
            warn("Continuing to release the assignment on the server.")
    try:
        result = api.release()
    except ApiError as e:
        if radio_message:
            warn(radio_message)
        fail(e)
    if radio_message:
        ok(radio_message)
    ok(f"Released {result.get('released', 'the radio')}.")


# -- status ----------------------------------------------------------------


@app.command()
def status() -> None:
    """Show what every K4 at the site is doing."""
    api, cfg = get_api()
    try:
        payload = api.status()
        state = api.session_state()
    except ApiError as e:
        fail(e)

    typer.echo(f"{'RADIO':<12} {'STATE':<26} {'UNTIL':<10} {'QUEUE':<6} REACHABLE")
    for row in payload["radios"]:
        if row["in_use"]:
            state_text = f"in use by {row['user']}"
            until = minutes(row.get("seconds_remaining"))
        elif row.get("held_for"):
            state_text = f"held for {row['held_for']}"
            until = "-"
        elif not row["enabled"]:
            state_text = "disabled"
            until = "-"
        else:
            state_text = "free"
            until = "-"
        reachable = {True: "yes", False: "NO", None: "?"}[row.get("reachable")]
        typer.echo(
            f"{row['id']:<12} {state_text:<26} {until:<10} {row['queue_depth']:<6} {reachable}"
        )

    typer.echo("")
    _print_session_summary(state)
    if cfg.has_local_radio:
        typer.echo(f"Your local radio: {local_radio.local_state(cfg)}")


def _print_session_summary(state: dict[str, Any]) -> None:
    assignment = state.get("assignment")
    offer = state.get("offer")
    queued = state.get("queued")
    if assignment:
        typer.echo(
            f"Your session: {assignment['radio']['name']}, "
            f"{minutes(assignment['seconds_remaining'])} remaining"
            + (" (expiring)" if assignment["expiring"] else "")
        )
    elif offer:
        typer.echo(
            f"{offer['radio']['name']} is being held for you for "
            f"{minutes(offer['seconds_remaining'])} — run 'k4cm watch' to accept."
        )
    elif queued:
        typer.echo(f"You are #{queued['position']} in the queue for {queued['radio']['name']}.")
    else:
        typer.echo("You have no session and are not queued.")


# -- watch -----------------------------------------------------------------


@app.command()
def watch() -> None:
    """Watch an existing session and handle timeout negotiation."""
    api, cfg = get_api()
    _watch_loop(api, cfg)


def _watch_loop(api: ServerApi, cfg: ClientConfig) -> None:
    typer.echo("\nWatching your session — Ctrl-C to stop (your session keeps running).")
    warned_expiring = False
    saw_session = False
    try:
        while True:
            try:
                state = api.session_state()
            except ApiError as e:
                warn(f"Could not reach the server: {e}")
                time.sleep(POLL_SECONDS)
                continue

            assignment, offer, queued = state.get("assignment"), state.get("offer"), state.get("queued")

            if assignment:
                saw_session = True
                if assignment["expiring"] and not assignment["acked"]:
                    if _negotiate_timeout(api, cfg, assignment):
                        return
                    warned_expiring = True
                elif assignment["expiring"] and not warned_expiring:
                    typer.echo(
                        f"Session ends in {minutes(assignment['seconds_remaining'])}."
                    )
                    warned_expiring = True
            elif offer:
                if _handle_offer(api, cfg, offer):
                    return
            elif queued:
                typer.echo(
                    f"Queued for {queued['radio']['name']}: position {queued['position']}."
                )
            else:
                typer.echo("\a")
                warn("Your session has ended (released, reclaimed, expired, or force-released).")
                if cfg.has_local_radio:
                    typer.echo(f"Local radio: {_end_of_session_disconnect(cfg, saw_session)}")
                return

            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        typer.echo("\nStopped watching. Your session is unaffected.")


def _end_of_session_disconnect(cfg: ClientConfig, saw_session: bool) -> str:
    """Drop the panel's link once the session we were watching is over.

    A real K4 does not disconnect an attached client when the server locks it
    (RRC0; stops new connections only), so without this the member's panel
    would stay on a radio the server already considers free. Only done when
    this watch saw the session, so a panel linked elsewhere is left alone.
    """
    if not saw_session:
        return local_radio.local_state(cfg)
    try:
        return local_radio.disconnect(cfg)
    except local_radio.RadioError as e:
        return f"could not disconnect it ({e}) — press DISCONNECT on the panel"


def _negotiate_timeout(api: ServerApi, cfg: ClientConfig, assignment: dict[str, Any]) -> bool:
    """Warn the user and take their decision. Returns True when watching should stop."""
    typer.echo("\a")
    warn(
        f"\n{assignment['radio']['name']} session ends in "
        f"{minutes(assignment['seconds_remaining'])}."
    )
    if not assignment["can_extend"]:
        warn(f"{assignment['queue_depth']} user(s) are waiting, so it cannot be extended.")

    if not interactive():
        warn("Not an interactive terminal — run 'k4cm watch' to accept or extend.")
        return False

    choice = typer.prompt(
        "[A]ccept the end time, [E]xtend, or [R]elease now?", default="A"
    ).strip().lower()[:1]

    try:
        if choice == "e":
            result = api.extend()
            ok(f"Extended until {result['assignment']['expires_at']}.")
            return False
        if choice == "r":
            release()
            return True
        api.ack()
        ok("Acknowledged — the radio will be handed back at the deadline.")
        return False
    except ApiError as e:
        err(str(e))
        if e.status_code == 409:
            try:
                api.ack()
                warn("Acknowledged the end time instead.")
            except ApiError as ack_error:
                err(str(ack_error))
        return False


def _handle_offer(api: ServerApi, cfg: ClientConfig, offer: dict[str, Any]) -> bool:
    typer.echo("\a")
    ok(
        f"\n{offer['radio']['name']} is free and is being held for you for "
        f"{minutes(offer['seconds_remaining'])}."
    )
    if not interactive():
        warn("Not an interactive terminal — run 'k4cm watch' to accept it.")
        return False

    if not typer.confirm("Take it now?", default=True):
        try:
            api.decline_offer()
            typer.echo("Declined; it goes to the next person in the queue.")
        except ApiError as e:
            err(str(e))
        return True

    try:
        assignment = api.accept_offer()
    except ApiError as e:
        err(str(e))
        return False

    _report_assignment(assignment)
    if cfg.has_local_radio:
        try:
            detail = local_radio.connect(
                cfg,
                assignment["radio"]["connect_host"],
                assignment["radio"]["connect_port"],
                assignment["password"],
                _server_sees_panel(api),
            )
            ok(f"Local radio: {detail}")
        except local_radio.RadioError as e:
            err(f"Could not connect your local radio: {e}")
            _print_manual_details(assignment)
    else:
        _print_manual_details(assignment)
    return False


# -- queue -----------------------------------------------------------------


@app.command("queue")
def queue_command(
    radio: str = typer.Argument(None, help="Radio to queue for; omit to show your position"),
    leave: bool = typer.Option(False, "--leave", help="Leave the queue"),
) -> None:
    """Join, inspect, or leave the wait queue for a radio."""
    api, _ = get_api()
    try:
        if leave:
            result = api.leave_queue()
            ok(f"Left the queue for {result['left']}.")
        elif radio:
            result = api.join_queue(radio)
            ok(f"Queued for {result['radio']['name']} at position {result['position']}.")
        else:
            _print_session_summary(api.session_state())
    except ApiError as e:
        fail(e)


# -- admin -----------------------------------------------------------------


@admin_app.command("users")
def admin_users(
    add: str = typer.Option(None, "--add", help="Create a user with this callsign"),
    make_admin: bool = typer.Option(False, "--admin", help="Give the new user admin rights"),
    delete: str = typer.Option(None, "--delete", help="Delete this user"),
    reset_key: str = typer.Option(None, "--reset-key", help="Issue a new API key for this user"),
) -> None:
    """List or manage user accounts."""
    api, _ = get_api()
    try:
        if add:
            result = api.create_user(add, make_admin)
            ok(f"Created {result['callsign']}.")
            typer.secho(f"API key (shown once): {result['api_key']}", bold=True)
        elif delete:
            api.delete_user(delete)
            ok(f"Deleted {delete.upper()}.")
        elif reset_key:
            result = api.reset_user_key(reset_key)
            ok(f"New key for {result['callsign']} (shown once):")
            typer.secho(result["api_key"], bold=True)
        else:
            typer.echo(f"{'CALLSIGN':<12} {'ADMIN':<7} ENABLED")
            for user in api.list_users():
                typer.echo(
                    f"{user['callsign']:<12} {'yes' if user['is_admin'] else 'no':<7} "
                    f"{'yes' if user['enabled'] else 'no'}"
                )
    except ApiError as e:
        fail(e)


@admin_app.command("radios")
def admin_radios(
    reload: bool = typer.Option(False, "--reload", help="Re-read the radio inventory from the config file"),
    power: str = typer.Option(None, "--power", help="Radio id to act on with --action"),
    action: str = typer.Option(None, "--action", help="on, off or cycle"),
) -> None:
    """List radios, reload the inventory, or control radio power."""
    api, _ = get_api()
    try:
        if reload:
            result = api.reload_radios()
            ok(f"Reloaded {result['radios']} radio(s).")
            if result.get("orphaned_active_assignments"):
                warn(
                    "Active sessions reference radios no longer in the config: "
                    + ", ".join(result["orphaned_active_assignments"])
                )
        elif power:
            if not action:
                err("--power needs --action on|off|cycle")
                raise typer.Exit(code=2)
            result = api.radio_power(power, action)
            ok(result["detail"])
        else:
            typer.echo(f"{'ID':<12} {'NAME':<20} {'CAT':<22} {'POWER':<9} REACHABLE")
            for row in api.list_radios():
                reachable = {True: "yes", False: "NO", None: "?"}[row.get("reachable")]
                cat_address = f"{row['cat_host']}:{row['cat_port']}"
                typer.echo(
                    f"{row['id']:<12} {row['name']:<20} {cat_address:<22} "
                    f"{row['power_method']:<9} {reachable}"
                )
    except ApiError as e:
        fail(e)


@admin_app.command("sessions")
def admin_sessions(
    limit: int = typer.Option(20, "--limit", help="How many sessions to show"),
    force_release: int = typer.Option(None, "--force-release", help="Assignment id to force-release"),
) -> None:
    """List sessions or force-release one."""
    api, _ = get_api()
    try:
        if force_release is not None:
            result = api.force_release(force_release)
            ok(f"Force-released {result['forced']} (was {result['callsign']}).")
            return
        typer.echo(f"{'ID':<6} {'RADIO':<12} {'CALLSIGN':<10} {'STATE':<11} EXPIRES")
        for row in api.list_assignments(limit):
            typer.echo(
                f"{row['id']:<6} {row['radio']['id']:<12} {row['callsign']:<10} "
                f"{row['state']:<11} {row['expires_at']}"
            )
    except ApiError as e:
        fail(e)


@admin_app.command("audit")
def admin_audit(
    hours: float = typer.Option(None, "--hours", help="Only entries from the last N hours"),
    actor: str = typer.Option(None, "--user", help="Only entries for this callsign"),
    limit: int = typer.Option(50, "--limit", help="Maximum entries to show"),
) -> None:
    """Read the audit trail."""
    api, _ = get_api()
    try:
        entries = api.audit(hours=hours, actor=actor, limit=limit)
    except ApiError as e:
        fail(e)
    typer.echo(f"{'TIMESTAMP':<26} {'ACTOR':<12} {'ACTION':<20} DETAIL")
    for entry in entries:
        typer.echo(
            f"{entry['timestamp']:<26} {entry['actor']:<12} {entry['action']:<20} {entry['detail']}"
        )


if __name__ == "__main__":
    app()
