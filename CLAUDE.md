# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`k4-connect-mgr` shares a set of Elecraft K4 transceivers at a central site among club
members operating from home. One Python package, `k4cm`, ships two entry points:

- `k4cm-server` (`k4cm.server.main:app`) — FastAPI service at the K4 site, SQLite-backed.
- `k4cm` (`k4cm.client.main:app`) — Typer CLI on each member's machine.

`README.md` is the user-facing manual (installation, session policy, CAT protocol table);
`packaging/README.md` covers building installers and antivirus false positives. Both are
current — read them before changing behaviour they describe.

## Commands

```bash
python -m venv .venv && .venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest                       # full suite (~6s, no hardware needed)
.venv/bin/python -m pytest tests/test_manager.py -k queue_offer   # one test / pattern
.venv/bin/python -m ruff check .                 # lint (E402 + bugbear config in pyproject)
```

Tags matching `v*` trigger `.github/workflows/build-packages.yml`, which runs the suite
and then builds four installers on their native runners (PyInstaller cannot cross-compile).

### Driving the system by hand

`K4CM_SERVER_DIR` and `K4CM_CLIENT_DIR` override the platform data directories
(`k4cm.paths`), which is what makes side-by-side manual runs possible. `tests/fake_k4.py`
runs standalone as a radio simulator — see the "Development" section of `README.md` for
the full sequence.

## Architecture

### The security model is the assignment model

There is no per-radio ACL anywhere. Access is enforced *by the radios themselves*:
`AssignmentManager._apply_password` sets a fresh `secrets`-generated password on the K4
(`RRP<pw>;` + `RRC1;`) when a session starts, and `_lock_radio` sets another fresh one and
sends `RRC0;` on every ending — release, reclaim, expiry, force-release. A stale password
therefore never opens a session. Passwords are never derived from a previous value and
there is deliberately no configurable seed. Any new code path that ends a session must go
through `AssignmentManager.end_assignment` so the lockout happens.

Radios are requested by name — there is no pool of interchangeable radios — so each radio
has its own wait queue, and "free" is a per-radio question.

### Race-free allocation

Allocation is two-phase. `request_assignment` first *claims* the radio with
`db.claim_radio` — an INSERT of a `pending` row — and partial unique indexes allow at most
one `pending`/`active` row per radio and per callsign, so simultaneous requests cannot both
win (the loser gets `ClaimConflict`). Only then does it set the password and `activate` the
claim. Every CAT sequence that changes a radio's password runs under
`manager.radio_lock(radio_id)`, so a lockout and a new session's setup never interleave.
State changes are conditional on the expected prior state (`finish_assignment`,
`set_expiry`); `end_assignment` re-reads the row under the lock and takes a `still_due`
predicate, because callers (the monitor especially) act on snapshots that go stale across
awaits. Orphaned `pending` claims are failed by `abandon_stale_claims` after
`claim_timeout`. Use `live_assignment_for_*` for "is this radio/user occupied".

### Two addresses per radio

`host`/`cat_port` is the LAN CAT address, used only by the server and never sent to
members. `public_host`/`public_port` (required / defaults to `remote_port`) is what members'
panels dial, exposed by `Radio.public()` as `connect_host`/`connect_port`.

### Where state lives

- **Radios** come from `server.toml` (`[[radios]]`), not the database. `k4cm admin radios
  --reload` re-reads only that section at runtime via `ServerConfig.reload_radios()`.
- **Everything else** (users, assignments, per-radio queue, audit) is SQLite —
  `k4cm.server.db.Database`, one connection guarded by an `RLock`, WAL mode. Schema is
  `CREATE TABLE IF NOT EXISTS` executed on every open; there is no migration framework.
- **Reachability** is in-memory only: `manager.reachability` / `manager.disconnected_since`,
  filled in by the monitor. `None` means "not probed yet" and is rendered as unknown.

### Server layout (`src/k4cm/server/`)

`app.py` is thin — routes authenticate via `auth.require_user` / `require_admin` and
delegate straight to `AssignmentManager`. Business logic and the only place radios are
touched is `manager.py`. `ManagerError(status, message, **extra)` is translated to a JSON
response by an exception handler in `create_app`, so managers raise it rather than
`HTTPException`.

`monitor.py` runs one `tick()` per poll interval doing three things: expiring queue offers,
enforcing session deadlines (an *acked* session ends at its deadline; an unacked one gets
`ack_grace` more), and reclaiming radios whose panel has been disconnected past
`disconnect_grace`. `tick()` is a plain coroutine specifically so tests can drive it
directly with shortened timers — keep it that way.

`create_app(..., background=False)` suppresses the monitor and backup loops; the test
fixtures rely on this.

`power.py`: powering *off* is CAT (`PS0;`), but powering *on* cannot be done in software —
a K4 accepts no commands while off and has no Wake-on-LAN — so it is delegated to a shell
command or an HTTP call against a PDU/relay under `[radios.power]`.

### Client layout (`src/k4cm/client/`)

`main.py` is Typer commands plus the **session watcher** (`_watch_loop`), which polls
`/assignments/current` and handles the two interactive negotiations: the expiry warning
(accept / extend / release) and a queued radio being offered. `request` runs the watcher
inline unless `--no-watch`.

Trust is a **pinned self-signed certificate**, not the public CA set: `k4cm config` fetches
the server cert, shows its fingerprint for the user to confirm against what the admin
published, and saves it next to `config.toml`; `ServerApi._verify()` passes that file to
httpx as the only acceptable CA. `_is_certificate_error` walks the exception chain because
httpx buries `ssl.SSLError` inside `ConnectError` — a mismatch must produce the
"certificate does not match" message, not a generic connectivity error. `check_transport`
refuses plain `http://` to anything but localhost.

`radio.py` drives the user's own K4/K4-0: `RRT<host>:<port>?<password>;` to connect,
`RRN;` to disconnect. A successful connect is *silent* on the K4, so success is confirmed
by polling `CC;`; failure arrives asynchronously as `RRT!;`, which is why
`CatClient.request` takes an `error_prefix`.

### CAT layer (`src/k4cm/cat.py`)

ASCII commands terminated by `;` over TCP. `CatClient.request` skips responses with other
prefixes (async status broadcasts) while waiting for the one it wants. `send_commands`
PINGs afterwards to confirm the radio consumed the commands; `power_off` deliberately does
not, because the radio stops answering.

## Testing

`tests/conftest.py` builds a whole site per test: three `FakeK4` instances (two central
radios, one standing in for the user's panel) on ephemeral ports, a generated `server.toml`,
a real `Database` in `tmp_path`, manager, monitor, and the app. `server.add_user()` /
`server.headers()` handle API keys. The `api` fixture wraps the app in an
`httpx.ASGITransport` with `ADMIN` and `W1AW` already created.

`tests/fake_k4.py` implements the CAT commands used here (`RRP`, `RRC`, `RRT`, `RRN`, `CC`,
`PING`, `PS0`) **including a real TCP link between two fakes**, so the entire
request → connect → release flow is testable without hardware. Extend the fake rather than
mocking `cat` when adding radio behaviour. The fake follows the K4 Programmer's Reference
(rev. D12) where it is specific — e.g. `CC;` is -1 on a radio connected as a client — and
`tests/hardware/` checks those assumptions against real radios (opt-in, skipped by default).

`test_e2e.py` goes further: uvicorn in a background thread and CLI invocations pushed to
worker threads (`asyncio.to_thread`) so the test's event loop stays free to serve the fake
radios. `pytest-asyncio` is in `asyncio_mode = "auto"` — async tests need no marker.

## Conventions worth keeping

- Datetimes are UTC-aware everywhere; use `models.utcnow()` / `isoformat()` / `parse_dt()`,
  and store ISO strings in SQLite.
- Every state change writes an audit row (`db.audit(actor, action, detail)`) with a
  dotted action name (`session.expired`, `queue.offer_lapsed`, `power.failed`, `auth.failed`).
- User-facing errors say what to do next ("Run 'k4cm config'", "force-release the session
  first"), and API keys are echoed exactly once at creation.
- Module docstrings carry the *why* — the non-obvious constraint behind the code. Keep that
  habit when adding modules.
- `write_default_config` and the installers never overwrite existing site data; reinstalls
  must preserve `server.toml`, the database, and pinned certificates.
