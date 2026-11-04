# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the Agent Core sidecar (one-file console exe, launched hidden by Tauri).
# Build via desktop/scripts/build-sidecar.bat; outputs go to agent-core/build/dist (git-ignored).
import os

from PyInstaller.utils.hooks import collect_submodules

SRC = os.path.abspath(os.path.join(SPECPATH, "..", "src"))

hidden = collect_submodules("sarae_agent") + collect_submodules("obsws_python") + collect_submodules("edge_tts")

a = Analysis(
    [os.path.join(SRC, "sarae_agent", "__main__.py")],
    pathex=[SRC],
    binaries=[],
    # The OBS overlay renderer page (built from web/ by `npm run build:overlay`), served by sarae_agent.overlay.server.
    datas=[(os.path.join(SRC, "sarae_agent", "overlay", "renderer"), os.path.join("sarae_agent", "overlay", "renderer")),
           (os.path.join(SRC, "sarae_agent", "monitoring", "resources"), os.path.join("sarae_agent", "monitoring", "resources"))],
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "IPython", "pytest", "PyQt5", "PyQt6", "PySide6", "pandas", "scipy"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="sarae-agent-core",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX-packed binaries trigger antivirus false positives
    console=True,  # stdio pipes are required; Tauri spawns it with CREATE_NO_WINDOW
    disable_windowed_traceback=False,
    target_arch=None,
)
