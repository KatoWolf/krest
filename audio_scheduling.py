"""Give the recognition thread Windows' bounded audio scheduling, not the whole app."""

from contextlib import contextmanager
import sys
import threading

_local = threading.local()


def status():
    return getattr(_local, "status", "standard")


@contextmanager
def recognition_priority():
    handle = None
    api = None
    _local.status = "standard"
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            api = ctypes.WinDLL("avrt", use_last_error=True)
            api.AvSetMmThreadCharacteristicsW.argtypes = [
                wintypes.LPCWSTR,
                ctypes.POINTER(wintypes.DWORD),
            ]
            api.AvSetMmThreadCharacteristicsW.restype = wintypes.HANDLE
            api.AvRevertMmThreadCharacteristics.argtypes = [wintypes.HANDLE]
            api.AvRevertMmThreadCharacteristics.restype = wintypes.BOOL
            index = wintypes.DWORD()
            handle = api.AvSetMmThreadCharacteristicsW("Audio", ctypes.byref(index))
            if handle:
                _local.status = "windows_audio"
        except (OSError, AttributeError):
            pass  # MMCSS can be unavailable; ordinary capture still works.
    try:
        yield status()
    finally:
        if handle:
            api.AvRevertMmThreadCharacteristics(handle)
        _local.status = "standard"
