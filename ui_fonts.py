"""Bundled Cyrillic UI fonts, registered for this process without admin rights."""

import ctypes
import os
from pathlib import Path

UI_FONT = "Roboto"
_loaded = False


def load_fonts(root=None):
    global _loaded
    if not _loaded:
        paths = list((Path(__file__).resolve().parent / "assets" / "fonts").glob("Roboto-*.ttf"))
        try:
            if os.name == "nt":
                add = ctypes.WinDLL("gdi32").AddFontResourceExW
                add.argtypes = [ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_void_p]
                add.restype = ctypes.c_int
                for path in paths:
                    add(str(path), 0x10, None)  # FR_PRIVATE: no system installation.
            elif os.name == "posix":
                from ctypes.util import find_library

                name = find_library("fontconfig")
                if name:
                    fc = ctypes.CDLL(name)
                    fc.FcConfigGetCurrent.restype = ctypes.c_void_p
                    fc.FcConfigAppFontAddFile.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
                    config = fc.FcConfigGetCurrent()
                    for path in paths:
                        fc.FcConfigAppFontAddFile(config, os.fsencode(path))
            _loaded = True
        except (OSError, AttributeError):
            pass  # Tk's normal fallback keeps the menu usable.
    if root is not None:
        from tkinter import font

        for name in (
            "TkDefaultFont",
            "TkTextFont",
            "TkMenuFont",
            "TkHeadingFont",
            "TkCaptionFont",
            "TkTooltipFont",
        ):
            font.nametofont(name, root=root).configure(family=UI_FONT, size=11)
