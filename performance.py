"""Bounded sampling and shared background-work policy; no GPU probes."""

import os
from pathlib import Path
import threading
import time
import psutil
from storage import atomic_write_json, read_json

MODES = {"auto", "balanced", "game", "economy"}
# Steam also installs desktop software. A manifest or a process under
# steamapps/common alone does not establish that a game is running.
DESKTOP_APP_IDS = {"431960", "993090", "228980", "250820"}
DESKTOP_FOLDERS = {
    "wallpaper_engine",
    "wallpaper engine",
    "lossless scaling",
    "steamworks shared",
    "steamvr",
}
BACKGROUND_PROCESSES = {
    "steam.exe",
    "steamservice.exe",
    "steamwebhelper.exe",
    "wallpaper32.exe",
    "wallpaper64.exe",
    "wallpaperservice32.exe",
    "wallpaperservice64.exe",
    "losslessscaling.exe",
    "vrserver.exe",
    "vrcompositor.exe",
    "vrmonitor.exe",
}


def background_process(name):
    name = name.casefold()
    return (
        name in BACKGROUND_PROCESSES
        or any(
            part in name
            for part in (
                "launcher",
                "crash",
                "updater",
                "uninstall",
                "installer",
                "redist",
                "helper",
                "anticheat",
                "battleye",
                "reporter",
            )
        )
        or name in {"setup.exe", "install.exe"}
    )


class PerformancePolicy:
    def __init__(self, settings, path):
        self.settings, self.path = settings, Path(path)
        self.override = None
        self.active = "balanced"
        self.game = ""
        self.game_process = ""
        self.game_folders = {}
        self.metrics = {"cpu": 0, "memory_mb": 0, "processes": 1}
        self.processes = {psutil.Process().pid: psutil.Process()}
        self._last_sample = self._last_detect = 0
        self._detect_config = None
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
            if (
                str(game.get("appid", "")) in DESKTOP_APP_IDS
                or str(game.get("type", "")).casefold()
                in {"application", "software", "tool", "dlc"}
                or str(game["install_dir"]).casefold() in DESKTOP_FOLDERS
            ):
                continue
            parent = Path(game["library"]) / "steamapps" / "common"
            path = (parent / game["install_dir"]).resolve()
            if path != parent.resolve() and path.is_relative_to(parent.resolve()):
                folders[str(path).casefold()] = str(game.get("name", "Игра"))[:100]
        with self._lock:
            if folders != self.game_folders:
                self.game_folders = folders
                self._detect_config = None

    def _detect_game(self, names):
        for process in psutil.process_iter(["name", "exe"]):
            try:
                name = process.info.get("name") or ""
                # Explicit game names can contain words such as "Crash". Only
                # the known desktop/service executables override this list.
                if name.casefold() in names and name.casefold() not in BACKGROUND_PROCESSES:
                    return name, name
                if background_process(name):
                    continue
                executable = str(process.info.get("exe") or "").casefold()
                if executable:
                    for folder, title in self.game_folders.items():
                        if executable.startswith(folder + os.sep):
                            return title, name
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return "", ""

    def refresh_activity(self, force=False):
        """Shared, bounded detection, also usable by the file worker on its own."""
        with self._lock:
            now = time.monotonic()
            cfg = self.settings().get("performance", {})
            mode = self.override or cfg.get("mode", "auto")
            names = cfg.get("game_processes", "cs2.exe,dota2.exe,Warframe.x64.exe,Warframe.exe")
            signature = (mode, names)
            if not force and signature == self._detect_config and now - self._last_detect < 5:
                return
            self._last_detect = now
            self._detect_config = signature
            names = {n.strip().casefold() for n in names.split(",") if n.strip()}
            # Detect games even when the resource profile is manually selected.
            try:
                self.game, self.game_process = self._detect_game(names)
            except psutil.Error:
                # A process disappearing during enumeration must not kill the
                # downloads worker or keep yesterday's game latched forever.
                self.game = self.game_process = ""
            self.active = ("game" if self.game else "balanced") if mode == "auto" else mode

    def tick(self, force=False):
        with self._lock:
            self.refresh_activity(force)
            now = time.monotonic()
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
        return bool(self.file_pause_reason)

    @property
    def file_pause_reason(self):
        with self._lock:
            if self.active == "economy":
                return "включён экономный профиль"
            if self.active == "game" and self.game:
                process = (
                    f" ({self.game_process})"
                    if self.game_process and self.game_process != self.game
                    else ""
                )
                return f"запущена игра «{self.game}»{process}"
            return ""

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
