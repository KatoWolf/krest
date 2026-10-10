"""Application identity for source-based launches and Windows taskbar grouping."""

import os
from pathlib import Path

ASSETS = Path(__file__).resolve().parent / "assets"


def set_process_identity():
    if os.name == "nt":
        import ctypes

        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("KatoWolf.Krest.Desktop")
        except (OSError, AttributeError):
            pass


def load_image(size=64):
    from PIL import Image

    with Image.open(ASSETS / "krest.png") as source:
        return source.convert("RGBA").resize((size, size), Image.Resampling.LANCZOS)


def install_window_icon(window):
    from PIL import ImageTk

    images = [ImageTk.PhotoImage(load_image(size), master=window) for size in (32, 64, 128)]
    window.iconphoto(True, *images)
    if os.name == "nt":
        window.iconbitmap(str(ASSETS / "krest.ico"))
    return images
