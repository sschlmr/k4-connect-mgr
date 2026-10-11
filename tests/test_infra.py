"""Backups, TLS certificate generation, and config handling."""

from __future__ import annotations

import logging
import ssl

import pytest

from k4cm.models import Radio
from k4cm.server import tls
from k4cm.server.backup import prune_backups, run_backup
from k4cm.server.config import load_config, write_default_config

# -- backup ----------------------------------------------------------------


def test_backup_snapshot_contains_the_data(server, tmp_path):
    server.db.create_user("N0BAK", "hash-value", is_admin=False)

    destination = run_backup(server.config, server.db)

    assert destination.exists()
    import sqlite3

    with sqlite3.connect(destination) as restored:
        rows = restored.execute("SELECT callsign FROM users").fetchall()
    assert ("N0BAK",) in rows


def test_backup_retention_keeps_only_the_newest(server):
    server.config.backup.retention = 3
    for index in range(5):
        path = server.config.backup_dir / f"k4cm-2026010{index}T000000Z.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"snapshot")

    prune_backups(server.config)

    remaining = sorted(p.name for p in server.config.backup_dir.glob("k4cm-*.db"))
    assert len(remaining) == 3
    assert remaining[0].endswith("20260102T000000Z.db"), "oldest snapshots are pruned first"


# -- TLS -------------------------------------------------------------------


def test_self_signed_certificate_covers_the_public_hostname(tmp_path):
    cert_path = tmp_path / "tls" / "server-cert.pem"
    key_path = tmp_path / "tls" / "server-key.pem"

    fingerprint = tls.generate_self_signed(cert_path, key_path, ["mh01.net", "127.0.0.1"])

    assert cert_path.exists() and key_path.exists()
    assert len(fingerprint.split(":")) == 32, "SHA-256 fingerprint, colon separated"
    assert tls.fingerprint(cert_path) == fingerprint

    loaded = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    loaded.load_cert_chain(cert_path, key_path)  # raises if the pair does not match

    from cryptography import x509

    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "mh01.net" in names.get_values_for_type(x509.DNSName)
    import ipaddress

    assert ipaddress.ip_address("127.0.0.1") in names.get_values_for_type(x509.IPAddress)


# -- config ----------------------------------------------------------------


def test_default_config_is_written_once_and_never_overwritten(tmp_path):
    path = tmp_path / "server.toml"

    assert write_default_config(path) is True
    path.write_text(path.read_text(encoding="utf-8") + "\n# site edit\n", encoding="utf-8")

    assert write_default_config(path) is False, "a reinstall must not clobber the site config"
    assert "# site edit" in path.read_text(encoding="utf-8")


def test_default_config_loads_with_no_radios(tmp_path):
    path = tmp_path / "server.toml"
    write_default_config(path)

    config = load_config(path=path, data_dir=tmp_path)

    assert config.radios == {}
    assert config.server.public_hostname == "mh01.net"
    assert config.session.max_session_minutes == 240
    assert config.database_path == tmp_path / "k4cm.db"


def test_radio_lookup_by_id_and_name(server):
    assert server.config.find_radio("k4a").name == "K4 Alpha"
    assert server.config.find_radio("K4 Alpha").id == "k4a"
    assert server.config.find_radio("k4 alpha").id == "k4a"
    assert server.config.find_radio("nope") is None


def test_duplicate_radio_ids_are_rejected(tmp_path):
    path = tmp_path / "server.toml"
    path.write_text(
        '[[radios]]\nid = "k4a"\nhost = "127.0.0.1"\npublic_host = "x"\n\n[[radios]]\nid = "k4a"\nhost = "127.0.0.2"\npublic_host = "x"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate radio id"):
        load_config(path=path, data_dir=tmp_path)


def test_radio_entry_requires_host(tmp_path):
    path = tmp_path / "server.toml"
    path.write_text('[[radios]]\nid = "k4a"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="host"):
        load_config(path=path, data_dir=tmp_path)


def test_missing_config_is_reported_clearly(tmp_path):
    with pytest.raises(FileNotFoundError, match="k4cm-server init"):
        load_config(path=tmp_path / "absent.toml", data_dir=tmp_path)


def test_members_get_the_public_address_not_the_cat_address():
    radio = Radio.from_dict(
        {"id": "k4a", "host": "10.0.0.11", "public_host": "mh01.net", "remote_port": 9204, "public_port": 19204}
    )
    public = radio.public()
    assert (public["connect_host"], public["connect_port"]) == ("mh01.net", 19204)
    assert "10.0.0.11" not in str(public), "the LAN CAT address must not reach members"


def test_public_port_defaults_to_the_radios_remote_port():
    radio = Radio.from_dict({"id": "k4a", "host": "10.0.0.11", "public_host": "mh01.net", "remote_port": 9204})
    assert radio.public()["connect_port"] == 9204


def test_radio_entry_requires_public_host(tmp_path):
    path = tmp_path / "server.toml"
    path.write_text('[[radios]]\nid = "k4a"\nhost = "10.0.0.11"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="public_host"):
        load_config(path=path, data_dir=tmp_path)


# -- database upgrades -----------------------------------------------------


def test_a_0_1_0_database_gains_the_client_addr_column(tmp_path):
    import sqlite3
    from datetime import timedelta

    from k4cm.models import utcnow
    from k4cm.server.db import Database

    path = tmp_path / "k4cm.db"
    with sqlite3.connect(path) as old:
        old.execute(
            "CREATE TABLE assignments (id INTEGER PRIMARY KEY AUTOINCREMENT, radio_id TEXT NOT NULL,"
            " callsign TEXT NOT NULL, password TEXT NOT NULL, started_at TEXT NOT NULL,"
            " expires_at TEXT NOT NULL, state TEXT NOT NULL, acked INTEGER NOT NULL DEFAULT 0,"
            " ended_at TEXT, end_reason TEXT NOT NULL DEFAULT '')"
        )

    db = Database(path)
    try:
        claim = db.claim_radio("k4a", "W1AW", "pw", utcnow() + timedelta(hours=1), "203.0.113.5")
        assert db.get_assignment(claim.id).client_addr == "203.0.113.5"
    finally:
        db.close()


def test_relay_port_becomes_the_default_public_port():
    radio = Radio.from_dict({"id": "k4a", "host": "10.0.0.11", "public_host": "mh01.net", "relay_port": 27201})
    assert radio.connect_port == 27201
    unrelayed = Radio.from_dict({"id": "k4b", "host": "10.0.0.12", "public_host": "mh01.net"})
    assert unrelayed.relay_port is None and unrelayed.connect_port == 9204


@pytest.fixture
def server_loggers():
    """configure_logging replaces handlers on process-wide loggers; put them back."""
    names = ("k4cm.server", "uvicorn.error")
    saved = {n: (logging.getLogger(n).handlers[:], logging.getLogger(n).propagate) for n in names}
    yield
    for n, (handlers, propagate) in saved.items():
        logger = logging.getLogger(n)
        for h in logger.handlers:
            if h not in handlers:
                h.close()
        logger.handlers[:] = handlers
        logger.propagate = propagate


def _run_server(data_dir):
    from typer.testing import CliRunner

    from k4cm.server.main import app

    return CliRunner().invoke(app, ["run", "--data-dir", str(data_dir)])


def _server_log(data_dir):
    return (data_dir / "logs" / "k4cm-server.log").read_text(encoding="utf-8")


def test_a_missing_config_stops_the_server_and_says_why_in_the_log(tmp_path, server_loggers):
    # Under Task Scheduler stderr goes nowhere, so the log is all an admin sees.
    result = _run_server(tmp_path)
    assert result.exit_code == 2
    assert "cannot start: server config not found" in _server_log(tmp_path)


def test_a_bad_config_stops_the_server_and_says_why_in_the_log(tmp_path, server_loggers):
    (tmp_path / "server.toml").write_text('[server]\nport = "unterminated\n', encoding="utf-8")
    result = _run_server(tmp_path)
    assert result.exit_code == 2
    assert "cannot start: config error" in _server_log(tmp_path)


def test_a_missing_certificate_stops_the_server_and_says_why_in_the_log(tmp_path, server_loggers):
    write_default_config(tmp_path / "server.toml")
    result = _run_server(tmp_path)
    assert result.exit_code == 2
    log = _server_log(tmp_path)
    assert "cannot start: TLS certificate missing" in log
    assert "k4cm-server init" in log


def test_uvicorn_startup_errors_reach_the_server_log(tmp_path, server_loggers):
    from k4cm.server.logging_setup import configure_logging

    write_default_config(tmp_path / "server.toml")
    configure_logging(load_config(data_dir=tmp_path))
    logging.getLogger("uvicorn.error").error("[Errno 10048] address already in use")
    assert "address already in use" in _server_log(tmp_path)
