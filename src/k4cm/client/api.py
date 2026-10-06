"""HTTPS calls to the central server, authenticated with the user's API key.

Trust is anchored on the pinned certificate saved by `k4cm config`, not the
public CA set.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx

from .config import ClientConfig, check_transport


class ApiError(Exception):
    """A request was refused by the server, or the server could not be reached."""

    def __init__(self, message: str, status_code: int | None = None, payload: dict | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.payload = payload or {}


class ServerApi:
    def __init__(self, config: ClientConfig, timeout: float = 15.0):
        self.config = config
        self.timeout = timeout
        check_transport(config.server_url)

    # -- plumbing ----------------------------------------------------------

    def _verify(self):
        parsed = urlparse(self.config.server_url)
        if parsed.scheme != "https":
            return False  # localhost development only; check_transport enforced this
        cert = self.config.cert_path
        if cert.exists():
            return str(cert)
        raise ApiError(
            f"no pinned server certificate at {cert}. Run 'k4cm config' to fetch and "
            "confirm the server's certificate fingerprint."
        )

    def _request(self, method: str, path: str, **kwargs) -> Any:
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        try:
            with httpx.Client(
                base_url=self.config.server_url.rstrip("/"),
                verify=self._verify(),
                timeout=self.timeout,
            ) as client:
                response = client.request(method, path, headers=headers, **kwargs)
        except httpx.HTTPError as e:
            # httpx wraps certificate failures inside ConnectError, so check the
            # cause chain before falling back to a plain connectivity message.
            if _is_certificate_error(e):
                raise ApiError(
                    "the server's certificate does not match the one pinned on this machine. "
                    "If the administrator rotated it, re-run 'k4cm config'; otherwise stop "
                    f"and investigate. ({e})"
                ) from e
            if isinstance(e, httpx.ConnectError):
                raise ApiError(f"cannot reach the server at {self.config.server_url}: {e}") from e
            raise ApiError(f"request to the server failed: {e}") from e

        if response.status_code >= 400:
            payload = {}
            try:
                payload = response.json()
            except ValueError:
                pass
            detail = payload.get("detail") or response.text.strip() or f"HTTP {response.status_code}"
            if isinstance(detail, list):  # FastAPI validation errors
                detail = "; ".join(str(d.get("msg", d)) for d in detail)
            raise ApiError(str(detail), response.status_code, payload)

        if not response.content:
            return None
        return response.json()

    # -- member calls ------------------------------------------------------

    def health(self) -> dict:
        return self._request("GET", "/health")

    def request_assignment(self, radio: str) -> dict:
        return self._request("POST", "/assignments", json={"radio": radio})

    def session_state(self) -> dict:
        return self._request("GET", "/assignments/current")

    def ack(self) -> dict:
        return self._request("POST", "/assignments/current/ack")

    def extend(self) -> dict:
        return self._request("POST", "/assignments/current/extend")

    def release(self) -> dict:
        return self._request("DELETE", "/assignments/current")

    def status(self) -> dict:
        return self._request("GET", "/status")

    def join_queue(self, radio: str) -> dict:
        return self._request("POST", "/queue", json={"radio": radio})

    def leave_queue(self) -> dict:
        return self._request("DELETE", "/queue")

    def accept_offer(self) -> dict:
        return self._request("POST", "/queue/accept")

    def decline_offer(self) -> dict:
        return self._request("POST", "/queue/decline")

    # -- admin calls -------------------------------------------------------

    def list_users(self) -> list:
        return self._request("GET", "/users")

    def create_user(self, callsign: str, is_admin: bool = False) -> dict:
        return self._request("POST", "/users", json={"callsign": callsign, "is_admin": is_admin})

    def delete_user(self, callsign: str) -> dict:
        return self._request("DELETE", f"/users/{callsign}")

    def reset_user_key(self, callsign: str) -> dict:
        return self._request("POST", f"/users/{callsign}/reset-key")

    def list_radios(self) -> list:
        return self._request("GET", "/radios")

    def reload_radios(self) -> dict:
        return self._request("POST", "/radios/reload")

    def radio_power(self, radio_id: str, action: str) -> dict:
        return self._request("POST", f"/radios/{radio_id}/power", json={"action": action})

    def list_assignments(self, limit: int = 100) -> list:
        return self._request("GET", "/assignments", params={"limit": limit})

    def force_release(self, assignment_id: int) -> dict:
        return self._request("DELETE", f"/assignments/{assignment_id}")

    def audit(self, hours: float | None = None, actor: str | None = None, limit: int = 200) -> list:
        params: dict[str, Any] = {"limit": limit}
        if hours:
            params["hours"] = hours
        if actor:
            params["actor"] = actor
        return self._request("GET", "/audit", params=params)


def _is_certificate_error(exc: BaseException) -> bool:
    """True when an ssl.SSLError appears anywhere in the exception chain."""
    import ssl

    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, ssl.SSLError):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False
