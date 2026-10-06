"""Opt-in tests against real Elecraft radios.

The rest of the suite runs against tests/fake_k4.py, which was written from the
K4 Programmer's Reference. These tests check the fake's assumptions — the ones
the security model rests on — against real hardware: that RRP/RRC take effect,
that RRC0 drops an attached client, that CC reads -1 on a connected panel, and
what a refused RRT actually looks like.

Everything is skipped unless tests/hardware/hardware.toml exists (see
hardware.example.toml). Raw observations are saved to observations.json so a
run can be reviewed afterwards, even where an assertion could not be made.
"""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

import pytest

HERE = Path(__file__).parent
CONFIG_PATH = Path(os.environ.get("K4CM_HARDWARE_CONFIG", HERE / "hardware.toml"))
OBSERVATIONS_PATH = HERE / "observations.json"


def _load() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open("rb") as fh:
        return tomllib.load(fh)


HARDWARE = _load()


def section(name: str) -> dict:
    """The named config section, or skip the test if it is not configured."""
    if name not in HARDWARE:
        pytest.skip(f"no [{name}] section in {CONFIG_PATH}")
    return HARDWARE[name]


@pytest.fixture
def panel() -> dict:
    return section("panel")


@pytest.fixture
def remote() -> dict:
    return section("remote")


@pytest.fixture
def site() -> dict:
    return section("site")


@pytest.fixture
def site_writable(site) -> dict:
    if not site.get("allow_changes"):
        pytest.skip("[site] allow_changes is not true — not touching the central radio's settings")
    return site


class Observations:
    def __init__(self) -> None:
        self.data: dict[str, object] = {}

    def record(self, key: str, value: object) -> None:
        self.data[key] = value
        print(f"[observed] {key}: {value!r}")


@pytest.fixture(scope="session")
def observed():
    log = Observations()
    yield log
    if log.data:
        OBSERVATIONS_PATH.write_text(json.dumps(log.data, indent=2, default=str), encoding="utf-8")
