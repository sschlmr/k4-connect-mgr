"""`k4cm calendar`: the local page server and its pass-through to the k4cm server."""

from __future__ import annotations

import asyncio
import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta

import pytest_asyncio
from test_e2e import live  # noqa: F401  (fixture)

from k4cm.client.api import ServerApi
from k4cm.client.calendar import TOKEN_HEADER, CalendarServer
from k4cm.client.config import load_config


@pytest_asyncio.fixture
async def page(live):  # noqa: F811
    config = load_config()
    server = CalendarServer(ServerApi(config), config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def call(server, method, path, body=None, token=True, host=None):
    request = urllib.request.Request(
        server.url.rstrip("/") + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    if token:
        request.add_header(TOKEN_HEADER, server.token)
    if host:
        request.add_header("Host", host)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


async def acall(*args, **kwargs):
    return await asyncio.to_thread(call, *args, **kwargs)


async def test_page_is_served_with_its_token(page):
    status, body = await acall(page, "GET", "/", token=False)
    assert status == 200
    assert page.token.encode() in body and b"K4 Calendar" in body


async def test_api_needs_the_page_token(page):
    status, _ = await acall(page, "GET", "/api/context", token=False)
    assert status == 403


async def test_other_hosts_are_refused(page):
    status, _ = await acall(page, "GET", "/api/context", host="evil.example:80")
    assert status == 403


async def test_context_and_booking_round_trip(page):
    status, body = await acall(page, "GET", "/api/context")
    context = json.loads(body)
    assert status == 200 and context["me"] == {"callsign": "W1AW", "is_admin": False}
    assert {r["id"] for r in context["radios"]} >= {"k4a", "k4b"}

    start = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d 18:00")  # noqa: DTZ005 - local wall time
    booking = {"radio": "k4b", "start": start, "duration": "90m", "tz": "UTC",
               "repeat": {"freq": "weekly", "on": [], "count": 2}}
    status, body = await acall(page, "POST", "/api/bookings?dry_run=true", booking)
    assert status == 200 and json.loads(body)["total"] == 2
    status, body = await acall(page, "POST", "/api/bookings", booking)
    created = json.loads(body)["booking"]
    assert created["tz"] == "UTC"

    status, body = await acall(page, "POST", "/api/bookings", booking | {"repeat": None})
    assert status == 409 and "already booked" in json.loads(body)["detail"]

    status, body = await acall(page, "DELETE", f"/api/bookings/{created['id']}?scope=all")
    assert status == 200
