# PyInstaller spec for the client CLI (macOS, one folder).
# Build:   pyinstaller packaging/macos-client/k4cm.spec
# Output:  dist/k4cm/  (folder; the launcher is dist/k4cm/k4cm)
#
# One-folder rather than one-file on purpose: one-file executables carry the
# packed archive inside the bootloader, which antivirus heuristics flag far more
# often, and the extra signature-stripping step it needs fails outright when a
# scanner holds the freshly written file. See packaging/README.md.

import os
import sys

from PyInstaller.utils.hooks import collect_submodules

sys.setrecursionlimit(5000)

project_root = os.path.abspath(os.getcwd())
entry = os.path.join(project_root, "packaging", "windows-client", "client_entry.py")

a = Analysis(
    [entry],
    pathex=[os.path.join(project_root, "src")],
    binaries=[],
    datas=[],
    # Typer's --install-completion finds the shell via shellingham, which
    # imports shellingham.posix / shellingham.nt by name at runtime.
    hiddenimports=collect_submodules("shellingham"),
    excludes=["tkinter", "matplotlib", "pytest", "uvicorn", "fastapi"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="k4cm",
    console=True,
    debug=False,
    strip=False,
    upx=False,
)

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="k4cm")
