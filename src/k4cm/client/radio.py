"""Driving the user's own K4 or K4/0 over its CAT port.

The panel is told to connect to the assigned central radio with
`RRT<host>:<port>?<password>;` and to disconnect with `RRN;`. A failed connect
answers `RRT!;`; success is silent, so it is confirmed with `CC;`, which reads
-1 on a radio that is connected *as a client* (1+ would mean clients attached
*to* it).
"""

from __future__ import annotations

import asyncio

from .. import cat
from .config import ClientConfig

CONNECT_TIMEOUT = 12.0
CONFIRM_INTERVAL = 0.4


class RadioError(Exception):
    """The local panel could not be reached or refused the instruction."""


def _require_local_radio(config: ClientConfig) -> tuple[str, int]:
    if not config.has_local_radio:
        raise RadioError(
            "no local radio configured. Run 'k4cm config' and give the CAT address of "
            "your K4 or K4/0."
        )
    return config.radio_host, config.radio_cat_port


async def _connect(host: str, port: int, target_host: str, target_port: int, password: str) -> str:
    """Tell the panel to connect, then poll CC; until the link shows up.

    Success is silent on the K4, so we confirm rather than wait out a timeout;
    a failure arrives asynchronously as RRT!; and is caught while polling.
    """
    refused = (
        f"your radio refused the connection to {target_host}:{target_port} (RRT!). The "
        "password may have been superseded, or the central radio may not be accepting "
        "connections."
    )
    try:
        async with cat.CatClient(host, port) as panel:
            await panel.send(f"RRT{target_host}:{target_port}?{password};")

            loop = asyncio.get_running_loop()
            deadline = loop.time() + CONNECT_TIMEOUT
            while loop.time() < deadline:
                try:
                    response = await panel.request("CC;", timeout=CONFIRM_INTERVAL, error_prefix="RRT!")
                except cat.CatError as e:
                    if "RRT!" in str(e):
                        raise RadioError(refused) from e
                    await asyncio.sleep(CONFIRM_INTERVAL)
                    continue
                if cat.parse_client_count(response) == -1:
                    return f"connected to {target_host}:{target_port}"
                await asyncio.sleep(CONFIRM_INTERVAL)

            return (
                "connect command accepted by your radio, but it has not confirmed the link "
                f"to {target_host}:{target_port} yet — check the panel"
            )
    except cat.CatError as e:
        raise RadioError(f"cannot talk to your local radio at {host}:{port}: {e}") from e


def connect(config: ClientConfig, target_host: str, target_port: int, password: str) -> str:
    host, port = _require_local_radio(config)
    return asyncio.run(_connect(host, port, target_host, target_port, password))


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
        return "connected to a remote K4"
    if count >= 1:
        return f"acting as a server with {count} remote client(s) attached"
    return "idle (not connected)"


def local_state(config: ClientConfig) -> str:
    if not config.has_local_radio:
        return "no local radio configured"
    return asyncio.run(_local_state(config.radio_host, config.radio_cat_port))
