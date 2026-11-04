"""Win32 process/window/system helpers via ctypes.

Every function degrades gracefully on non-Windows so unit tests can import the package anywhere.
"""
from __future__ import annotations

import ctypes
import os
import platform
import sys
from ctypes import wintypes

IS_WINDOWS = os.name == "nt"

if IS_WINDOWS:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL

    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                    ctypes.POINTER(wintypes.DWORD)]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
GW_OWNER = 4
ERROR_ALREADY_EXISTS = 183


def hostname() -> str:
    return platform.node() or os.environ.get("COMPUTERNAME", "windows-agent")


def os_description() -> str:
    return f"{platform.system()} {platform.release()} ({platform.version()}) {platform.machine()}"


def process_image_path(pid: int) -> str | None:
    if not IS_WINDOWS:
        return None
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return None
    finally:
        kernel32.CloseHandle(handle)


def is_process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if not IS_WINDOWS:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # Access denied still means the process exists.
        return ctypes.get_last_error() == 5
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def top_level_windows() -> list[dict]:
    """Visible, unowned top-level windows with pid, title, rect and minimized flag."""
    if not IS_WINDOWS:
        return []
    found: list[dict] = []

    def callback(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, GW_OWNER):
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            length = user32.GetWindowTextLengthW(hwnd)
            title_buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, title_buffer, length + 1)
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            found.append({
                "hwnd": int(hwnd), "pid": int(pid.value), "title": title_buffer.value,
                "rect": (rect.left, rect.top, rect.right, rect.bottom),
                "minimized": bool(user32.IsIconic(hwnd)),
            })
        except Exception:  # noqa: BLE001 - a single odd window must not abort enumeration
            pass
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return found


def window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    if not IS_WINDOWS or not user32.IsWindow(hwnd):
        return None
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return rect.left, rect.top, rect.right, rect.bottom


def is_minimized(hwnd: int) -> bool:
    return bool(IS_WINDOWS and user32.IsIconic(hwnd))


class SingleInstance:
    """Named mutex in the per-session Local\\ namespace: one Agent Core per Windows logon session."""

    def __init__(self, name: str = "Local\\SaraeAgentCore.v1"):
        self.name = name
        self.handle = None

    def acquire(self) -> bool:
        if not IS_WINDOWS:
            return True
        handle = kernel32.CreateMutexW(None, True, self.name)
        if not handle:
            return False
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        self.handle = handle
        return True

    def release(self) -> None:
        if self.handle and IS_WINDOWS:
            kernel32.CloseHandle(self.handle)
            self.handle = None


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))
