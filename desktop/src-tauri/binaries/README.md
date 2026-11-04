Generated folder. `scripts/build-sidecar.bat` writes the PyInstaller build of `agent-core` here as
`sarae-agent-core-<target-triple>.exe` (e.g. `sarae-agent-core-x86_64-pc-windows-msvc.exe`),
which Tauri bundles through `bundle.externalBin`. Never commit the binary.
