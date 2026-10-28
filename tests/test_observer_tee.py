"""scripts/observer_tee.py, the observer experiment, against a scripted radio.

The experiment is run on hardware with people watching, so its plumbing is
checked here first: what reaches each observer, that nothing an observer sends
reaches the radio, and that the controller's end drops the observers.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "observer_tee.py"
spec = importlib.util.spec_from_file_location("observer_tee", SCRIPT)
observer_tee = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observer_tee)


class Radio:
    """Sends HELLO on connect, then whatever the test pushes; records what it hears."""

    def __init__(self):
        self.heard = bytearray()
        self.writer: asyncio.StreamWriter | None = None
        self.connected = asyncio.Event()

    async def handle(self, reader, writer):
        self.writer = writer
        writer.write(b"HELLO")
        await writer.drain()
        self.connected.set()
        while data := await reader.read(1024):
            self.heard += data

    async def push(self, data: bytes) -> None:
        self.writer.write(data)
        await self.writer.drain()


@pytest.fixture
async def site(tmp_path):
    radio = Radio()
    radio_server = await asyncio.start_server(radio.handle, "127.0.0.1", 0)
    args = argparse.Namespace(
        radio=("127.0.0.1", radio_server.sockets[0].getsockname()[1]),
        allow=None, prime_seconds=0.2, max_lag=1024 * 1024, capture=4096, out=str(tmp_path / "out"),
    )
    tee = observer_tee.Tee(args)
    servers = {
        "controller": await asyncio.start_server(tee.controller, "127.0.0.1", 0),
        "live": await asyncio.start_server(lambda r, w: tee.observer("live", r, w), "127.0.0.1", 0),
        "replay": await asyncio.start_server(lambda r, w: tee.observer("replay", r, w), "127.0.0.1", 0),
    }

    async def dial(role: str):
        return await asyncio.open_connection("127.0.0.1", servers[role].sockets[0].getsockname()[1])

    yield radio, tee, dial
    for server in [*servers.values(), radio_server]:
        server.close()


async def read_exactly(reader, n: int) -> bytes:
    return await asyncio.wait_for(reader.readexactly(n), 2)


async def until(predicate) -> None:
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_early_observer_gets_every_byte_and_controls_nothing(site):
    radio, tee, dial = site
    obs_r, obs_w = await dial("live")
    await until(lambda: tee.waiting)
    ctl_r, ctl_w = await dial("controller")
    await asyncio.wait_for(radio.connected.wait(), 2)

    await radio.push(b"STATE")
    assert await read_exactly(ctl_r, 10) == b"HELLOSTATE"
    assert await read_exactly(obs_r, 10) == b"HELLOSTATE"

    ctl_w.write(b"TUNE;")
    obs_w.write(b"EVIL;")
    await ctl_w.drain()
    await obs_w.drain()
    await until(lambda: radio.heard == b"TUNE;")
    await asyncio.sleep(0.1)
    assert radio.heard == b"TUNE;"


async def test_late_observers_live_and_replay(site):
    radio, tee, dial = site
    ctl_r, ctl_w = await dial("controller")  # kept: a collected writer closes the link
    await asyncio.wait_for(radio.connected.wait(), 2)
    await radio.push(b"STATE")
    assert await read_exactly(ctl_r, 10) == b"HELLOSTATE"
    await asyncio.sleep(0.3)  # past --prime-seconds
    await radio.push(b"-")    # the first chunk after the window closes the prime
    await until(lambda: not tee.prime_open)

    live_r, live_w = await dial("live")
    replay_r, replay_w = await dial("replay")
    await until(lambda: len(tee.observers) == 2)
    await radio.push(b"DELTA")
    assert await read_exactly(live_r, 5) == b"DELTA"
    assert await read_exactly(replay_r, 15) == b"HELLOSTATEDELTA"
    for writer in (ctl_w, live_w, replay_w):
        writer.close()


async def test_controller_leaving_drops_observers(site):
    radio, tee, dial = site
    _, ctl_w = await dial("controller")
    await asyncio.wait_for(radio.connected.wait(), 2)
    obs_r, obs_w = await dial("live")
    await until(lambda: tee.observers)

    ctl_w.close()
    assert await closed(obs_r)
    await until(lambda: any(s["role"] == "live-observer" for s in tee.summaries))
    entry = next(s for s in tee.summaries if s["role"] == "live-observer")
    assert entry["ended_by"] == "controller disconnected"
    obs_w.close()


async def test_second_controller_refused(site):
    radio, _, dial = site
    _, first_w = await dial("controller")
    await asyncio.wait_for(radio.connected.wait(), 2)
    second_r, _ = await dial("controller")
    assert await closed(second_r)
    first_w.close()


async def closed(reader) -> bool:
    """The tee aborts refused connections, so the peer may see a reset or an EOF."""
    try:
        return await asyncio.wait_for(reader.read(), 2) == b""
    except ConnectionResetError:
        return True
