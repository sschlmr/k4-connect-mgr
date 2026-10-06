"""End-to-end: the real CLI against a live HTTP server and fake K4s.

The server runs in its own thread; CLI invocations run in worker threads so the
test's event loop stays free to serve the fake radios.
"""

from __future__ import annotations

import asyncio
import threading

import pytest
import pytest_asyncio
import uvicorn
from typer.testing import CliRunner

from k4cm.client.config import ClientConfig, save_config
from k4cm.client.main import app as cli


class LiveServer:
    def __init__(self, fixture, base_url: str):
        self.fixture = fixture
        self.base_url = base_url


@pytest_asyncio.fixture
async def live(server, tmp_path, monkeypatch):
    """Serve the app over localhost HTTP and point a configured CLI at it."""
    server.add_user("ADMIN", is_admin=True)
    server.add_user("W1AW")

    config = uvicorn.Config(server.app, host="127.0.0.1", port=0, log_level="error", lifespan="on")
    uvicorn_server = uvicorn.Server(config)
    thread = threading.Thread(target=uvicorn_server.run, daemon=True)
    thread.start()

    for _ in range(200):
        if uvicorn_server.started:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("uvicorn did not start")

    port = uvicorn_server.servers[0].sockets[0].getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"

    client_dir = tmp_path / "client"
    client_dir.mkdir()
    monkeypatch.setenv("K4CM_CLIENT_DIR", str(client_dir))
    save_config(
        ClientConfig(
            server_url=base_url,
            callsign="W1AW",
            api_key=server.keys["W1AW"],
            radio_host="127.0.0.1",
            radio_cat_port=server.local_radio.cat_port,
        ),
        client_dir / "config.toml",
    )

    try:
        yield LiveServer(server, base_url)
    finally:
        uvicorn_server.should_exit = True
        await asyncio.to_thread(thread.join, 10)


async def run_cli(*args: str) -> object:
    """Invoke the CLI off the event loop so the fake radios keep serving."""
    return await asyncio.to_thread(CliRunner().invoke, cli, list(args))


async def test_status_lists_the_sites_radios(live):
    result = await run_cli("status")
    assert result.exit_code == 0, result.output
    assert "k4a" in result.output
    assert "free" in result.output


async def test_request_connects_the_local_panel_then_release_frees_it(live):
    fixture = live.fixture

    result = await run_cli("request", "k4a", "--no-watch")
    assert result.exit_code == 0, result.output
    assert "K4 Alpha is yours" in result.output
    assert "connected" in result.output

    # The central radio holds the issued password and one attached client.
    assert fixture.radio_a.client_count == 1
    assert fixture.db.live_assignment_for_radio("k4a").callsign == "W1AW"

    status = await run_cli("status")
    assert "in use by W1AW" in status.output

    released = await run_cli("release")
    assert released.exit_code == 0, released.output
    assert fixture.radio_a.client_count == 0
    assert fixture.radio_a.rrc == 0
    assert fixture.db.live_assignment_for_radio("k4a") is None


async def test_requesting_an_unreachable_radio_reports_the_failure(live):
    result = await run_cli("request", "k4dead", "--no-watch")
    assert result.exit_code == 1
    assert "not reachable" in result.output
    assert live.fixture.db.live_assignment_for_radio("k4dead") is None


async def test_busy_radio_offers_the_queue(live):
    fixture = live.fixture
    await run_cli("request", "k4a", "--no-watch")  # W1AW takes it

    # A second user, configured in a separate client directory.
    import os

    second_dir = fixture.config.data_dir / "client2"
    second_dir.mkdir(parents=True, exist_ok=True)
    fixture.add_user("K2ABC")
    save_config(
        ClientConfig(
            server_url=live.base_url,
            callsign="K2ABC",
            api_key=fixture.keys["K2ABC"],
            radio_host="127.0.0.1",
            radio_cat_port=fixture.local_radio.cat_port,
        ),
        second_dir / "config.toml",
    )
    os.environ["K4CM_CLIENT_DIR"] = str(second_dir)

    result = await run_cli("request", "k4a", "--no-watch", "--queue")

    assert result.exit_code == 1
    assert "in use by W1AW" in result.output
    assert "Queued for K4 Alpha at position 1" in result.output
    assert fixture.db.queue_depth("k4a") == 1


async def test_member_cannot_run_admin_commands(live):
    result = await run_cli("admin", "users")
    assert result.exit_code == 1
    assert "admin privileges required" in result.output


async def test_admin_commands_with_an_admin_key(live, tmp_path):
    import os

    fixture = live.fixture
    admin_dir = tmp_path / "admin"
    admin_dir.mkdir()
    save_config(
        ClientConfig(
            server_url=live.base_url, callsign="ADMIN", api_key=fixture.keys["ADMIN"]
        ),
        admin_dir / "config.toml",
    )
    os.environ["K4CM_CLIENT_DIR"] = str(admin_dir)

    users = await run_cli("admin", "users")
    assert users.exit_code == 0, users.output
    assert "W1AW" in users.output

    radios = await run_cli("admin", "radios")
    assert radios.exit_code == 0, radios.output
    assert "K4 Alpha" in radios.output

    created = await run_cli("admin", "users", "--add", "n0new")
    assert created.exit_code == 0, created.output
    assert "API key (shown once)" in created.output
    assert fixture.db.get_user("N0NEW") is not None


async def test_admin_force_release_and_audit(live, tmp_path):
    import os

    fixture = live.fixture
    await run_cli("request", "k4a", "--no-watch")
    assignment_id = fixture.db.live_assignment_for_radio("k4a").id

    admin_dir = tmp_path / "admin2"
    admin_dir.mkdir()
    save_config(
        ClientConfig(server_url=live.base_url, callsign="ADMIN", api_key=fixture.keys["ADMIN"]),
        admin_dir / "config.toml",
    )
    os.environ["K4CM_CLIENT_DIR"] = str(admin_dir)

    forced = await run_cli("admin", "sessions", "--force-release", str(assignment_id))
    assert forced.exit_code == 0, forced.output
    assert fixture.db.live_assignment_for_radio("k4a") is None

    audit = await run_cli("admin", "audit")
    assert audit.exit_code == 0, audit.output
    assert "session.forced" in audit.output
