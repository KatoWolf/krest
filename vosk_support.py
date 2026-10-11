"""Vosk preflight and Windows filesystem compatibility, without importing models."""

import os
from pathlib import Path


def model_problem(folder):
    root = Path(folder)
    required = ["am/final.mdl", "conf/model.conf", "conf/mfcc.conf"]
    if (root / "graph/HCLG.fst").is_file():
        required.append("graph/HCLG.fst")
    else:
        required.extend(["graph/HCLr.fst", "graph/Gr.fst", "graph/disambig_tid.int"])
    missing = []
    for name in required:
        try:
            if (root / name).stat().st_size == 0:
                missing.append(name)
        except OSError:
            missing.append(name)
    return "Неполная модель Vosk: " + ", ".join(missing) if missing else ""


def native_model_path(folder):
    """Kaldi builds may fail on non-ANSI paths; use an existing DOS alias if available."""
    path = str(Path(folder).resolve())
    if os.name != "nt" or path.isascii():
        return path
    import ctypes

    get_short = ctypes.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
    get_short.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
    get_short.restype = ctypes.c_uint
    size = get_short(path, None, 0)
    if size:
        buffer = ctypes.create_unicode_buffer(size)
        if 0 < get_short(path, buffer, size) < size and buffer.value.isascii():
            return buffer.value
    return path
