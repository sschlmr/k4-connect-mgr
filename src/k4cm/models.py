"""Shared data models for the K4 Connect Manager.

Radios are described by the server's config file, so `Radio` and `PowerConfig`
are built from TOML rather than loaded from the database. Sessions (who has
which radio) live in SQLite and are represented by `Assignment`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

DEFAULT_CAT_PORT = 9200
DEFAULT_REMOTE_PORT = 9204


def utcnow() -> datetime:
    return datetime.now(UTC)


def isoformat(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class AssignmentState(StrEnum):
    """Lifecycle of a session. Radios themselves carry no stored state.

    A request first *claims* the radio as PENDING — an atomic insert that the
    database refuses if the radio (or the caller) already has a live session —
    and only becomes ACTIVE once the radio has accepted the new password.
    """

    PENDING = "pending"        # claimed; password being set on the radio
    ACTIVE = "active"
    RELEASED = "released"      # user released it
    RECLAIMED = "reclaimed"    # client stayed disconnected past the grace period
    EXPIRED = "expired"        # hit its deadline
    FORCED = "forced"          # admin force-released
    FAILED = "failed"          # claim abandoned: the radio never took the password


# States that occupy a radio. At most one row per radio, and one per callsign,
# may be in these states — enforced by partial unique indexes in the database.
LIVE_STATES = (AssignmentState.PENDING, AssignmentState.ACTIVE)


class PowerMethod(StrEnum):
    NONE = "none"
    COMMAND = "command"
    HTTP = "http"


@dataclass
class PowerConfig:
    """How a radio can be powered on.

    Powering *off* is always CAT (`PS0;`). Powering *on* needs hardware — the K4
    accepts no commands while off and has no Wake-on-LAN — so it is delegated to
    a shell command or an HTTP call against a smart PDU / relay board.
    """

    method: PowerMethod = PowerMethod.NONE
    command: str | None = None            # method=command
    url: str | None = None                # method=http
    http_method: str = "GET"
    body: str | None = None
    address: str | None = None            # PDU address, informational
    outlet: str | None = None             # PDU outlet/port, informational
    credentials_ref: str | None = None    # env var name holding PDU credentials
    cycle_delay_s: int = 10               # wait between power off and power on

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PowerConfig:
        method = PowerMethod(str(data.get("method", "none")).lower())
        return cls(
            method=method,
            command=data.get("command"),
            url=data.get("url"),
            http_method=str(data.get("http_method", "GET")).upper(),
            body=data.get("body"),
            address=data.get("address"),
            outlet=data.get("outlet"),
            credentials_ref=data.get("credentials_ref"),
            cycle_delay_s=int(data.get("cycle_delay_s", 10)),
        )


@dataclass
class Radio:
    """One central-site K4, as described in the server config file.

    A radio has two addresses. `host`/`cat_port` is how the *server* reaches it
    on the site LAN — never handed to members, since CAT has no authentication.
    `public_host`/`public_port` is where a member's panel connects from home
    (usually the site's public address and a forwarded port); `remote_port` is
    the radio's own remote-link port on the LAN, and the default public port.
    """

    id: str
    name: str
    host: str
    public_host: str
    cat_port: int = DEFAULT_CAT_PORT
    remote_port: int = DEFAULT_REMOTE_PORT
    public_port: int | None = None
    enabled: bool = True
    description: str = ""
    url: str = ""                          # optional web/reference URL for the radio
    capabilities: list[str] = field(default_factory=list)
    power: PowerConfig = field(default_factory=PowerConfig)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Radio:
        missing = [k for k in ("id", "host", "public_host") if not data.get(k)]
        if missing:
            where = f"radio {data['id']}" if data.get("id") else "radio entry"
            hint = (
                " (public_host is the address members' panels connect to — the site's "
                "public name or IP, or the same as host if members reach the site over a VPN)"
                if "public_host" in missing
                else ""
            )
            raise ValueError(f"{where} is missing required field(s): {', '.join(missing)}{hint}")
        radio_id = str(data["id"])
        remote_port = int(data.get("remote_port", DEFAULT_REMOTE_PORT))
        return cls(
            id=radio_id,
            name=str(data.get("name", radio_id)),
            host=str(data["host"]),
            public_host=str(data["public_host"]),
            cat_port=int(data.get("cat_port", DEFAULT_CAT_PORT)),
            remote_port=remote_port,
            public_port=int(data.get("public_port", remote_port)),
            enabled=bool(data.get("enabled", True)),
            description=str(data.get("description", "")),
            url=str(data.get("url", "")),
            capabilities=[str(c) for c in data.get("capabilities", [])],
            power=PowerConfig.from_dict(data.get("power", {})),
        )

    @property
    def connect_port(self) -> int:
        return self.public_port if self.public_port is not None else self.remote_port

    def public(self) -> dict[str, Any]:
        """Radio details safe to hand to any authenticated member. Deliberately
        omits the LAN CAT address."""
        return {
            "id": self.id,
            "name": self.name,
            "connect_host": self.public_host,
            "connect_port": self.connect_port,
            "enabled": self.enabled,
            "description": self.description,
            "url": self.url,
            "capabilities": list(self.capabilities),
        }


@dataclass
class User:
    callsign: str
    api_key_hash: str
    is_admin: bool = False
    enabled: bool = True


@dataclass
class Assignment:
    id: int
    radio_id: str
    callsign: str
    password: str
    started_at: datetime
    expires_at: datetime
    state: AssignmentState
    acked: bool = False
    ended_at: datetime | None = None
    end_reason: str = ""

    def is_expiring(self, warn_period: timedelta, now: datetime | None = None) -> bool:
        now = now or utcnow()
        return now >= self.expires_at - warn_period

    def seconds_remaining(self, now: datetime | None = None) -> int:
        now = now or utcnow()
        return int((self.expires_at - now).total_seconds())


@dataclass
class QueueEntry:
    """A user waiting for one specific radio.

    `offered_at` is set when the radio has come free and is being held for this
    user; the hold lapses after the configured window and passes to the next.
    """

    id: int
    radio_id: str
    callsign: str
    requested_at: datetime
    offered_at: datetime | None = None


@dataclass
class AuditEntry:
    id: int
    timestamp: datetime
    actor: str
    action: str
    detail: str
