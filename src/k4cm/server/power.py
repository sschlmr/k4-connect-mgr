"""Radio power control.

Power-off is native CAT (`PS0;`). Power-on cannot be done in software — a K4
accepts no commands while off and has no Wake-on-LAN — so it is delegated to
whatever hardware the site has: a shell command or an HTTP call against a smart
PDU or relay board, configured per radio.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import sys

import httpx

from .. import cat
from ..models import PowerMethod, Radio


class PowerError(Exception):
    """Raised when a power action cannot be carried out."""


async def power_off(radio: Radio, timeout: float = 5.0) -> str:
    try:
        await cat.power_off(radio.host, radio.cat_port, timeout)
    except cat.CatError as e:
        raise PowerError(f"could not send PS0; to {radio.name}: {e}") from e
    return f"{radio.name}: PS0; sent (radio powering down)"


async def power_on(radio: Radio, logger: logging.Logger | None = None) -> str:
    logger = logger or logging.getLogger(__name__)
    power = radio.power

    if power.method is PowerMethod.NONE:
        raise PowerError(
            f"{radio.name} has no remote power-on method configured. A K4 cannot be "
            "powered on over the network; configure a PDU/relay under [radios.power]."
        )

    if power.method is PowerMethod.COMMAND:
        if not power.command:
            raise PowerError(f"{radio.name}: power.method is 'command' but no command is set")
        return await _run_command(radio, power.command, logger)

    if not power.url:
        raise PowerError(f"{radio.name}: power.method is 'http' but no url is set")
    return await _call_http(radio, logger)


async def power_cycle(radio: Radio, timeout: float = 5.0, logger: logging.Logger | None = None) -> str:
    logger = logger or logging.getLogger(__name__)
    steps = []
    try:
        steps.append(await power_off(radio, timeout))
    except PowerError as e:
        # An unreachable/already-off radio is exactly the case a cycle is for.
        steps.append(f"{radio.name}: power-off skipped ({e})")
    await asyncio.sleep(radio.power.cycle_delay_s)
    steps.append(await power_on(radio, logger))
    return "; ".join(steps)


async def _run_command(radio: Radio, command: str, logger: logging.Logger) -> str:
    env = dict(os.environ)
    logger.info("power-on command for %s: %s", radio.id, command)
    args = command if sys.platform == "win32" else shlex.split(command)
    try:
        if isinstance(args, str):
            proc = await asyncio.create_subprocess_shell(
                args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env
            )
        stdout, _ = await asyncio.wait_for(proc.communicate(), 60)
    except (TimeoutError, OSError) as e:
        raise PowerError(f"{radio.name}: power-on command failed: {e}") from e
    output = stdout.decode(errors="replace").strip()
    if proc.returncode != 0:
        raise PowerError(f"{radio.name}: power-on command exited {proc.returncode}: {output}")
    return f"{radio.name}: power-on command ok{f' ({output})' if output else ''}"


async def _call_http(radio: Radio, logger: logging.Logger) -> str:
    power = radio.power
    auth = None
    if power.credentials_ref:
        raw = os.environ.get(power.credentials_ref)
        if not raw:
            raise PowerError(
                f"{radio.name}: credentials_ref '{power.credentials_ref}' is not set in the "
                "server environment"
            )
        username, _, password = raw.partition(":")
        auth = (username, password)
    logger.info("power-on HTTP %s %s for %s", power.http_method, power.url, radio.id)
    try:
        async with httpx.AsyncClient(timeout=20.0, auth=auth) as client:
            response = await client.request(power.http_method, power.url, content=power.body)
    except httpx.HTTPError as e:
        raise PowerError(f"{radio.name}: power-on request failed: {e}") from e
    if response.status_code >= 400:
        raise PowerError(f"{radio.name}: power-on request returned HTTP {response.status_code}")
    return f"{radio.name}: power-on request ok (HTTP {response.status_code})"
