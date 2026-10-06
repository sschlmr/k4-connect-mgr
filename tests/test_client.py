"""Client configuration, certificate pinning, and local-radio control."""

from __future__ import annotations

import pytest

from k4cm.client import radio as local_radio
from k4cm.client.config import (
    ClientConfig,
    ConfigError,
    check_transport,
    fingerprint_of_pem,
    load_config,
    pinned_fingerprint,
    save_config,
    save_pinned_certificate,
)
from k4cm.server import tls

# -- configuration ---------------------------------------------------------


def test_config_round_trip(tmp_path):
    path = tmp_path / "config.toml"
    original = ClientConfig(
        server_url="https://mh01.net:8443",
        callsign="W1AW",
        api_key="secret-key-value",
        radio_host="192.168.1.50",
        radio_cat_port=9200,
        path=path,
    )
    save_config(original, path)

    loaded = load_config(path)

    assert loaded.server_url == original.server_url
    assert loaded.callsign == "W1AW"
    assert loaded.api_key == "secret-key-value"
    assert loaded.radio_host == "192.168.1.50"
    assert loaded.radio_cat_port == 9200


def test_config_values_with_quotes_survive(tmp_path):
    path = tmp_path / "config.toml"
    save_config(ClientConfig(server_url="https://mh01.net", callsign="W1AW", api_key='a"b\\c'), path)
    assert load_config(path).api_key == 'a"b\\c'


def test_missing_config_is_reported(tmp_path):
    with pytest.raises(ConfigError, match="k4cm config"):
        load_config(tmp_path / "absent.toml")


def test_incomplete_config_is_rejected():
    with pytest.raises(ConfigError, match="api_key"):
        ClientConfig(server_url="https://mh01.net", callsign="W1AW").validate()


# -- transport security ----------------------------------------------------


def test_https_is_required_for_remote_servers():
    check_transport("https://mh01.net:8443")  # fine
    with pytest.raises(ConfigError, match="must use https"):
        check_transport("http://mh01.net:8443")


def test_plain_http_allowed_only_for_localhost():
    check_transport("http://localhost:8000")
    check_transport("http://127.0.0.1:8000")
    with pytest.raises(ConfigError):
        check_transport("http://192.168.1.10:8000")


def test_pinned_fingerprint_matches_the_servers_certificate(tmp_path):
    cert_path = tmp_path / "gen" / "cert.pem"
    key_path = tmp_path / "gen" / "key.pem"
    server_fingerprint = tls.generate_self_signed(cert_path, key_path, ["mh01.net"])

    config = ClientConfig(
        server_url="https://mh01.net", callsign="W1AW", api_key="k", path=tmp_path / "config.toml"
    )
    save_pinned_certificate(config, cert_path.read_text(encoding="utf-8"))

    assert pinned_fingerprint(config) == server_fingerprint, (
        "the fingerprint the user confirms must equal the one the admin publishes"
    )


def test_fingerprint_is_sha256_of_the_der_certificate(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    tls.generate_self_signed(cert_path, key_path, ["mh01.net"])

    fingerprint = fingerprint_of_pem(cert_path.read_text(encoding="utf-8"))

    assert len(fingerprint.split(":")) == 32
    assert fingerprint == tls.fingerprint(cert_path)


def test_no_pinned_certificate_is_a_clear_error(tmp_path):
    from k4cm.client.api import ApiError, ServerApi

    config = ClientConfig(
        server_url="https://mh01.net", callsign="W1AW", api_key="k", path=tmp_path / "config.toml"
    )
    with pytest.raises(ApiError, match="k4cm config"):
        ServerApi(config).health()


# -- local radio control ---------------------------------------------------


async def test_connect_drives_the_local_panel(server):
    server.radio_a.password = "session-password"
    server.radio_a.rrc = 1
    config = ClientConfig(
        server_url="https://mh01.net",
        callsign="W1AW",
        api_key="k",
        radio_host="127.0.0.1",
        radio_cat_port=server.local_radio.cat_port,
    )

    import asyncio

    message = await asyncio.to_thread(
        local_radio.connect, config, "127.0.0.1", server.radio_a.remote_port, "session-password"
    )

    assert "connected" in message
    assert server.radio_a.client_count == 1
    assert any(c.startswith("RRT") for c in server.local_radio.commands)


async def test_connect_reports_a_refused_password(server):
    server.radio_a.password = "the-real-one"
    server.radio_a.rrc = 1
    config = ClientConfig(
        server_url="https://mh01.net",
        callsign="W1AW",
        api_key="k",
        radio_host="127.0.0.1",
        radio_cat_port=server.local_radio.cat_port,
    )

    import asyncio

    with pytest.raises(local_radio.RadioError, match="RRT!"):
        await asyncio.to_thread(
            local_radio.connect, config, "127.0.0.1", server.radio_a.remote_port, "wrong-password"
        )


async def test_disconnect_sends_rrn(server):
    server.radio_a.password = "pw"
    server.radio_a.rrc = 1
    config = ClientConfig(
        server_url="https://mh01.net",
        callsign="W1AW",
        api_key="k",
        radio_host="127.0.0.1",
        radio_cat_port=server.local_radio.cat_port,
    )

    import asyncio

    await asyncio.to_thread(
        local_radio.connect, config, "127.0.0.1", server.radio_a.remote_port, "pw"
    )
    assert server.radio_a.client_count == 1

    await asyncio.to_thread(local_radio.disconnect, config)

    assert server.radio_a.client_count == 0
    assert "RRN" in server.local_radio.commands


async def test_unreachable_local_radio_is_reported(server):
    dead = server.config.find_radio("k4dead")
    config = ClientConfig(
        server_url="https://mh01.net",
        callsign="W1AW",
        api_key="k",
        radio_host=dead.host,
        radio_cat_port=dead.cat_port,
    )

    import asyncio

    with pytest.raises(local_radio.RadioError, match="cannot talk to your local radio"):
        await asyncio.to_thread(local_radio.disconnect, config)


def test_missing_local_radio_config_is_reported():
    config = ClientConfig(server_url="https://mh01.net", callsign="W1AW", api_key="k")
    with pytest.raises(local_radio.RadioError, match="no local radio configured"):
        local_radio.disconnect(config)
