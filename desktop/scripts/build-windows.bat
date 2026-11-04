@echo off
rem Release build: Python sidecar -> React bundle -> Tauri NSIS installer.
rem Output: desktop\release\SaraeAgent-Setup-<version>.exe (copy of the Tauri NSIS bundle).
setlocal EnableExtensions
set "DESKTOP=%~dp0.."
pushd "%DESKTOP%" || exit /b 1

call "%~dp0build-sidecar.bat" --no-pause || goto :fail

echo [build] npm ci
if exist "package-lock.json" (call npm ci --no-audit --no-fund) else (call npm install --no-audit --no-fund)
if errorlevel 1 goto :fail

echo [build] tauri build (NSIS)
call npx tauri build --bundles nsis || goto :fail

set "VERSION="
for /f "usebackq delims=" %%v in (`node -p "require('./src-tauri/tauri.conf.json').version"`) do set "VERSION=%%v"
set "BUNDLE=src-tauri\target\release\bundle\nsis"
if not exist "release" mkdir "release"
for %%f in ("%BUNDLE%\*_%VERSION%_x64-setup.exe") do copy /y "%%f" "release\SaraeAgent-Setup-%VERSION%.exe" >nul
if not exist "release\SaraeAgent-Setup-%VERSION%.exe" (echo [build] Khong tim thay installer & goto :fail)
echo [build] OK: %DESKTOP%\release\SaraeAgent-Setup-%VERSION%.exe
popd
exit /b 0

:fail
echo [build] FAILED
popd
pause
exit /b 1
