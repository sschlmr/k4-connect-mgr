"""Shared fixtures: fake K4s, a configured server, and an HTTP client."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import httpx
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).parent))

from fake_k4 import FakeK4

from k4cm.server.app import create_app
from k4cm.server.auth import generate_api_key, hash_api_key
from k4cm.server.config import load_config
from k4cm.server.db import Database
from k4cm.server.manager import AssignmentManager
from k4cm.server.monitor import Monitor

CONFIG_TEMPLATE = """
[server]
host = "127.0.0.1"
public_host = "127.0.0.1"
port = 8443
public_hostname = "mh01.net"

[session]
max_session_minutes = 240
warn_period_minutes = 15
ack_grace_minutes = 5
extension_minutes = 60
queue_hold_minutes = {queue_hold}

[monitor]
poll_interval_seconds = 30
disconnect_grace_minutes = {disconnect_grace}
cat_timeout_seconds = 2.0

[storage]
database = "k4cm.db"
log_level = "CRITICAL"

[backup]
enabled = false
directory = "backups"
retention = 3

[[radios]]
id = "k4a"
name = "K4 Alpha"
host = "127.0.0.1"
public_host = "127.0.0.1"
cat_port = {a_cat}
remote_port = {a_remote}
description = "Test radio A"
capabilities = ["hf"]

[[radios]]
id = "k4b"
name = "K4 Bravo"
host = "127.0.0.1"
public_host = "127.0.0.1"
cat_port = {b_cat}
remote_port = {b_remote}
description = "Test radio B"

  [radios.power]
  method = "none"

[[radios]]
id = "k4dead"
name = "K4 Offline"
host = "127.0.0.1"
public_host = "127.0.0.1"
cat_port = {dead_port}
remote_port = {dead_port}
description = "Never answers - used for unreachable-path tests"
"""


class ServerFixture:
    """A configured server plus the fake radios it manages."""

    def __init__(self, config, db, manager, monitor, app, radio_a, radio_b, local_radio):
        self.config = config
        self.db = db
        self.manager = manager
        self.monitor = monitor
        self.app = app
        self.radio_a = radio_a          # fake central K4 "k4a"
        self.radio_b = radio_b          # fake central K4 "k4b"
        self.local_radio = local_radio  # fake K4/0 standing in for a user's panel
        self.keys: dict[str, str] = {}

    def add_user(self, callsign: str, is_admin: bool = False) -> str:
        api_key = generate_api_key()
        self.db.create_user(callsign, hash_api_key(api_key), is_admin)
        self.keys[callsign.upper()] = api_key
        return api_key

    def headers(self, callsign: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.keys[callsign.upper()]}"}


@pytest_asyncio.fixture
async def server(tmp_path: Path) -> ServerFixture:
    radio_a = FakeK4("k4a")
    radio_b = FakeK4("k4b")
    local_radio = FakeK4("local")
    for fake in (radio_a, radio_b, local_radio):
        await fake.start()

    # A port nothing listens on, for the unreachable-radio paths.
    dead = FakeK4("dead")
    await dead.start()
    dead_port = dead.cat_port
    await dead.stop()

    config_path = tmp_path / "server.toml"
    config_path.write_text(
        CONFIG_TEMPLATE.format(
            a_cat=radio_a.cat_port,
            a_remote=radio_a.remote_port,
            b_cat=radio_b.cat_port,
            b_remote=radio_b.remote_port,
            dead_port=dead_port,
            queue_hold=10,
            disconnect_grace=5,
        ),
        encoding="utf-8",
    )

    config = load_config(path=config_path, data_dir=tmp_path)
    db = Database(config.database_path)
    logger = logging.getLogger("k4cm.test")
    logger.addHandler(logging.NullHandler())
    manager = AssignmentManager(config, db, logger)
    monitor = Monitor(manager)
    app = create_app(config, db, logger, background=False)

    fixture = ServerFixture(config, db, manager, monitor, app, radio_a, radio_b, local_radio)
    try:
        yield fixture
    finally:
        db.close()
        for fake in (radio_a, radio_b, local_radio):
            await fake.stop()


@pytest_asyncio.fixture
async def api(server: ServerFixture):
    """An HTTP client bound to the app, with ADMIN and W1AW users created."""
    server.add_user("ADMIN", is_admin=True)
    server.add_user("W1AW")
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
        yield client
