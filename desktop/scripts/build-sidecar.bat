@echo off
rem Build the Python Agent Core into a single exe and place it where Tauri expects the sidecar:
rem   desktop\src-tauri\binaries\sarae-agent-core-<rust target triple>.exe
setlocal EnableExtensions
set "DESKTOP=%~dp0.."
set "CORE=%~dp0..\..\agent-core"
pushd "%CORE%" || goto :pathfail

if not exist ".venv\Scripts\python.exe" (
  echo [sidecar] Creating agent-core virtualenv
  call :create_venv || goto :fail
)
".venv\Scripts\python.exe" -m pip --version >nul 2>&1
if errorlevel 1 (
  echo [sidecar] Restoring pip in agent-core virtualenv
  ".venv\Scripts\python.exe" -m ensurepip --upgrade || goto :fail
)
echo [sidecar] Installing agent-core dependencies
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -e ".[dev]" || goto :fail

set "TRIPLE="
for /f "tokens=2" %%t in ('rustc -vV ^| findstr /b "host:"') do set "TRIPLE=%%t"
if "%TRIPLE%"=="" set "TRIPLE=x86_64-pc-windows-msvc"
echo [sidecar] Target triple: %TRIPLE%

echo [sidecar] Running PyInstaller
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --log-level WARN ^
  --distpath build\dist --workpath build\work build\sarae-agent-core.spec || goto :fail

echo [sidecar] Verifying bundled native dependencies
"build\dist\sarae-agent-core.exe" --self-check || goto :fail

if not exist "%DESKTOP%\src-tauri\binaries" mkdir "%DESKTOP%\src-tauri\binaries"
copy /y "build\dist\sarae-agent-core.exe" "%DESKTOP%\src-tauri\binaries\sarae-agent-core-%TRIPLE%.exe" >nul || goto :fail
echo [sidecar] OK: src-tauri\binaries\sarae-agent-core-%TRIPLE%.exe
popd
exit /b 0

:fail
popd
:pathfail
echo [sidecar] FAILED - see the error above.
rem Keep errors visible when launched from Explorer. Parent scripts opt out.
if /i not "%~1"=="--no-pause" pause
exit /b 1

:create_venv
rem Support both the Python launcher and regular Python PATH installations.
py -3.12 -c "import sys; assert (3, 11) <= sys.version_info[:2] < (3, 14)" >nul 2>&1
if not errorlevel 1 (
  py -3.12 -m venv .venv
  exit /b
)
for %%p in (python python3) do (
  %%p -c "import sys; assert (3, 11) <= sys.version_info[:2] < (3, 14)" >nul 2>&1
  if not errorlevel 1 (
    %%p -m venv .venv
    exit /b
  )
)
echo [sidecar] Python 3.11-3.13 is required. Install Python 3.12 and add it to PATH.
exit /b 1
