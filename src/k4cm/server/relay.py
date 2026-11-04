"""Relay for each radio's remote link, so that ending a session really ends it.

A K4 keeps an attached client when its password is changed and RRC0; is sent;
those only stop *new* connections, and there is no CAT command that disconnects
a client (verified on hardware 2026-10-09). So the server owns the path. For a
radio with `relay_port` set, members' panels connect to this server, which
passes bytes to the radio's own remote port, and ending a session closes the
member's link. A K4 drops a client that goes silent (the Programmer's Reference
says within 10 s; about 2 s was observed).

The remote link is plain TCP. Relaying a live K4/0 session carried audio, the
waterfall and tuning with no UDP at all, so a byte pump is all this needs.

A connection is admitted only while the radio has an ACTIVE session and, with
`[relay] check_address`, only from the address the member last used to reach
the API. The radio's own password is still required behind the relay, so the
relay adds a gate and never removes one. Refusals are logged but not audited,
since a forwarded port attracts scanners and the audit trail would fill up.

An admitted link's end is audited (`relay.disconnect`) with how long it lasted,
the bytes each way, and which side closed first. Without that, a link that
drops mid-session leaves only a run of `relay.connect` redials, which looks
the same whether the member's internet, the site LAN or the radio failed.

`[relay] timing` (off by default) is for a link that stays up but stutters,
such as a panadapter that freezes for a moment every second or two. Ping can
look perfect while that happens, so the relay times the stream itself: gaps
between arrivals from each side, how long a send waited for the far side to
take it, and how late this server's event loop ran. Each link logs one line per
`timing_report_seconds`: gaps already in what the radio sends point at the
radio or the site LAN; event-loop lag points at this server; long send waits or
a growing backlog toward the member point at the path to them. A smooth stream
with none of those leaves the internet path beyond the send buffer, or the
panel itself. It goes to the log, not the audit trail, and `k4cm admin radios
--reload` turns it on or off, live links included, without a restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import time
from collections import defaultdict

from ..models import AssignmentState, Radio
from .config import ServerConfig
from .db import Database

PUMP_CHUNK = 65536
LAG_TICK = 0.05  # how often the timing loop checks that the event loop is on time


def normalize_addr(addr: str | None) -> str | None:
    """Compare addresses canonically, so an IPv4 peer seen through a dual-stack
    socket (::ffff:203.0.113.5) matches the same peer seen over IPv4."""
    if not addr:
        return None
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return addr
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return str(ip)


class _Flow:
    """Timing of one direction of a link over one report period."""

    def __init__(self, now: float, gap_threshold: float):
        self.threshold = gap_threshold
        self.last = now
        self.chunks = 0
        self.bytes = 0
        self.max_gap = 0.0
        self.long_gaps = 0
        self.max_wait = 0.0
        self.long_waits = 0
        self.max_backlog = 0

    def arrived(self, now: float, size: int) -> None:
        gap = now - self.last
        self.last = now
        self.chunks += 1
        self.bytes += size
        self.max_gap = max(self.max_gap, gap)
        if gap >= self.threshold:
            self.long_gaps += 1

    def sent(self, wait: float, backlog: int) -> None:
        self.max_wait = max(self.max_wait, wait)
        if wait >= self.threshold:
            self.long_waits += 1
        self.max_backlog = max(self.max_backlog, backlog)

    def report(self, now: float) -> str:
        # Silence since the last chunk counts as a gap too, or a stream that
        # stops dead would report as smooth.
        max_gap = max(self.max_gap, now - self.last)
        limit = _ms(self.threshold)
        return (
            f"{self.chunks} chunks, {_size(self.bytes)}, longest gap {_ms(max_gap)} "
            f"({self.long_gaps} >= {limit}), longest send wait {_ms(self.max_wait)} "
            f"({self.long_waits} >= {limit}), backlog up to {_size(self.max_backlog)}"
        )


class _Timing:
    """Both directions of one link, reset at each report."""

    def __init__(self, now: float, gap_threshold: float):
        self.started = now
        self.flows = {side: _Flow(now, gap_threshold) for side in ("member", "radio")}

    def report(self, now: float, lag: float) -> str:
        line = (
            f"{_duration(now - self.started)}: radio->member {self.flows['radio'].report(now)}; "
            f"member->radio {self.flows['member'].report(now)}; event loop late by up to {_ms(lag)}"
        )
        threshold = self.flows["radio"].threshold
        self.started = now
        self.flows = {side: _Flow(flow.last, threshold) for side, flow in self.flows.items()}
        return line


class _Link:
    """One member's relayed connection. Registered before the onward connection
    is opened, so a session that ends meanwhile still cuts it."""

    def __init__(self, assignment_id: int, member: asyncio.StreamWriter, addr: str | None):
        self.assignment_id = assignment_id
        self.member = member
        self.addr = addr
        self.radio: asyncio.StreamWriter | None = None
        self.cut = False
        self.sent = {"member": 0, "radio": 0}  # bytes each side sent through us
        self.heard = {"member": 0.0, "radio": 0.0}  # monotonic time each side last sent
        self.ended_by: str | None = None       # the first side to close, and how
        self.timing: _Timing | None = None     # set while [relay] timing is on

    def abort(self) -> None:
        self.cut = True
        for writer in (self.member, self.radio):
            if writer is not None:
                writer.transport.abort()


class RelayManager:
    def __init__(self, config: ServerConfig, db: Database, logger: logging.Logger):
        self.config = config
        self.db = db
        self.logger = logger
        self._listeners: dict[str, tuple[int, asyncio.Server]] = {}
        self._links: defaultdict[str, set[_Link]] = defaultdict(set)
        self._timing_task: asyncio.Task | None = None

    # -- listeners ---------------------------------------------------------

    async def sync(self) -> None:
        """Make the listeners match the radio inventory: start one for each
        radio with a relay_port, and stop (cutting their links) any whose radio
        was removed or moved to another port."""
        wanted = {r.id: r for r in self.config.radios.values() if r.relay_port}
        for radio_id, (port, server) in list(self._listeners.items()):
            radio = wanted.get(radio_id)
            if radio is None or radio.relay_port != port:
                await self._stop_listener(radio_id, server)
        for radio in wanted.values():
            if radio.id in self._listeners:
                continue
            try:
                server = await asyncio.start_server(
                    lambda r, w, radio_id=radio.id: self._handle(radio_id, r, w),
                    self.config.relay.bind,
                    radio.relay_port,
                )
            except OSError as e:
                self.logger.error("cannot relay %s on port %s: %s", radio.id, radio.relay_port, e)
                self.db.audit("system", "relay.failed", f"{radio.id}: cannot listen on {radio.relay_port}: {e}")
                continue
            self._listeners[radio.id] = (radio.relay_port, server)
            self.logger.info("relaying %s: port %s -> %s:%s", radio.id, radio.relay_port, radio.host, radio.remote_port)
        await self._sync_timing()

    async def stop(self) -> None:
        for radio_id, (_, server) in list(self._listeners.items()):
            await self._stop_listener(radio_id, server)
        await self._stop_timing()

    async def _stop_listener(self, radio_id: str, server: asyncio.Server) -> None:
        self._listeners.pop(radio_id, None)
        server.close()
        self.cut(radio_id)
        with contextlib.suppress(TimeoutError, OSError):
            await asyncio.wait_for(server.wait_closed(), timeout=2)

    def listening_port(self, radio_id: str) -> int | None:
        entry = self._listeners.get(radio_id)
        return entry[1].sockets[0].getsockname()[1] if entry and entry[1].sockets else None

    # -- timing ------------------------------------------------------------

    @property
    def timing(self) -> bool:
        return self._timing_task is not None

    async def _sync_timing(self) -> None:
        """Start or stop timing to match `[relay] timing`, for open links too."""
        if self.config.relay.timing and self._timing_task is None:
            now = time.monotonic()
            for links in self._links.values():
                for link in links:
                    link.timing = self._new_timing(now)
            self._timing_task = asyncio.create_task(self._timing_loop())
            self.logger.info("relay timing on: a report per link every %ss", self.config.relay.timing_report_seconds)
        elif not self.config.relay.timing and self._timing_task is not None:
            await self._stop_timing()
            self.logger.info("relay timing off")

    async def _stop_timing(self) -> None:
        task, self._timing_task = self._timing_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for links in self._links.values():
            for link in links:
                link.timing = None

    def _new_timing(self, now: float) -> _Timing:
        return _Timing(now, self.config.relay.timing_gap_ms / 1000)

    async def _timing_loop(self) -> None:
        """Measure how late the event loop wakes, and report each link.

        A late wake-up means this process stalled, and every relayed byte
        stalled with it; that is the one cause the per-link numbers can't show.
        """
        lag = 0.0
        last_report = time.monotonic()
        while True:
            before = time.monotonic()
            await asyncio.sleep(LAG_TICK)
            now = time.monotonic()
            lag = max(lag, now - before - LAG_TICK)
            if now - last_report < self.config.relay.timing_report_seconds:
                continue
            for radio_id, links in list(self._links.items()):
                for link in list(links):
                    if link.timing is not None and link.radio is not None:
                        self.logger.info("relay timing %s from %s %s", radio_id, link.addr, link.timing.report(now, lag))
            lag, last_report = 0.0, now

    # -- links -------------------------------------------------------------

    def cut(self, radio_id: str) -> int:
        """Close every relayed link to a radio. Synchronous, so a caller can cut
        without yielding between ending a session and severing it."""
        links = self._links.pop(radio_id, set())
        for link in links:
            link.abort()
        if links:
            self.logger.info("cut %d relayed link(s) to %s", len(links), radio_id)
        return len(links)

    def link_count(self, radio_id: str) -> int:
        return len(self._links.get(radio_id, ()))

    async def _handle(self, radio_id: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        addr = normalize_addr(peer[0] if peer else None)
        radio = self.config.find_radio(radio_id)
        assignment = self.db.live_assignment_for_radio(radio_id)

        refusal = None
        if radio is None:
            refusal = "radio no longer configured"
        elif assignment is None or assignment.state is not AssignmentState.ACTIVE:
            refusal = "no active session"
        elif self.config.relay.check_address and addr != normalize_addr(assignment.client_addr):
            refusal = f"not {assignment.callsign}'s address"
        if refusal is not None:
            self.logger.info("relay %s: refused %s (%s)", radio_id, addr, refusal)
            writer.transport.abort()
            return

        link = _Link(assignment.id, writer, addr)
        self._links[radio_id].add(link)
        try:
            await self._pump(radio, link, reader)
        finally:
            self._links.get(radio_id, set()).discard(link)
            link.abort()

    async def _pump(self, radio: Radio, link: _Link, member_reader: asyncio.StreamReader) -> None:
        try:
            radio_reader, link.radio = await asyncio.wait_for(
                asyncio.open_connection(radio.host, radio.remote_port),
                self.config.monitor.cat_timeout_seconds,
            )
        except (TimeoutError, OSError) as e:
            detail = f"{radio.id}: cannot reach {radio.host}:{radio.remote_port}: {e or type(e).__name__}"
            self.logger.warning("relay %s", detail)
            self.db.audit(self._callsign(link), "relay.unreachable", detail)
            return
        if link.cut:  # the session ended while we were connecting
            return

        callsign = self._callsign(link)
        self.db.audit(callsign, "relay.connect", f"{radio.id} from {link.addr}")
        started = time.monotonic()
        link.heard = {"member": started, "radio": started}
        if self.timing:  # timed from the onward connection, not the dial
            link.timing = self._new_timing(started)
        await asyncio.gather(
            self._copy(member_reader, link.radio, link, "member"),
            self._copy(radio_reader, link.member, link, "radio"),
            return_exceptions=True,
        )
        ended = time.monotonic()
        how = "session ended" if link.cut else (link.ended_by or "closed")
        # A K4 hangs up on a client silent for ~10 s, so "radio closed the link"
        # after a long member silence means the member's path died, not the radio.
        detail = (
            f"{radio.id} from {link.addr} after {_duration(ended - started)}: {how}; "
            f"member sent {_size(link.sent['member'])} (last {_duration(ended - link.heard['member'])} before), "
            f"radio sent {_size(link.sent['radio'])} (last {_duration(ended - link.heard['radio'])} before)"
        )
        self.logger.info("relay %s", detail)
        self.db.audit(callsign, "relay.disconnect", detail)

    def _callsign(self, link: _Link) -> str:
        assignment = self.db.get_assignment(link.assignment_id)
        return assignment.callsign if assignment else "?"

    @staticmethod
    async def _copy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, link: _Link, side: str) -> None:
        """Pump what `side` sends to the other side, recording who ends the link.

        A read error is `side` failing; a write error is the other side failing.
        """
        other = "radio" if side == "member" else "member"
        who, how = side, "closed the link"
        try:
            while True:
                try:
                    data = await reader.read(PUMP_CHUNK)
                except OSError as e:
                    how = f"failed ({type(e).__name__})"
                    break
                if not data:
                    break
                link.sent[side] += len(data)
                link.heard[side] = arrived = time.monotonic()
                timing = link.timing
                if timing is not None:
                    timing.flows[side].arrived(arrived, len(data))
                try:
                    writer.write(data)
                    backlog = writer.transport.get_write_buffer_size()
                    await writer.drain()
                    if timing is not None and link.timing is timing:
                        timing.flows[side].sent(time.monotonic() - arrived, backlog)
                except OSError as e:
                    who, how = other, f"failed ({type(e).__name__})"
                    break
        finally:
            # The first side to stop wins; the other copy only sees the abort below.
            if link.ended_by is None and not link.cut:
                link.ended_by = f"{who} {how}"
            # Either side going away ends the link in both directions.
            writer.transport.abort()

def _size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _ms(seconds: float) -> str:
    return f"{seconds * 1000:.0f} ms"


def _duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m{secs:02d}s" if minutes < 60 else f"{minutes // 60}h{minutes % 60:02d}m"
