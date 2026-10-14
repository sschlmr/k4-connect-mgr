"""`k4cm calendar` — the booking calendar in the member's own browser.

The page is served from this machine (127.0.0.1) and talks to the k4cm server
*through* this process, which adds the member's API key and trusts only the
pinned certificate. The browser never sees the key, and the server needs no
login page or browser sessions.

A local web server is reachable by any web page the member has open, so two
checks keep other sites out: every API call must carry a random token that only
the page we served knows (a cross-site page cannot read it), and the Host header
must be our own address, which defeats DNS-rebinding tricks.

The page itself lives in calendar_page.py as a string, so the PyInstaller
builds need no extra data files.
"""

from __future__ import annotations

import hmac
import json
import secrets
import socketserver
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .api import ApiError, ServerApi
from .calendar_page import PAGE
from .config import ClientConfig

TOKEN_HEADER = "X-K4CM-Token"


class CalendarServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, api: ServerApi, config: ClientConfig, port: int = 0):
        super().__init__(("127.0.0.1", port), _Handler)
        self.api = api
        self.config = config
        self.token = secrets.token_urlsafe(24)
        self.lock = threading.Lock()  # ServerApi opens a client per call; keep calls orderly

    def server_bind(self) -> None:
        # HTTPServer.server_bind does a reverse-DNS lookup (getfqdn) that can
        # stall for half a minute on some machines; we only ever serve 127.0.0.1.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/"

    def page(self) -> bytes:
        return PAGE.replace("__TOKEN__", self.token).encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    server: CalendarServer

    def log_message(self, format: str, *args: Any) -> None:
        pass  # the terminal is the member's; keep it quiet

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    # -- plumbing ----------------------------------------------------------

    def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"))

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        port = self.server.server_address[1]
        return host in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _dispatch(self, method: str) -> None:
        if not self._host_ok():
            self._json(403, {"detail": "wrong host"})
            return
        parsed = urlparse(self.path)
        if method == "GET" and parsed.path in ("/", "/index.html"):
            self._send(200, self.server.page(), "text/html; charset=utf-8")
            return
        if not parsed.path.startswith("/api/"):
            self._json(404, {"detail": "not found"})
            return
        token = self.headers.get(TOKEN_HEADER) or ""
        if not hmac.compare_digest(token, self.server.token):
            self._json(403, {"detail": "missing or wrong page token"})
            return
        query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
        try:
            body = self._body() if method in ("POST", "PATCH") else {}
            with self.server.lock:
                result = self._route(method, parsed.path[len("/api") :], query, body)
        except ApiError as e:
            self._json(e.status_code or 502, {"detail": e.message, **e.payload})
            return
        except (ValueError, KeyError) as e:
            self._json(400, {"detail": f"bad request: {e}"})
            return
        self._json(200, result)

    def _route(self, method: str, path: str, query: dict[str, str], body: dict[str, Any]) -> Any:
        api, cfg = self.server.api, self.server.config
        dry_run = query.get("dry_run") == "true"
        parts = [p for p in path.split("/") if p]
        if method == "GET" and parts == ["context"]:
            status = api.status()
            return {
                "me": api.me(),
                "tz": cfg.local_timezone,
                "radios": [{"id": r["id"], "name": r["name"], "enabled": r["enabled"]} for r in status["radios"]],
            }
        if method == "GET" and parts == ["status"]:
            return api.status()
        if method == "GET" and parts == ["notices"]:
            return {"notices": api.notices()}
        if parts[:1] == ["bookings"]:
            if len(parts) == 1 and method == "GET":
                return api.bookings(query.get("start"), query.get("end"), query.get("radio"), query.get("owner"))
            if len(parts) == 1 and method == "POST":
                return api.create_booking(body, dry_run=dry_run)
            if len(parts) == 2:
                booking_id = int(parts[1])
                if method == "GET":
                    return api.get_booking(booking_id)
                if method == "PATCH":
                    return api.edit_booking(booking_id, body, dry_run=dry_run)
                if method == "DELETE":
                    return api.delete_booking(booking_id, query.get("scope", "all"), query.get("date"))
        raise KeyError(f"{method} {path}")


def run_calendar(api: ServerApi, config: ClientConfig, port: int = 0, open_browser: bool = True) -> None:
    server = CalendarServer(api, config, port)
    print(f"Calendar at {server.url} — Ctrl-C to stop.")
    if open_browser:
        webbrowser.open(server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nCalendar stopped.")
    finally:
        server.server_close()
