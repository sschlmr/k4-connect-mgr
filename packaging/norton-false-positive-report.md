# Norton false-positive submission — K4 Connect Manager client

Prepared for submission to <https://submit.norton.com/> (choose **Dispute a
detection / Submit a false positive**). Everything below is verified from this
machine except the two items marked **NEEDED** — see "Before you submit".

---

## Submitter

| Field | Value |
|---|---|
| Name | *(your name)* |
| Email | *(the address you want replies at)* |
| Company / organisation | *(leave blank, or your club's name)* |
| Are you the software vendor? | Yes — I am the author of the flagged software |

## Detection details

| Field | Value |
|---|---|
| Norton product | Norton (Norton.app) 26.6.1 |
| Operating system | macOS 26.5.2 (build 25F84), Apple silicon |
| Detection name reported | `malwareX-gen` |
| Detected file paths | `<repo-dir>/dist/k4cm` and `<repo-dir>/build/k4cm/k4cm` |
| Date first observed | 2026-08-19 |
| File SHA-256 | **NEEDED** — see below |
| Action taken by Norton | File removed/quarantined automatically, within a second of being written |

## What the file is

`k4cm` is the command-line client of **K4 Connect Manager**, software I wrote to
share a group of Elecraft K4 amateur-radio transceivers among licensed operators.
The client asks a central server for a radio, receives a one-time password over
HTTPS, and instructs the operator's own radio to connect to it.

The flagged file is not hand-written machine code. It is a **PyInstaller 6.22.2
bundle**: a stock, unmodified PyInstaller bootloader with my Python application
appended to it. PyInstaller is a widely used, open-source packaging tool
(<https://pyinstaller.org>).

## Why I believe this is a false positive

1. **The detection matches the packer, not the payload.** The only native code in
   the file is PyInstaller's stock bootloader, shipped unmodified from the
   official PyPI wheel. Its SHA-256 on this machine is:

   ```
   c7dc4db94a43d640c440bb2a7556126d44cfc9f80a7845175e0dbeb19ce69b8b
   ```

   (`PyInstaller/bootloader/Darwin-64bit/run`, 154448 bytes, Mach-O universal
   x86_64 + arm64, ad-hoc/linker-signed.) This identical binary ships inside every
   PyInstaller application on macOS. `malwareX-gen` is a generic heuristic name,
   consistent with a packer-shape match rather than identification of specific
   malicious behaviour.

2. **The application's behaviour is narrow and documented.** It makes outbound
   HTTPS requests only to the one server address the user configures, and plain
   TCP connections only to the amateur-radio transceiver on the user's own
   network (Elecraft's documented CAT control port). It installs no service, sets
   no persistence, modifies no other programs, reads no browser or credential
   stores, and touches only its own configuration directory
   (`~/.config/k4cm/`). Its only stored secrets are the user's own API key and a
   server certificate the user explicitly confirms.

3. **The complete source is available for inspection**, along with the build
   recipe that reproduces the flagged file exactly: the PyInstaller spec at
   `packaging/macos-client/k4cm.spec` and the build script at
   `packaging/macos-client/build.sh`. The project has an automated test suite
   (91 tests) that exercises the whole system.

4. **The detection is disruptive to legitimate development.** Norton removes the
   output file between PyInstaller writing it and the next build step reading it,
   so the build cannot complete at all:

   ```
   SystemError: codesign command (['/usr/bin/codesign', '--remove',
   '--all-architectures', '.../build/k4cm/k4cm']) failed with error code 1!
   output: .../build/k4cm/k4cm: Operation not permitted
   ```

   The file no longer exists by the time `codesign` runs.

## Reproduction

On macOS with Python 3.11+ and Norton real-time protection enabled:

```
git clone <repository>          # or unpack the source archive
cd K4_connect_mgr
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m PyInstaller --clean --noconfirm packaging/macos-client/k4cm.spec
```

The build fails as shown above, and `build/k4cm/k4cm` is absent afterwards.

## Request

Please review the sample and, if you agree it is benign, whitelist it and
consider tuning the `malwareX-gen` heuristic so that unmodified PyInstaller
bootloaders are not matched on their shape alone.

---

## Before you submit

Two things I could not supply for you:

**1. The sample file and its SHA-256 (required — Norton's form needs the file).**
Norton deleted it, so recover it one of two ways:

- *From quarantine (best — it is the exact file Norton flagged):* open Norton →
  **Security History** → find the `malwareX-gen` entry for `k4cm` → **Restore**,
  and use the "report this as an incorrect detection" option there if offered.
  Norton's own UI submits the sample directly, which is easier than the web form.

- *By rebuilding:* add a temporary exclusion scoped to this project's build
  output (Norton → Settings → Antivirus → Scans and Risks → Items to Exclude,
  add `<repo-dir>/build` and `.../dist`), then:

  ```
  cd <repo-dir>
  PATH="$PWD/.venv/bin:$PATH" bash packaging/macos-client/build.sh
  shasum -a 256 dist/k4cm/k4cm
  ```

  Attach `dist/k4cm/k4cm` (zip it if the form requires) and paste the hash into
  the "File SHA-256" row above. Remove the exclusion afterwards if you prefer.

**2. Your name, and confirmation of the contact email.**

A note on expectations: signed and notarized builds get cleared much faster, and
often are not flagged at all. If you have an Apple Developer ID, signing the
binary before submitting (the commands are printed at the end of
`packaging/macos-client/build.sh`) makes this both a stronger submission and a
better experience for anyone you distribute to. If you would rather not deal with
any of it, `pipx install k4-connect-mgr` skips frozen binaries entirely and is
not affected by this detection.
