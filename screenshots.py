"""Explicit screenshots saved atomically to a visible, predictable folder."""

from datetime import datetime
from pathlib import Path
import os
import uuid

from app_paths import data_dir


def screenshot_folder():
    return data_dir() / "screenshots"


def capture_screen(folder=None, grab=None):
    if grab is None:
        from PIL import ImageGrab

        grab = ImageGrab.grab
    folder = Path(folder) if folder is not None else screenshot_folder()
    folder.mkdir(parents=True, exist_ok=True)
    name = datetime.now().strftime("Крест_%Y-%m-%d_%H-%M-%S_") + uuid.uuid4().hex[:8] + ".png"
    target = folder / name
    temporary = target.with_suffix(".tmp")
    image = grab(all_screens=True)
    try:
        image.save(temporary, format="PNG")
        os.replace(temporary, target)
    finally:
        image.close()
        temporary.unlink(missing_ok=True)
    return target


def open_folder():
    folder = screenshot_folder()
    folder.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        raise RuntimeError("Открытие папки доступно в Windows: " + str(folder))
    os.startfile(folder)
    return str(folder)
