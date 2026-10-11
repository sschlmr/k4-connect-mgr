# Operating the mh01.net K4s from home with k4cm

This guide is for club members. It covers everything you need to borrow one of
the central K4s at mh01.net from your own K4 or K4/0: one-time setup, taking a
radio, the end of a session, waiting in a queue, and what to do when something
goes wrong.

## What k4cm does

The K4s at mh01.net are shared. `k4cm` is the program on your computer that
asks the station's server for a radio, gets a password that works only for
your session, and tells your own K4 or K4/0 to connect with it. When your
session ends, the server disconnects you and changes the radio's password, so
the radio is free for the next member.

You never need to know or type a radio password. You never connect your panel
to the K4s directly either: the station only accepts a panel during a session
that k4cm has set up.

The radios available are:

| Radio | Use this name |
|---|---|
| K4-1 | `k4-1` |
| K4-2 | `k4-2` |

`k4cm status` always shows the current list.

## Before you start

You need:

- **Your callsign and API key.** The station administrator gives you these. The
  API key is a long string of random characters; treat it like a password. If
  you lose it, ask the administrator for a new one; the old one then stops
  working.
- **The server's certificate fingerprint**, also from the administrator. It
  lets k4cm confirm it is talking to the real station server.
- **Your panel's network address.** k4cm drives your K4 or K4/0 over its
  network CAT port, so the panel must be on your home network and your
  computer must be able to reach it. You need its IP address (for example
  `192.168.4.117`) and its CAT port (normally `9200`). Give the panel a fixed
  address in your router (a DHCP reservation) so it doesn't change.
- **Your computer and your panel on the same internet connection.** The station
  only lets a panel in from the address your computer uses to reach the server.
  A panel at home with a laptop on a phone hotspot, or on a work VPN, won't be
  admitted.

## Installing

Download the installer for your computer from
<https://github.com/sschlmr/k4-connect-mgr/releases/latest>:

| Computer | File |
|---|---|
| Windows | `k4cm-client-setup.exe` |
| Mac | `k4cm-client-<version>.pkg` |
| Linux | `k4cm-client-<version>-linux-<arch>.tar.gz` (unpack, then `sudo ./install.sh`) |

Windows may warn about the installer because it is not code-signed. In Edge,
choose **⋯ → Keep → Show more → Keep anyway**, and when it runs, **More info →
Run anyway**. On a Mac, if the package is blocked, open **System Settings →
Privacy & Security** and click **Open Anyway**.

Installing a newer version over an older one is fine: your settings are kept.

After installing, open a new terminal window (Terminal on a Mac, Command Prompt
or PowerShell on Windows). Every k4cm command is typed there.

## One-time setup: `k4cm config`

```
k4cm config
```

It asks five questions:

| Question | Answer |
|---|---|
| Server URL | `https://mh01.net:8443` (press Enter to accept the default) |
| Your callsign | your callsign |
| Your API key | paste the key from the administrator |
| CAT address of your local K4 / K4-0 | your panel's IP address, e.g. `192.168.4.117` |
| CAT port of your local radio | `9200` unless you changed it on the panel |

It then fetches the server's certificate and shows its fingerprint. **Compare it
with the fingerprint the administrator gave you, character for character**, and
answer `y` only if they match. From then on k4cm accepts only that
certificate, so nobody can impersonate the station server.

It finishes with `Server reachable: k4cm 0.1.0 at https://mh01.net:8443`. If it
says the server couldn't be contacted, see [Troubleshooting](#troubleshooting).

Run `k4cm config` again any time to change an answer. It shows your current
values as defaults, so you only retype what changes.

If you leave the panel address blank, k4cm still works, but it prints the
connection details and you enter them on the panel yourself (see
[Connecting by hand](#connecting-by-hand)).

### Optional: Tab completion

```
k4cm --install-completion
```

then open a new terminal window. Typing `k4cm ` and pressing Tab now offers the
commands. On a Mac using bash (not the default zsh), also run this once:

```
echo '[ -f ~/.bash_completions/k4cm.sh ] && . ~/.bash_completions/k4cm.sh' >> ~/.bash_profile
```

## Seeing what's free: `k4cm status`

```
k4cm status
```

```
RADIO        STATE                      UNTIL      QUEUE  REACHABLE
k4-1         in use by W1XYZ            72m        0      yes
k4-2         free                       -          0      yes

You have no session and are not queued.
Your local radio: idle (not connected)
```

- **STATE**: `free`; `in use by <callsign>`; `held for <callsign>` (freed and
  being offered to the next member in its queue); or `disabled` (taken out of
  service by the administrator).
- **UNTIL**: how long the current session has left.
- **QUEUE**: how many members are waiting for that radio.
- **REACHABLE**: whether the server can talk to the radio. `NO` means the radio
  is off or has a problem; tell the administrator. `?` means it hasn't been
  checked yet.

## Taking a radio: `k4cm request`

```
k4cm request k4-1
```

k4cm:

1. asks the server for K4-1;
2. receives a password made for this session alone;
3. tells your panel to connect to the station;
4. waits, up to 12 seconds, for the station to confirm your panel arrived;
5. reports how long the session lasts, and then keeps watching it (see below).

```
K4-1 is yours until 2026-10-10T22:15:00+00:00.
  Time allowed: 240m
Local radio: connected to mh01.net:27201

Watching your session — Ctrl-C to stop (your session keeps running).
```

Times are shown in UTC.

**Leave that window open.** The watcher warns you before the session ends and
lets you extend it. If you close it or press Ctrl-C, your session carries on;
run `k4cm watch` to pick it up again.

If your panel doesn't connect, k4cm gives the radio straight back so it isn't
left tied up, and tells you why.

You can have one session at a time.

### Options

| Option | What it does |
|---|---|
| `--queue` or `-q` | If the radio is busy, join its queue without asking |
| `--no-watch` | Connect, then return to the prompt instead of watching. Run `k4cm watch` later |
| `--no-connect` | Take the radio but don't drive your panel. Prints the details to enter by hand |

## How long a session lasts

| | |
|---|---|
| Session length | 4 hours |
| Warning before the end | 15 minutes |
| An extension adds | 1 hour |
| Extensions allowed | only while nobody is queued for that radio |

Fifteen minutes before the end, the watcher beeps and asks:

```
K4-1 session ends in 15m.
[A]ccept the end time, [E]xtend, or [R]elease now?
```

- **A**: keep operating until the deadline. The session then ends and your panel
  is disconnected.
- **E**: add an hour. Refused if someone is waiting for the radio; your answer
  then counts as **A**.
- **R**: end the session now.

**If you don't answer** (you've walked away, or the watcher isn't running),
you get 5 minutes past the deadline, and then the radio is taken back
automatically.

**If your panel loses its connection** and stays disconnected for 5 minutes,
the server ends your session so the radio isn't held for nobody. Reconnecting
within that time keeps the session.

## Finishing: `k4cm release`

```
k4cm release
```

Disconnects your panel and hands the radio back. Please release as soon as
you're finished rather than letting the session run out: the next member gets
the radio straight away.

Whenever a session ends, for any reason, the station disconnects your panel
within a few seconds and changes the radio's password. If the watcher is
running, it also tells you the session ended and disconnects your panel itself.

## Waiting for a busy radio

When the radio you ask for is in use, k4cm says so and offers to queue you:

```
k4cm request k4-2
K4-2 is in use by W1XYZ until 2026-10-10T20:30:00+00:00
Join the queue for it? [Y/n]:
```

Each radio has its own queue. After joining, run `k4cm watch` and leave it
running while you wait. When the radio frees up and it's your
turn, it's **held for you for 10 minutes**, and the watcher beeps and asks:

```
K4-2 is free and is being held for you for 10m.
Take it now? [Y/n]:
```

Answer **Y** and your panel is connected as usual. Answer **n** and it goes to
the next member. If you don't answer within 10 minutes, it goes to the next
member anyway, so stay near the computer while you're queued.

Other queue commands:

| Command | What it does |
|---|---|
| `k4cm queue k4-2` | Join K4-2's queue |
| `k4cm queue` | Show your place in the queue |
| `k4cm queue --leave` | Leave the queue |

## Command summary

| Command | What it does |
|---|---|
| `k4cm config` | Set up or change your settings |
| `k4cm status` | What every radio is doing, your session, your panel |
| `k4cm request <radio>` | Take a radio and connect your panel |
| `k4cm watch` | Resume watching your session or queue place |
| `k4cm release` | Disconnect and hand the radio back |
| `k4cm queue [<radio>]` | Join a queue, or show your place in it |
| `k4cm queue --leave` | Leave the queue |
| `k4cm --help`, `k4cm request --help` | Help for any command |

## Connecting by hand

If you set up k4cm without a panel address, or used `--no-connect`, k4cm prints
the details instead:

```
Connect your panel manually with these details:
  Host:     mh01.net
  Port:     27201
  Password: Xq3...
```

Enter them in your panel's remote-connection settings and connect. The password
works for this session only, and the station still admits only the address your
computer is using. Use **Disconnect** on the panel and then `k4cm release` when
you're done.

## Troubleshooting

**`config is incomplete ... Run 'k4cm config'`**
k4cm hasn't been set up on this computer, or a setting is missing. Run
`k4cm config`.

**`invalid or missing API key`**
The key is wrong, or the administrator has replaced it. Run `k4cm config` and
paste the current key. Repeated wrong keys make the server answer more and more
slowly for a while.

**`certificate does not match`**
The server is presenting a different certificate from the one you pinned. If
the administrator has announced a new certificate, run `k4cm config`, compare
the new fingerprint, and accept it. If nobody announced a change, don't accept
it; tell the administrator.

**The server can't be contacted**
Check that your internet connection works and that the server URL is
`https://mh01.net:8443`. If it still fails, the station may be down; tell the
administrator.

**`cannot talk to your local radio at 192.168.x.x:9200`**
k4cm can't reach your own panel. Check that it is on, on your home network, and
at the address you gave `k4cm config` (its address may have changed), and that
its CAT port is 9200. The radio is handed back automatically, so request it
again once the panel answers.

**`your radio did not connect ... within 12 seconds`**
Your panel was told to connect, but the station never saw it arrive. Common
causes:
- your panel and your computer are reaching the internet differently (VPN,
  hotspot, a second router), so the station refused the panel's address;
- your home network blocks outgoing connections to mh01.net ports 27201 and
  27211;
- the central radio is off or unreachable (check `REACHABLE` in `k4cm status`).

**`<radio> is disabled by the administrator`**
The radio is out of service. Try the other one.

**`you already have ... assigned; release it first`**
You have a session already. Run `k4cm status` to see it, and `k4cm release` to
end it.

**`Your local radio: connected (or connecting) to a remote K4`** with no session
Your panel is still linked from an earlier attempt, or is connecting somewhere
else. Press **Disconnect** on the panel.

**On Windows, `'k4cm' is not recognized`**
Open a new Command Prompt or PowerShell window after installing. Windows only
picks up the new program location in windows opened afterwards.

Anything else: tell the station administrator what you typed and the exact
message k4cm printed.

## Where things are kept

| | Windows | Mac / Linux |
|---|---|---|
| Your settings and API key | `%APPDATA%\k4cm\config.toml` | `~/.config/k4cm/config.toml` |
| Pinned server certificate | `%APPDATA%\k4cm\server-cert.pem` | `~/.config/k4cm/server-cert.pem` |
| This guide | Start menu: **K4 Connect Manager Client → K4CM User Guide** | `/usr/local/share/doc/k4cm/k4cm-user-guide.md` |

The settings file holds your API key. Don't share it or copy it onto other
people's computers. Uninstalling k4cm leaves the settings in place; delete the
folder yourself if you want them gone.
