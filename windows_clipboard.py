"""Small, bounded Unicode clipboard backend; Windows calls only on demand."""

import ctypes
from ctypes import wintypes
import os


def decode_text(raw):
    # GlobalSize may include unused allocation bytes after the UTF-16 terminator.
    end = next(
        (i for i in range(0, len(raw) - 1, 2) if raw[i : i + 2] == b"\0\0"), len(raw) // 2 * 2
    )
    return raw[:end].decode("utf-16-le")


def api():
    if os.name != "nt":
        raise ValueError("Буфер обмена доступен в Windows")
    user, kernel = (
        ctypes.WinDLL("user32", use_last_error=True),
        ctypes.WinDLL("kernel32", use_last_error=True),
    )
    user.OpenClipboard.argtypes = [wintypes.HWND]
    user.GetClipboardData.argtypes = [wintypes.UINT]
    user.GetClipboardData.restype = wintypes.HANDLE
    user.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user.SetClipboardData.restype = wintypes.HANDLE
    user.GetForegroundWindow.restype = wintypes.HWND
    user.GetClipboardOwner.restype = wintypes.HWND
    user.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    kernel.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel.GlobalLock.restype = ctypes.c_void_p
    kernel.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel.GlobalSize.argtypes = [wintypes.HGLOBAL]
    kernel.GlobalSize.restype = ctypes.c_size_t
    kernel.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel.GlobalFree.argtypes = [wintypes.HGLOBAL]
    return user, kernel


def read(previous=None):
    user, kernel = api()
    sequence = user.GetClipboardSequenceNumber()
    if sequence == previous or not user.IsClipboardFormatAvailable(13):
        return sequence, "", ""
    if not user.OpenClipboard(None):
        raise ValueError("Буфер сейчас занят другим приложением")
    try:
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(user.GetClipboardOwner(), ctypes.byref(pid))
        excluded = user.RegisterClipboardFormatW("ExcludeClipboardContentFromMonitorProcessing")
        if excluded and user.IsClipboardFormatAvailable(excluded):
            return sequence, "", ""
        handle = user.GetClipboardData(13)
        length = kernel.GlobalSize(handle) if handle else 0
        if not length or length > 8002:
            return sequence, "", ""
        pointer = kernel.GlobalLock(handle)
        if not pointer:
            raise ValueError("Не удалось прочитать буфер")
        try:
            text = decode_text(ctypes.string_at(pointer, length))
        finally:
            kernel.GlobalUnlock(handle)
    finally:
        user.CloseClipboard()
    import psutil

    try:
        application = psutil.Process(pid.value).name()
    except psutil.Error:
        application = ""
    return sequence, text, application


def write(text):
    user, kernel = api()
    user.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.HMENU,
        wintypes.HINSTANCE,
        ctypes.c_void_p,
    ]
    user.CreateWindowExW.restype = wintypes.HWND
    user.DestroyWindow.argtypes = [wintypes.HWND]
    owner = user.CreateWindowExW(
        0, "STATIC", "Krest clipboard", 0, 0, 0, 0, 0, wintypes.HWND(-3), None, None, None
    )
    if not owner:
        raise ValueError("Не удалось открыть окно буфера")
    data = (text + "\0").encode("utf-16-le")
    handle = kernel.GlobalAlloc(2, len(data))
    if not handle:
        user.DestroyWindow(owner)
        raise MemoryError("Буфер не выделен")
    transferred = False
    try:
        pointer = kernel.GlobalLock(handle)
        if not pointer:
            raise ValueError("Буфер не заблокирован")
        try:
            ctypes.memmove(pointer, data, len(data))
        finally:
            kernel.GlobalUnlock(handle)
        if not user.OpenClipboard(owner):
            raise ValueError("Буфер сейчас занят другим приложением")
        try:
            if not user.EmptyClipboard() or not user.SetClipboardData(13, handle):
                raise ValueError("Windows не подтвердила копирование")
            transferred = True
        finally:
            user.CloseClipboard()
    finally:
        if not transferred:
            kernel.GlobalFree(handle)
        user.DestroyWindow(owner)
