"""CAT protocol layer against the fake K4."""

from __future__ import annotations

import asyncio

import pytest

from k4cm import cat


async def wait_for(predicate, timeout: float = 2.0) -> bool:
    """Poll until predicate() is true — PS0; is fire-and-forget, so the effect
    lands a moment after the command is sent."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


async def test_ping_and_unreachable(server):
    assert await cat.reachable("127.0.0.1", server.radio_a.cat_port)
    dead_radio = server.config.find_radio("k4dead")
    assert not await cat.reachable(dead_radio.host, dead_radio.cat_port, timeout=1.0)


async def test_set_password_and_read_back(server):
    async with cat.CatClient("127.0.0.1", server.radio_a.cat_port) as client:
        await client.send("RRPhelloworld;")
        await client.send("RRC1;")
        assert await client.request("RRP;") == "RRPhelloworld"
    assert server.radio_a.password == "helloworld"
    assert server.radio_a.rrc == 1


async def test_client_count_reflects_remote_connections(server):
    server.radio_a.password = "secret"
    server.radio_a.rrc = 1
    assert await cat.client_count("127.0.0.1", server.radio_a.cat_port) == 0

    # Drive the "local" fake to connect to radio A, exactly as the client CLI does.
    async with cat.CatClient("127.0.0.1", server.local_radio.cat_port) as local:
        await local.send(f"RRT127.0.0.1:{server.radio_a.remote_port}?secret;")
        assert await local.ping()

    assert await cat.client_count("127.0.0.1", server.radio_a.cat_port) == 1
    assert server.local_radio.connected_to_server


async def test_wrong_password_is_refused(server):
    server.radio_a.password = "correct"
    server.radio_a.rrc = 1
    async with cat.CatClient("127.0.0.1", server.local_radio.cat_port) as local:
        await local.send(f"RRT127.0.0.1:{server.radio_a.remote_port}?wrong;")
        assert await local.read_response() == "RRT!"
    assert await cat.client_count("127.0.0.1", server.radio_a.cat_port) == 0


async def test_disconnect_with_rrn(server):
    server.radio_a.password = "secret"
    server.radio_a.rrc = 1
    async with cat.CatClient("127.0.0.1", server.local_radio.cat_port) as local:
        await local.send(f"RRT127.0.0.1:{server.radio_a.remote_port}?secret;")
        assert await local.ping()
        assert await cat.client_count("127.0.0.1", server.radio_a.cat_port) == 1
        await local.send("RRN;")
        assert await local.ping()
    assert await cat.client_count("127.0.0.1", server.radio_a.cat_port) == 0


async def test_power_off_stops_responses(server):
    await cat.power_off("127.0.0.1", server.radio_b.cat_port)
    assert await wait_for(lambda: not server.radio_b.powered_on)
    assert not await cat.reachable("127.0.0.1", server.radio_b.cat_port, timeout=1.0)


async def test_cat_error_on_closed_port(server):
    dead = server.config.find_radio("k4dead")
    with pytest.raises(cat.CatError):
        await cat.query(dead.host, dead.cat_port, "CC;", timeout=1.0)


def test_client_count_decodes_the_client_role():
    # K4 Programmer's Reference: n clients when serving, -1 when connected as a client.
    assert cat.parse_client_count("CC2") == 2
    assert cat.parse_client_count("CC-1") == -1
    assert cat.parse_client_count("CC0") == 0
    assert cat.parse_client_count("CC?") is None
