# Packaging

Each platform's package is built on that platform — PyInstaller cannot
cross-compile. The GitHub Actions workflow in `.github/workflows/build-packages.yml`
builds all four on clean runners; the scripts here do the same locally.

| Target | Build with | Produces |
|---|---|---|
| Windows server | `packaging\windows-server\build.ps1` | `packaging\out\k4cm-server-setup.exe` |
| Windows client | `packaging\windows-client\build.ps1` | `packaging\out\k4cm-client-setup.exe` |
| macOS client | `packaging/macos-client/build.sh` | `packaging/out/k4cm-client-<version>.pkg` |
| Linux client | `packaging/linux-client/build.sh` | `packaging/out/k4cm-client-<version>-linux-<arch>.tar.gz` (+ `.deb` if `fpm` is installed) |

Prerequisites: Python 3.11+ everywhere, plus [Inno Setup 6](https://jrsoftware.org/isinfo.php)
for the Windows installers. The Windows server installer itself checks for the
Microsoft VC++ 2015–2022 x64 runtime on the target machine and installs it if
missing; everything else it relies on (Task Scheduler, PowerShell) ships with
Windows, and the PyInstaller bundle carries its own Python.

## Where data lives

Installers never write over site or user data, so reinstalling and upgrading are
safe:

- Server: `C:\ProgramData\k4cm\` — `server.toml`, `k4cm.db`, `tls\`, `logs\`, `backups\`
- Client: `%APPDATA%\k4cm\` (Windows) or `~/.config/k4cm/` (macOS, Linux) —
  `config.toml` and the pinned `server-cert.pem`

Installing a new version over an old one is an upgrade, and each installer
clears the old program files first so nothing stale is left behind: the macOS
package with a `preinstall` script (`macos-client/scripts/`), the Windows
installers with `[InstallDelete]`. The Windows server installer also stops the
running server and its watchdog before copying files (they would otherwise hold
them locked), then re-registers both tasks and starts the server again.

Uninstalling removes the program files and, on the server, the scheduled tasks.
Both uninstallers tell the user where the data was left.

## Antivirus false positives

**PyInstaller executables are routinely flagged by antivirus products** —
Norton, Defender, and others — because a great deal of real malware is packed
with the same tool, and the shared bootloader is what the heuristics match. The
code being packaged is irrelevant to the detection.

This bites in two places:

1. **On the build machine.** If a scanner quarantines the executable the instant
   PyInstaller writes it, the build fails partway through. On macOS the symptom
   is:

   ```
   SystemError: codesign command (['/usr/bin/codesign', '--remove', ...])
   failed with error code 1!  output: ... Operation not permitted
   ```

   PyInstaller wrote the file, the scanner removed it, and the next step found
   nothing to work on. Check with `ls build/k4cm/k4cm` — if it is missing right
   after a failed build, that is what happened.

2. **On users' machines**, where a downloaded installer may be blocked or
   silently deleted.

What actually helps, in order of value:

- **Sign the binaries.** A signed, notarized macOS package and an
  Authenticode-signed Windows installer are trusted far more readily and are
  what you need for distribution anyway. The commands are printed at the end of
  `packaging/macos-client/build.sh`; on Windows use `signtool sign /fd sha256
  /tr <timestamp-url> /td sha256 /a packaging\out\*.exe`.
- **Build releases in CI**, not on a workstation. The runners are clean and
  nothing interferes with the intermediate files.
- **Report false positives to the vendor** if a signed release is still
  flagged — Norton's submission form is at
  <https://submit.norton.com/> and Microsoft's at
  <https://www.microsoft.com/en-us/wdsi/filesubmission>. Vendors usually clear
  legitimate signed software within a few days.
- **Exclude your own build directory** on the machine where *you* build, so
  local builds complete. Scope it to the project's `build/` and `dist/`
  directories rather than turning protection off.

If frozen binaries are more trouble than they are worth for your group, skip
them: the project is an ordinary Python package, so

```
pipx install k4-connect-mgr
```

installs the same `k4cm` and `k4cm-server` commands on any machine that has
Python 3.11+, with no bundled bootloader for a scanner to object to.
