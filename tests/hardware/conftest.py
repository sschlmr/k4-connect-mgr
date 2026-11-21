"""Opt-in tests against real Elecraft radios.

The rest of the suite runs against tests/fake_k4.py, which was written from the
K4 Programmer's Reference. These tests check the fake's assumptions — the ones
the security model rests on — against real hardware: that RRP/RRC take effect,
that RRC0 drops an attached client, that CC reads -1 on a connected panel, and
what a refused RRT actually looks like.

Everything is skipped unless tests/hardware/hardware.toml exists (see
hardware.example.toml) AND K4CM_HARDWARE=1 is set. The file alone is not
enough: it stays on the developer's machine, and a plain `pytest` run must
never reach the real radios. These tests send RRN; to the panel and dial the
central radio, which drops whatever live session the panel is carrying, so
they are run deliberately at the last stage before a release.

Even then nothing runs unless the tester can get the central radio: the
session fixture `claim` requests `[claim] radio` through the tester's own
k4cm login (their client config) before the first test and releases it after
the last. If the radio is in use, held, booked, or the tester already has a
session (whose panel link the tests would cut), every test is skipped, so a
hardware run can never interrupt anyone. The claimed session's password, the
one the radio is really using, stands in for `[remote] password`. Raw observations are saved to observations.json so a
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


ENABLED = os.environ.get("K4CM_HARDWARE") == "1"
HARDWARE = _load() if ENABLED else {}


def section(name: str) -> dict:
    """The named config section, or skip the test if it is not configured."""
    if not ENABLED:
        pytest.skip("real-radio tests run only with K4CM_HARDWARE=1 (release testing; see README)")
    if name not in HARDWARE:
        pytest.skip(f"no [{name}] section in {CONFIG_PATH}")
    return HARDWARE[name]


@pytest.fixture
def panel() -> dict:
    return section("panel")


@pytest.fixture(scope="session", autouse=True)
def claim():
    """Hold the central radio for the whole run, or run nothing."""
    from k4cm.client.api import ApiError, ServerApi
    from k4cm.client.config import ConfigError, load_config

    radio = section("claim").get("radio")
    if not radio:
        pytest.skip(f"set [claim] radio (the k4cm radio id, e.g. k4-1) in {CONFIG_PATH}")
    try:
        config = load_config()
        config.validate()
        api = ServerApi(config)
        current = api.session_state().get("assignment")
        if current:
            pytest.skip(
                f"{config.callsign} already has {current['radio']['name']}; the tests would cut that "
                "session's panel link. Release it ('k4cm release') and run again."
            )
        # Short, so a run that dies before releasing does not hold the radio for long.
        assignment = api.request_assignment(radio, duration_minutes=30)
    except (ConfigError, ApiError) as e:
        pytest.skip(f"could not get {radio} for the hardware tests, so none were run: {e}")
    print(f"[claim] {config.callsign} holds {radio} for the hardware tests")
    try:
        yield assignment
    finally:
        try:
            api.release()
            print(f"[claim] released {radio}")
        except ApiError as e:
            print(f"[claim] could not release {radio}: {e}. Run 'k4cm release'.")


@pytest.fixture
def remote(claim) -> dict:
    return {**section("remote"), "password": claim["password"]}


@pytest.fixture
def site() -> dict:
    site = section("site")
    if not site.get("cat_host"):
        pytest.skip("[site] has no cat_host: the central radio's CAT port is reachable only from the site LAN or a VPN")
    return site


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
