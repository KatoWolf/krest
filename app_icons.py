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


def load_image(size=64, tone="metal"):
    from PIL import Image

    size = max(16, min(1024, int(size)))
    available = (16, 24, 32, 48, 64) if size <= 64 else (128, 256, 512)
    chosen = next((s for s in available if s >= size), available[-1])
    tone = tone if tone in ("metal", "white", "black") else "metal"
    path = ASSETS / "relic" / f"{tone}-{chosen}.png"
    with Image.open(path if path.exists() else ASSETS / "krest.png") as source:
        return source.convert("RGBA").resize((size, size), Image.Resampling.LANCZOS)


def load_wordmark(width=160, tone="metal"):
    from PIL import Image

    tone = tone if tone in ("metal", "white", "black") else "metal"
    with Image.open(ASSETS / "relic" / f"wordmark-{tone}.png") as source:
        width = max(64, min(800, int(width)))
        return source.convert("RGBA").resize(
            (width, round(width * source.height / source.width)), Image.Resampling.LANCZOS
        )


def install_window_icon(window):
    from PIL import ImageTk

    images = [ImageTk.PhotoImage(load_image(size), master=window) for size in (32, 64, 128)]
    window.iconphoto(True, *images)
    if os.name == "nt":
        window.iconbitmap(str(ASSETS / "krest.ico"))
    return images
