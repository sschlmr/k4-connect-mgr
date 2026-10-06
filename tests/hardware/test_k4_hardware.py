"""Checks the CAT behaviour k4cm depends on, against real radios.

Skipped unless tests/hardware/hardware.toml is present — see conftest.py and
hardware.example.toml. Run with -s to watch the raw responses as they arrive.
Passwords are never written to the observations file, only their lengths.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets

import pytest

from k4cm import cat
from k4cm.client import radio as local_radio
from k4cm.models import Radio
from k4cm.server.config import ServerConfig
from k4cm.server.db import Database
from k4cm.server.manager import AssignmentManager, new_password

LINK_WAIT_S = 15.0
REFUSAL_WAIT_S = 12.0
POLL_S = 0.5


# -- helpers ---------------------------------------------------------------


def address(cfg: dict) -> tuple[str, int]:
    return cfg["cat_host"], int(cfg.get("cat_port", cat.DEFAULT_CAT_PORT))


async def client_count(cfg: dict) -> int | None:
    host, port = address(cfg)
    return cat.parse_client_count(await cat.query(host, port, "CC;"))


async def wait_for_count(cfg: dict, wanted, seconds: float = LINK_WAIT_S) -> list[int | None]:
    """Poll CC; until `wanted(value)` holds or time runs out. Returns every value seen."""
    seen: list[int | None] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while True:
        seen.append(await client_count(cfg))
        if wanted(seen[-1]) or loop.time() >= deadline:
            return seen
        await asyncio.sleep(POLL_S)


async def collect(client: cat.CatClient, seconds: float) -> list[str]:
    """Everything the radio sends on this connection for `seconds`."""
    responses: list[str] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while (remaining := deadline - loop.time()) > 0:
        try:
            responses.append(await client.read_response(timeout=remaining))
        except cat.CatError:
            break
    return responses


async def send_rrt(panel: dict, host: str, port: int, password: str, listen_s: float) -> list[str]:
    async with cat.CatClient(*address(panel)) as client:
        await client.send(f"RRT{host}:{port}?{password};")
        return await collect(client, listen_s)


async def make_idle(panel: dict) -> None:
    host, port = address(panel)
    async with cat.CatClient(host, port) as client:
        await client.send("RRN;")
    seen = await wait_for_count(panel, lambda v: v == 0)
    assert seen[-1] == 0, f"panel did not go idle after RRN; (CC values: {seen})"


# -- panel: your own K4/0 --------------------------------------------------


async def test_panel_answers_ping(panel):
    assert await cat.reachable(*address(panel)), "PING; got no PONG; from the panel"


async def test_panel_is_idle_after_rrn(panel, observed):
    await make_idle(panel)
    observed.record("panel.idle_cc", await client_count(panel))


async def test_connected_panel_reads_cc_minus_one(panel, remote, observed):
    await make_idle(panel)
    try:
        immediate = await send_rrt(panel, remote["host"], int(remote["port"]), remote["password"], 1.0)
        observed.record("rrt_valid.immediate_responses", immediate)
        seen = await wait_for_count(panel, lambda v: v not in (0, None))
        observed.record("rrt_valid.cc_sequence", seen)
        assert seen[-1] == -1, f"expected CC-1 on a connected panel, saw {seen}"
    finally:
        await make_idle(panel)


async def test_client_connect_and_disconnect_code_work_on_real_hardware(panel, remote, observed):
    await make_idle(panel)
    host, port = address(panel)
    try:
        detail = await local_radio._connect(host, port, remote["host"], int(remote["port"]), remote["password"])
        observed.record("client_connect.detail", detail)
        assert detail.startswith("connected"), detail
    finally:
        observed.record("client_disconnect.detail", await local_radio._disconnect(host, port))
    seen = await wait_for_count(panel, lambda v: v == 0)
    assert seen[-1] == 0, f"panel still connected after the client's RRN; (CC values: {seen})"


async def test_wrong_password_is_refused(panel, remote, observed):
    await make_idle(panel)
    wrong = secrets.token_urlsafe(12)
    try:
        responses = await send_rrt(panel, remote["host"], int(remote["port"]), wrong, REFUSAL_WAIT_S)
        observed.record("rrt_wrong_password.responses", responses)
        final = await client_count(panel)
        observed.record("rrt_wrong_password.final_cc", final)
    finally:
        await make_idle(panel)

    assert final != -1, "SECURITY: the panel connected with a wrong password"
    assert any(r.startswith("RRT!") for r in responses), (
        f"the client detects refusal by an RRT! response, but the radio sent {responses}; "
        "update k4cm.client.radio to match"
    )


# -- site: the central K4 --------------------------------------------------


async def test_site_answers_ping(site):
    assert await cat.reachable(*address(site)), "PING; got no PONG; from the central radio"


async def test_site_reports_its_settings(site, observed):
    host, port = address(site)
    observed.record("site.cc", await client_count(site))
    observed.record("site.rrc", await cat.query(host, port, "RRC;"))
    rrp = await cat.query(host, port, "RRP;")
    observed.record("site.rrp_readable", rrp.startswith("RRP"))
    observed.record("site.rrp_length", len(rrp) - 3)


@pytest.fixture
async def site_restored(site_writable, observed):
    """Remember the central radio's RRP/RRC and put them back afterwards."""
    host, port = address(site_writable)
    original_rrp = await cat.query(host, port, "RRP;")
    original_rrc = await cat.query(host, port, "RRC;")
    observed.record("site.original_rrc", original_rrc)
    try:
        yield site_writable
    finally:
        commands = []
        if original_rrp.startswith("RRP") and len(original_rrp) > 3:
            commands.append(f"{original_rrp};")
        if original_rrc.startswith("RRC"):
            commands.append(f"{original_rrc};")
        if commands:
            await cat.send_commands(host, port, commands)
        else:
            logging.getLogger(__name__).warning("could not restore RRP/RRC; the radio did not report them")


async def test_site_stores_generated_passwords_exactly(site_restored, observed):
    host, port = address(site_restored)
    candidates = {
        "generated": new_password(),
        "url_safe_punctuation": "Ab-_" + secrets.token_urlsafe(9),
    }
    for label, password in candidates.items():
        await cat.send_commands(host, port, [f"RRP{password};"])
        readback = await cat.query(host, port, "RRP;")
        observed.record(f"site.rrp_roundtrip.{label}.length", len(password))
        assert readback == f"RRP{password}", f"{label}: radio stored {len(readback) - 3} chars, not the password sent"

    for length in (24, 32, 48):
        probe = secrets.token_urlsafe(length)[:length]
        await cat.send_commands(host, port, [f"RRP{probe};"])
        observed.record(f"site.rrp_roundtrip.{length}_chars", await cat.query(host, port, "RRP;") == f"RRP{probe}")


async def test_site_applies_rrc(site_restored):
    host, port = address(site_restored)
    for value in (1, 0):
        await cat.send_commands(host, port, [f"RRC{value};"])
        assert await cat.query(host, port, "RRC;") == f"RRC{value}"


async def test_full_session_lifecycle_locks_the_member_out(site_restored, panel, tmp_path, observed):
    """The security model end to end: assign, connect, release, and prove that
    both the live link and the old password are dead."""
    site = site_restored
    radio = Radio(
        id="hw",
        name="hardware test K4",
        host=site["cat_host"],
        cat_port=int(site.get("cat_port", cat.DEFAULT_CAT_PORT)),
        public_host=site["public_host"],
        public_port=int(site["public_port"]),
    )
    config = ServerConfig(data_dir=tmp_path, path=tmp_path / "server.toml", radios={radio.id: radio})
    db = Database(tmp_path / "k4cm.db")
    manager = AssignmentManager(config, db, logging.getLogger("k4cm.hardware"))
    panel_host, panel_port = address(panel)

    await make_idle(panel)
    try:
        assignment = await manager.request_assignment("HWTEST", "hw")
        password = assignment["password"]

        detail = await local_radio._connect(
            panel_host, panel_port, assignment["radio"]["connect_host"], assignment["radio"]["connect_port"], password
        )
        observed.record("lifecycle.connect", detail)
        assert detail.startswith("connected"), detail
        site_seen = await wait_for_count(site, lambda v: v == 1)
        observed.record("lifecycle.site_cc_while_connected", site_seen)
        assert site_seen[-1] == 1, f"central radio should report one client, saw {site_seen}"

        await manager.release("HWTEST")

        panel_seen = await wait_for_count(panel, lambda v: v == 0)
        observed.record("lifecycle.panel_cc_after_release", panel_seen)
        assert panel_seen[-1] == 0, f"SECURITY: RRC0; did not drop the attached panel (CC values: {panel_seen})"

        # Reopen the radio under a new password, then retry the old one.
        await cat.send_commands(radio.host, radio.cat_port, [f"RRP{new_password()};", "RRC1;"])
        responses = await send_rrt(panel, radio.public_host, radio.connect_port, password, REFUSAL_WAIT_S)
        observed.record("lifecycle.old_password_responses", responses)
        final = await client_count(panel)
        assert final != -1, "SECURITY: a password from an ended session still connects"
    finally:
        with contextlib.suppress(cat.CatError, AssertionError):
            await make_idle(panel)
        db.close()
