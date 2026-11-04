@echo off
rem Development run: builds the Python sidecar for this machine's Rust target triple (once, or
rem when --rebuild-core is passed), installs npm packages, then starts `tauri dev`.
setlocal EnableExtensions
set "DESKTOP=%~dp0.."
pushd "%DESKTOP%" || exit /b 1

rem Developer machines only; end users install the NSIS package and need none of these.
where cargo >nul 2>&1 || (echo [dev] Can cai Rust toolchain: https://rustup.rs & goto :fail)
where npm >nul 2>&1 || (echo [dev] Can cai Node.js 20+ & goto :fail)

set "TRIPLE="
for /f "tokens=2" %%t in ('rustc -vV ^| findstr /b "host:"') do set "TRIPLE=%%t"
if "%TRIPLE%"=="" set "TRIPLE=x86_64-pc-windows-msvc"

if /i "%~1"=="--rebuild-core" goto :sidecar
if exist "src-tauri\binaries\sarae-agent-core-%TRIPLE%.exe" goto :npm
:sidecar
call "%~dp0build-sidecar.bat" --no-pause || goto :fail

:npm
if not exist "node_modules" (
  echo [dev] npm install
  call npm install --no-audit --no-fund || goto :fail
)
echo [dev] Starting Tauri dev (Vite on http://127.0.0.1:1420)
call npx tauri dev || goto :fail
popd
exit /b 0

:fail
popd
pause
exit /b 1
