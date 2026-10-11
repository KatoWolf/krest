"""Resource, user-data and child-process paths for source and compiled releases."""

from pathlib import Path
import os
import sys

RESOURCE_DIR = Path(__file__).resolve().parent


def compiled():
    return bool(getattr(sys, "frozen", False) or "__compiled__" in globals())


def data_dir():
    configured = os.getenv("KREST_CONFIG_PATH")
    if configured:
        return Path(configured).resolve().parent
    if compiled():
        return Path(os.getenv("LOCALAPPDATA", Path.home())) / "Krest" / "Data"
    return RESOURCE_DIR


def app_command(*args):
    if compiled():
        return [sys.executable, *map(str, args)]
    return [sys.executable, str(RESOURCE_DIR / "main.py"), *map(str, args)]


def program_dir():
    """Writable sidecar models are placed beside the executable, outside its bundle."""
    return Path(sys.executable).resolve().parent if compiled() else RESOURCE_DIR
