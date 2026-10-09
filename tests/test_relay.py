"""The server-side relay: ending a session must actually disconnect the member.

A real K4 keeps an attached client after its password changes and RRC0; is
sent (and so does the fake, to match), so these tests check that a relayed
session's link is cut by the server, and that the relay admits only the
session's member.
"""

from __future__ import annotations

import asyncio
import socket

import pytest_asyncio

from k4cm import cat
from k4cm.models import AssignmentState
from k4cm.server.manager import AssignmentManager
from k4cm.server.relay import normalize_addr

MEMBER = "127.0.0.1"
ELSEWHERE = "203.0.113.5"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest_asyncio.fixture
async def relayed(server):
    """The site from conftest, with k4a relayed through the server."""
    radio = server.config.radios["k4a"]
    radio.relay_port = free_port()
    radio.public_port = radio.relay_port
    server.config.relay.bind = "127.0.0.1"
    await server.manager.relay.sync()
    try:
        yield server
    finally:
        await server.manager.relay.stop()


async def panel_connect(server, port: int, password: str) -> bool:
    """Have the fake K4/0 dial `port`; True if it ends up connected."""
    async with cat.CatClient("127.0.0.1", server.local_radio.cat_port) as local:
        await local.send(f"RRT127.0.0.1:{port}?{password};")
        assert await local.ping()
    return server.local_radio.connected_to_server


async def wait_until(predicate, seconds: float = 3.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while not predicate():
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.02)
    return True


def relay_port(server) -> int:
    return server.config.radios["k4a"].relay_port


async def test_members_are_given_the_relay_port(relayed):
    result = await relayed.manager.request_assignment("W1AW", "k4a", MEMBER)
    assert result["radio"]["connect_port"] == relay_port(relayed)


async def test_relayed_session_connects_through_to_the_radio(relayed):
    result = await relayed.manager.request_assignment("W1AW", "k4a", MEMBER)

    assert await panel_connect(relayed, relay_port(relayed), result["password"])
    assert await wait_until(lambda: relayed.radio_a.client_count == 1)
    assert relayed.manager.relay.link_count("k4a") == 1


async def test_ending_a_session_cuts_the_relayed_link(relayed):
    result = await relayed.manager.request_assignment("W1AW", "k4a", MEMBER)
    await panel_connect(relayed, relay_port(relayed), result["password"])
    assert await wait_until(lambda: relayed.radio_a.client_count == 1)

    await relayed.manager.release("W1AW")

    assert await wait_until(lambda: relayed.radio_a.client_count == 0), "the radio still has the member attached"
    assert await wait_until(lambda: not relayed.local_radio.connected_to_server), "the panel still thinks it is linked"
    assert relayed.manager.relay.link_count("k4a") == 0


async def test_force_release_cuts_the_relayed_link(relayed):
    result = await relayed.manager.request_assignment("W1AW", "k4a", MEMBER)
    await panel_connect(relayed, relay_port(relayed), result["password"])
    assert await wait_until(lambda: relayed.radio_a.client_count == 1)

    await relayed.manager.force_release(result["id"], "ADMIN")

    assert await wait_until(lambda: relayed.radio_a.client_count == 0)


async def test_the_old_password_cannot_reconnect_through_the_relay(relayed):
    result = await relayed.manager.request_assignment("W1AW", "k4a", MEMBER)
    await relayed.manager.release("W1AW")

    assert not await panel_connect(relayed, relay_port(relayed), result["password"])
    assert relayed.radio_a.client_count == 0


async def test_relay_refuses_when_no_session_is_active(relayed):
    relayed.radio_a.password = "known"
    relayed.radio_a.rrc = 1  # the radio itself would accept

    assert not await panel_connect(relayed, relay_port(relayed), "known")
    assert relayed.radio_a.client_count == 0


async def test_relay_refuses_an_address_other_than_the_members(relayed):
    result = await relayed.manager.request_assignment("W1AW", "k4a", ELSEWHERE)

    assert not await panel_connect(relayed, relay_port(relayed), result["password"])
    assert relayed.radio_a.client_count == 0


async def test_address_check_can_be_turned_off(relayed):
    relayed.config.relay.check_address = False
    result = await relayed.manager.request_assignment("W1AW", "k4a", ELSEWHERE)

    assert await panel_connect(relayed, relay_port(relayed), result["password"])


async def test_the_member_address_follows_their_api_calls(relayed):
    result = await relayed.manager.request_assignment("W1AW", "k4a", ELSEWHERE)
    relayed.manager.note_client_addr("W1AW", MEMBER)

    assert relayed.db.get_assignment(result["id"]).client_addr == MEMBER
    assert await panel_connect(relayed, relay_port(relayed), result["password"])


async def test_reload_moves_and_removes_relay_listeners(relayed):
    radio = relayed.config.radios["k4a"]
    old_port, new_port = radio.relay_port, free_port()
    text = relayed.config.path.read_text(encoding="utf-8")
    relayed.config.path.write_text(
        text.replace('id = "k4a"', f'id = "k4a"\nrelay_port = {new_port}', 1), encoding="utf-8"
    )

    await relayed.manager.reload_radios("ADMIN")

    assert relayed.manager.relay.listening_port("k4a") == new_port
    with socket.socket() as s:
        assert s.connect_ex(("127.0.0.1", old_port)) != 0, "the old relay port is still listening"

    relayed.config.path.write_text(text, encoding="utf-8")  # relay_port removed again
    await relayed.manager.reload_radios("ADMIN")
    assert relayed.manager.relay.listening_port("k4a") is None


async def test_api_records_where_the_member_called_from(api, server):
    response = await api.post("/assignments", json={"radio": "k4a"}, headers=server.headers("W1AW"))
    assert response.status_code == 201, response.text

    assignment = server.db.get_assignment(response.json()["id"])
    assert assignment.client_addr == "127.0.0.1"  # httpx.ASGITransport's client address
    assert assignment.state is AssignmentState.ACTIVE


async def test_link_state_reads_the_central_radios_client_count(relayed):
    result = await relayed.manager.request_assignment("W1AW", "k4a", MEMBER)
    assert (await relayed.manager.link_state("W1AW"))["attached"] is False

    await panel_connect(relayed, relay_port(relayed), result["password"])
    assert await wait_until(lambda: relayed.radio_a.client_count == 1)

    state = await relayed.manager.link_state("W1AW")
    assert state["attached"] is True and state["relayed_links"] == 1


async def test_link_endpoint_requires_an_active_session(api, server):
    response = await api.get("/assignments/current/link", headers=server.headers("W1AW"))
    assert response.status_code == 404


def test_ipv4_mapped_addresses_compare_equal():
    assert normalize_addr("::ffff:203.0.113.5") == "203.0.113.5"
    assert normalize_addr("203.0.113.5") == "203.0.113.5"
    assert normalize_addr(None) is None


def test_manager_is_built_with_a_relay(server):
    assert isinstance(server.manager, AssignmentManager)
    assert server.manager.relay.link_count("k4a") == 0
