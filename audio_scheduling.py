"""Keep inference out of Windows' real-time audio scheduling class.

PortAudio schedules the capture callback. Decoding and synthesis are ordinary
CPU work and must not compete with the audio engine at MMCSS Audio priority.
"""

from contextlib import contextmanager
import sys
import threading

_local = threading.local()


def status():
    return getattr(_local, "status", "standard")


@contextmanager
def _thread_priority(priority, label):
    api = handle = previous = None
    changed = False
    old_status = status()
    _local.status = "standard"
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            api = ctypes.WinDLL("kernel32", use_last_error=True)
            api.GetCurrentThread.argtypes = []
            api.GetCurrentThread.restype = wintypes.HANDLE
            api.GetThreadPriority.argtypes = [wintypes.HANDLE]
            api.GetThreadPriority.restype = ctypes.c_int
            api.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
            api.SetThreadPriority.restype = wintypes.BOOL
            handle = api.GetCurrentThread()
            previous = api.GetThreadPriority(handle)
            if previous != 0x7FFFFFFF:
                changed = previous != priority and bool(api.SetThreadPriority(handle, priority))
                if previous == priority or changed:
                    _local.status = label
        except (OSError, AttributeError):
            pass
    try:
        yield status()
    finally:
        if changed:
            api.SetThreadPriority(handle, previous)
        _local.status = old_status


def recognition_priority():
    return _thread_priority(0, "windows_normal")


@contextmanager
def synthesis_priority(gaming):
    if gaming:
        with _thread_priority(-1, "windows_background"):
            yield
    else:
        yield
