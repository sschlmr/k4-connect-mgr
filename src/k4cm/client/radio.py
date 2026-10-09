"""Driving the user's own K4 or K4/0 over its CAT port.

The panel is told to connect to the assigned central radio with
`RRT<host>:<port>?<password>;` and to disconnect with `RRN;`.

The panel cannot say whether it actually connected. A real K4/0 (tested
2026-10-09) answers `CC-1` ("client") the moment it is told to connect, even
to a port where nothing listens, and keeps saying so for about 10 s before
falling back to `CC0`. A wrong password draws no `RRT!;` either, despite the
Programmer's Reference. So success is confirmed from the other end: the caller
supplies `attached`, normally a server call that reads the *central* radio's
client count, and a link it has not confirmed by the deadline is a failure.
`RRT!;` is still honoured as an early refusal if a radio does send it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from .. import cat
from .config import ClientConfig

CONNECT_TIMEOUT = 12.0
CONFIRM_INTERVAL = 0.5


class RadioError(Exception):
    """The local panel could not be reached or refused the instruction."""


def _require_local_radio(config: ClientConfig) -> tuple[str, int]:
    if not config.has_local_radio:
        raise RadioError(
            "no local radio configured. Run 'k4cm config' and give the CAT address of "
            "your K4 or K4/0."
        )
    return config.radio_host, config.radio_cat_port


async def _connect(
    host: str,
    port: int,
    target_host: str,
    target_port: int,
    password: str,
    attached: Callable[[], bool],
) -> str:
    """Tell the panel to connect, then wait for `attached()` to confirm it.

    `attached` is synchronous (it is usually an HTTPS call) and is run in a
    worker thread. The panel is polled meanwhile only to catch an `RRT!;`.
    """
    causes = (
        "The password may have been superseded, the central radio may not be accepting "
        "connections, or it may be unreachable from your network."
    )
    try:
        async with cat.CatClient(host, port) as panel:
            await panel.send(f"RRT{target_host}:{target_port}?{password};")

            loop = asyncio.get_running_loop()
            deadline = loop.time() + CONNECT_TIMEOUT
            while loop.time() < deadline:
                try:
                    await panel.request("CC;", timeout=CONFIRM_INTERVAL, error_prefix="RRT!")
                except cat.CatError as e:
                    if "RRT!" in str(e):
                        raise RadioError(
                            f"your radio refused the connection to {target_host}:{target_port} "
                            f"(RRT!). {causes}"
                        ) from e
                if await asyncio.to_thread(attached):
                    return f"connected to {target_host}:{target_port}"
                await asyncio.sleep(CONFIRM_INTERVAL)

        raise RadioError(
            f"your radio did not connect to {target_host}:{target_port} within "
            f"{CONNECT_TIMEOUT:.0f} seconds. {causes}"
        )
    except cat.CatError as e:
        raise RadioError(f"cannot talk to your local radio at {host}:{port}: {e}") from e


def connect(
    config: ClientConfig,
    target_host: str,
    target_port: int,
    password: str,
    attached: Callable[[], bool],
) -> str:
    host, port = _require_local_radio(config)
    return asyncio.run(_connect(host, port, target_host, target_port, password, attached))


async def _disconnect(host: str, port: int) -> str:
    try:
        async with cat.CatClient(host, port) as panel:
            await panel.send("RRN;")
            if not await panel.ping():
                raise cat.CatError("radio stopped responding after RRN;")
    except cat.CatError as e:
        raise RadioError(f"cannot talk to your local radio at {host}:{port}: {e}") from e
    return "local radio disconnected"


def disconnect(config: ClientConfig) -> str:
    host, port = _require_local_radio(config)
    return asyncio.run(_disconnect(host, port))


async def _local_state(host: str, port: int) -> str:
    try:
        count = await cat.client_count(host, port, timeout=3.0)
    except cat.CatError as e:
        return f"not reachable ({e})"
    if count == -1:
        # Also what a K4/0 reports for ~10 s while a connect is failing.
        return "connected (or connecting) to a remote K4"
    if count >= 1:
        return f"acting as a server with {count} remote client(s) attached"
    return "idle (not connected)"


def local_state(config: ClientConfig) -> str:
    if not config.has_local_radio:
        return "no local radio configured"
    return asyncio.run(_local_state(config.radio_host, config.radio_cat_port))
