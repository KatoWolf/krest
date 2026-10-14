"""Bounded sampling and shared background-work policy; no GPU probes."""

from storage import read_json


from pathlib import Path
import threading
import time
import psutil
from storage import atomic_write_json

MODES = {"auto", "balanced", "game", "economy"}


class PerformancePolicy:
    def __init__(self, settings, path):
        self.settings, self.path = settings, Path(path)
        self.override = None
        self.active = "balanced"
        self.game = ""
        self.game_folders = {}
        self.metrics = {"cpu": 0, "memory_mb": 0, "processes": 1}
        self.processes = {psutil.Process().pid: psutil.Process()}
        self._last_sample = self._last_detect = 0
        self._lock = threading.RLock()

    def set_mode(self, mode):
        if mode not in MODES:
            raise ValueError("Профиль: auto, balanced, game или economy")
        with self._lock:
            self.override = mode
            self._last_detect = 0
        self.tick(force=True)

    def update_games(self, games):
        folders = {}
        for game in games:
            if not game.get("installed") or not game.get("library") or not game.get("install_dir"):
                continue
            parent = Path(game["library"]) / "steamapps" / "common"
            path = (parent / game["install_dir"]).resolve()
            if path.is_relative_to(parent.resolve()):
                folders[str(path).casefold()] = str(game.get("name", "Игра"))[:100]
        with self._lock:
            self.game_folders = folders

    def tick(self, force=False):
        with self._lock:
            now = time.monotonic()
            cfg = self.settings().get("performance", {})
            mode = self.override or cfg.get("mode", "auto")
            if force or now - self._last_detect >= 10:
                self._last_detect = now
                self.game = ""
                # Detect games even when the resource profile is manually selected.
                names = {
                    n.strip().casefold()
                    for n in cfg.get("game_processes", "cs2.exe,dota2.exe").split(",")
                    if n.strip()
                }
                for process in psutil.process_iter(["name", "exe"]):
                    if (process.info["name"] or "").casefold() in names:
                        self.game = process.info["name"]
                        break
                    executable = str(process.info.get("exe") or "").casefold()
                    if executable and not any(
                        v in (process.info.get("name") or "").casefold()
                        for v in ("launcher", "crash", "updater", "uninstall")
                    ):
                        matched = next(
                            (
                                name
                                for folder, name in self.game_folders.items()
                                if executable.startswith(folder + __import__("os").sep)
                            ),
                            None,
                        )
                        if matched:
                            self.game = matched
                            break
                self.active = (
                    "game"
                    if mode == "auto" and self.game
                    else "balanced"
                    if mode == "auto"
                    else mode
                )
            interval = 5 if self.active == "balanced" else 10
            if force or now - self._last_sample >= interval:
                self._last_sample = now
                root = psutil.Process()
                try:
                    children = root.children(recursive=True)
                except psutil.Error:
                    children = []
                alive = {p.pid: p for p in [root, *children]}
                self.processes = {pid: self.processes.get(pid, proc) for pid, proc in alive.items()}
                cpu, memory = 0, 0
                for process in self.processes.values():
                    try:
                        cpu += process.cpu_percent(None)
                        memory += process.memory_info().rss
                    except psutil.Error:
                        continue
                self.metrics = {
                    "cpu": round(cpu / (psutil.cpu_count() or 1), 1),
                    "memory_mb": round(memory / 1024**2, 1),
                    "processes": len(alive),
                }
                atomic_write_json(
                    self.path, {"mode": self.active, "game": self.game, "updated": time.time()}
                )

    @property
    def defer_files(self):
        return self.active in ("game", "economy")

    @property
    def poll_multiplier(self):
        return 1 if self.active == "balanced" else 3


def foreground_title():
    import os

    if os.name != "nt":
        return "Чтение активного окна доступно в Windows"
    import ctypes
    from ctypes import wintypes

    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetForegroundWindow.restype = wintypes.HWND
    user.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    window = user.GetForegroundWindow()
    text = ctypes.create_unicode_buffer(min(user.GetWindowTextLengthW(window) + 1, 2048))
    user.GetWindowTextW(window, text, len(text))
    return text.value


def shared_multiplier(folder):
    import json

    try:
        value = read_json(Path(folder) / "activity_profile.json")
        if 0 <= time.time() - value["updated"] < 60 and value["mode"] in ("game", "economy"):
            return 3
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return 1
