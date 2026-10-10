# PyInstaller spec for the client CLI (Linux, single file).
# Build:   pyinstaller packaging/linux-client/k4cm.spec
# Output:  dist/k4cm

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
    a.binaries,
    a.datas,
    [],
    name="k4cm",
    console=True,
    debug=False,
    strip=False,
    upx=False,
)
