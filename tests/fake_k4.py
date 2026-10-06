"""A fake Elecraft K4 for tests and local end-to-end runs.

Simulates the CAT commands the connection manager uses:

  PING;                 -> PONG;
  RRP<text>;            set remote-access password (GET: RRP; -> RRP<text>;)
  RRC<n>;               set allowed remote connections; RRC0; drops clients
  RRT<host>:<port>?<pw>;  connect as a client to another (fake) K4's remote
                          port; replies RRT!; on failure, nothing on success
  RRN;                  drop the outgoing remote connection
  CC;                   -> CC<n>; n = attached remote clients (server role),
                           -1 when connected as a client, else 0

The "remote link" between two fakes is a plain TCP connection to the server
fake's remote port carrying the password on the first line; the server
answers OK or NO. This stands in for Elecraft's real remote protocol so the
whole assignment workflow can run without hardware.

Run standalone:  python tests/fake_k4.py --name K4A --cat-port 9200 --remote-port 9204
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib


class FakeK4:
    def __init__(self, name: str = "K4", cat_port: int = 0, remote_port: int = 0):
        self.name = name
        self.cat_port = cat_port
        self.remote_port = remote_port
        self.password = ""
        self.rrc = 0
        self.powered_on = True               # PS0; turns this off, like a real K4
        self.commands: list[str] = []        # every CAT command received, for assertions
        self._remote_clients: dict[asyncio.StreamWriter, str] = {}
        self._outgoing: asyncio.StreamWriter | None = None
        self._cat_server: asyncio.Server | None = None
        self._remote_server: asyncio.Server | None = None

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        self._cat_server = await asyncio.start_server(self._handle_cat, "127.0.0.1", self.cat_port)
        self.cat_port = self._cat_server.sockets[0].getsockname()[1]
        self._remote_server = await asyncio.start_server(self._handle_remote, "127.0.0.1", self.remote_port)
        self.remote_port = self._remote_server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        # Drop live connections before closing the listeners: wait_closed() waits
        # for in-flight handlers, which block on read until their peer goes away.
        await self._drop_outgoing()
        for writer in list(self._remote_clients):
            writer.close()
        self._remote_clients.clear()
        for server in (self._cat_server, self._remote_server):
            if server is not None:
                server.close()
                with contextlib.suppress(asyncio.TimeoutError, OSError):
                    await asyncio.wait_for(server.wait_closed(), timeout=2)
        self._cat_server = None
        self._remote_server = None

    # -- state helpers -----------------------------------------------------

    @property
    def client_count(self) -> int:
        return len(self._remote_clients)

    @property
    def connected_to_server(self) -> bool:
        return self._outgoing is not None

    # -- CAT interface -----------------------------------------------------

    async def _handle_cat(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        buffer = ""
        if not self.powered_on:
            # a powered-down K4 accepts no CAT traffic at all
            writer.close()
            return
        try:
            while True:
                chunk = await reader.read(1024)
                if not chunk:
                    break
                buffer += chunk.decode("ascii", errors="replace")
                while ";" in buffer:
                    command, _, buffer = buffer.partition(";")
                    command = command.strip()
                    if command:
                        self.commands.append(command)
                        reply = await self._execute(command)
                        if reply:
                            writer.write(reply.encode("ascii"))
                            await writer.drain()
        except OSError:
            pass
        finally:
            writer.close()

    async def _execute(self, command: str) -> str:
        if command == "PING":
            return "PONG;"
        if command == "PS":
            return "PS1;"
        if command == "PS0":
            self.powered_on = False
            for writer in list(self._remote_clients):
                writer.close()
            self._remote_clients.clear()
            await self._drop_outgoing()
            return ""
        if command == "CC":
            if self._remote_clients:
                return f"CC{len(self._remote_clients)};"
            return "CC-1;" if self._outgoing else "CC0;"
        if command == "RRP":
            return f"RRP{self.password};"
        if command.startswith("RRP"):
            self.password = command[3:]
            return ""
        if command == "RRC":
            return f"RRC{self.rrc};"
        if command.startswith("RRC"):
            self.rrc = int(command[3:])
            if self.rrc == 0:
                # dropping allowed connections kicks attached clients
                for writer in list(self._remote_clients):
                    writer.close()
                self._remote_clients.clear()
            return ""
        if command == "RRN":
            await self._drop_outgoing()
            return ""
        if command.startswith("RRT"):
            return await self._remote_connect(command[3:])
        return ""  # unknown commands are ignored, like a lenient radio

    # -- outgoing remote link (client role) --------------------------------

    async def _remote_connect(self, spec: str) -> str:
        try:
            target, _, password = spec.partition("?")
            host, _, port = target.partition(":")
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, int(port)), 5)
            writer.write((password + "\n").encode("ascii"))
            await writer.drain()
            verdict = await asyncio.wait_for(reader.readline(), 5)
            if verdict.strip() != b"OK":
                writer.close()
                return "RRT!;"
            await self._drop_outgoing()
            self._outgoing = writer
            asyncio.ensure_future(self._watch_outgoing(reader))
            return ""
        except (TimeoutError, OSError, ValueError):
            return "RRT!;"

    async def _watch_outgoing(self, reader: asyncio.StreamReader) -> None:
        try:
            while await reader.read(1024):
                pass
        except OSError:
            pass
        self._outgoing = None

    async def _drop_outgoing(self) -> None:
        if self._outgoing is not None:
            self._outgoing.close()
            self._outgoing = None

    # -- incoming remote link (server role) --------------------------------

    async def _handle_remote(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            offered = (await asyncio.wait_for(reader.readline(), 10)).decode("ascii").strip()
        except (TimeoutError, OSError):
            writer.close()
            return
        accept = bool(self.password) and offered == self.password and len(self._remote_clients) < self.rrc
        writer.write(b"OK\n" if accept else b"NO\n")
        try:
            await writer.drain()
        except OSError:
            accept = False
        if not accept:
            writer.close()
            return
        self._remote_clients[writer] = offered
        try:
            while await reader.read(1024):
                pass
        except OSError:
            pass
        self._remote_clients.pop(writer, None)
        writer.close()


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Run a fake K4 radio")
    parser.add_argument("--name", default="K4")
    parser.add_argument("--cat-port", type=int, default=9200)
    parser.add_argument("--remote-port", type=int, default=9204)
    args = parser.parse_args()

    fake = FakeK4(args.name, args.cat_port, args.remote_port)
    await fake.start()
    print(f"{fake.name}: CAT on 127.0.0.1:{fake.cat_port}, remote link on 127.0.0.1:{fake.remote_port}")
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        await fake.stop()


if __name__ == "__main__":
    asyncio.run(_main())
