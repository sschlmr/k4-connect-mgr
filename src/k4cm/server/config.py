"""Server configuration: policy settings plus the site's radio inventory.

Radios live here rather than in the database — admins add or change a K4 by
editing this file and running `k4cm admin radios reload`.

Deliberately absent: any random seed. Passwords and API keys come from the
platform entropy source via `secrets`; a configured seed would make them
predictable to anyone who could read this file.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from ..models import Radio
from ..paths import server_data_dir

CONFIG_FILENAME = "server.toml"

DEFAULT_CONFIG_TEMPLATE = '''\
# K4 Connect Manager - server configuration
#
# Paths are relative to the server data directory unless absolute.
# After editing the [[radios]] entries, run:  k4cm admin radios reload

[server]
host = "0.0.0.0"
port = 8443
public_hostname = "mh01.net"
tls_cert = "tls/server-cert.pem"
tls_key = "tls/server-key.pem"

[session]
# Default session length, and how long before the deadline the user is warned.
max_session_minutes = 240
warn_period_minutes = 15
# Grace after the deadline for an unresponsive user before we take the radio back.
ack_grace_minutes = 5
# How much time a granted extension adds.
extension_minutes = 60
# How long a freed radio is held for the next user in its queue.
queue_hold_minutes = 10

[schedule]
# How long a reservation is held for its owner before others may borrow the
# radio (they are warned the owner can take it back).
hold_minutes = 10
# How many days before a series' last date its owner is offered an extension.
ending_notice_days = 30

[monitor]
poll_interval_seconds = 30
# How long an assigned radio may sit with no client attached before reclaiming.
disconnect_grace_minutes = 5
cat_timeout_seconds = 5.0

[relay]
# For radios with relay_port set, members' panels connect to this server, which
# passes the link on to the radio and cuts it when the session ends. A K4 does
# not disconnect an attached panel by itself, so this is what makes the end of
# a session stick. Address to listen on for relayed radios:
bind = "0.0.0.0"
# Admit only the address the member last reached this server's API from. Turn
# off only if members' panels and computers reach the internet differently.
check_address = true

[storage]
database = "k4cm.db"
log_level = "INFO"
log_max_bytes = 5242880
log_backup_count = 5

[backup]
enabled = true
directory = "backups"
interval_hours = 24
retention = 14

# --- Radio inventory -------------------------------------------------------
# One [[radios]] block per central-site K4.
#
# Each radio has these addresses:
#   host / cat_port          how this server reaches the radio on the site LAN.
#                            Never forward the CAT port to the internet: CAT has
#                            no authentication.
#   host / remote_port       the radio's own remote-link port on the LAN.
#   relay_port               (recommended) a port on THIS server that relays the
#                            remote link, so the server can cut it when a session
#                            ends. Forward the public port to this server's
#                            relay_port, never to the radio.
#   public_host / public_port where members' panels connect from home — the
#                            site's public name or IP and the forwarded port.
#                            public_port defaults to relay_port if set, else
#                            remote_port. If members reach the site over a VPN,
#                            public_host can be this server's LAN address.

# [[radios]]
# id = "k4a"
# name = "K4 #1"
# host = "10.0.0.11"
# cat_port = 9200
# remote_port = 9204
# relay_port = 9204
# public_host = "mh01.net"
# public_port = 9204
# enabled = true
# description = "Main station K4, tri-bander"
# url = "https://mh01.net/radios/k4a"
# capabilities = ["hf", "6m"]
#
#   # Power management. Power-off is always CAT (PS0;). Power-on needs hardware:
#   # method = "http"    -> url is called to switch the outlet on
#   # method = "command" -> command is run on the server
#   # method = "none"    -> no remote power-on for this radio
#   [radios.power]
#   method = "http"
#   url = "http://10.0.0.50/api/outlet/3/on"
#   http_method = "POST"
#   address = "10.0.0.50"
#   outlet = "3"
#   credentials_ref = "K4CM_PDU_MAIN"   # name of an env var holding credentials
#   cycle_delay_s = 10
'''


@dataclass
class ServerSettings:
    host: str = "0.0.0.0"
    port: int = 8443
    public_hostname: str = "mh01.net"
    tls_cert: str = "tls/server-cert.pem"
    tls_key: str = "tls/server-key.pem"


@dataclass
class SessionSettings:
    max_session_minutes: int = 240
    warn_period_minutes: int = 15
    ack_grace_minutes: int = 5
    extension_minutes: int = 60
    queue_hold_minutes: int = 10

    @property
    def max_session(self) -> timedelta:
        return timedelta(minutes=self.max_session_minutes)

    @property
    def warn_period(self) -> timedelta:
        return timedelta(minutes=self.warn_period_minutes)

    @property
    def ack_grace(self) -> timedelta:
        return timedelta(minutes=self.ack_grace_minutes)

    @property
    def extension(self) -> timedelta:
        return timedelta(minutes=self.extension_minutes)

    @property
    def queue_hold(self) -> timedelta:
        return timedelta(minutes=self.queue_hold_minutes)


@dataclass
class ScheduleSettings:
    hold_minutes: int = 10
    ending_notice_days: int = 30

    @property
    def hold(self) -> timedelta:
        return timedelta(minutes=self.hold_minutes)

    @property
    def ending_notice(self) -> timedelta:
        return timedelta(days=self.ending_notice_days)


@dataclass
class MonitorSettings:
    poll_interval_seconds: float = 30.0
    disconnect_grace_minutes: int = 5
    cat_timeout_seconds: float = 5.0

    @property
    def disconnect_grace(self) -> timedelta:
        return timedelta(minutes=self.disconnect_grace_minutes)


@dataclass
class RelaySettings:
    bind: str = "0.0.0.0"
    check_address: bool = True


@dataclass
class StorageSettings:
    database: str = "k4cm.db"
    log_level: str = "INFO"
    log_max_bytes: int = 5 * 1024 * 1024
    log_backup_count: int = 5


@dataclass
class BackupSettings:
    enabled: bool = True
    directory: str = "backups"
    interval_hours: float = 24.0
    retention: int = 14


@dataclass
class ServerConfig:
    data_dir: Path
    path: Path
    server: ServerSettings = field(default_factory=ServerSettings)
    session: SessionSettings = field(default_factory=SessionSettings)
    schedule: ScheduleSettings = field(default_factory=ScheduleSettings)
    monitor: MonitorSettings = field(default_factory=MonitorSettings)
    relay: RelaySettings = field(default_factory=RelaySettings)
    storage: StorageSettings = field(default_factory=StorageSettings)
    backup: BackupSettings = field(default_factory=BackupSettings)
    radios: dict[str, Radio] = field(default_factory=dict)

    # -- resolved paths ----------------------------------------------------

    def resolve(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else self.data_dir / p

    @property
    def database_path(self) -> Path:
        return self.resolve(self.storage.database)

    @property
    def tls_cert_path(self) -> Path:
        return self.resolve(self.server.tls_cert)

    @property
    def tls_key_path(self) -> Path:
        return self.resolve(self.server.tls_key)

    @property
    def backup_dir(self) -> Path:
        return self.resolve(self.backup.directory)

    @property
    def log_path(self) -> Path:
        return self.data_dir / "logs" / "k4cm-server.log"

    # -- radio lookup ------------------------------------------------------

    def find_radio(self, ref: str) -> Radio | None:
        """Look a radio up by id, or failing that by name (case-insensitive)."""
        if ref in self.radios:
            return self.radios[ref]
        needle = ref.strip().lower()
        for radio in self.radios.values():
            if radio.id.lower() == needle or radio.name.lower() == needle:
                return radio
        return None

    def reload_radios(self) -> int:
        """Re-read only the radio inventory from disk. Returns the radio count."""
        raw = _read_toml(self.path)
        self.radios = _parse_radios(raw)
        return len(self.radios)


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"server config not found: {path} (run 'k4cm-server init' first)")
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _parse_radios(raw: dict[str, Any]) -> dict[str, Radio]:
    radios: dict[str, Radio] = {}
    for entry in raw.get("radios", []):
        radio = Radio.from_dict(entry)
        if radio.id in radios:
            raise ValueError(f"duplicate radio id in config: {radio.id}")
        radios[radio.id] = radio
    return radios


def _section(raw: dict[str, Any], name: str, cls):
    data = raw.get(name, {})
    known = {f for f in cls.__dataclass_fields__}
    return cls(**{k: v for k, v in data.items() if k in known})


def default_config_path(data_dir: Path | None = None) -> Path:
    return (data_dir or server_data_dir()) / CONFIG_FILENAME


def load_config(path: Path | None = None, data_dir: Path | None = None) -> ServerConfig:
    data_dir = data_dir or server_data_dir()
    path = path or default_config_path(data_dir)
    raw = _read_toml(path)
    return ServerConfig(
        data_dir=data_dir,
        path=path,
        server=_section(raw, "server", ServerSettings),
        session=_section(raw, "session", SessionSettings),
        schedule=_section(raw, "schedule", ScheduleSettings),
        monitor=_section(raw, "monitor", MonitorSettings),
        relay=_section(raw, "relay", RelaySettings),
        storage=_section(raw, "storage", StorageSettings),
        backup=_section(raw, "backup", BackupSettings),
        radios=_parse_radios(raw),
    )


def write_default_config(path: Path) -> bool:
    """Write the commented default config unless one already exists.

    Returns True if a file was written. Never overwrites an existing config —
    reinstalls must keep the site's radio inventory and policy intact.
    """
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
    return True
