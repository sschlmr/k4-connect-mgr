# PyInstaller spec for the central server (Windows).
# Build on Windows:  pyinstaller packaging\windows-server\k4cm-server.spec
# Output:            dist\k4cm-server\  (one folder, contains k4cm-server.exe)

import os
import sys

from PyInstaller.utils.hooks import collect_submodules

sys.setrecursionlimit(5000)

project_root = os.path.abspath(os.path.join(os.getcwd()))
entry = os.path.join(project_root, "packaging", "windows-server", "server_entry.py")

a = Analysis(
    [entry],
    pathex=[os.path.join(project_root, "src")],
    binaries=[],
    datas=[],
    hiddenimports=[
        # uvicorn and its protocol implementations are imported dynamically.
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.loops.asyncio",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
        "uvicorn.lifespan.off",
        # Typer's --install-completion: shellingham imports these by name.
        *collect_submodules("shellingham"),
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="k4cm-server",
    console=True,
    debug=False,
    strip=False,
    upx=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="k4cm-server",
)
