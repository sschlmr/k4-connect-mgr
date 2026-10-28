"""Experiment: can a second K4/0 watch a session by copying the radio's stream?

An observer would see everything the controlling panel sees but control
nothing: the relay copies what the K4 sends to the controller to each observer
as well, and throws away whatever the observer's panel sends. The K4 only ever
has one client, so it needs no extra password or keepalives. Whether this works
depends on something the Programmer's Reference doesn't cover: whether a K4/0
can use a stream that was set up for a different panel's connection, especially
one that has already started. This script finds out on hardware.

It is a standalone tee relay, not the k4cm relay. Run it on a machine that can
reach the K4's remote port (the site server PC). It listens on three ports:

  --controller-port  the one panel in control: a normal two-way link to the K4
  --live-port        observers that get the stream from the moment they join
  --replay-port      observers that first get the controller's opening bytes
                     (its first --prime-seconds of downstream), then the
                     stream from the moment they join

An observer may connect before the controller does; it is held until the
controller's link opens, and then gets the stream from its first byte. Each
observer has its own buffer. One that falls --max-lag bytes behind is
disconnected, so a slow observer never slows the controller. When the controller
disconnects, every observer is disconnected.

Every connection's first --capture bytes in each direction are saved under
--out, with a summary.json. Comparing the controller's opening upstream bytes
across two connections shows whether the login is the same every time; the
printable fraction of the downstream suggests whether it is encrypted.

Procedure (radio idle; two K4/0s; nobody else needs the radio):

 1. An admin takes a k4cm session on the radio, so k4cm sets a fresh password
    and enables remote connections, and notes the password:
        k4cm request K4-1 --no-watch
    k4cm's own relay is not involved; this script dials the radio's remote
    port on the LAN directly.
 2. On the server PC, with three ports forwarded to it from the router (or
    reachable over the VPN) that are not the production relay ports:
        .venv/bin/python scripts/observer_tee.py --radio 10.0.0.11:9204 \\
            --controller-port 9310 --live-port 9311 --replay-port 9312
    Add --allow ADDR for each tester's public address to keep scanners out.
 3. Each tester enters the site host, the port and the password manually on
    the K4/0. Observers use the same password; it is discarded either way.

    A. Baseline: controller alone on 9310. Audio, waterfall and tuning work?
       (Confirms the tee itself is transparent, as the k4cm relay is.)
    B. Join at the start: observer connects to 9311 *first*, then the controller
       to 9310 within a few seconds (the observer's panel is held with no reply
       until then, and may give up). Does the observer show the waterfall and
       play audio? Does its display follow when the controller tunes? This is
       the decisive case: the observer sees the whole stream, handshake
       included. If B fails, C and D cannot work either.
    C. Join late: controller connected for a minute, then observer on 9311.
    D. Join late with replay: as C, but observer on 9312.
    E. Control is one way: observer turns its VFO and presses buttons. Nothing
       should change on the K4 or the controller's panel. What does the
       observer's own display do?
    F. Teardown: controller disconnects. The observer should be dropped at once.

    Repeat C and D once more so the captures include two controller logins.
 4. Stop this script (Ctrl-C) *before* releasing the k4cm session. k4cm's
    lockout does not drop an attached client, so a link through this script
    would otherwise outlive the session.
 5. Send back what each panel showed in A-F and the --out directory.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ipaddress
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

CHUNK = 65536


def log(message: str) -> None:
    print(f"{datetime.now().astimezone():%H:%M:%S.%f}"[:-3] + f"  {message}", flush=True)


def normalize_addr(addr: str | None) -> str | None:
    if not addr:
        return None
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return addr
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return str(ip)


class Capture:
    """The first `limit` bytes one side of a connection sent, and its totals."""

    def __init__(self, limit: int):
        self.limit = limit
        self.head = bytearray()
        self.total = 0
        self.first_at: float | None = None

    def add(self, data: bytes) -> None:
        if self.first_at is None:
            self.first_at = time.monotonic()
        self.total += len(data)
        room = self.limit - len(self.head)
        if room > 0:
            self.head += data[:room]

    def summary(self, opened: float) -> dict:
        printable = sum(32 <= b < 127 or b in (9, 10, 13) for b in self.head)
        return {
            "bytes": self.total,
            "first_byte_after_s": None if self.first_at is None else round(self.first_at - opened, 3),
            "printable_fraction": round(printable / len(self.head), 3) if self.head else None,
            "first_64_hex": self.head[:64].hex(" "),
        }


class Observer:
    def __init__(self, n: int, kind: str, addr: str | None, writer: asyncio.StreamWriter, max_lag: int):
        self.n = n
        self.kind = kind
        self.addr = addr
        self.writer = writer
        self.max_lag = max_lag
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.queued = 0
        self.dropped_for: str | None = None

    def offer(self, data: bytes) -> None:
        """Queue `data` without waiting; drop the observer if it falls too far behind."""
        if self.dropped_for is not None:
            return
        if self.queued + len(data) > self.max_lag:
            self.drop(f"fell more than {self.max_lag} bytes behind")
            return
        self.queued += len(data)
        self.queue.put_nowait(data)

    def drop(self, why: str) -> None:
        if self.dropped_for is None:
            self.dropped_for = why
            self.queue.put_nowait(None)
            self.writer.transport.abort()


class Tee:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.allow = {normalize_addr(a) for a in args.allow} if args.allow else None
        self.out = Path(args.out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.counter = 0
        self.summaries: list[dict] = []
        self.controlling: asyncio.StreamWriter | None = None
        self.linked = False  # the controller's link to the radio is open
        self.prime = bytearray()
        self.prime_open = False
        self.observers: set[Observer] = set()  # receiving the stream
        self.waiting: set[Observer] = set()    # joined before any controller

    def _admit(self, role: str, writer: asyncio.StreamWriter) -> tuple[int, str | None] | None:
        peer = writer.get_extra_info("peername")
        addr = normalize_addr(peer[0] if peer else None)
        if self.allow is not None and addr not in self.allow:
            log(f"refused {role} from {addr} (not in --allow)")
            writer.transport.abort()
            return None
        self.counter += 1
        return self.counter, addr

    def _save(self, n: int, role: str, addr: str | None, opened: float, captures: dict[str, Capture], **extra) -> None:
        for direction, capture in captures.items():
            (self.out / f"{n:02d}-{role}-{direction}.bin").write_bytes(capture.head)
        duration = time.monotonic() - opened
        entry = {
            "n": n,
            "role": role,
            "addr": addr,
            "opened": (datetime.now().astimezone() - timedelta(seconds=duration)).isoformat(timespec="seconds"),
            "duration_s": round(duration, 1),
            **extra,
            **{direction: c.summary(opened) for direction, c in captures.items()},
        }
        self.summaries.append(entry)
        (self.out / "summary.json").write_text(json.dumps(self.summaries, indent=2) + "\n")

    # -- controller ----------------------------------------------------------

    async def controller(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        admitted = self._admit("controller", writer)
        if admitted is None:
            return
        n, addr = admitted
        if self.controlling is not None:
            log(f"#{n} refused controller from {addr}: one is already connected")
            writer.transport.abort()
            return
        self.controlling = writer
        host, port = self.args.radio
        log(f"#{n} controller from {addr}; dialling the radio at {host}:{port}")
        try:
            radio_reader, radio_writer = await asyncio.wait_for(asyncio.open_connection(host, port), 5)
        except (TimeoutError, OSError) as e:
            log(f"#{n} cannot reach the radio: {e or type(e).__name__}")
            self.controlling = None
            writer.transport.abort()
            return

        opened = time.monotonic()
        up, down = Capture(self.args.capture), Capture(self.args.capture)
        self.prime = bytearray()
        self.prime_open = True
        self.linked = True
        # Before the first downstream read, so early observers get every byte.
        self.observers |= self.waiting
        self.waiting.clear()
        ended = {}

        async def upstream() -> None:
            await self._copy(reader, radio_writer, up, ended, "controller")

        async def downstream() -> None:
            try:
                while True:
                    try:
                        data = await radio_reader.read(CHUNK)
                    except OSError:
                        ended.setdefault("by", "radio failed")
                        break
                    if not data:
                        ended.setdefault("by", "radio closed")
                        break
                    down.add(data)
                    if self.prime_open:
                        if time.monotonic() - opened < self.args.prime_seconds and len(self.prime) < self.args.max_lag:
                            self.prime += data
                        else:
                            self.prime_open = False
                            log(f"#{n} replay prime fixed at {len(self.prime)} bytes")
                    for obs in list(self.observers):
                        obs.offer(data)
                    try:
                        writer.write(data)
                        await writer.drain()
                    except OSError:
                        ended.setdefault("by", "controller failed")
                        break
            finally:
                writer.transport.abort()

        await asyncio.gather(upstream(), downstream(), return_exceptions=True)
        radio_writer.transport.abort()
        writer.transport.abort()
        self.controlling = None
        self.linked = False
        self.prime_open = False
        for obs in list(self.observers):
            obs.drop("controller disconnected")
        log(f"#{n} controller link ended ({ended.get('by', 'closed')}); observers dropped")
        self._save(n, "controller", addr, opened, {"up": up, "down": down}, ended_by=ended.get("by"))

    @staticmethod
    async def _copy(reader, writer, capture: Capture, ended: dict, side: str) -> None:
        try:
            while True:
                try:
                    data = await reader.read(CHUNK)
                except OSError:
                    ended.setdefault("by", f"{side} failed")
                    break
                if not data:
                    ended.setdefault("by", f"{side} closed")
                    break
                capture.add(data)
                try:
                    writer.write(data)
                    await writer.drain()
                except OSError:
                    ended.setdefault("by", "radio failed")
                    break
        finally:
            writer.transport.abort()

    # -- observers -----------------------------------------------------------

    async def observer(self, kind: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        admitted = self._admit(f"{kind} observer", writer)
        if admitted is None:
            return
        n, addr = admitted
        opened = time.monotonic()
        up, down = Capture(self.args.capture), Capture(self.args.capture)
        obs = Observer(n, kind, addr, writer, self.args.max_lag)
        waited = not self.linked
        if waited:
            self.waiting.add(obs)
        elif kind == "replay":
            # Prime and registration with no await between them, so no
            # downstream chunk is missed or sent twice.
            obs.offer(bytes(self.prime))
            self.observers.add(obs)
            log(f"#{n} replay observer from {addr}: {len(self.prime)} opening bytes, then live")
        else:
            self.observers.add(obs)
        if kind == "live" or waited:
            log(f"#{n} {kind} observer from {addr}" + ("; waiting for a controller" if waited else ""))

        async def discard_upstream() -> None:
            # Whatever the observer's panel sends goes nowhere.
            with contextlib.suppress(OSError):
                while data := await reader.read(CHUNK):
                    up.add(data)
            obs.drop("observer closed")

        async def feed() -> None:
            try:
                while (data := await obs.queue.get()) is not None:
                    obs.queued -= len(data)
                    down.add(data)
                    writer.write(data)
                    await writer.drain()
            except OSError:
                obs.drop("observer failed")
            finally:
                self.observers.discard(obs)
                self.waiting.discard(obs)

        reading = asyncio.create_task(discard_upstream())
        try:
            await feed()
        finally:
            obs.drop(obs.dropped_for or "closed")
            reading.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reading
        log(f"#{n} {kind} observer ended: {obs.dropped_for}; it sent {up.total} bytes, all discarded")
        self._save(
            n, f"{kind}-observer", addr, opened, {"up": up, "down": down},
            joined_before_controller=waited, ended_by=obs.dropped_for,
        )


def host_port(text: str) -> tuple[str, int]:
    host, _, port = text.rpartition(":")
    if not host or not port.isdigit():
        raise argparse.ArgumentTypeError("expected HOST:PORT")
    return host, int(port)


async def run(args: argparse.Namespace) -> None:
    tee = Tee(args)
    servers = [
        await asyncio.start_server(tee.controller, args.bind, args.controller_port),
        await asyncio.start_server(lambda r, w: tee.observer("live", r, w), args.bind, args.live_port),
        await asyncio.start_server(lambda r, w: tee.observer("replay", r, w), args.bind, args.replay_port),
    ]
    host, port = args.radio
    log(f"radio {host}:{port}; controller :{args.controller_port}, live observers :{args.live_port}, "
        f"replay observers :{args.replay_port}; captures in {tee.out}")
    if tee.allow is None:
        log("no --allow given: any address may connect (observers can listen, but never control)")
    try:
        await asyncio.Event().wait()
    finally:
        for server in servers:
            server.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--radio", type=host_port, required=True, help="the K4's remote-link address on the LAN")
    parser.add_argument("--bind", default="0.0.0.0", help="address to listen on (default 0.0.0.0)")
    parser.add_argument("--controller-port", type=int, default=9310)
    parser.add_argument("--live-port", type=int, default=9311)
    parser.add_argument("--replay-port", type=int, default=9312)
    parser.add_argument("--allow", action="append", metavar="ADDR", help="admit only these addresses (repeatable)")
    parser.add_argument(
        "--prime-seconds", type=float, default=3.0, help="controller downstream kept for replay (default 3)"
    )
    parser.add_argument(
        "--max-lag", type=int, default=4 * 1024 * 1024, help="bytes an observer may fall behind (default 4 MB)"
    )
    parser.add_argument(
        "--capture", type=int, default=256 * 1024, help="bytes saved per direction per connection (default 256 KB)"
    )
    parser.add_argument(
        "--out", default=f"observer-tee-{datetime.now().astimezone():%Y%m%d-%H%M%S}", help="capture directory"
    )
    args = parser.parse_args()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
