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

    async def stop(self) -> None:
        for radio_id, (_, server) in list(self._listeners.items()):
            await self._stop_listener(radio_id, server)

    async def _stop_listener(self, radio_id: str, server: asyncio.Server) -> None:
        self._listeners.pop(radio_id, None)
        server.close()
        self.cut(radio_id)
        with contextlib.suppress(TimeoutError, OSError):
            await asyncio.wait_for(server.wait_closed(), timeout=2)

    def listening_port(self, radio_id: str) -> int | None:
        entry = self._listeners.get(radio_id)
        return entry[1].sockets[0].getsockname()[1] if entry and entry[1].sockets else None

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
                link.heard[side] = time.monotonic()
                try:
                    writer.write(data)
                    await writer.drain()
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


def _duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m{secs:02d}s" if minutes < 60 else f"{minutes // 60}h{minutes % 60:02d}m"
