"""Client configuration and server-certificate pinning.

The client trusts exactly one certificate — the pinned copy saved during
`k4cm config` — rather than the public CA set, so a self-signed server
certificate works and a swapped certificate fails loudly.
"""

from __future__ import annotations

import hashlib
import ssl
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from ..models import DEFAULT_CAT_PORT
from ..paths import client_config_dir

CONFIG_FILENAME = "config.toml"
CERT_FILENAME = "server-cert.pem"


class ConfigError(Exception):
    """Configuration is missing or unusable."""


@dataclass
class ClientConfig:
    server_url: str = "https://mh01.net:8443"
    callsign: str = ""
    api_key: str = ""
    radio_host: str = ""
    radio_cat_port: int = DEFAULT_CAT_PORT
    timezone: str = ""                  # IANA name; empty means this computer's zone
    path: Path | None = None

    @property
    def local_timezone(self) -> str:
        """The zone bookings are entered in: the configured one, else this
        computer's, else UTC."""
        if self.timezone:
            return self.timezone
        try:
            import tzlocal

            return tzlocal.get_localzone_name() or "UTC"
        except (LookupError, OSError, ValueError):  # an odd system zone must not break the CLI
            return "UTC"

    @property
    def cert_path(self) -> Path:
        directory = self.path.parent if self.path else client_config_dir()
        return directory / CERT_FILENAME

    @property
    def has_local_radio(self) -> bool:
        return bool(self.radio_host)

    def validate(self) -> None:
        missing = [
            name
            for name, value in (
                ("server_url", self.server_url),
                ("callsign", self.callsign),
                ("api_key", self.api_key),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                f"config is incomplete (missing: {', '.join(missing)}). Run 'k4cm config'."
            )
        check_transport(self.server_url)


def check_transport(server_url: str) -> None:
    """Refuse plaintext to anywhere but localhost — API keys travel on this link."""
    parsed = urlparse(server_url)
    if parsed.scheme == "https":
        return
    if parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1"):
        return
    raise ConfigError(
        f"server_url must use https (got {server_url!r}). Plain http is only allowed "
        "for localhost during development."
    )


def config_path() -> Path:
    return client_config_dir() / CONFIG_FILENAME


def load_config(path: Path | None = None) -> ClientConfig:
    path = path or config_path()
    if not path.exists():
        raise ConfigError(f"no client config at {path}. Run 'k4cm config' to create one.")
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    server = raw.get("server", {})
    radio = raw.get("radio", {})
    schedule = raw.get("schedule", {})
    return ClientConfig(
        server_url=str(server.get("url", "")),
        callsign=str(server.get("callsign", "")),
        api_key=str(server.get("api_key", "")),
        radio_host=str(radio.get("host", "")),
        radio_cat_port=int(radio.get("cat_port", DEFAULT_CAT_PORT)),
        timezone=str(schedule.get("timezone", "")),
        path=path,
    )


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def save_config(config: ClientConfig, path: Path | None = None) -> Path:
    path = path or config.path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "# K4 Connect Manager - client configuration\n"
        "# The server's certificate is pinned alongside this file as "
        f"{CERT_FILENAME}.\n\n"
        "[server]\n"
        f"url = {_quote(config.server_url)}\n"
        f"callsign = {_quote(config.callsign)}\n"
        f"api_key = {_quote(config.api_key)}\n\n"
        "# Your local K4 or K4/0 - the panel that will be told to connect.\n"
        "[radio]\n"
        f"host = {_quote(config.radio_host)}\n"
        f"cat_port = {config.radio_cat_port}\n"
    )
    if config.timezone:
        content += (
            "\n# Time zone bookings are entered in (IANA name). Remove to follow this computer.\n"
            f"[schedule]\ntimezone = {_quote(config.timezone)}\n"
        )
    path.write_text(content, encoding="utf-8")
    try:
        path.chmod(0o600)  # the API key lives here
    except OSError:
        pass
    config.path = path
    return path


# -- certificate pinning ---------------------------------------------------


def fingerprint_of_pem(pem: str) -> str:
    der = ssl.PEM_cert_to_DER_cert(pem)
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def fetch_server_certificate(server_url: str, timeout: float = 10.0) -> tuple[str, str]:
    """Retrieve the server's certificate without validating it, so the user can
    confirm its fingerprint against what the admin published."""
    parsed = urlparse(server_url)
    host, port = parsed.hostname, parsed.port or 443
    if not host:
        raise ConfigError(f"cannot parse a hostname out of {server_url!r}")
    try:
        pem = ssl.get_server_certificate((host, port), timeout=timeout)
    except (OSError, ssl.SSLError) as e:
        raise ConfigError(f"could not reach {host}:{port} to fetch its certificate: {e}") from e
    return pem, fingerprint_of_pem(pem)


def save_pinned_certificate(config: ClientConfig, pem: str) -> Path:
    path = config.cert_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(pem, encoding="utf-8")
    return path


def pinned_fingerprint(config: ClientConfig) -> str | None:
    if not config.cert_path.exists():
        return None
    return fingerprint_of_pem(config.cert_path.read_text(encoding="utf-8"))
