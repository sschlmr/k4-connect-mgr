"""Backups, TLS certificate generation, and config handling."""

from __future__ import annotations

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
