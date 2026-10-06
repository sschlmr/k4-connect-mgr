"""Platform-appropriate locations for server and client data."""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR_NAME = "k4cm"


def server_data_dir() -> Path:
    """Where the server keeps its config, database, certs, logs and backups.

    Windows: C:\\ProgramData\\k4cm   (survives reinstall of Program Files)
    POSIX:   /var/lib/k4cm, or ~/.local/share/k4cm when not running as root.
    Overridable with K4CM_SERVER_DIR (used by tests and portable installs).
    """
    override = os.environ.get("K4CM_SERVER_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("ProgramData", r"C:\ProgramData"))
        return base / APP_DIR_NAME
    system = Path("/var/lib") / APP_DIR_NAME
    if os.access("/var/lib", os.W_OK):
        return system
    return Path.home() / ".local" / "share" / APP_DIR_NAME


def client_config_dir() -> Path:
    """Where the client CLI keeps its config and the pinned server certificate.

    Windows: %APPDATA%\\k4cm    POSIX: ~/.config/k4cm
    Overridable with K4CM_CLIENT_DIR.
    """
    override = os.environ.get("K4CM_CLIENT_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / APP_DIR_NAME
    return Path.home() / ".config" / APP_DIR_NAME
