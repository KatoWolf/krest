"""Windowed EXE workers keep explicitly inherited pipes, never allocate a console."""

import os
import sys


def prepare_streams():
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel.GetStdHandle.restype = wintypes.HANDLE
    kernel.GetFileType.argtypes = [wintypes.HANDLE]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.DuplicateHandle.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    for name, number, mode in (("stdin", -10, "r"), ("stdout", -11, "w"), ("stderr", -12, "w")):
        if getattr(sys, name) is not None:
            continue
        stream = None
        handle = kernel.GetStdHandle(number & 0xFFFFFFFF)
        if handle and handle != ctypes.c_void_p(-1).value and kernel.GetFileType(handle) == 3:
            duplicate = wintypes.HANDLE()
            process = kernel.GetCurrentProcess()
            if kernel.DuplicateHandle(
                process, handle, process, ctypes.byref(duplicate), 0, False, 2
            ):
                descriptor = msvcrt.open_osfhandle(
                    duplicate.value, os.O_RDONLY if mode == "r" else os.O_WRONLY
                )
                stream = os.fdopen(
                    descriptor, mode, encoding="utf-8", errors="replace", buffering=1
                )
        setattr(sys, name, stream or open(os.devnull, mode, encoding="utf-8"))


def relaunch_source_gui(arguments):
    if os.name != "nt" or getattr(sys, "frozen", False) or os.getenv("KREST_WINDOWLESS"):
        return False
    from pathlib import Path

    if Path(sys.executable).name.casefold() != "python.exe":
        return False
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file():
        return False
    import subprocess

    subprocess.Popen(
        [str(pythonw), str(Path(__file__).with_name("main.py")), *arguments],
        env={**os.environ, "KREST_WINDOWLESS": "1"},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return True
