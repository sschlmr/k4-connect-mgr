"""Measure the latency the server-side relay adds to a radio's remote link.

The relay's own cost is too small to see against a member's internet round
trip, so this isolates it: a TCP echo server stands in for the K4's remote
port, and the same pings go to it both directly and through a real
`RelayManager`. The difference between the two round trips is what the relay
adds, both directions together.

The relay runs in its own process, as it does on the site server. Sharing an
event loop (or, through the GIL, a process) with the pinging client would
charge the client's own scheduling to the relay.

A live K4 link is not idle: audio and the waterfall flow downstream all the
time, and a ping waits behind them. `--stream KBPS` has the echo server send
filler at that rate on each connection, so latency can be measured under load.

Run it on the site server to get numbers for that machine:

    .venv/bin/python scripts/relay_latency.py
    .venv/bin/python scripts/relay_latency.py --stream 400 --count 5000
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import multiprocessing
import socket
import statistics
import struct
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path

from k4cm.models import utcnow
from k4cm.server.config import load_config
from k4cm.server.db import Database
from k4cm.server.relay import RelayManager

# Frames are a type byte and a 16-bit length, so pings can be picked out of
# the filler stream on the way back.
HEADER = struct.Struct(">cH")
PING = b"P"
FILLER = b"F"
FILLER_FRAME = 1024
STREAM_TICK = 0.01

CONFIG = """
[relay]
bind = "127.0.0.1"
check_address = true

[[radios]]
id = "bench"
host = "127.0.0.1"
public_host = "127.0.0.1"
remote_port = {echo_port}
relay_port = {relay_port}
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def read_frame(reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
    kind, length = HEADER.unpack(await reader.readexactly(HEADER.size))
    return kind, await reader.readexactly(length)


def frame(kind: bytes, payload: bytes) -> bytes:
    return HEADER.pack(kind, len(payload)) + payload


# -- the relay, in its own process -------------------------------------------


def relay_process(echo_port: int, conn) -> None:
    asyncio.run(_serve_relay(echo_port, conn))


async def _serve_relay(echo_port: int, conn) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        path = data_dir / "server.toml"
        path.write_text(CONFIG.format(echo_port=echo_port, relay_port=free_port()), encoding="utf-8")
        config = load_config(path, data_dir)
        db = Database(data_dir / "k4cm.db")
        # The relay admits only an ACTIVE session's member, from its address.
        claim = db.claim_radio("bench", "BENCH", "unused", utcnow() + timedelta(hours=1), "127.0.0.1")
        db.activate(claim.id)

        relay = RelayManager(config, db, logging.getLogger("relay-bench"))
        await relay.sync()
        conn.send(relay.listening_port("bench"))
        await asyncio.get_running_loop().run_in_executor(None, conn.recv)  # until told to stop
        await relay.stop()
        db.close()


# -- the stand-in radio ------------------------------------------------------


async def start_echo(stream_kbps: float) -> asyncio.Server:
    per_tick = int(stream_kbps * 1000 / 8 * STREAM_TICK)

    async def stream(writer: asyncio.StreamWriter) -> None:
        loop = asyncio.get_running_loop()
        filler = frame(FILLER, bytes(FILLER_FRAME))
        due = loop.time()
        owed = 0
        while True:
            owed += per_tick
            while owed >= FILLER_FRAME:
                writer.write(filler)
                owed -= FILLER_FRAME
            await writer.drain()
            due += STREAM_TICK
            await asyncio.sleep(max(0.0, due - loop.time()))

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        streamer = asyncio.create_task(stream(writer)) if per_tick else None
        try:
            while True:
                kind, payload = await read_frame(reader)
                if kind == PING:
                    writer.write(frame(PING, payload))
        except (asyncio.IncompleteReadError, OSError):
            pass
        finally:
            if streamer:
                streamer.cancel()
            writer.transport.abort()

    return await asyncio.start_server(handle, "127.0.0.1", 0)


# -- the pinging client ------------------------------------------------------


class Pinger:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, size: int):
        self.reader = reader
        self.writer = writer
        self.padding = bytes(max(0, size - 8))
        self.waiting: asyncio.Future | None = None
        self.received = asyncio.create_task(self._receive())

    @classmethod
    async def connect(cls, port: int, size: int) -> Pinger:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        return cls(reader, writer, size)

    async def _receive(self) -> None:
        while True:
            kind, payload = await read_frame(self.reader)
            if kind == PING and self.waiting and not self.waiting.done():
                self.waiting.set_result(payload)

    async def ping(self, seq: int) -> float:
        """One round trip, in microseconds."""
        self.waiting = asyncio.get_running_loop().create_future()
        payload = struct.pack(">Q", seq) + self.padding
        start = time.perf_counter_ns()
        self.writer.write(frame(PING, payload))
        echoed = await asyncio.wait_for(self.waiting, 5)
        elapsed = time.perf_counter_ns() - start
        if echoed[:8] != payload[:8]:
            raise RuntimeError("echo out of order")
        return elapsed / 1000

    def close(self) -> None:
        self.received.cancel()
        self.writer.transport.abort()


# -- reporting ---------------------------------------------------------------


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(p / 100 * len(ordered)))]


def summarize(name: str, values: list[float]) -> dict[str, float]:
    stats = {
        "p50": percentile(values, 50),
        "p90": percentile(values, 90),
        "p99": percentile(values, 99),
        "max": max(values),
        "mean": statistics.fmean(values),
    }
    print(f"  {name:<8}" + "".join(f"{stats[k]:>10.0f}" for k in stats))
    return stats


async def benchmark(args: argparse.Namespace) -> None:
    echo = await start_echo(args.stream)
    echo_port = echo.sockets[0].getsockname()[1]

    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    process = ctx.Process(target=relay_process, args=(echo_port, child), daemon=True)
    process.start()
    loop = asyncio.get_running_loop()
    relay_port = await loop.run_in_executor(None, parent.recv)

    direct = await Pinger.connect(echo_port, args.size)
    relayed = await Pinger.connect(relay_port, args.size)
    try:
        # Alternate the two paths, so drift in machine load hits both alike.
        results: dict[str, list[float]] = {"direct": [], "relayed": []}
        for seq in range(args.warmup + args.count):
            for name, pinger in (("direct", direct), ("relayed", relayed)):
                rtt = await pinger.ping(seq)
                if seq >= args.warmup:
                    results[name].append(rtt)
            if args.interval:
                await asyncio.sleep(args.interval / 1000)
    finally:
        direct.close()
        relayed.close()
        parent.send("stop")
        await loop.run_in_executor(None, process.join, 5)
        echo.close()

    load = f", {args.stream:g} kbit/s downstream filler" if args.stream else ""
    print(f"\nRound trips, microseconds ({args.count} pings of {args.size} bytes{load}):\n")
    print("  " + " " * 8 + "".join(f"{k:>10}" for k in ("p50", "p90", "p99", "max", "mean")))
    a = summarize("direct", results["direct"])
    b = summarize("relayed", results["relayed"])
    print("  " + "-" * 58)
    print(f"  {'added':<8}" + "".join(f"{b[k] - a[k]:>10.0f}" for k in ("p50", "p90", "p99", "max", "mean")))
    print(
        f"\nThe relay adds about {(b['p50'] - a['p50']) / 1000:.2f} ms per round trip at the median "
        f"({(b['p99'] - a['p99']) / 1000:.2f} ms at p99), both directions together."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--count", type=int, default=2000, help="pings per path (default 2000)")
    parser.add_argument("--warmup", type=int, default=100, help="pings discarded first (default 100)")
    parser.add_argument("--size", type=int, default=32, help="ping payload in bytes, at least 8 (default 32)")
    parser.add_argument("--interval", type=float, default=2.0, help="ms between ping pairs (default 2)")
    parser.add_argument(
        "--stream", type=float, default=0, help="downstream filler per connection, kbit/s (default 0: idle link)"
    )
    args = parser.parse_args()
    if args.size < 8:
        parser.error("--size must be at least 8")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(benchmark(args))


if __name__ == "__main__":
    sys.exit(main())
