# K4 Connect Manager

Shares a set of Elecraft K4 transceivers at a central site among members
operating from home. A central server tracks which K4s exist, who is using
which one, and which are free; a local client CLI requests a specific radio,
receives a one-time password, and tells the user's own K4 or K4/0 to connect.

Access is enforced by the radios themselves: every assignment sets a freshly
generated random password on the K4, and every ending — release, reclaim,
expiry, force-release — replaces it with another random one, so an old password
never opens a session.

```
  home                                  central site (mh01.net)
  ─────────────────────────           ──────────────────────────────
  k4cm  ──── HTTPS ───────────────▶   k4cm-server  ──── CAT ────▶  K4 #1
   │        (API key,                  (SQLite: users,              K4 #2
   │         pinned cert)               sessions, queue, audit)     K4 #3
   ▼                                              │
  your K4 / K4-0  ──────── Elecraft remote link ──┘
```

## How a session works

1. `k4cm request k4a` asks the server for that specific radio.
2. The server checks it is enabled and free, and claims it for you in one
   atomic database step, so two simultaneous requests can never both get it.
   It then generates a random password and sets it on the radio
   (`RRP<password>;` plus `RRC1;`); if the radio doesn't answer, the claim is
   dropped.
3. The client tells your panel to connect to the radio's public address
   (`RRT<host>:<port>?<password>;`) and confirms the link came up.
4. The client keeps watching the session. Before the deadline it warns you and
   asks whether to accept the end time, extend, or release now.
5. On release the server scrambles the radio's password again and offers it to
   whoever is next in that radio's queue.

Each K4 is requested by name — there is no pool of interchangeable radios — so
each has its own wait queue.

## Installing

Installers for every platform are on the
[Releases page](https://github.com/sschlmr/k4-connect-mgr/releases/latest):

| File | Who installs it |
|---|---|
| `k4cm-server-setup.exe` | The site administrator, on the Windows machine at the K4 site |
| `k4cm-client-setup.exe` | Members on Windows |
| `k4cm-client-<version>.pkg` | Members on macOS |
| `k4cm-client-<version>-linux-<arch>.tar.gz` (or `.deb`) | Members on Linux |

### Server (Windows, at the K4 site)

Download and run `k4cm-server-setup.exe`. It installs into `Program Files\K4 Connect Manager`,
registers two Task Scheduler jobs (start at boot, plus a watchdog every five
minutes that probes `/health` and relaunches the server if it is not
answering), and installs the Microsoft VC++ runtime if that is missing.

Then initialise the site:

```
k4cm-server init
```

This creates the config and database, generates a self-signed TLS certificate,
prints its SHA-256 fingerprint, and creates the admin account, printing its API
key once. **Publish that fingerprint to your users** — their client asks them to
confirm it.

Add your radios to `C:\ProgramData\k4cm\server.toml`:

```toml
[[radios]]
id = "k4a"
name = "K4 #1"
host = "10.0.0.11"           # LAN address the server uses for CAT
cat_port = 9200
remote_port = 9204            # the radio's own remote-link port
public_host = "mh01.net"      # where members' panels connect from home
public_port = 9204            # forwarded to remote_port; defaults to it
enabled = true
description = "Main station K4, tri-bander"
url = "https://mh01.net/radios/k4a"
capabilities = ["hf", "6m"]

  # Power-off is CAT (PS0;). Power-on needs hardware: a K4 accepts no commands
  # while off and has no Wake-on-LAN, so point this at your PDU or relay.
  [radios.power]
  method = "http"                     # or "command", or "none"
  url = "http://10.0.0.50/api/outlet/3/on"
  http_method = "POST"
  address = "10.0.0.50"
  outlet = "3"
  credentials_ref = "K4CM_PDU_MAIN"   # env var holding "user:password"
  cycle_delay_s = 10
```

Then `k4cm admin radios --reload` picks them up without a restart.

Members are only ever given `public_host`/`public_port`. Forward each radio's
public port to its `remote_port`, and **never forward a CAT port**: CAT has no
authentication, so anyone who can reach it can set the radio's password. If
members reach the site over a VPN instead, set `public_host` to the LAN address.

### Client (Windows, macOS, Linux)

Download and install the client package for your platform from the
[Releases page](https://github.com/sschlmr/k4-connect-mgr/releases/latest), then:

```
k4cm config
```

It asks for the server URL, your callsign, and the API key your administrator
issued, fetches the server's certificate, and shows you its fingerprint to
confirm against the published one. The confirmed certificate is pinned and
becomes the only certificate this machine will accept from the server.

On any machine with Python 3.11+ you can skip the installers entirely:

```
pipx install git+https://github.com/sschlmr/k4-connect-mgr
```

See [packaging/README.md](packaging/README.md) for building the installers, and
for what to do about antivirus false positives (PyInstaller executables are
commonly flagged regardless of what they contain).

## Using it

```
k4cm status                 what every K4 is doing, and your own session
k4cm request k4a            take a specific K4 and connect your panel
k4cm request k4a --queue    join its queue instead if it is busy
k4cm watch                  resume the session watcher
k4cm release                disconnect and hand the radio back
k4cm queue --leave          leave the queue
```

`k4cm request` stays running as the session watcher unless you pass
`--no-watch`. The watcher is what prompts you when the session is nearly over
and when a queued radio is offered to you; Ctrl-C stops watching without
affecting the session.

### Administration

```
k4cm admin users                          list accounts
k4cm admin users --add W1AW               create one, printing its API key once
k4cm admin users --reset-key W1AW         issue a replacement key
k4cm admin radios                         list radios and reachability
k4cm admin radios --reload                re-read the radio inventory
k4cm admin radios --power k4a --action cycle
k4cm admin sessions                       recent and active sessions
k4cm admin sessions --force-release 42    end someone's session
k4cm admin audit --hours 24               read the audit trail
```

If the admin key is ever lost, `k4cm-server reset-admin` run **on the server
machine** issues a new one. There is deliberately no remote path for this.

## Session policy

Set in `[session]` in `server.toml`:

| Setting | Default | Meaning |
|---|---|---|
| `max_session_minutes` | 240 | Length of a new session |
| `warn_period_minutes` | 15 | How early the user is warned |
| `ack_grace_minutes` | 5 | Extra time after the deadline for an unresponsive user |
| `extension_minutes` | 60 | What a granted extension adds |
| `queue_hold_minutes` | 10 | How long a freed radio is held for the next in queue |

An extension is granted only when nobody is queued for that radio. A user who
never answers the warning loses the radio once the ack grace expires, so radios
are never left tied up. Separately, `[monitor] disconnect_grace_minutes`
(default 5) reclaims a radio whose panel has been disconnected that long.

## Operations

- **Logs**: `C:\ProgramData\k4cm\logs\k4cm-server.log`, rotated.
- **Audit trail**: every assignment, release, extension, reclaim, admin action,
  power action and failed login, in the database and readable with
  `k4cm admin audit`.
- **Backups**: nightly by default into `C:\ProgramData\k4cm\backups\`, keeping
  14, taken with SQLite's online backup API so they are safe while running.
  `k4cm-server backup` takes one now. Point `[backup] directory` at a network
  share for off-machine copies. To restore: stop the scheduled task, replace
  `k4cm.db`, start it again.
- **Health**: `GET /health` needs no authentication and exposes no data.

## Security notes

- All client/server traffic is HTTPS. The client refuses `http://` for anything
  but localhost, so API keys and radio passwords never cross the network in the
  clear.
- Trust is a pinned self-signed certificate, confirmed by fingerprint at setup.
  If the certificate changes, every call fails loudly until the user re-runs
  `k4cm config` — rotate deliberately and tell your users.
- API keys are stored hashed (SHA-256); passwords and keys come from the
  platform entropy source via Python's `secrets`. There is no seed anywhere,
  deliberately.
- Repeated bad keys from one address are slowed exponentially and recorded.
- This secures the manager's own control plane. The Elecraft remote link between
  a K4/0 and a K4 carries whatever protection Elecraft's protocol provides.

## Development

```
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest
```

The suite runs against `tests/fake_k4.py`, a simulator implementing the CAT
commands used here (`RRP`, `RRC`, `RRT`, `RRN`, `CC`, `PING`, `PS0`) including a
real TCP link between two fakes, so everything is testable without hardware.

You can also drive the whole system by hand:

```
# two fake radios and a fake panel
python tests/fake_k4.py --cat-port 19200 --remote-port 19204 &
python tests/fake_k4.py --cat-port 19300 --remote-port 19304 &

export K4CM_SERVER_DIR=/tmp/k4cm-site
k4cm-server init --hostname 127.0.0.1 --admin W1AW
#  ... add a [[radios]] entry for 127.0.0.1:19200 (remote_port 19204,
#      public_host 127.0.0.1) to /tmp/k4cm-site/server.toml
k4cm-server run &

export K4CM_CLIENT_DIR=/tmp/k4cm-user
k4cm config          # pin the certificate, paste the API key
k4cm request k4a
```

`K4CM_SERVER_DIR` and `K4CM_CLIENT_DIR` override the platform data locations,
which is what makes side-by-side testing possible.

### Testing against real radios

`tests/hardware/` checks the simulator's assumptions against real hardware:
that a panel reads `CC-1` when connected, that a wrong password is refused with
`RRT!`, that `RRC0;` drops an attached panel, and that an ended session's
password no longer connects. It is skipped unless you create
`tests/hardware/hardware.toml` (copy `hardware.example.toml`; the file is
gitignored). Tests that change the central radio's password need
`allow_changes = true` and restore its original settings afterwards.

```
.venv/bin/python -m pytest tests/hardware -v -s
```

Raw responses are saved to `tests/hardware/observations.json`.

## Protocol reference

Commands used, from the K4 Programmer's Reference (rev. D12) and the K4/0
Owner's Manual:

| Command | Sent to | Purpose |
|---|---|---|
| `RRP<password>;` | central K4 | Set the remote-access password |
| `RRC<n>;` | central K4 | Allow n remote connections (0 disables and disconnects) |
| `RRT<host>:<port>?<password>;` | local K4/K4-0 | Connect to a remote K4; failure answers `RRT!;` |
| `RRN;` | local K4/K4-0 | Disconnect |
| `CC;` | either | Client count: n ≥ 1 clients attached (server), -1 connected as a client, 0 idle |
| `PING;` → `PONG;` | either | Reachability |
| `PS0;` | central K4 | Clean remote power-down |
