"""`k4cm-server` — the central server's command line.

    k4cm-server init          first-run setup: config, database, TLS cert, admin user
    k4cm-server run           serve the API (what Task Scheduler launches)
    k4cm-server backup        take one database snapshot now
    k4cm-server reset-admin   regenerate the admin API key (local recovery path)
    k4cm-server fingerprint   print the TLS certificate fingerprint to publish
"""

from __future__ import annotations

from pathlib import Path

import typer

from ..paths import server_data_dir
from . import tls
from .auth import generate_api_key, hash_api_key
from .backup import run_backup
from .config import default_config_path, load_config, write_default_config
from .db import Database
from .logging_setup import configure_logging, log_startup_failure

app = typer.Typer(help="K4 Connect Manager — central server", no_args_is_help=True)


def _data_dir_option() -> Path:
    return typer.Option(
        None, "--data-dir", help="Server data directory (default: ProgramData\\k4cm or /var/lib/k4cm)"
    )


def _load(data_dir: Path | None, starting: bool = False):
    """Load the config, or exit with the reason. `starting` (the run command)
    also writes the reason to the log file, where a hidden server's admin looks."""
    resolved = Path(data_dir) if data_dir else server_data_dir()
    try:
        return load_config(data_dir=resolved)
    except FileNotFoundError as e:
        message = str(e)
    except ValueError as e:
        message = f"config error: {e}"
    if starting:
        log_startup_failure(resolved / "logs" / "k4cm-server.log", message)
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=2)


@app.command()
def init(
    data_dir: Path = _data_dir_option(),
    hostname: list[str] = typer.Option(
        None, "--hostname", help="Name or IP for the TLS certificate (repeatable)"
    ),
    admin: str = typer.Option(None, "--admin", help="Callsign of the admin account to create"),
    force_cert: bool = typer.Option(False, "--force-cert", help="Replace an existing certificate"),
) -> None:
    """Prepare a fresh installation. Safe to re-run: nothing existing is overwritten."""
    resolved = Path(data_dir) if data_dir else server_data_dir()
    resolved.mkdir(parents=True, exist_ok=True)

    config_path = default_config_path(resolved)
    if write_default_config(config_path):
        typer.echo(f"Wrote default config: {config_path}")
    else:
        typer.echo(f"Keeping existing config: {config_path}")

    config = _load(resolved)

    db = Database(config.database_path)
    typer.echo(f"Database ready: {config.database_path}")

    cert_path, key_path = config.tls_cert_path, config.tls_key_path
    if cert_path.exists() and not force_cert:
        typer.echo(f"Keeping existing certificate: {cert_path}")
    else:
        names = list(hostname) if hostname else []
        if not names:
            default_name = config.server.public_hostname
            entered = typer.prompt(
                "Hostname(s)/IP(s) for the TLS certificate, comma separated", default=default_name
            )
            names = [n.strip() for n in entered.split(",") if n.strip()]
        tls.generate_self_signed(cert_path, key_path, names)
        typer.echo(f"Generated certificate for {', '.join(names)}: {cert_path}")

    typer.echo(f"Certificate SHA-256 fingerprint:\n  {tls.fingerprint(cert_path)}")
    typer.secho(
        "Publish that fingerprint to your users — their client asks them to confirm it.",
        fg=typer.colors.YELLOW,
    )

    if db.count_admins() == 0:
        callsign = admin or typer.prompt("Callsign for the admin account")
        api_key = generate_api_key()
        db.create_user(callsign, hash_api_key(api_key), is_admin=True)
        db.audit(callsign.strip().upper(), "user.create", "initial admin account")
        typer.secho(f"\nAdmin account {callsign.strip().upper()} created.", fg=typer.colors.GREEN)
        typer.secho(f"API key (shown once): {api_key}", fg=typer.colors.GREEN, bold=True)
    else:
        typer.echo("Admin account already exists — leaving it alone.")

    typer.echo(f"\nNext: add your K4s to {config_path} under [[radios]], then run 'k4cm-server run'.")
    db.close()


@app.command()
def run(
    data_dir: Path = _data_dir_option(),
    host: str = typer.Option(None, "--host", help="Override the bind address"),
    port: int = typer.Option(None, "--port", help="Override the bind port"),
    no_tls: bool = typer.Option(False, "--no-tls", help="Serve plain HTTP (development only)"),
) -> None:
    """Run the API server."""
    import uvicorn

    from .app import create_app

    config = _load(data_dir, starting=True)
    logger = configure_logging(config)

    ssl_args = {}
    if not no_tls:
        if not config.tls_cert_path.exists() or not config.tls_key_path.exists():
            # The console handler already shows this on stderr.
            logger.error(
                "cannot start: TLS certificate missing (%s). Run 'k4cm-server init' "
                "or pass --no-tls for local development.",
                config.tls_cert_path,
            )
            raise typer.Exit(code=2)
        ssl_args = {
            "ssl_certfile": str(config.tls_cert_path),
            "ssl_keyfile": str(config.tls_key_path),
        }

    try:
        db = Database(config.database_path)
        application = create_app(config, db, logger)
    except Exception:
        logger.exception("cannot start: could not open %s", config.database_path)
        raise typer.Exit(code=1) from None

    scheme = "http" if no_tls else "https"
    bind_host = host or config.server.host
    bind_port = port or config.server.port
    logger.info("serving on %s://%s:%s", scheme, bind_host, bind_port)
    uvicorn.run(application, host=bind_host, port=bind_port, log_config=None, **ssl_args)


@app.command()
def backup(data_dir: Path = _data_dir_option()) -> None:
    """Take one database snapshot immediately."""
    config = _load(data_dir)
    db = Database(config.database_path)
    destination = run_backup(config, db)
    typer.secho(f"Backup written: {destination}", fg=typer.colors.GREEN)
    db.close()


@app.command("reset-admin")
def reset_admin(
    callsign: str = typer.Argument(None, help="Admin callsign (defaults to the only admin)"),
    data_dir: Path = _data_dir_option(),
) -> None:
    """Regenerate an admin API key. Local-only recovery — there is no remote path."""
    config = _load(data_dir)
    db = Database(config.database_path)

    if callsign:
        target = db.get_user(callsign)
        if target is None:
            typer.secho(f"No such user: {callsign}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)
    else:
        admins = [u for u in db.list_users() if u.is_admin]
        if len(admins) != 1:
            typer.secho(
                "Specify which admin: " + (", ".join(u.callsign for u in admins) or "none exist"),
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)
        target = admins[0]

    api_key = generate_api_key()
    db.set_user_key(target.callsign, hash_api_key(api_key))
    db.audit(target.callsign, "user.reset_key", "local reset-admin")
    typer.secho(f"New API key for {target.callsign} (shown once):", fg=typer.colors.GREEN)
    typer.secho(api_key, bold=True)
    db.close()


@app.command()
def fingerprint(data_dir: Path = _data_dir_option()) -> None:
    """Print the TLS certificate fingerprint for users to confirm."""
    config = _load(data_dir)
    if not config.tls_cert_path.exists():
        typer.secho(f"No certificate at {config.tls_cert_path}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    typer.echo(tls.fingerprint(config.tls_cert_path))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
