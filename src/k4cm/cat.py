"""CAT-over-TCP client for Elecraft K4 / K4-0 radios.

K4 CAT commands are ASCII strings terminated by ';'. GET commands echo a
response with the same prefix (e.g. "CC;" -> "CC1;"). Some commands answer
with an error form such as "RRT!;".
"""

from __future__ import annotations

import asyncio
from typing import Self

DEFAULT_CAT_PORT = 9200
DEFAULT_TIMEOUT = 5.0


class CatError(Exception):
    """Raised when a radio is unreachable or a command fails."""


class CatClient:
    """A single TCP connection to a radio's CAT interface."""

    def __init__(self, host: str, port: int = DEFAULT_CAT_PORT, timeout: float = DEFAULT_TIMEOUT):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._buffer = ""

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def connect(self) -> None:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), self.timeout
            )
        except (TimeoutError, OSError) as e:
            raise CatError(f"cannot connect to radio at {self.host}:{self.port}: {e}") from e

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except OSError:
                pass
            self._writer = None
            self._reader = None

    async def send(self, command: str) -> None:
        """Send a command without waiting for any response."""
        if self._writer is None:
            raise CatError("not connected")
        if not command.endswith(";"):
            command += ";"
        self._writer.write(command.encode("ascii"))
        try:
            await asyncio.wait_for(self._writer.drain(), self.timeout)
        except (TimeoutError, OSError) as e:
            raise CatError(f"write to {self.host}:{self.port} failed: {e}") from e

    async def read_response(self, timeout: float | None = None) -> str:
        """Read the next ';'-terminated response (without the ';')."""
        if self._reader is None:
            raise CatError("not connected")
        while ";" not in self._buffer:
            try:
                chunk = await asyncio.wait_for(self._reader.read(1024), timeout or self.timeout)
            except TimeoutError as e:
                raise CatError(f"timed out waiting for response from {self.host}:{self.port}") from e
            except OSError as e:
                raise CatError(f"read from {self.host}:{self.port} failed: {e}") from e
            if not chunk:
                raise CatError(f"connection to {self.host}:{self.port} closed by radio")
            self._buffer += chunk.decode("ascii", errors="replace")
        response, _, self._buffer = self._buffer.partition(";")
        return response.strip()

    async def request(
        self,
        command: str,
        prefix: str | None = None,
        timeout: float | None = None,
        error_prefix: str | None = None,
    ) -> str:
        """Send a GET command and return the matching response.

        Responses with other prefixes (async status broadcasts) are skipped,
        except `error_prefix` — used to catch an asynchronous failure notice such
        as `RRT!;` that may arrive while we are polling for something else.
        """
        want = prefix if prefix is not None else command.rstrip(";")
        await self.send(command)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (timeout or self.timeout)
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise CatError(f"no {want} response from {self.host}:{self.port}")
            response = await self.read_response(timeout=remaining)
            if error_prefix and response.startswith(error_prefix):
                raise CatError(f"radio at {self.host}:{self.port} reported {response};")
            if response.startswith(want):
                return response

    async def ping(self) -> bool:
        try:
            response = await self.request("PING;", prefix="PONG")
            return response.startswith("PONG")
        except CatError:
            return False


async def send_commands(host: str, port: int, commands: list[str], timeout: float = DEFAULT_TIMEOUT) -> None:
    """Open a connection, send a series of commands, and verify the radio is
    still responsive with a PING afterwards."""
    async with CatClient(host, port, timeout) as cat:
        for command in commands:
            await cat.send(command)
        if not await cat.ping():
            raise CatError(f"radio at {host}:{port} stopped responding after commands")


async def query(host: str, port: int, command: str, timeout: float = DEFAULT_TIMEOUT) -> str:
    """One-shot GET against a radio."""
    async with CatClient(host, port, timeout) as cat:
        return await cat.request(command)


async def reachable(host: str, port: int, timeout: float = DEFAULT_TIMEOUT) -> bool:
    """True when the radio answers a PING."""
    try:
        async with CatClient(host, port, timeout) as cat:
            return await cat.ping()
    except CatError:
        return False


def parse_client_count(response: str) -> int | None:
    """Decode a CC response. Per the K4 Programmer's Reference: n >= 1 when the
    radio is a server with n clients attached, -1 when it is itself a client
    connected to a server, 0 otherwise. None if the reply is not a number."""
    try:
        return int(response[2:].strip())
    except ValueError:
        return None


async def client_count(host: str, port: int, timeout: float = DEFAULT_TIMEOUT) -> int:
    """The radio's CC; value — see parse_client_count. Unparseable replies read as 0."""
    response = await query(host, port, "CC;", timeout)
    count = parse_client_count(response)
    return 0 if count is None else count


async def power_off(host: str, port: int, timeout: float = DEFAULT_TIMEOUT) -> None:
    """Clean remote power-down (PS0;). The radio stops answering afterwards, so
    unlike send_commands this does not PING to confirm."""
    async with CatClient(host, port, timeout) as cat:
        await cat.send("PS0;")
