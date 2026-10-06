# PyInstaller spec for the client CLI (Windows).
# Build on Windows:  pyinstaller packaging\windows-client\k4cm.spec
# Output:            dist\k4cm\  (one folder, contains k4cm.exe)

import os
import sys

sys.setrecursionlimit(5000)

project_root = os.path.abspath(os.getcwd())
entry = os.path.join(project_root, "packaging", "windows-client", "client_entry.py")

a = Analysis(
    [entry],
    pathex=[os.path.join(project_root, "src")],
    binaries=[],
    datas=[],
    hiddenimports=[],
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
